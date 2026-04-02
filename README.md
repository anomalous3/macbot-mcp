# macbot-mcp

An MCP server that gives AI agents hands on macOS — via the Accessibility API, AppleScript, and Quartz CGEvents.

The macOS counterpart to [ahk-mcp](https://github.com/anomalous3/ahk-mcp). Same thesis: the accessibility tree already contains a machine-readable description of everything on screen. Screenshots throw that away and make the model re-derive it from pixels. Why?

Token cost per action: **~200-700 tokens** (vs ~2000-3500 for screenshot-based).

## How it works

macbot-mcp exposes 14 tools over MCP's stdio transport:

- **Observation tools** read the macOS accessibility tree, window properties, and browser URLs — returning structured text with element roles, names, values, and **screen coordinates**
- **Action tools** click, drag, scroll, type, and send keystrokes via Quartz CGEvents and AppleScript
- **`mac_run_applescript`** is the escape hatch — execute arbitrary AppleScript for anything the built-in tools don't cover

Every observation tool returns coordinates. Find a button with `mac_ui_find`, get its `x,y` position, and click it with `mac_click` — no screenshot needed.

## Token cost comparison

| Approach | Tokens per action | What you get |
|---|---|---|
| Screenshot-based (full screen PNG) | ~2000-3500 | Pixels. Model must OCR, locate elements, interpret layout. |
| macbot-mcp (structured text) | ~200-700 | Element roles, titles, values, and pixel coordinates. |

A 20-step workflow: ~50k tokens with screenshots, ~8k with macbot. The structured output is also more reliable — the model doesn't guess where the "Save" button is when the accessibility tree says `AXButton title="Save" @1043,672 88x32`.

## Installation

### Prerequisites

- **macOS 12+** (Monterey or later)
- **Python 3.10+**

### Setup

```bash
git clone https://github.com/anomalous3/macbot-mcp.git
cd macbot-mcp

# Create a virtual environment
python3 -m venv .venv
# or: uv venv .venv

# Activate
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### macOS Permissions

macbot needs two permissions, granted to your terminal app (Terminal, kitty, iTerm2, etc.):

1. **Accessibility** — System Settings > Privacy & Security > Accessibility
2. **Screen Recording** — System Settings > Privacy & Security > Screen Recording

Without Accessibility, observation and input tools won't work. Without Screen Recording, `mac_screenshot` will fail.

## Claude Code MCP configuration

Add to your `~/.claude.json`:

```json
{
  "mcpServers": {
    "macbot": {
      "command": "/path/to/macbot-mcp/.venv/bin/python3",
      "args": ["/path/to/macbot-mcp/server.py"],
      "env": {
        "MACBOT_SCREENSHOT_DIR": "/tmp/macbot"
      }
    }
  }
}
```

After adding the config, restart Claude Code. The tools appear with the `mcp__macbot__` prefix.

## Tool reference

### Observation

| Tool | Description |
|---|---|
| `mac_ui_tree` | Dump the accessibility tree of any app. Returns roles, titles, values, descriptions, and screen coordinates. Configurable depth and node limit. **This is the primary observation tool — use it before reaching for screenshots.** |
| `mac_ui_find` | Search for UI elements by name and/or role. Returns matches with coordinates. Find that "Submit" button without scanning the whole tree. |
| `mac_ui_url` | Get the current URL from the active browser's address bar (Firefox, Chrome, Safari). |
| `mac_get_windows` | List all visible windows with app name, title, position, and size. |
| `mac_screenshot` | Capture full screen, a specific window, or a region. Returns a PNG file path. **The fallback when you genuinely need pixels.** |

### Action

| Tool | Description |
|---|---|
| `mac_click` | Click at screen coordinates. Uses Quartz CGEvents (falls back to cliclick if installed). |
| `mac_drag` | Click and drag between two points. Configurable duration for smooth drags. Works for rotating 3D plots, selecting text, moving windows. |
| `mac_scroll` | Scroll at a screen position. Vertical and horizontal. |
| `mac_type_text` | Type text into the frontmost app. Uses clipboard paste by default (fast, Unicode-safe). Optional keystroke mode for modifier-sensitive fields. |
| `mac_key_press` | Send a key press with modifiers. Supports named keys (return, tab, escape, arrows, F-keys) and characters with command/option/control/shift. |
| `mac_focus_app` | Bring an application to the front. |
| `mac_get_clipboard` | Read the system clipboard. |
| `mac_set_clipboard` | Set the system clipboard. |

### Escape hatch

| Tool | Description |
|---|---|
| `mac_run_applescript` | Execute arbitrary AppleScript. Full access to System Events, app scripting dictionaries, and everything else AppleScript can do. |

## Browser automation via the Accessibility API

Modern browsers expose their full UI through the macOS Accessibility API. macbot reads this directly — no browser extension, no WebDriver, no Playwright needed.

**Read the tab bar** with element coordinates:
```
> mac_ui_tree app="Firefox" max_depth=5 max_nodes=50

AXApplication title="Firefox"
  AXWindow title="GitHub - anomalous3/macbot-mcp" @36,30 1752x957
    AXGroup desc="GitHub - anomalous3/macbot-mcp" @36,30 1752x957
      AXToolbar desc="Browser tabs" @36,30 1752x44
        AXTabGroup (tab group) @190,30 1518x44
          AXRadioButton title="GitHub - anomalous3/macbot-mcp" value="True" @193,30 210x44
          AXRadioButton title="New Tab" @403,30 210x44
```

**Find a specific element** and get its click coordinates:
```
> mac_ui_find app="Firefox" name="Submit" role="AXButton"

[{"role": "AXButton", "title": "Submit", "x": 450, "y": 320, "width": 80, "height": 32}]
```

**Get the URL** without screenshots or clipboard tricks:
```
> mac_ui_url app="Firefox"

{"url": "https://github.com/anomalous3/macbot-mcp", "field": "Search with Google or enter address"}
```

**Use Firefox.** It exposes the richest accessibility tree of the major browsers — more element detail, better labeling, and more consistent structure than Chrome or Safari.

## The coordinate system

All coordinates are absolute screen pixels in macOS logical coordinates (not Retina physical pixels). Origin (0,0) is the top-left of the primary display.

The `@x,y WxH` format in `mac_ui_tree` output gives position and size directly. To click the center of `@450,320 80x32`, click at `(490, 336)`.

## Works with everything

macbot works with any macOS application that implements the Accessibility API (which is most of them):

- **Browsers** — Firefox, Chrome, Safari (Firefox recommended for richest tree)
- **Terminals** — kitty, Terminal.app, iTerm2 (can read content, type commands)
- **Editors** — VS Code, Sublime Text, TextEdit
- **System apps** — Finder, System Settings, Activity Monitor
- **Any app** — if it has windows and controls, macbot can probably read and drive it

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MACBOT_SCREENSHOT_DIR` | `/tmp/macbot` | Directory for screenshot PNGs |

## Platform

macOS only. For Windows, see [ahk-mcp](https://github.com/anomalous3/ahk-mcp). The approach is the same — read the accessibility tree, act via synthetic input — just different platform APIs.

## License

MIT
