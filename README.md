# Blender Hub

**An MCP server that lets many AI agents work in Blender at the same time** — each in its own Blender, or several agents sharing one scene, each in its own collection. **By default every session opens its own Blender window**, so you watch the agent build live; background (headless) workers are one flag away for batch work.

Built for Claude (Desktop / Code) and any MCP client. Windows only. No dependencies beyond Python and Blender.

| | Single Blender add-on (typical MCP setup) | Blender Hub |
|---|---|---|
| Agents in parallel | 1 Blender, one command at a time | Pool of up to 10 Blenders (configurable), one per agent |
| Two agents, one scene | They can overwrite each other silently | Each agent owns a collection; touching someone else's work is reported to both |
| Identity | None | Lease token per session + `agent_key` per agent |
| Review renders | One viewport capture per call | `hub_turntable`: 12 views + one contact sheet in ~1 s |
| Fitting parts together | Guess coordinates | `hub_measure`: distance, intersection, penetration and the exact move to make them touch |
| Crash / forgotten agent | Work lost | Rotating autosaves, archives, `hub_restart_worker`, `hub_restore_archive` |
| Watch live | Yes (one window) | Yes, by default: one window per session, with `hub_view` / `hub_screenshot` and a live panel of who is working |
| Shared helper code | Pasted into every call | `BLENDER_HUB_MODULES`: your kit is preloaded in every `hub_exec` and reloaded when the file changes |

---

## Contents
- [Requirements](#requirements)
- [Install (humans)](#install-humans)
- [For AI assistants: install procedure](#for-ai-assistants-install-procedure)
- [For AI assistants: how to use the tools](#for-ai-assistants-how-to-use-the-tools)
- [Tool reference](#tool-reference)
- [Watching live (window mode)](#watching-live-window-mode)
- [Configuration](#configuration)
- [Recovery](#recovery)
- [Testing](#testing)
- [How it works / limits](#how-it-works--limits)
- [Security](#security)

## Requirements
- **Windows 10/11** (the hub uses Windows file locks and process APIs).
- **Python 3.10+** on PATH (`py -3` or `python`). No pip packages.
- **Blender 4.2+** (tested on 5.3). Auto-detected from Program Files, Steam, PATH or the `.blend` file association; or set `BLENDER_EXE`.
- RAM: about **210 MB per idle background worker** and about **700 MB per window** (plus some GPU memory). The hub refuses to start a worker when free RAM is below 1.5 GB, and caps open windows at 4 (`BLENDER_HUB_MAX_WINDOWS`).
- A desktop session that can open a Blender window (agents work in a visible window by default). On VMs/RDP/remote machines without OpenGL, install with `-Background`.

## Install (humans)
```powershell
git clone https://github.com/PhreakZip/Blender-Hub.git
cd Blender-Hub
powershell -ExecutionPolicy Bypass -File install.ps1 -ClaudeDesktop -Test
```
Then **quit Claude Desktop completely** (tray icon → Quit) and open it again. The tools appear as `hub_*`.

Other clients:
```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -ClaudeCode      # claude mcp add (user scope)
powershell -ExecutionPolicy Bypass -File install.ps1 -Cursor          # ~/.cursor/mcp.json
powershell -ExecutionPolicy Bypass -File install.ps1 -Print           # just print the JSON entry
powershell -ExecutionPolicy Bypass -File install.ps1 -ClaudeDesktop -Background   # no windows by default (VM/RDP)
```
Manual entry (any MCP client that uses `mcpServers`):
```json
{
  "mcpServers": {
    "blender-hub": {
      "command": "C:\\Path\\To\\python.exe",
      "args": ["C:\\Path\\To\\Blender-Hub\\hub.py"],
      "env": {
        "BLENDER_EXE": "C:\\Program Files\\Blender Foundation\\Blender 5.3\\blender.exe",
        "BLENDER_HUB_GUI_DEFAULT": "1"
      }
    }
  }
}
```
`BLENDER_HUB_GUI_DEFAULT` is optional (`1` = a live window per session, the default; `0` = background, for machines without a desktop/OpenGL).

Uninstall: `powershell -ExecutionPolicy Bypass -File install.ps1 -ClaudeDesktop -Uninstall` (or `-ClaudeCode` / `-Cursor`; add `-Config "<path>"` if several Claude Desktop configs exist). Every config file is backed up before it is written (`<file>.bak-blender-hub-<timestamp>`). Keep the cloned folder where it is: the config points to it.

---

## For AI assistants: install procedure
Follow these steps exactly. Stop and tell the user if a step fails; do not improvise workarounds that change system settings.

0. **Clone into a permanent folder and run everything from inside it.** The client config stores absolute paths to this folder's `hub.py` and to the Python that ran the installer; moving or deleting either breaks the server (re-run the installer after a move).
   ```powershell
   git clone https://github.com/PhreakZip/Blender-Hub.git
   cd Blender-Hub
   ```
1. **Check the environment** (changes nothing):
   ```powershell
   powershell -ExecutionPolicy Bypass -File install.ps1 -Check
   ```
   Expect `[ OK ] Windows + Python ...` and `[ OK ] Blender X.Y at <path>`. If Blender is not found, ask the user for the full path to `blender.exe` (not `blender-launcher.exe`) and add `-Blender "<path>"` to every command below.
2. **Ask the user which client to register**: Claude Desktop (`-ClaudeDesktop`), Claude Code (`-ClaudeCode`) or Cursor (`-Cursor`). Editing a client's config is the user's decision; do not pick one silently. For Claude Desktop, ask the user to **quit it first** (tray icon → Quit): it may rewrite its config file while running.
3. **Smoke-test and register** in one command (the test runs first; nothing is registered if it fails):
   ```powershell
   powershell -ExecutionPolicy Bypass -File install.ps1 -ClaudeDesktop -Test -Yes
   powershell -ExecutionPolicy Bypass -File install.ps1 -ClaudeCode -Test -Yes
   powershell -ExecutionPolicy Bypass -File install.ps1 -Cursor -Test -Yes
   ```
   - `[ OK ] smoke test passed` means a background Blender started, ran code, measured, rendered and stopped; `[ OK ] window test passed` means a Blender window can open here.
   - `[WARN] a Blender WINDOW could not be opened here`: re-run the same command with `-Background` (agents then work without windows by default).
   - `several Claude Desktop configs found`: show the list to the user and re-run with `-Config "<chosen path>"`.
   - `is not valid JSON` / `is not a JSON object`: the client's config is broken; show the user the path, nothing was changed.
4. **Restart the client** — new MCP servers load only at startup:
   - Claude Desktop: tray icon → Quit, open it again, then confirm the `blender-hub` entry is still in the config file.
   - Claude Code: start a new session; `claude mcp list` (or `/mcp`) must show `blender-hub`.
   - Cursor: restart or reload the window, then enable `blender-hub` in Settings → MCP.
5. **Verify after restart:**
   1. `hub_list_workers()` → `{}` on a fresh install.
   2. `hub_acquire(mode="exclusive", label="install-check", gui=false)` → note `worker` and `token` (`gui=false` so the check does not leave a window open).
   3. `hub_exec(worker=<worker>, token=<token>, agent="install-check", code="result = bpy.app.version_string")` → the Blender version.
   4. `hub_release(worker=<worker>, token=<token>)`.

Ports: the hub uses **9881+** (installer smoke test 9961+, test suite 9981+) and never **9876**, the usual Blender MCP add-on port, so both can be installed side by side. Nothing to configure.

Uninstall (works even if Blender was removed; add `-Config "<path>"` if several Claude Desktop configs exist):
```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -ClaudeDesktop -Uninstall -Yes
```

## For AI assistants: how to use the tools
### The workflow
```
hub_acquire(mode="exclusive" | "shared", label="what you are doing")   # label is required: it is shown live
      -> {worker, token}                       # opens a Blender window the user watches; keep BOTH for the whole task
hub_exec(worker, token, agent="YourName", code="...")
      -> first call also returns agent_key     # keep it: needed to resume the agent later
hub_turntable(worker, token, agent="YourName", collection="Agent_YourName")
hub_measure(worker, token, a=..., b=...)  /  hub_view / hub_screenshot
hub_export_collection(worker, token, agent="YourName", collection="Agent_YourName", path="C:\\...\\part.blend")
hub_release(worker, token)   # when you are done
```

### Rules that matter
- **Always pass `token`**, and pass the **same `agent` name** to `hub_exec`, `hub_turntable` and `hub_export_collection` for the whole task (they create/bind your agent; add `agent_key` when resuming). Store `worker`, `token` and `agent_key` somewhere you will not lose them (e.g. your notes/plan). If your context gets summarized, you need them to continue.
- **Always give a `label`** (what you are doing, e.g. `"castle walls v2"`): it identifies your session in the window's live panel and in `hub_list_workers`. A lease created without one (Python API) takes the name of its first agent.
- **Leases:** `hub_lease` / `hub_acquire` default to `mode="exclusive"` — pass `mode="shared"` to join a shared scene. A lease lasts `ttl` seconds (default 900, max 3600) and every call renews it; an agent idle for longer loses it.
- **`hub_exec` runs Python inside Blender** with `bpy`, `bmesh`, `mathutils`, `np`, `math` available, plus:
  - `coll` — your collection `Agent_<agent>`; `bpy.ops` that create objects put them there automatically;
  - `link(ob)` — link an object made with `bpy.data.objects.new` into your collection;
  - `result = ...` — whatever you assign is returned as JSON (it must be JSON-serializable); `print()` output is returned as `stdout` (last 4000 characters).
- **Read every response**:
  - `violations` — you changed something owned by another agent (or by the file, owner `"base"`). Undo it unless the user asked for it.
  - `notices` — someone changed *your* objects. Re-check them before continuing.
  - `warning` — e.g. you were taken out of the user's Edit Mode, or a key was reissued.
  - `lease.expires_in` — seconds left; every call renews it.
- **Shared scene etiquette:** read anything, change only your own collection. To fit your part against another agent's part, call `hub_measure(a=<their object>, b=<yours>)` and move **your** object by `-delta_a_to_b`. Do not move theirs. `delta_a_to_b` is `null` when the objects already penetrate (`penetrating: true`): move yours apart first.
- **Exclusive** = you alone (the `"base"` objects from an opened `.blend` are yours too). **Shared** = several leases; nobody can upgrade to exclusive while others are inside.
- **Timeouts:** `hub_exec` has `timeout` (default 120 s, max 1800). Long C-level calls cannot be interrupted; if a worker is stuck, use `hub_kill_worker` (allowed only for the sole lease holder or after the job's own deadline + 60 s).
- **Reviewing your work:** `hub_turntable(worker, token, agent="<you>", collection="Agent_<you>", views=12)` returns one contact-sheet image with 12 angles (low to high). Use it instead of many single captures.
- **Live by default:** `hub_acquire` / `hub_start_worker` open a Blender **window** unless you pass `gui=false` (server default `BLENDER_HUB_GUI_DEFAULT=1`). One window per session: call `hub_view(collection=...)` after a meaningful step so the user's viewport frames your work, and `hub_screenshot` to see exactly what they see. Use `gui=false` for background or many-parallel batch work.
- **When you finish:** `hub_release`, then ask the user whether to close the window (`hub_stop_worker`). Windows never close on their own, are never recycled, and count against both the pool limit (10) and the window cap (4); when a limit is hit, the error lists the open windows to close.
- **Errors are actionable**: `your lease expired` → `hub_lease(worker, mode=<your original mode>, label=...)` for a new token, then pass `agent_key` in your next `hub_exec` / `hub_turntable` / `hub_export_collection`; `requires a valid lease token ... was RECYCLED ...` → `hub_restore_archive(archive)`; `already exists: resuming it requires its agent_key` → use your key, or `hub_agent_key` with a token that still uses the agent.
- Never stop or kill a worker another session is using; the hub refuses, so do not try to work around it.

### Typical multi-agent setup
1. A lead agent acquires one **shared** worker (a window by default, so the user watches everyone build; `gui=false` for headless) and shares `worker` with the other agents.
2. Every agent calls `hub_lease(worker, mode="shared", label=...)` to get its own token, then works only in `Agent_<name>`.
3. Parts are fitted with `hub_measure`; each agent reviews its part with `hub_turntable`.
4. To produce a final file: every agent `hub_export_collection(...)` → the lead acquires an **exclusive** worker (`gui=false`: no extra window just to merge) → `hub_merge(paths=[...])` → `hub_save(path)`.

For fully independent tasks (10 different assets), give each agent its own **exclusive** `hub_acquire`; they run truly in parallel (pass `gui=false` unless the user wants 10 windows).

## Tool reference
| Tool | Purpose |
|---|---|
| `hub_acquire` | Get a free worker from the pool (or start one) and lease it. Opens a window by default. Args: `mode`, `label`, `blend`, `ttl`, `gui` (default true). Returns `worker`, `token`. |
| `hub_lease` | Join a specific worker (e.g. a shared scene). Default `mode` is `exclusive`; pass `shared` to join others. Returns a token. |
| `hub_renew` / `hub_release` | Renew / release only your own lease. |
| `hub_exec` | Run Python in Blender as `agent` (required). Args: `code`, `timeout`, `agent_key`. |
| `hub_measure` | Min distance, intersecting faces, penetration (ray-parity), manifold flags and `delta_a_to_b` between two objects. `tol` = edge sample spacing. |
| `hub_turntable` | N renders around the target (Workbench) + a contact sheet returned as an image. `agent` required; `views` 1–48 (default 12), `size` 64–1024 (default 256). |
| `hub_view` | Window mode: frame the viewport on a collection/objects (keeps the user's selection). |
| `hub_screenshot` | Window mode: screenshot of the Blender window, returned as an image. |
| `hub_export_collection` | Save your collection to a `.blend`. `agent` required. |
| `hub_merge` | Append collections from several `.blend` files (exclusive lease). |
| `hub_save` | Save the worker's file (copy by default; in-place needs exclusive). |
| `hub_agent_key` | Get a new `agent_key` for an agent your token is currently using. |
| `hub_list_workers` | Status of all workers: leases, agents, owners, busy job, RAM, autosave. Never blocks on long jobs. |
| `hub_start_worker` / `hub_stop_worker` / `hub_restart_worker` | Lifecycle (stop refuses while other sessions hold leases; restart loads the newest autosave and keeps the window mode, also after the user closed the window). |
| `hub_kill_worker` | Kill a stuck worker and restart it from its autosave (sole lease holder, or job overdue). |
| `hub_list_archives` / `hub_restore_archive` | Work archived when a worker was recycled or its name reused; restore it into a new worker with its agent keys. |

## Watching live (window mode)
```powershell
python examples\demo_live.py          # opens a Blender window; two agents build a small scene
python examples\demo_live.py --close  # close it
```
The demo uses your real pool: its worker `live` takes one slot of the limit until you close it.
Window mode is the default for the MCP tools (`gui=false` or `BLENDER_HUB_GUI_DEFAULT=0` for background). The worker runs inside a normal Blender window: commands are served by a timer (≤ 30 ms per tick), so the UI stays responsive and you can orbit while agents work. Extra safety in window mode:
- your Edit/Sculpt mode is exited before an agent command (otherwise leaving Edit Mode would overwrite the agent's mesh change);
- autosave ~5 s after any change, including your own manual edits (not during animation playback); 20 autosaves kept;
- closing with the window's X asks "Save?" as usual; pool recycling never closes a window.

In window mode **you own everything**: your manual edits are not reported as violations.

**Live panel.** Every window shows, at the bottom-left of the 3D viewport (like Blender's own statistics overlay), how many agents are working in that Blender and who they are, updated every second:
```
Blender Hub  w1  |  3 agents working
castle    ·  running exec 3s  ·  lease 'castle walls v2' exclusive            (green: running now)
walls     ·  active, last command 12s ago  ·  lease 'walls' shared            (white: active in the last minute)
props     ·  idle 6m  ·  lease 'props pass' shared                            (grey: idle)
```
The same lines are in `hub_list_workers` (`overlay`). Turn it off with `BLENDER_HUB_OVERLAY=0`.

## Shared modules (your kit in every hub_exec)
Point `BLENDER_HUB_MODULES` at Python files and every worker loads them into the `hub_exec` namespace — no more pasting a 2000-line helper kit into each call:
```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -ClaudeDesktop -Module "K=C:\path\to\my_kit.py" -Test -Yes
```
- Format of the env var: `NAME=PATH` entries separated by `;` (`NAME` defaults to the file stem; ASCII letters, digits and `_`; `bpy`, `bmesh`, `mathutils`, `np`, `math`, `agent`, `coll`, `link`, `result` are reserved). Installer: repeat `--module NAME=PATH` for `install.py`, or give a comma list to `install.ps1 -Module "K=a.py","L=b.py"`. Each file is executed into its own dict, exactly like `K = {}; exec(open(path).read(), K)`, so code written for that pattern works unchanged: `K["Builder"](...)`.
- A changed file is reloaded on the next `hub_exec` (loading has a 60 s watchdog); a broken file, an invalid or a reserved name is reported in `hub_list_workers` (`modules`) and never blocks `hub_exec`.
- Every agent gets **its own instance** of each module (executed once per agent and file version): `K` is the kit functions' own globals, so `K["SCALE"] = 2; K["build"]()` works as in the plain pattern, and nothing an agent sets in its `K` reaches other agents. Modules must not change the scene when they are loaded.

## Configuration
Environment variables (set them in the MCP entry's `env`):

| Variable | Default | Meaning |
|---|---|---|
| `BLENDER_EXE` | auto-detect | Blender executable |
| `BLENDER_HUB_STATE` | `%LOCALAPPDATA%\blender-hub` | registry, logs, autosaves, archives, renders |
| `BLENDER_HUB_MAX_WINDOWS` | `4` | cap on open windows (they never idle-exit) |
| `BLENDER_HUB_MODULES` | — | `NAME=PATH;...` Python files preloaded into every `hub_exec` (see Shared modules) |
| `BLENDER_HUB_OVERLAY` | `1` | live panel in windows (who is working); `0` hides it |
| `BLENDER_HUB_GUI_DEFAULT` | `1` | MCP tools open a window unless `gui=false`; `0` makes background the default (the Python API always defaults to background) |
| `BLENDER_HUB_PORT_BASE` | `9881` | first worker port (9876 is always skipped) |
| `BLENDER_HUB_MAX_WORKERS` | `10` | pool limit |
| `BLENDER_HUB_MIN_FREE_MB` | `1500` | do not start a worker below this free RAM |
| `BLENDER_HUB_IDLE` | `1800` | background worker exits after N s with no lease and no command |
| `BLENDER_HUB_AUTOSAVE` | `300` | background autosave interval (s); window mode saves ~5 s after changes |
| `BLENDER_HUB_KILL_GRACE` | `60` | `hub_kill_worker` by time: seconds past the job's own deadline |

## Recovery
- **Worker crashed or exited idle:** `hub_restart_worker(name)` loads its newest autosave and keeps the agent keys.
- **Pool was full and your worker got recycled / its name reused:** your error message says `RECYCLED` and where the work went; `hub_list_archives` lists archives; `hub_restore_archive(archive)` brings it back as `<archive>-r`.
- **Lost `agent_key`:** `hub_agent_key` with the token that still uses the agent, or an exclusive lease reissues it (recorded; the old key's owner is told who and when).
- Archiving never deletes work: if a folder cannot be moved (open file handles), it is copied and verified; if that fails, the worker is not recycled.

## Testing
```powershell
python tests\test_suite.py --max-workers 10 --jobs 20
```
Uses its own state folder and ports **9981+**, so it never touches real workers or your Blender. It starts up to 10 Blenders (needs ~3 GB free RAM), briefly opens a few visible Blender windows (window-mode and MCP-default checks), and takes about 4 minutes. 133 checks: pool, identity, leases, 18 kinds of cross-agent changes, false-alarm cases, watchdog, kill, autosave/restore, measure accuracy, turntable, window mode, MCP stdio protocol. Results go to `tests/results/`.

## How it works / limits
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design, the guarantees and the known limits (no undo in background Blender, C calls cannot be interrupted, scene-wide settings are shared, detection cost ≈ 45 ms per command with 20k vertices owned by others).

## Security
- Workers listen on **127.0.0.1 only** and require a per-worker secret; leases require tokens. This separates sessions; it is **not** a sandbox.
- `hub_exec` runs **arbitrary Python** inside Blender with your user's permissions — the same trust level as any Blender MCP add-on. Only connect AI clients you trust.

## License
[MIT](LICENSE)
