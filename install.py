"""Blender Hub installer (Windows).

Registers hub.py as an MCP server for Claude Desktop, Claude Code and/or Cursor, after checking
Python, Blender and (optionally) running a smoke test. Safe to run again: it updates the entry.

    python install.py --check                      # only verify Python + Blender, change nothing
    python install.py --claude-desktop --test      # smoke test, then register (nothing registered if it fails)
    python install.py --claude-code                # register with `claude mcp add` (user scope)
    python install.py --cursor                     # register in ~/.cursor/mcp.json
    python install.py --print                      # print the JSON entry to paste by hand
    python install.py --claude-desktop --uninstall # remove the entry again

Options:
    --blender PATH   Blender executable (default: auto-detect, see blender_paths.py)
    --config PATH    Claude Desktop config file (default: auto-detect; required if several exist)
    --name NAME      MCP server name (default: blender-hub)
    --yes            do not ask before writing a config file

Every config file is backed up next to itself (<file>.bak-blender-hub-<timestamp>) before writing.
"""
import os
import re
import sys
import json
import glob
import time
import shutil
import argparse
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
HUB = os.path.join(HERE, "hub.py")
MIN_BLENDER = (4, 2)
TESTED_BLENDER = "5.3"


def say(msg, kind="info"):
    tag = {"ok": "[ OK ]", "warn": "[WARN]", "err": "[FAIL]", "info": "[....]"}[kind]
    print(f"{tag} {msg}", flush=True)


def fail(msg):
    say(msg, "err")
    sys.exit(1)


# ---------------------------------------------------------------- checks
def check_platform():
    if os.name != "nt":
        fail("Blender Hub currently supports Windows only (it uses msvcrt file locks and kernel32 process APIs).")
    if sys.version_info < (3, 10):
        fail(f"Python 3.10+ is required (this is {sys.version.split()[0]}).")
    say(f"Windows + Python {sys.version.split()[0]} ({sys.executable})", "ok")


def check_blender(path):
    sys.path.insert(0, HERE)
    from blender_paths import find_blender, candidates
    exe = os.path.abspath(path) if path else find_blender()
    if exe and os.path.basename(exe).lower().startswith("blender-launcher"):
        fail(f"{exe} is the Blender launcher; pass blender.exe from the same folder instead.")
    if not exe or not os.path.isfile(exe):
        found = candidates()
        fail("Blender not found. Install Blender 4.2+ or pass --blender \"C:\\path\\to\\blender.exe\"."
             + (f" Candidates seen: {found}" if found else ""))
    try:
        out = subprocess.run([exe, "-b", "--factory-startup", "--python-expr",
                              "import bpy; print('BLENDER_HUB_VERSION', bpy.app.version_string)"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        fail(f"could not run Blender at {exe}: {e}")
    m = re.search(r"BLENDER_HUB_VERSION (\d+)\.(\d+)\S*", out)
    if not m:
        fail(f"Blender at {exe} did not report its version. Output tail: {out[-400:]!r}")
    ver = (int(m.group(1)), int(m.group(2)))
    if ver < MIN_BLENDER:
        fail(f"Blender {m.group(0).split()[-1]} is too old: 4.2+ required.")
    note = "" if f"{ver[0]}.{ver[1]}" == TESTED_BLENDER else f" (tested on {TESTED_BLENDER}; others should work)"
    say(f"Blender {m.group(0).split()[-1]} at {exe}{note}", "ok")
    return exe


# ---------------------------------------------------------------- entry
def server_entry(blender):
    return {"command": sys.executable, "args": [HUB], "env": {"BLENDER_EXE": blender}}


def backup(path):
    if os.path.exists(path):
        base = f"{path}.bak-blender-hub-{time.strftime('%Y%m%d-%H%M%S')}"
        dst, n = base, 1
        while os.path.exists(dst):  # two writes in the same second must not overwrite the first backup
            dst, n = f"{base}-{n}", n + 1
        shutil.copy2(path, dst)
        say(f"backup: {dst}", "ok")


def confirm(msg, yes):
    if yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        fail(f"{msg} -> needs confirmation; re-run with --yes to write without asking.")
    return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")


def write_mcp_json(path, name, entry, yes, remove=False, key="mcpServers"):
    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as f:  # -sig: files saved by PowerShell 5.1 carry a BOM
                txt = f.read().strip()
            data = json.loads(txt) if txt else {}
        except json.JSONDecodeError as e:
            fail(f"{path} is not valid JSON ({e}); fix it by hand first, nothing was changed.")
    if not isinstance(data, dict):
        fail(f"{path}: the top level is not a JSON object; fix it by hand first, nothing was changed.")
    if data.get(key) is None:
        data[key] = {}
    servers = data[key]
    if not isinstance(servers, dict):
        fail(f"{path}: \"{key}\" is not a JSON object; fix it by hand first, nothing was changed.")
    if remove:
        if name not in servers:
            say(f"{name} is not registered in {path}", "warn")
            return
        if not confirm(f"Remove '{name}' from {path}?", yes):
            fail("cancelled")
        backup(path)
        servers.pop(name)
    else:
        action = "update" if name in servers else "add"
        if not confirm(f"{action.capitalize()} '{name}' in {path}?", yes):
            fail("cancelled")
        backup(path)
        servers[name] = entry
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp-blender-hub"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    say(f"{'removed' if remove else 'wrote'} '{name}' in {path}", "ok")


def claude_desktop_configs():
    appdata, local = os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", "")
    found = [os.path.join(appdata, "Claude", "claude_desktop_config.json")]
    found += glob.glob(os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude", "claude_desktop_config.json"))
    found += glob.glob(os.path.join(local, "Claude-Data", "*", "claude_desktop_config.json"))
    return [p for p in found if os.path.exists(p)]


def do_claude_desktop(args, entry):
    path = args.config
    if not path:
        cfgs = claude_desktop_configs()
        if len(cfgs) == 1:
            path = cfgs[0]
        elif not cfgs:
            if args.uninstall:
                say("no Claude Desktop config found: nothing to remove", "warn")
                return
            path = os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude_desktop_config.json")
            say(f"no Claude Desktop config found; creating {path}", "warn")
        else:
            fail("several Claude Desktop configs found; choose one with --config:\n  " + "\n  ".join(cfgs))
    write_mcp_json(path, args.name, entry, args.yes, remove=args.uninstall)
    say("quit Claude Desktop completely (tray icon -> Quit) and open it again so it "
        + ("drops" if args.uninstall else "loads") + " the server", "info")


def do_cursor(args, entry):
    path = os.path.join(os.path.expanduser("~"), ".cursor", "mcp.json")
    write_mcp_json(path, args.name, entry, args.yes, remove=args.uninstall)
    say("restart Cursor (or reload the window) and enable the server in Settings -> MCP", "info")


def do_claude_code(args, entry):
    claude = shutil.which("claude")
    if args.uninstall:
        cmd = ["claude", "mcp", "remove", "--scope", "user", args.name]
    else:
        cmd = ["claude", "mcp", "add", "--scope", "user", args.name,
               "-e", f"BLENDER_EXE={entry['env']['BLENDER_EXE']}", "--", entry["command"], HUB]
    if not claude:
        say("`claude` CLI not on PATH. Run this yourself:\n  " + subprocess.list2cmdline(cmd), "warn")
        return
    cmd[0] = claude
    if not args.uninstall:  # idempotent: drop an old entry with the same name first
        subprocess.run([claude, "mcp", "remove", "--scope", "user", args.name], capture_output=True, text=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        fail(f"claude mcp failed: {r.stderr.strip() or r.stdout.strip()}")
    say(f"{'removed' if args.uninstall else 'registered'} '{args.name}' in Claude Code (user scope); "
        "start a new Claude Code session and check with `claude mcp list`", "ok")


# ---------------------------------------------------------------- smoke test
def smoke_test(blender):
    """Starts one background worker in a throwaway state dir, runs the main tools, stops it."""
    state = tempfile.mkdtemp(prefix="blender-hub-install-test-")
    os.environ.update(BLENDER_EXE=blender, BLENDER_HUB_STATE=state, BLENDER_HUB_PORT_BASE="9961")
    sys.path.insert(0, HERE)
    import importlib
    import hub as hubmod
    importlib.reload(hubmod)
    h = hubmod.Hub()
    t0 = time.time()
    try:
        lease = h.acquire("exclusive", label="install-test")
        w, tok = lease["worker"], lease["token"]
        r = h.call(w, "exec", token=tok, agent="installer", code=(
            "bpy.ops.mesh.primitive_cube_add(location=(0,0,0)); bpy.context.object.name='a'\n"
            "bpy.ops.mesh.primitive_cube_add(location=(3,0,0)); bpy.context.object.name='b'\n"
            "result = sorted(o.name for o in coll.objects)"))
        assert r.get("ok") and r["result"]["result"] == ["a", "b"], r
        m = h.call(w, "measure", token=tok, a="a", b="b")
        assert m.get("ok") and abs(m["result"]["min_distance"] - 1.0) < 1e-3, m
        tt = h.call(w, "turntable", token=tok, agent="installer", collection="Agent_installer", views=4, size=128)
        assert tt.get("ok") and os.path.exists(tt["result"]["sheet"]), tt
        h.stop_worker(w, token=tok)
        say(f"smoke test passed in {time.time() - t0:.1f}s (worker start, exec, measure, turntable, stop)", "ok")
        os.environ.pop("BLENDER_HUB_STATE", None)
        os.environ.pop("BLENDER_HUB_PORT_BASE", None)
    except Exception as e:
        with __import__("contextlib").suppress(Exception):
            h.kill_all_unsafe()
        fail(f"smoke test failed: {e!r}  (worker logs kept in {state})")  # state kept on purpose
    shutil.rmtree(state, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="Install Blender Hub as an MCP server.")
    ap.add_argument("--blender")
    ap.add_argument("--config")
    ap.add_argument("--name", default="blender-hub")
    ap.add_argument("--claude-desktop", action="store_true")
    ap.add_argument("--claude-code", action="store_true")
    ap.add_argument("--cursor", action="store_true")
    ap.add_argument("--print", action="store_true", dest="print_entry")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    check_platform()
    if args.uninstall:  # removing must work even if Blender is gone
        entry = {"command": sys.executable, "args": [HUB], "env": {}}
        blender = None
    else:
        blender = check_blender(args.blender)
        entry = server_entry(blender)
    if args.check:
        say("check only: nothing was changed", "ok")
        return
    if args.test and not args.uninstall:  # test first: register only a hub that works
        smoke_test(blender)
    did = False
    if args.claude_desktop:
        do_claude_desktop(args, entry)
        did = True
    if args.claude_code:
        do_claude_code(args, entry)
        did = True
    if args.cursor:
        do_cursor(args, entry)
        did = True
    if args.print_entry or not (did or args.test or args.uninstall):
        print(json.dumps({"mcpServers": {args.name: entry}}, indent=2))
        if not did:
            say("nothing registered; use --claude-desktop / --claude-code / --cursor, or paste the JSON above", "info")


if __name__ == "__main__":
    main()
