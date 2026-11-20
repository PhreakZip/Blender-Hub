# Changelog

## 0.13.0 — 2026-11-20
Everything the standard Blender MCP addon offers, except AI generation (out of scope):
- `hub_turntable`: new `shading` modes `xray`, `wireframe` (wire copies of the evaluated meshes; originals only hidden from the render) and `cycles` (16 samples + denoise, the hub's light rig); `camera="scene"` renders through the scene camera with the render aspect; `timeline` (a count or a list of frames) makes an animation strip from one fixed view framing the whole motion, restoring the current frame, subframe and unkeyed edits of animated properties; it refuses scenes with unbaked simulations unless `force=true`. Workbench sheets get a temporary world when the scene has none (it was blank). The scene camera and timelines never reuse a sheet.
- `hub_screenshot`: `shading` for one capture (the user's viewport shading is restored) and `image` (`"Render Result"`, an image in the file or a file path; no window needed).
- New `hub_scene`: scene facts as text — location, world size, on ground / floating / below, rotation, scale, hierarchy, visibility, faces/bones/light power, materials, modifiers, animation, topology health (quads, tris, ngons, non-manifold, boundary, loose, poles), skin weights, and a scene settings line.
- New `hub_assets`: search and import from Poly Haven (CC0, no key), Sketchfab (search keyless, import with `SKETCHFAB_API_KEY`) and Poly Pizza (`POLYPIZZA_API_KEY`), with an optional thumbnail sheet. Imports land in the agent's collection with source, author and license as `asset_*` properties, scaled to `target_size`; textures build a PBR material on your own objects; HDRIs need an exclusive lease. Downloads are cached in the state folder.
- Token economy: the `token`, `agent_key` and `note` parameters are explained once in the server instructions instead of in every tool. Tool definitions + instructions ~3,130 tokens for 23 tools (0.12.0: ~2,840 for 21); typical session ~11,260 (+2.6%), still −21% against the standard addon at the same quality.
- 160-check suite (asset-library checks are reported as SKIPPED when offline).

## 0.12.0 — 2026-11-20
- `hub_turntable(shading="rendered")`: the same contact sheet rendered with EEVEE (16 samples), so materials, emissive and light can be reviewed before export. Same image size as the Workbench sheet, so the same token cost (~1,100 for 12 views). With isolation (default) it uses the hub's fixed world and key/fill sun lights, so every review is lit the same way.
- Reuse of an unchanged sheet works in rendered mode too, with a wider fingerprint (render, EEVEE incl. nested structs, view layer, display device, object visibility flags, texture space, pass index, object custom props). Materials that read images or other objects (Texture Coordinate object, non-geometry Attribute nodes) always re-render.
- Any turntable now neutralises, then restores, what would make the sheet differ from the model: compositor, sequencer, render border, pixel aspect, multiview, Freestyle, motion blur, simplify, background scene, view-layer material override and samples, look, exposure, gamma, curve mapping.
- Tool definitions +90 tokens (the new `shading` and `isolate` parameters); typical session ~10,970 tokens (+1%).

## 0.11.0 — 2026-11-20
- Token savings that keep quality identical:
  - An identical `hub_turntable` of an unchanged **self-contained mesh** answers `unchanged` with no image (~1,090 → ~70 tokens); the fingerprint covers the evaluated mesh, all modifier settings (render-only too), world matrix, mesh and object-linked materials and scene display settings. Curves/text/metaballs, particles, Geometry Nodes, modifiers that point at other objects, TEXTURE color, isolate=false and custom tag/out_dir always render; `force=true` always renders.
  - `hub_exec(script=<.py>, args={...})` runs a saved file: edit it between attempts instead of re-sending the code (`args` is now a reserved module name).
  - Replies carry the lease block only when it needs attention (expiring in < 5 min, or lost).

## 0.10.0 — 2026-11-20
- Token diet (typical session ~21.3k → ~11.1k tokens, measured by the new `tests/token_budget.py`):
  - `hub_screenshot` captures only the 3D viewport and scales it to `size` px (default 1024); `full=true` for the whole window.
  - `hub_turntable` no longer lists the 12 frame paths in its reply (the image is the answer).
  - `hub_list_workers` is short by default (`verbose=true` for everything).
  - Compact JSON replies, a shorter lease block, rounded `hub_measure` numbers, shorter tool descriptions and instructions.

## 0.9.0 — 2026-11-19
- The live panel shows what each agent is doing: its declared task (new `hub_note`), the current step (`note=` on `hub_exec` / `hub_turntable` / `hub_export_collection`), and — seen by the hub itself after every command — what changed in its collection (objects added / changed / removed) plus its object and face totals. Also in `hub_list_workers` (`agents_info`).

## 0.8.0 — 2026-11-19
- Live panel in every window (bottom-left of the 3D viewport): worker name, how many agents are working, and per agent whether it is running a command, active or idle, with its lease. Same lines in `hub_list_workers` (`overlay`); `BLENDER_HUB_OVERLAY=0` hides it.
- `label` is required on the MCP `hub_acquire` / `hub_lease`; a lease created without one is named after its first agent.
- `BLENDER_HUB_MODULES`: Python files preloaded into every `hub_exec` (as `NAME`), reloaded when changed; errors reported in `hub_list_workers` (`modules`). Installer: `--module NAME=PATH` / `-Module`.

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
