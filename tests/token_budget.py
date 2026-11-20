"""Token budget of the MCP surface: tool definitions, typical responses and images.

    python tests/token_budget.py [--json out.json]

Runs hub.py as a real MCP stdio server against a throwaway state dir (ports 9971+), opens one
windowed worker, and measures what an agent pays for each step. Text tokens are estimated as
characters / 3.6 (typical for JSON); image tokens as width * height / 750 (Claude's image rule).
"""
import os
import sys
import json
import time
import base64
import struct
import tempfile
import subprocess
import threading
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATE = os.path.join(tempfile.gettempdir(), "blender-hub-token-budget")


def tok_text(s):
    return round(len(s) / 3.6)


def png_size(b64):
    raw = base64.b64decode(b64[:200] + "=" * (-len(b64[:200]) % 4))
    w, h = struct.unpack(">II", raw[16:24])
    return w, h


def main():
    env = dict(os.environ, BLENDER_HUB_STATE=STATE, BLENDER_HUB_PORT_BASE="9971")
    sys.path.insert(0, ROOT)
    os.environ.update(BLENDER_HUB_STATE=STATE, BLENDER_HUB_PORT_BASE="9971")
    import hub as hubmod
    hubmod.Hub().kill_all_unsafe()
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "hub.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env)
    replies, lock = {}, threading.Lock()

    def reader():
        for line in p.stdout:
            m = json.loads(line)
            with lock:
                replies[m.get("id")] = m
    threading.Thread(target=reader, daemon=True).start()
    n = [0]

    def rpc(method, params=None, t=180):
        n[0] += 1
        i = n[0]
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
        p.stdin.flush()
        t0 = time.time()
        while time.time() - t0 < t:
            with lock:
                if i in replies:
                    return replies.pop(i)
            time.sleep(0.02)
        raise TimeoutError(method)

    def call(_tool, **args):
        r = rpc("tools/call", {"name": _tool, "arguments": args})["result"]
        if r.get("isError"):
            raise RuntimeError(f"{_tool}: " + "".join(c.get("text", "") for c in r["content"])[:400])
        return r

    out = {}
    try:
        _run(rpc, call, out)
    finally:  # never leave a window or the server behind, whatever failed
        with contextlib.suppress(Exception):
            p.stdin.close()
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
        with contextlib.suppress(Exception):
            hubmod.Hub().kill_all_unsafe()
    _report(out)


def _run(rpc, call, out):
    init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "budget"}})
    out["instructions"] = tok_text(init["result"].get("instructions", ""))
    tools = rpc("tools/list")["result"]["tools"]
    out["tool_definitions"] = tok_text(json.dumps(tools))
    out["tool_count"] = len(tools)

    def text_of(r):
        return "".join(c.get("text", "") for c in r["content"] if c.get("type") == "text")

    def measure(label, r):
        txt = text_of(r)
        imgs = [png_size(c["data"]) for c in r["content"] if c.get("type") == "image"]
        out[label] = {"text_tokens": tok_text(txt), "image_tokens": sum(round(w * h / 750) for w, h in imgs),
                      "images": imgs}
        return r

    acq = measure("hub_acquire", call("hub_acquire", label="token budget", gui=True))
    d = json.loads(text_of(acq))
    w, t = d["worker"], d["token"]
    measure("hub_note", call("hub_note", worker=w, token=t, agent="A", task="measuring the token budget"))
    measure("hub_exec_small", call("hub_exec", worker=w, token=t, agent="A", note="add a cube",
                                   code="bpy.ops.mesh.primitive_cube_add(); result = 1"))
    measure("hub_exec_rock", call("hub_exec", worker=w, token=t, agent="A", note="rock",
                                  code="bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, location=(3,0,0)); bpy.context.object.name='rock'; result = {'faces': len(bpy.context.object.data.polygons)}"))
    measure("hub_measure", call("hub_measure", worker=w, token=t, a="Cube", b="rock"))
    measure("hub_turntable_12", call("hub_turntable", worker=w, token=t, agent="A", collection="Agent_A", views=12))
    measure("hub_screenshot", call("hub_screenshot", worker=w, token=t))
    measure("hub_list_workers_1", call("hub_list_workers"))
    measure("hub_release", call("hub_release", worker=w, token=t))
    call("hub_stop_worker", name=w)


def _report(out):
    # a typical modeling session: 1 acquire, 1 note, 25 exec, 4 turntables, 4 screenshots, 2 list, 1 release
    per = {k: v["text_tokens"] + v["image_tokens"] for k, v in out.items() if isinstance(v, dict)}
    session = (out["tool_definitions"] + out["instructions"] + per["hub_acquire"] + per["hub_note"]
               + 25 * per["hub_exec_small"] + 4 * per["hub_turntable_12"] + 4 * per["hub_screenshot"]
               + 2 * per["hub_list_workers_1"] + per["hub_release"])
    out["typical_session_estimate"] = session
    print(json.dumps(out, indent=1))
    if "--json" in sys.argv:
        with open(sys.argv[sys.argv.index("--json") + 1], "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
