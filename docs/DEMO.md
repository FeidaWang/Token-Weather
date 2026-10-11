# Scenario planner walkthrough

The current main source contains a dark weather interface. It makes real local MCP calls with supplied numbers. It is a browser planner, not an installed ChatGPT plugin or an automatic compaction controller. Version remains 1.0.0; the original v1.0.0 release ZIP and tag retain their original UI.

1. Run `python3 -B mcp/demo/server.py --port 0` from the latest source root, then open the printed loopback URL. Default figures are a synthetic weather overview.
2. Read the two-column metrics, history/forecast, current/planned usage and five-band weather scale. Until reference means linear reference crossing, not actual auto-compaction timing.
3. Select Edit scenario to open the same-page editor. Choose Next task for the 100000/65000/20000/5000 example; current headroom is 35000 and planned headroom 10000.
4. Choose Task exceeds budget. The plan goes from 85000 to 110000 against 100000. Use 10k for the task to retain the 5000 reserve and fit exactly at the reference.
5. Choose Unknown reference. Available arithmetic stays visible; Add reference limit opens and focuses the missing field. Blank values remain unknown; zero is an explicit value.
6. Use whole counts, comma grouping, k/M suffixes and up to six compatible turn totals. Recent totals use comma-separated ungrouped counts/suffixes or one count per line. Their last total must match used tokens.
7. Illegal counts receive an inline error and focused input. Last valid result labels prevent draft edits from being mistaken for a new calculation. Copying is disabled until a successful update.
8. Optionally enable Update as I type. Valid edits recalculate after a short pause. Copy summary copies the current result to your clipboard only when clicked.
9. Use local Codex readings opens a guide to the separate precisely bound local tool. Escape closes the dialog and returns focus. Details opens the actual numeric MCP exchange and a synthetic rejection check; Totals opens the exact forecast table.

Stop the demo with Ctrl+C. No paid model API, asset service or background installation is used. Original local weather/UI/hooks remain unchanged.

For future directory review, record the actual intended-host connection/version rather than presenting this standalone harness as an installed host plugin. No public MCP or review submission is part of this UI update.

## Sidebar layout

Normal overview captures fit one viewport at 320x640, 320x720, 360x600, 400x800, 480x800 and 640x900. The forecast keeps a readable minimum height; detailed inputs/results are in viewport-bounded dialogs with sticky Done controls. Very narrow/short effective viewports and complex messages use vertical reflow rather than clipped content. The field editor retains full arithmetic and validation. Axis work is bounded for large reference ratios.
