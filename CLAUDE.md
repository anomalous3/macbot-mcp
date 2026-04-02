# macbot-mcp

macOS GUI automation via the Accessibility API, AppleScript, and Quartz.

## When to use which tool

**Prefer accessibility tools over screenshots.** They're 5-15x cheaper in tokens and return structured, searchable data.

| Goal | Tool | Tokens |
|------|------|--------|
| Read UI state (windows, buttons, tabs, text) | `mac_ui_tree` | ~200-700 |
| Find a specific element (button, link, field) | `mac_ui_find` | ~100-300 |
| Get the browser URL | `mac_ui_url` | ~50 |
| See what something looks like visually | `mac_screenshot` + Read | ~3000 |

**Use screenshots only when you genuinely need visual information** — layout, colors, images, charts, or anything the accessibility tree can't describe.

## Common workflows

### Click a button without a screenshot

```
1. mac_ui_find(app="Firefox", name="Submit")
   -> {"role": "AXButton", "title": "Submit", "x": 450, "y": 320, "width": 80, "height": 32}
2. mac_click(x=490, y=336)
```

### Navigate browser tabs

```
1. mac_ui_tree(app="Firefox", max_depth=5, max_nodes=50)
   -> Find AXRadioButton elements in the tab group with titles and coordinates
2. mac_click at the tab's coordinates
3. mac_ui_url to confirm you're on the right page
```

### Type into a focused app

```
1. mac_focus_app(app_name="TextEdit")
2. mac_type_text(text="Hello world")
3. mac_key_press(key="return")
```

### Scroll a page or zoom a plot

```
mac_scroll(x=500, y=400, dy=-5)   # scroll down
mac_scroll(x=500, y=400, dy=5)    # scroll up (zoom in for Plotly 3D)
```

### Drag to rotate a 3D visualization

```
mac_drag(start_x=500, start_y=300, end_x=700, end_y=200, duration=0.6)
```

## Accessibility roles reference

Common `role` values for `mac_ui_find`:

- `AXButton` — buttons
- `AXTextField` — text input fields
- `AXStaticText` — labels and text content
- `AXRadioButton` — browser tabs (in Firefox)
- `AXCheckBox` — checkboxes
- `AXComboBox` — dropdowns and combo boxes
- `AXLink` — hyperlinks (in some apps)
- `AXGroup` — container elements
- `AXScrollArea` — scrollable regions
- `AXToolbar` — toolbars
- `AXMenuItem` — menu items
- `AXTextArea` — multi-line text areas (terminals, editors)

## Coordinate system

All coordinates are **absolute screen pixels** (logical, not Retina physical). The origin (0,0) is the top-left corner of the primary display.

The `@x,y WxH` format in `mac_ui_tree` output gives you the position and size directly. To click the center of an element at `@450,320 80x32`, click at `(490, 336)`.

## Permissions

macbot needs two macOS permissions:
- **Screen Recording** (for `mac_screenshot`) — System Settings > Privacy > Screen Recording
- **Accessibility** (for all other tools) — System Settings > Privacy > Accessibility

Grant these to the terminal app that runs Claude Code (e.g. kitty, Terminal, iTerm2).
