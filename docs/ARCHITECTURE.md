# Blender Hub — architecture, guarantees and limits

## Why
A typical Blender MCP add-on gives one AI client one Blender. Its command queue runs on Blender's main thread, so a Blender executes one command at a time, and nothing tracks who owns what. With several agents that means:
- no real parallelism (one Blender),
- silent overwrites when two agents touch the same scene,
- no identity, no reservation, no recovery when an agent disappears.

Measured on Blender 5.3 (20 cores, 32 GB): a modeling step takes ~15–70 ms while an agent thinks 10–60 s between steps, so **Blender CPU is not the bottleneck**. The value of the hub is isolation, ownership, recovery and two tools that remove whole rounds of agent conversation: `hub_measure` (fit parts without asking the other agent for coordinates) and `hub_turntable` (12-angle review in one call and one image).

## Processes
```
MCP client (Claude Desktop/Code, Cursor...) --stdio--> hub.py (MCP server, stateless)
                                                       |  TCP 127.0.0.1, one JSON line per request
                        +------------------------------+-------------------------------+
                        v                              v                               v
          worker "w0" (blender -b, port 9881)  worker "w1" (shared scene)     worker "live" (window mode)
```
- **`worker.py`** runs inside Blender. A network thread accepts connections and answers `ping` / `status` / `check_token` itself (never blocked by a long job). Everything that touches `bpy` goes through a queue served on Blender's main thread: a blocking loop in background mode, or a `bpy.app.timers` tick (≤ 30 ms of queue per tick) in window mode.
- **`hub.py`** is both a library and an MCP stdio server. It holds no session state: identity is the lease token passed by the agent, so several conversations sharing one MCP process stay separate and a server restart locks nobody out. The worker registry lives in `%LOCALAPPDATA%\blender-hub\registry.json`, written atomically under an `msvcrt` lock.

## Identity and leases
- Every request carries the worker's **secret** (from the registry). Keys starting with `_` from outside are dropped.
- `lease` returns a **token**. `exclusive` = alone; `shared` = several participants, each with its own TTL. A token with a request queued or running never expires mid-job; each command renews it. Expired tokens get an explicit `your lease expired`.
- The first use of an **agent** name returns an **`agent_key`** (only its hash is stored, persisted next to the autosaves). Resuming the name after the lease ended requires the key. A lost key can be renewed by the token that still uses the agent (`hub_agent_key`) or reissued by an exclusive lease; reissues are recorded and shown to the old key's holder.

## Ownership and violation detection
- Each agent works in `Agent_<name>` (`hub_owner` custom property). Whatever came from the opened `.blend` is owned by `"base"` (exclusive leases own it).
- Before and after each scene-changing command the worker hashes everything owned by others: per-channel transforms (animated channels excluded, NLA included), mesh topology and all generic attributes (UV, colors, creases… not selection), vertex groups and weights, shape keys, pose bones, curves, modifiers (including Geometry Nodes inputs on 4.x and 5.x), constraints, custom properties, materials with every node, node group and color ramp, and collections (owner, link in scene, contents, visibility).
- A difference becomes a `violation` in the response and a `notice` delivered to the owner on their next call. There is **no undo** in background Blender: the rotating autosaves are the way back.
- Cost: ≈ 45 ms per command with 20k vertices owned by others, ≈ 160 ms with 150k.

## Watchdog and stuck workers
- `hub_exec` has a timeout enforced by a watchdog thread that raises an async exception in the main thread **only while the agent's code is on the stack** (generation counter + frame check), re-sent every second if the code swallows it. No per-line tracing cost.
- C-level calls (a huge boolean, `time.sleep`) cannot be interrupted. `hub_kill_worker` kills the process (after verifying it is really that `blender.exe` by image name and creation time) when the caller holds the only lease, or when the running job is past its own deadline + grace. The worker comes back from its newest autosave.

## Pool, lifecycle and archives
- `hub_acquire` reuses a worker with no lease, no owned collections and the same `gui` mode, or starts a new one within the limit and the free-RAM floor.
- Background workers exit after 30 min with no lease and no command, saving first. Autosaves rotate (5 kept; 20 in window mode).
- When the pool is full, the idle worker unused for the longest is **recycled**. Windows are never recycled. Its autosaves and keys move to `autosave/<name>-<date>/` and a record goes to `recycled.json`. The same archiving happens whenever a worker name is reused after an idle exit or a crash.
- Archiving is verified. It tries a rename with retries, then a copy whose `.blend` list is compared to the original. If neither works, the worker is not recycled and no worker starts under that name.
- A returning agent with an old token gets `was RECYCLED at ... archived in ...`, and `hub_restore_archive` brings the work back with its keys.

## Window mode
- Default for the MCP tools (`gui` omitted = window, configurable with `BLENDER_HUB_GUI_DEFAULT`): each session gets its own Blender window, so the user can watch and orbit while agents work. The Python API defaults to background. Windows never idle-exit and are never recycled, so they have their own cap (`BLENDER_HUB_MAX_WINDOWS`, 4); the mode is stored in `autosave/<name>/meta.json` so restart, kill and restore keep it.
- The UI is protected in four ways:
  - the user's Edit/Sculpt mode is exited before agent commands;
  - an autosave runs ~5 s after any change, including manual edits, but not during playback or selection-only changes;
  - closing through the hub skips the "Save?" prompt, while the window's X keeps it;
  - the user's selection is restored after `hub_view`.

## Measure
- Samples are vertices plus points along every edge (spacing `tol`, default 0.02, widened before allocating for huge meshes), from both sides, against the other object's BVH.
- Penetration candidates are confirmed by 3-direction ray parity on a random sample; open meshes report `manifold: false`.
- `delta_a_to_b` (point on B − point on A) is `None` when the objects penetrate.

## Known limits (by design)
- Scene-wide settings (units, world, frame range) are shared without ownership.
- Any session can restore an archive or, with an exclusive lease, reissue the key of an agent with no live token — broad on purpose (recover work of an agent that vanished), and always recorded. Lease labels are informative, not authentication.
- Kill-by-time deadlines: `exec` uses its own `timeout`, turntable 1800 s, every other command a fixed 600 s.
- A single autosave copy taking more than 180 s could lose the name reservation.
- The window cap is checked before a window boots but not atomically with it: two sessions opening windows at the same instant can exceed it by one.
- Windows only.
