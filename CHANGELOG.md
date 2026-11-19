# Changelog

## 0.7.0 — 2026-11-19
- Live by default: `hub_acquire` / `hub_start_worker` open a Blender window unless `gui=false` (one window per session). `BLENDER_HUB_GUI_DEFAULT=0` restores background as the default. The Python API keeps background as its default.
- MCP instructions and the `hub_acquire` description tell agents to release and ask the user before closing the window when done.
- Windows never idle-exit and are never recycled, so they count against the pool limit: new cap `BLENDER_HUB_MAX_WINDOWS` (4); limit errors list the open windows to close and suggest `gui=false`.
- The window mode survives recycle, `hub_kill_worker`, `hub_restart_worker` (also after the user closed the window: stored in `autosave/<name>/meta.json`) and `hub_restore_archive`; both tools accept `gui`.
- `gui` sent as a string (`"false"`) is parsed correctly.
- Installer: `--test` also checks that a Blender window can open (warns otherwise); new `--background` / `-Background` sets `BLENDER_HUB_GUI_DEFAULT=0`. A window boot failure now hints at `gui=false`.

## 0.6.3 — 2026-11-19 — first public release
- Pool of background Blender workers with leases (exclusive/shared), tokens and per-agent keys.
- Per-collection ownership with violation detection and owner notices.
- `hub_measure` (edge-sampled distance, ray-parity penetration) and `hub_turntable` (contact sheet image).
- Watchdog timeouts, safe kill/restart, rotating autosaves, verified archiving on recycle and name reuse, `hub_restore_archive`.
- Window mode (`gui=true`) to watch agents live, with `hub_view` and `hub_screenshot`.
- Installer for Claude Desktop, Claude Code and Cursor; 120-check test suite.
