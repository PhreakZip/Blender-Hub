# Changelog

## 0.6.3 — 2026-11-19 — first public release
- Pool of background Blender workers with leases (exclusive/shared), tokens and per-agent keys.
- Per-collection ownership with violation detection and owner notices.
- `hub_measure` (edge-sampled distance, ray-parity penetration) and `hub_turntable` (contact sheet image).
- Watchdog timeouts, safe kill/restart, rotating autosaves, verified archiving on recycle and name reuse, `hub_restore_archive`.
- Window mode (`gui=true`) to watch agents live, with `hub_view` and `hub_screenshot`.
- Installer for Claude Desktop, Claude Code and Cursor; 120-check test suite.
