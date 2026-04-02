"""
macbot-mcp: macOS GUI automation tools via MCP.

Token-efficient alternative to screenshot-based computer use on macOS.
Uses the Accessibility API, AppleScript, and Quartz CGEvents to read
UI state and drive input — returning structured text instead of images.

Token cost per action: ~200-700 tokens (vs ~2000-3500 for screenshot-based).
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from ApplicationServices import (
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeNames,
    AXUIElementCopyAttributeValue,
)
from mcp.server.lowlevel.server import Server, NotificationOptions
from mcp.server.stdio import stdio_server
import mcp.types as types

SCREENSHOT_DIR = Path(os.environ.get("MACBOT_SCREENSHOT_DIR", "/tmp/macbot"))
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("macbot")
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="[macbot] %(message)s")

server = Server("macbot")


# --- Accessibility Helpers ---

def _parse_ax_point(val) -> tuple[int, int] | None:
    """Extract x,y from an AXValue CGPoint string representation."""
    if val is None:
        return None
    m = re.search(r"x:([\d.]+)\s+y:([\d.]+)", str(val))
    return (int(float(m.group(1))), int(float(m.group(2)))) if m else None


def _parse_ax_size(val) -> tuple[int, int] | None:
    """Extract w,h from an AXValue CGSize string representation."""
    if val is None:
        return None
    m = re.search(r"w:([\d.]+)\s+h:([\d.]+)", str(val))
    return (int(float(m.group(1))), int(float(m.group(2)))) if m else None


def _get_pid(app_name: str) -> int:
    """Get PID for an app by name. Tries exact match, then prefix match."""
    for flags in (["-xi"], ["-if"]):
        result = subprocess.run(
            ["pgrep"] + flags + [app_name], capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip().split()[0])
    raise RuntimeError(f"App not running: {app_name}")


def _get_frontmost_pid() -> int:
    """Get PID of the frontmost application."""
    script = 'tell application "System Events" to unix id of first application process whose frontmost is true'
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError("Could not get frontmost app PID")
    return int(result.stdout.strip())


def _ax_attr(elem, attr: str):
    """Get a single AX attribute, returning None on error."""
    err, val = AXUIElementCopyAttributeValue(elem, attr, None)
    return val if err == 0 else None


def _walk_ax_tree(elem, depth: int = 0, max_depth: int = 4,
                  counter: list | None = None, max_nodes: int = 200) -> list[str]:
    """Walk AX tree, returning lines of structured text."""
    if counter is None:
        counter = [0]
    if depth > max_depth or counter[0] >= max_nodes:
        return []
    counter[0] += 1

    role = _ax_attr(elem, "AXRole") or "?"
    title = _ax_attr(elem, "AXTitle")
    value = _ax_attr(elem, "AXValue")
    desc = _ax_attr(elem, "AXDescription")
    rdesc = _ax_attr(elem, "AXRoleDescription")
    pos = _parse_ax_point(_ax_attr(elem, "AXPosition"))
    size = _parse_ax_size(_ax_attr(elem, "AXSize"))

    parts = [str(role)]
    if title:
        parts.append(f'title="{str(title)[:80]}"')
    if value and str(value).strip():
        parts.append(f'value="{str(value)[:80]}"')
    if desc:
        parts.append(f'desc="{str(desc)[:60]}"')
    if not title and not desc and rdesc:
        parts.append(f"({rdesc})")
    if pos:
        parts.append(f"@{pos[0]},{pos[1]}")
    if size:
        parts.append(f"{size[0]}x{size[1]}")

    indent = "  " * depth
    lines = [f"{indent}{' '.join(parts)}"]

    children = _ax_attr(elem, "AXChildren")
    if children:
        for child in children:
            lines.extend(_walk_ax_tree(child, depth + 1, max_depth, counter, max_nodes))

    return lines


def _find_ax_elements(elem, name: str = "", role_filter: str = "",
                      depth: int = 0, max_depth: int = 8,
                      results: list | None = None, max_results: int = 30) -> list[dict]:
    """Search AX tree for elements matching name/role."""
    if results is None:
        results = []
    if depth > max_depth or len(results) >= max_results:
        return results

    elem_role = str(_ax_attr(elem, "AXRole") or "")
    elem_title = str(_ax_attr(elem, "AXTitle") or "")
    elem_value = str(_ax_attr(elem, "AXValue") or "")
    elem_desc = str(_ax_attr(elem, "AXDescription") or "")

    role_match = not role_filter or role_filter.lower() in elem_role.lower()
    name_match = not name or (
        name.lower() in elem_title.lower()
        or name.lower() in elem_desc.lower()
        or name.lower() in elem_value.lower()
    )

    if role_match and name_match and (name or role_filter):
        pos = _parse_ax_point(_ax_attr(elem, "AXPosition"))
        size = _parse_ax_size(_ax_attr(elem, "AXSize"))
        entry = {
            "role": elem_role,
            "title": elem_title[:80] if elem_title else None,
            "value": elem_value[:120] if elem_value else None,
            "description": elem_desc[:80] if elem_desc else None,
        }
        if pos:
            entry["x"], entry["y"] = pos
        if size:
            entry["width"], entry["height"] = size
        results.append(entry)

    children = _ax_attr(elem, "AXChildren")
    if children:
        for child in children:
            _find_ax_elements(child, name, role_filter, depth + 1, max_depth, results, max_results)

    return results


# --- Script Helpers ---

def run_applescript(script: str) -> str:
    """Run an AppleScript and return its output."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript error: {result.stderr.strip()}")
    return result.stdout.strip()


def run_jxa(script: str) -> str:
    """Run JavaScript for Automation and return output."""
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", script],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"JXA error: {result.stderr.strip()}")
    return result.stdout.strip()


# --- Tool Definitions ---

TOOLS = [
    types.Tool(
        name="mac_screenshot",
        description=(
            "Take a screenshot. Returns the file path to the PNG. "
            "Use Read tool on the returned path to view it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "window": {
                    "type": "string",
                    "description": "App name to capture (e.g. 'Firefox'). Omit for full screen.",
                },
                "region": {
                    "type": "string",
                    "description": "Region as 'x,y,w,h' (e.g. '100,200,800,600'). Omit for full screen/window.",
                },
            },
        },
    ),
    types.Tool(
        name="mac_get_windows",
        description="List all visible windows with app name, title, position, and size.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="mac_focus_app",
        description="Bring an application to the front.",
        inputSchema={
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "Application name (e.g. 'Firefox', 'Finder')",
                },
            },
            "required": ["app_name"],
        },
    ),
    types.Tool(
        name="mac_scroll",
        description="Scroll at a screen position. Positive dy = scroll up, negative = scroll down. Positive dx = scroll right, negative = scroll left.",
        inputSchema={
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "X coordinate to scroll at (default: center of screen)"},
                "y": {"type": "number", "description": "Y coordinate to scroll at (default: center of screen)"},
                "dy": {"type": "integer", "description": "Vertical scroll amount (positive=up, negative=down). Typical: -3 to -5 for page scroll."},
                "dx": {"type": "integer", "description": "Horizontal scroll amount (positive=right, negative=left). Default: 0."},
            },
            "required": ["dy"],
        },
    ),
    types.Tool(
        name="mac_drag",
        description="Click and drag from one screen coordinate to another. Useful for rotating 3D plots, moving windows, selecting text, etc.",
        inputSchema={
            "type": "object",
            "properties": {
                "start_x": {"type": "number", "description": "Starting X coordinate"},
                "start_y": {"type": "number", "description": "Starting Y coordinate"},
                "end_x": {"type": "number", "description": "Ending X coordinate"},
                "end_y": {"type": "number", "description": "Ending Y coordinate"},
                "duration": {
                    "type": "number",
                    "description": "Duration in seconds (default: 0.5). Longer = smoother.",
                },
                "button": {
                    "type": "string",
                    "enum": ["left", "right"],
                    "description": "Mouse button (default: left)",
                },
            },
            "required": ["start_x", "start_y", "end_x", "end_y"],
        },
    ),
    types.Tool(
        name="mac_click",
        description="Click at screen coordinates. Coordinates are absolute screen pixels.",
        inputSchema={
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "X coordinate"},
                "y": {"type": "number", "description": "Y coordinate"},
                "button": {
                    "type": "string",
                    "enum": ["left", "right"],
                    "description": "Mouse button (default: left)",
                },
                "clicks": {
                    "type": "integer",
                    "description": "Number of clicks (default: 1, use 2 for double-click)",
                },
            },
            "required": ["x", "y"],
        },
    ),
    types.Tool(
        name="mac_type_text",
        description="Type text into the frontmost application. Uses clipboard paste for reliability.",
        inputSchema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type"},
                "use_keys": {
                    "type": "boolean",
                    "description": "If true, use keystroke (slow but works with modifier-sensitive fields). Default false uses clipboard paste.",
                },
            },
            "required": ["text"],
        },
    ),
    types.Tool(
        name="mac_key_press",
        description=(
            "Send a key press. Use key names like 'return', 'tab', 'escape', 'space', "
            "'delete', 'left arrow', 'right arrow', or single characters."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key name or character"},
                "modifiers": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["command", "option", "control", "shift"]},
                    "description": "Modifier keys to hold",
                },
            },
            "required": ["key"],
        },
    ),
    types.Tool(
        name="mac_get_clipboard",
        description="Read the current clipboard text content.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="mac_set_clipboard",
        description="Set the clipboard text content.",
        inputSchema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to put on clipboard"},
            },
            "required": ["text"],
        },
    ),
    types.Tool(
        name="mac_run_applescript",
        description="Run arbitrary AppleScript. Escape hatch for anything the other tools can't do.",
        inputSchema={
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "AppleScript source code"},
            },
            "required": ["script"],
        },
    ),
    types.Tool(
        name="mac_ui_tree",
        description=(
            "Dump the accessibility tree of an application. Returns structured text "
            "with element roles, titles, values, descriptions, and screen coordinates. "
            "Much cheaper than screenshots (~200-700 tokens vs ~3000). Use this to read "
            "UI state, find elements, and understand window layout."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "App name (e.g. 'Firefox'). Omit for frontmost app.",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Max tree depth (default: 4). Deeper = more detail but more output.",
                },
                "max_nodes": {
                    "type": "integer",
                    "description": "Max nodes to return (default: 200).",
                },
            },
        },
    ),
    types.Tool(
        name="mac_ui_find",
        description=(
            "Search for UI elements by name and/or role within an app's accessibility tree. "
            "Returns matching elements with their properties and screen coordinates. Use to "
            "find buttons, text fields, links, URLs, or any named element without a screenshot."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "App name. Omit for frontmost app.",
                },
                "name": {
                    "type": "string",
                    "description": "Text to search for in element title/description/value (partial match).",
                },
                "role": {
                    "type": "string",
                    "description": "Role filter: AXButton, AXTextField, AXStaticText, AXLink, AXGroup, etc.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results (default: 30).",
                },
            },
        },
    ),
    types.Tool(
        name="mac_ui_url",
        description="Get the URL from the active browser's address bar via accessibility API. Works with Firefox, Chrome, Safari.",
        inputSchema={
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "Browser app name (default: frontmost app).",
                },
            },
        },
    ),
]


# --- Tool Handlers ---

async def handle_screenshot(args: dict) -> str:
    ts = int(time.time() * 1000)
    path = SCREENSHOT_DIR / f"screen_{ts}.png"

    cmd = ["screencapture", "-x"]  # -x = no sound

    if args.get("region"):
        parts = args["region"].split(",")
        if len(parts) != 4:
            return json.dumps({"error": "Region must be 'x,y,w,h'"})
        cmd.extend(["-R", ",".join(p.strip() for p in parts)])
    elif args.get("window"):
        app = args["window"]
        try:
            run_applescript(f'tell application "{app}" to activate')
            time.sleep(0.5)
        except RuntimeError as e:
            return json.dumps({"error": f"Could not focus {app}: {e}"})
        cmd.append("-l")
        try:
            wid = run_jxa(
                f'Application("System Events").processes["{app}"]'
                f'.windows[0].attributes["AXWindowNumber"].value()'
            )
            cmd.append(wid)
        except RuntimeError:
            cmd = ["screencapture", "-x"]

    cmd.append(str(path))

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        return json.dumps({"error": f"screencapture failed: {result.stderr.strip()}"})

    return json.dumps({"path": str(path), "hint": "Use the Read tool on this path to view the screenshot."})


async def handle_get_windows(args: dict) -> str:
    script = """
    var se = Application("System Events");
    var results = [];
    var procs = se.processes.whose({visible: true})();
    for (var i = 0; i < procs.length; i++) {
        var proc = procs[i];
        var pname = proc.name();
        try {
            var wins = proc.windows();
            for (var j = 0; j < wins.length; j++) {
                var w = wins[j];
                var pos = w.position();
                var sz = w.size();
                results.push({
                    app: pname,
                    title: w.title() || "",
                    x: pos[0], y: pos[1],
                    width: sz[0], height: sz[1]
                });
            }
        } catch(e) {}
    }
    JSON.stringify(results);
    """
    try:
        raw = run_jxa(script)
        return raw
    except RuntimeError as e:
        return json.dumps({"error": str(e)})


async def handle_focus_app(args: dict) -> str:
    app = args["app_name"]
    try:
        run_applescript(f'tell application "{app}" to activate')
        return json.dumps({"status": "ok", "focused": app})
    except RuntimeError as e:
        return json.dumps({"error": str(e)})


async def handle_scroll(args: dict) -> str:
    x = int(args.get("x", 900))
    y = int(args.get("y", 500))
    dy = int(args["dy"])
    dx = int(args.get("dx", 0))

    script = f"""
import Quartz
point = Quartz.CGPointMake({x}, {y})
move = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, point, Quartz.kCGMouseButtonLeft)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
scroll = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 2, {dy}, {dx})
Quartz.CGEventPost(Quartz.kCGHIDEventTap, scroll)
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return json.dumps({"error": f"Scroll failed: {result.stderr.strip()}"})
        return json.dumps({"status": "ok", "x": x, "y": y, "dy": dy, "dx": dx})
    except Exception as e:
        return json.dumps({"error": f"Scroll failed: {e}"})


async def handle_drag(args: dict) -> str:
    start_x, start_y = int(args["start_x"]), int(args["start_y"])
    end_x, end_y = int(args["end_x"]), int(args["end_y"])
    duration = float(args.get("duration", 0.5))
    button = args.get("button", "left")

    steps = max(10, int(duration / 0.015))
    sleep_per_step = duration / steps

    is_right = button == "right"
    down_type = "Quartz.kCGEventRightMouseDown" if is_right else "Quartz.kCGEventLeftMouseDown"
    drag_type = "Quartz.kCGEventRightMouseDragged" if is_right else "Quartz.kCGEventLeftMouseDragged"
    up_type = "Quartz.kCGEventRightMouseUp" if is_right else "Quartz.kCGEventLeftMouseUp"
    btn_const = "Quartz.kCGMouseButtonRight" if is_right else "Quartz.kCGMouseButtonLeft"

    script = f"""
import Quartz, time
start_x, start_y = {start_x}, {start_y}
end_x, end_y = {end_x}, {end_y}
steps = {steps}
sleep_per = {sleep_per_step}

pt = Quartz.CGPointMake(start_x, start_y)
evt = Quartz.CGEventCreateMouseEvent(None, {down_type}, pt, {btn_const})
Quartz.CGEventPost(Quartz.kCGHIDEventTap, evt)
time.sleep(0.03)

for i in range(1, steps + 1):
    frac = i / steps
    x = start_x + (end_x - start_x) * frac
    y = start_y + (end_y - start_y) * frac
    pt = Quartz.CGPointMake(x, y)
    evt = Quartz.CGEventCreateMouseEvent(None, {drag_type}, pt, {btn_const})
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, evt)
    time.sleep(sleep_per)

pt = Quartz.CGPointMake(end_x, end_y)
evt = Quartz.CGEventCreateMouseEvent(None, {up_type}, pt, {btn_const})
Quartz.CGEventPost(Quartz.kCGHIDEventTap, evt)
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=max(10, int(duration) + 5),
        )
        if result.returncode != 0:
            return json.dumps({"error": f"Drag failed: {result.stderr.strip()}"})
        return json.dumps({
            "status": "ok",
            "from": [start_x, start_y],
            "to": [end_x, end_y],
            "duration": duration,
        })
    except Exception as e:
        return json.dumps({"error": f"Drag failed: {e}"})


async def handle_click(args: dict) -> str:
    x, y = int(args["x"]), int(args["y"])
    button = args.get("button", "left")
    clicks = args.get("clicks", 1)

    # Use cliclick if available, fall back to Quartz
    try:
        subprocess.run(["which", "cliclick"], capture_output=True, check=True)
        action = "rc" if button == "right" else ("dc" if clicks == 2 else "c")
        subprocess.run(["cliclick", f"{action}:{x},{y}"], check=True, timeout=5)
        return json.dumps({"status": "ok", "x": x, "y": y, "button": button, "method": "cliclick"})
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # Quartz CGEvent fallback
    try:
        is_right = button == "right"
        down_t = "Quartz.kCGEventRightMouseDown" if is_right else "Quartz.kCGEventLeftMouseDown"
        up_t = "Quartz.kCGEventRightMouseUp" if is_right else "Quartz.kCGEventLeftMouseUp"
        btn_c = "Quartz.kCGMouseButtonRight" if is_right else "Quartz.kCGMouseButtonLeft"
        result = subprocess.run(
            [sys.executable, "-c", f"""
import Quartz, time
point = Quartz.CGPointMake({x}, {y})
for _ in range({clicks}):
    evt_down = Quartz.CGEventCreateMouseEvent(None, {down_t}, point, {btn_c})
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, evt_down)
    time.sleep(0.05)
    evt_up = Quartz.CGEventCreateMouseEvent(None, {up_t}, point, {btn_c})
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, evt_up)
    time.sleep(0.05)
"""],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return json.dumps({"status": "ok", "x": x, "y": y, "button": button, "method": "quartz"})
        return json.dumps({"error": f"Click failed: {result.stderr.strip()}"})
    except Exception as e:
        return json.dumps({"error": f"Click failed: {e}. Install cliclick: brew install cliclick"})


async def handle_type_text(args: dict) -> str:
    text = args["text"]
    use_keys = args.get("use_keys", False)

    if use_keys:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        try:
            run_applescript(f'tell application "System Events" to keystroke "{escaped}"')
            return json.dumps({"status": "ok", "method": "keystroke", "length": len(text)})
        except RuntimeError as e:
            return json.dumps({"error": str(e)})
    else:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        try:
            old_clip = run_applescript('the clipboard as text')
        except RuntimeError:
            old_clip = None

        try:
            run_applescript(f'set the clipboard to "{escaped}"')
            run_applescript(
                'tell application "System Events" to keystroke "v" using command down'
            )
            time.sleep(0.1)
            if old_clip is not None:
                restore = old_clip.replace("\\", "\\\\").replace('"', '\\"')
                run_applescript(f'set the clipboard to "{restore}"')
            return json.dumps({"status": "ok", "method": "paste", "length": len(text)})
        except RuntimeError as e:
            return json.dumps({"error": str(e)})


async def handle_key_press(args: dict) -> str:
    key = args["key"]
    modifiers = args.get("modifiers", [])

    mod_map = {
        "command": "command down",
        "option": "option down",
        "control": "control down",
        "shift": "shift down",
    }
    mod_parts = [mod_map[m] for m in modifiers if m in mod_map]

    key_code_names = {
        "return", "tab", "escape", "space", "delete", "forward delete",
        "left arrow", "right arrow", "up arrow", "down arrow",
        "home", "end", "page up", "page down",
        "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
    }

    try:
        if key.lower() in key_code_names:
            key_code_map = {
                "return": 36, "tab": 48, "escape": 53, "space": 49,
                "delete": 51, "forward delete": 117,
                "left arrow": 123, "right arrow": 124,
                "up arrow": 126, "down arrow": 125,
                "home": 115, "end": 119, "page up": 116, "page down": 121,
                "f1": 122, "f2": 120, "f3": 99, "f4": 118,
                "f5": 96, "f6": 97, "f7": 98, "f8": 100,
                "f9": 101, "f10": 109, "f11": 103, "f12": 111,
            }
            code = key_code_map.get(key.lower())
            if code is not None:
                using = f" using {{{', '.join(mod_parts)}}}" if mod_parts else ""
                run_applescript(
                    f'tell application "System Events" to key code {code}{using}'
                )
            else:
                return json.dumps({"error": f"Unknown key: {key}"})
        else:
            using = f" using {{{', '.join(mod_parts)}}}" if mod_parts else ""
            escaped = key.replace("\\", "\\\\").replace('"', '\\"')
            run_applescript(
                f'tell application "System Events" to keystroke "{escaped}"{using}'
            )
        return json.dumps({"status": "ok", "key": key, "modifiers": modifiers})
    except RuntimeError as e:
        return json.dumps({"error": str(e)})


async def handle_get_clipboard(args: dict) -> str:
    try:
        text = run_applescript("the clipboard as text")
        return json.dumps({"text": text})
    except RuntimeError as e:
        return json.dumps({"error": str(e), "hint": "Clipboard may be empty or contain non-text data"})


async def handle_set_clipboard(args: dict) -> str:
    text = args["text"]
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    try:
        run_applescript(f'set the clipboard to "{escaped}"')
        return json.dumps({"status": "ok", "length": len(text)})
    except RuntimeError as e:
        return json.dumps({"error": str(e)})


async def handle_run_applescript(args: dict) -> str:
    try:
        output = run_applescript(args["script"])
        return json.dumps({"output": output})
    except RuntimeError as e:
        return json.dumps({"error": str(e)})


async def handle_ui_tree(args: dict) -> str:
    try:
        app_name = args.get("app")
        pid = _get_pid(app_name) if app_name else _get_frontmost_pid()
        max_depth = args.get("max_depth", 4)
        max_nodes = args.get("max_nodes", 200)

        app_elem = AXUIElementCreateApplication(pid)
        lines = _walk_ax_tree(app_elem, max_depth=max_depth, max_nodes=max_nodes)
        return "\n".join(lines)
    except Exception as e:
        return json.dumps({"error": str(e)})


async def handle_ui_find(args: dict) -> str:
    try:
        app_name = args.get("app")
        pid = _get_pid(app_name) if app_name else _get_frontmost_pid()
        name = args.get("name", "")
        role = args.get("role", "")
        max_results = args.get("max_results", 30)

        if not name and not role:
            return json.dumps({"error": "Provide at least 'name' or 'role' to search for"})

        app_elem = AXUIElementCreateApplication(pid)
        results = _find_ax_elements(app_elem, name, role, max_results=max_results)
        return json.dumps(results, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


async def handle_ui_url(args: dict) -> str:
    try:
        app_name = args.get("app")
        pid = _get_pid(app_name) if app_name else _get_frontmost_pid()

        app_elem = AXUIElementCreateApplication(pid)
        results = _find_ax_elements(
            app_elem, name="", role_filter="AXTextField", max_results=5,
        )
        for r in results:
            desc = (r.get("description") or "").lower()
            if "address" in desc or "url" in desc or "location" in desc:
                return json.dumps({"url": r.get("value", ""), "field": r.get("description")})

        for r in results:
            val = r.get("value") or ""
            if val.startswith(("http://", "https://", "file://")):
                return json.dumps({"url": val, "field": r.get("description")})

        return json.dumps({"error": "No URL bar found", "text_fields": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


HANDLERS = {
    "mac_screenshot": handle_screenshot,
    "mac_get_windows": handle_get_windows,
    "mac_focus_app": handle_focus_app,
    "mac_scroll": handle_scroll,
    "mac_drag": handle_drag,
    "mac_click": handle_click,
    "mac_type_text": handle_type_text,
    "mac_key_press": handle_key_press,
    "mac_get_clipboard": handle_get_clipboard,
    "mac_set_clipboard": handle_set_clipboard,
    "mac_run_applescript": handle_run_applescript,
    "mac_ui_tree": handle_ui_tree,
    "mac_ui_find": handle_ui_find,
    "mac_ui_url": handle_ui_url,
}


# --- MCP Wiring ---

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    handler = HANDLERS.get(name)
    if not handler:
        return [types.TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
    try:
        result = await handler(arguments or {})
        return [types.TextContent(type="text", text=result)]
    except Exception as e:
        logger.error(f"Tool {name} failed: {e}")
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def main():
    logger.info("macbot MCP server starting")
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import anyio
    anyio.run(main)
