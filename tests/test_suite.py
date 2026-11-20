"""Blender Hub test suite: performance + one check per guarantee (identity, leases, ownership, recovery, window mode, MCP).

    python tests/test_suite.py [--max-workers 10] [--jobs 20]

Uses its own registry and ports (temp folder, ports 9981+): never touches a real session
worker nor the user's windowed Blender (9876). Writes results/suite-<timestamp>.json.
"""
import os
import sys
import json
import time
import socket
import threading
import subprocess
import statistics
import tempfile
import contextlib
import base64

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ["BLENDER_HUB_STATE"] = os.path.join(tempfile.gettempdir(), "blender-hub-bench-state")
os.environ["BLENDER_HUB_PORT_BASE"] = "9981"
os.environ["BLENDER_HUB_GUI_DEFAULT"] = "1"  # the suite checks the shipped default, whatever the dev's env says
# a module every worker loads into hub_exec (BLENDER_HUB_MODULES), plus a broken one that must not block exec
_MODDIR = os.path.join(tempfile.gettempdir(), "blender-hub-suite-modules")
os.makedirs(_MODDIR, exist_ok=True)
MOD_OK = os.path.join(_MODDIR, "suite_kit.py")
MOD_BAD = os.path.join(_MODDIR, "broken_mod.py")
with open(MOD_OK, "w", encoding="utf-8") as _f:
    _f.write("VALUE = 1\ndef double(x):\n    return 2 * x\ndef get_value():\n    return VALUE\n")
with open(MOD_BAD, "w", encoding="utf-8") as _f:
    _f.write("raise RuntimeError('broken on purpose')\n")
os.environ["BLENDER_HUB_MODULES"] = f"KT={MOD_OK};{MOD_BAD};bad-name={MOD_OK};np={MOD_OK}"
os.environ.setdefault("BLENDER_HUB_IDLE", "600")
import hub as hubmod  # noqa: E402
from hub import Hub, free_mb  # noqa: E402

ARGS = sys.argv[1:]
MAXW = int(ARGS[ARGS.index("--max-workers") + 1]) if "--max-workers" in ARGS else 10
JOBS = int(ARGS[ARGS.index("--jobs") + 1]) if "--jobs" in ARGS else 20
OUT = os.path.join(HERE, "results")
TMP = os.path.join(tempfile.gettempdir(), "blender-hub-bench")
os.makedirs(OUT, exist_ok=True)
os.makedirs(TMP, exist_ok=True)
R = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "checks": {}, "errors": [], "perf": {}}
H = Hub()
KEYS = {}

JOB = r'''
import random
random.seed(SEED)
bm = bmesh.new()
bmesh.ops.create_icosphere(bm, subdivisions=4, radius=1.0)
for v in bm.verts:
    v.co *= 1.0 + random.uniform(-0.12, 0.12)
bmesh.ops.dissolve_limit(bm, angle_limit=0.06, verts=bm.verts, edges=bm.edges)
me = bpy.data.meshes.new(f"rock_{agent}_{SEED}")
bm.to_mesh(me); bm.free()
ob = link(bpy.data.objects.new(me.name, me))
ob.location = (SEED % 10 * 3.0, SEED // 10 * 3.0, 0)
mod = ob.modifiers.new("bev", "BEVEL"); mod.width = 0.03; mod.segments = 2
dg = bpy.context.evaluated_depsgraph_get()
me2 = bpy.data.meshes.new_from_object(ob.evaluated_get(dg))
ob.modifiers.clear(); old = ob.data; ob.data = me2; bpy.data.meshes.remove(old)
me2.uv_layers.new(name="UV")
attr = me2.color_attributes.new("Col", "BYTE_COLOR", "CORNER")
mat = bpy.data.materials.get(f"mat_{agent}") or bpy.data.materials.new(f"mat_{agent}")
mat.use_nodes = True
me2.materials.append(mat)
result = {"name": ob.name, "faces": len(me2.polygons)}
'''
CLEAN = "for o in list(bpy.data.objects): bpy.data.objects.remove(o)\nfor m in list(bpy.data.meshes): bpy.data.meshes.remove(m)"


def check(name, cond, detail=None):
    R["checks"][name] = {"ok": bool(cond), "detail": detail}
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"), flush=True)
    return bool(cond)


def res(r, what):
    if not r.get("ok"):
        R["errors"].append(f"{what}: {r.get('error')}")
        return None
    out = r["result"]
    if isinstance(out, dict) and out.get("error"):
        R["errors"].append(f"{what}: {out['error'][-300:]}")
    return out


def ex(worker, token, agent, code, **kw):
    if agent in KEYS and "agent_key" not in kw:
        kw["agent_key"] = KEYS[agent]
    r = H.call(worker, "exec", token=token, agent=agent, code=code, **kw)
    if r.get("agent_key"):
        KEYS[agent] = r["agent_key"]
    return r


def main():
    H.kill_all_unsafe()
    import shutil
    for sub in ("autosave", "keys", "renders"):  # the suite's own state: starts clean on every run
        shutil.rmtree(os.path.join(os.environ["BLENDER_HUB_STATE"], sub), ignore_errors=True)
    with contextlib.suppress(OSError):
        os.remove(os.path.join(os.environ["BLENDER_HUB_STATE"], "recycled.json"))
    R["free_mb_before"] = free_mb()

    # ================= performance: pool of MAXW exclusive leases in parallel
    leases, errs = {}, []

    def acq(i):
        try:
            leases[i] = Hub().acquire("exclusive", label=f"bench{i}")
        except Exception as e:
            errs.append(repr(e))
    t0 = time.perf_counter()
    th = [threading.Thread(target=acq, args=(i,)) for i in range(MAXW)]
    [t.start() for t in th]
    [t.join() for t in th]
    R["perf"]["acquire_parallel_s"] = round(time.perf_counter() - t0, 2)
    workers = sorted({v["worker"] for v in leases.values()})
    check("pool: MAXW acquires become MAXW distinct workers", len(workers) == MAXW and not errs, {"workers": workers, "errs": errs})
    tok = {v["worker"]: v["token"] for v in leases.values()}
    R["perf"]["rss_idle_mb"] = {w: H.status(w)["rss_mb"] for w in workers}
    R["free_mb_after_boot"] = free_mb()
    try:
        Hub().acquire("exclusive", label="extra")
        check("pool: worker limit respected", False, "started the 11th")
    except RuntimeError as e:
        check("pool: worker limit respected", "limit" in str(e), str(e))
    try:
        H.start_worker("extra_direct", limit=MAXW)
        check("limit also applies to start_worker (MCP path)", False, "started")
    except RuntimeError as e:
        check("limit also applies to start_worker (MCP path)", "limit" in str(e), str(e))

    def run(names, jobs):
        per = {n: [] for n in names}
        for j in range(jobs):
            per[names[j % len(names)]].append(j)
        ms = []

        def work(n):
            h = Hub()
            for j in per[n]:
                r = h.call(n, "exec", token=tok[n], agent=f"b_{n}", code=JOB.replace("SEED", str(j)),
                           agent_key=KEYS.get(f"b_{n}"))
                if r.get("agent_key"):
                    KEYS[f"b_{n}"] = r["agent_key"]
                res(r, f"job {j} {n}")
                ms.append(r.get("ms", 0) + r.get("wait_ms", 0))
        t0 = time.perf_counter()
        th = [threading.Thread(target=work, args=(n,)) for n in names]
        [t.start() for t in th]
        [t.join() for t in th]
        return round(time.perf_counter() - t0, 3), ms
    R["perf"]["throughput"] = {}
    for k in sorted({1, 2, 5, MAXW}):
        for n in workers[:k]:
            res(ex(n, tok[n], f"b_{n}", CLEAN), "clean")
        wall, ms = run(workers[:k], JOBS)
        R["perf"]["throughput"][k] = {"wall_s": wall, "job_ms_median": round(statistics.median(ms), 1)}
    base = R["perf"]["throughput"][1]["wall_s"]
    for v in R["perf"]["throughput"].values():
        v["speedup"] = round(base / v["wall_s"], 2)

    for w in workers[4:]:
        H.stop_worker(w, token=tok[w])
    W1, W2, W3, W4 = workers[:4]
    res(ex(W1, tok[W1], f"b_{W1}", CLEAN + "\nfor c in list(bpy.data.collections): bpy.data.collections.remove(c)"), "clean W1")
    res(ex(W2, tok[W2], f"b_{W2}", CLEAN + "\nfor c in list(bpy.data.collections): bpy.data.collections.remove(c)"), "clean W2")
    for w in workers[:4]:
        H.call(w, "release", token=tok[w])

    # ================= the pool does not hand out a "dirty" worker (one holding someone's collection)
    g = Hub().acquire("exclusive", label="pool-clean")
    check("pool: does not hand out a worker with someone else's work in the scene", g["worker"] in (W1, W2), g["worker"])
    H.call(g["worker"], "release", token=g["token"])

    # ================= full pool: 11th session after 10 releases -> recycles the free worker idle the longest
    old_max = hubmod.MAX_WORKERS
    hubmod.MAX_WORKERS = len(H.workers())
    busy1 = res(H.call(W1, "lease", mode="shared", label="busy1"), "busy1")["token"]
    busy2 = res(H.call(W2, "lease", mode="shared", label="busy2"), "busy2")["token"]
    # handle open inside the autosave folder (like Defender/indexer/Explorer): renaming the folder fails
    holds = []
    for wn in (W3, W4):
        d = os.path.join(os.environ["BLENDER_HUB_STATE"], "autosave", wn)
        os.makedirs(d, exist_ok=True)
        hp = os.path.join(d, "hold.txt")
        with open(hp, "w") as f:
            f.write("open handle")
        holds.append(open(hp, "rb"))
    g = _safe(lambda: Hub().acquire("exclusive", label="11th"))
    for hf in holds:
        hf.close()
    st = H.status(g["worker"]) if isinstance(g, dict) and g.get("worker") else {}
    arch_blends = hubmod._blends(g["recycled"]["archived"]) if isinstance(g, dict) and g.get("recycled") else []
    check("full pool: recycles a free worker (starts clean, work archived)",
          isinstance(g, dict) and g.get("recycled") and g.get("worker") in (W3, W4) and not st.get("owners") and arch_blends,
          (g, st.get("owners")))
    check("archive with an open handle: rename fails, verified copy keeps every .blend",
          isinstance(g, dict) and g.get("recycled") and arch_blends == g["recycled"]["saved"]["blends"]
          and os.path.isdir(os.path.join(os.environ["BLENDER_HUB_STATE"], "autosave", g["worker"])), arch_blends)
    if isinstance(g, dict) and g.get("token"):
        H.call(g["worker"], "release", token=g["token"])
    H.call(W1, "release", token=busy1)
    H.call(W2, "release", token=busy2)
    hubmod.MAX_WORKERS = old_max
    if isinstance(g, dict) and g.get("worker"):
        rw = g["worker"]
        ag = f"b_{rw}"
        r = H.call(rw, "exec", token=tok[rw], agent=ag, agent_key=KEYS.get(ag), code="result = 1")
        check("an owner returning to the recycled worker is warned and gets the archive folder",
              not r["ok"] and "RECYCLED" in r["error"] and (r.get("recycled") or {}).get("archived"), r.get("error"))
        arch = (r.get("recycled") or {}).get("archived")
        rest = _safe(lambda: H.restore_archive(arch))
        nm = rest.get("name") if isinstance(rest, dict) else None
        ok_back = False
        if nm:
            lt = res(H.call(nm, "lease", mode="shared", label="back"), "restored lease")["token"]
            r2 = ex(nm, lt, ag, "result = len(coll.objects)")
            ok_back = r2["ok"] and (r2["result"] or {}).get("result", 0) > 0
            H.call(nm, "release", token=lt)
            H.stop_worker(nm)
        check("hub_restore_archive brings the work back and the agent resumes in shared with its key", ok_back, rest)
        check("restore: default name with -r (the archived snapshot stays intact)", bool(nm) and nm.endswith("-r") and hubmod._blends(arch), nm)
        again = _safe(lambda: H.restore_archive(arch))
        check("restoring the same archive again points to hub_restart_worker (does not swap the restored worker's keys)",
              isinstance(again, dict) and "hub_restart_worker" in again.get("error", ""), again)
        recs = H.recycled(rw)
        check("the recycling record notes where it was restored", recs and recs[-1].get("restored_as") == nm, recs[-1:] if recs else recs)
        # the "RECYCLED" hint does not show up for a lease that merely expired on the replacement
        ce = res(H.call(rw, "lease", mode="shared", label="short-replacement", ttl=2), "short replacement lease")["token"]
        time.sleep(3.2)
        r = H.call(rw, "exec", token=ce, agent="CE", code="result = 1")
        check("a lease that only expired on the replacement does not get the recycling hint",
              not r["ok"] and "expired" in r["error"] and "RECYCLED" not in r["error"], r.get("error"))
        r = _safe(lambda: H.restore_archive(os.path.join(os.environ["BLENDER_HUB_STATE"], "autosave", rw)))
        check("restore: refuses a folder that is not a registered recycling archive", isinstance(r, dict) and "registered" in r.get("error", ""), r)
        # archiving fails completely: does NOT recycle, gives the worker back and nothing is lost
        other = W4 if rw == W3 else W3
        oag = f"b_{other}"
        old_max = hubmod.MAX_WORKERS
        hubmod.MAX_WORKERS = len(H.workers())
        occ = [res(H.call(n, "lease", mode="shared", label="busy"), "busy")["token"] for n in (W1, W2, rw)]
        real_arch = hubmod._archive_worker_files

        def boom(name, arch):
            raise OSError("disk refused (simulated)")
        hubmod._archive_worker_files = boom
        g2 = _safe(lambda: Hub().acquire("exclusive", label="12th"))
        hubmod._archive_worker_files = real_arch
        for n, t_ in zip((W1, W2, rw), occ):
            H.call(n, "release", token=t_)
        hubmod.MAX_WORKERS = old_max
        check("archiving failed: does not recycle and explains", isinstance(g2, dict) and "did not recycle" in g2.get("error", ""), g2)
        st = _safe(lambda: H.status(other))
        lt = res(H.call(other, "lease", mode="shared", label="owner-back"), "lease other")
        r = ex(other, lt["token"], oag, "result = len(coll.objects)") if lt else {"ok": False}
        check("archiving failed: worker comes back from autosave and the owner resumes with the key (nothing deleted)",
              r.get("ok") and (r["result"] or {}).get("result", 0) > 0, (st, r.get("error")))
        if lt:
            H.call(other, "release", token=lt["token"])

    # ================= really corrupted registry: becomes .bad and starts over empty
    saved_reg = hubmod.REGISTRY
    hubmod.REGISTRY = os.path.join(TMP, "reg_corrupt.json")
    with open(hubmod.REGISTRY, "w") as f:
        f.write("{garbage")
    got = _safe(hubmod._read_reg)
    bads = [n for n in os.listdir(TMP) if n.startswith("reg_corrupt.json.") and n.endswith(".bad")]
    hubmod.REGISTRY = saved_reg
    check("corrupted registry: keeps .bad and starts over empty", got == {} and bads, (got, bads))

    # ================= registry: concurrent reads and writes (os.replace on Windows)
    stop, rerrs = time.time() + 2.0, []

    def writer():
        while time.time() < stop:
            try:
                with hubmod._locked():
                    reg = hubmod._read_reg()
                    hubmod._write_reg(reg)
            except Exception as e:
                rerrs.append(repr(e))

    def reader():
        while time.time() < stop:
            try:
                hubmod._read_reg()
            except Exception as e:
                rerrs.append(repr(e))
    th = [threading.Thread(target=writer) for _ in range(2)] + [threading.Thread(target=reader) for _ in range(6)]
    [t.start() for t in th]
    [t.join() for t in th]
    check("registry: 2 s of concurrent reads+writes without errors", not rerrs, rerrs[:3])
    with hubmod._locked():
        reg = hubmod._read_reg()
        reg["ghost"] = {"name": "ghost", "port": 9999, "pid": 0, "secret": "x", "started": time.time() - 1000}
        hubmod._write_reg(reg)
    check("registry: old pid=0 entry is cleaned up", "ghost" not in H.workers(clean=True))

    # ================= identity and leases (shared)
    a = res(H.call(W1, "lease", mode="shared", label="sessionA"), "lease A")["token"]
    b = res(H.call(W1, "lease", mode="shared", label="sessionB"), "lease B")["token"]
    for j in range(3):
        res(ex(W1, a, "A", JOB.replace("SEED", str(j))), "A job")
        res(ex(W1, b, "B", JOB.replace("SEED", str(j + 50))), "B job")
    check("agent_key: returned on the agent's first use", "A" in KEYS and "B" in KEYS, list(KEYS))
    r = H.call(W1, "exec", token=b, agent="A", code="result = 1")
    check("identity: B cannot use agent A's name", not r["ok"] and "another lease" in r["error"], r.get("error"))
    r = H.call(W1, "exec", token=None, agent="C", code="result = 1")
    check("identity: command without a token is refused", not r["ok"] and "token" in r["error"], r.get("error"))
    r = H._conn(W1).call({"cmd": "exec", "secret": "wrong", "token": a, "agent": "A", "code": "result=1"})
    check("identity: wrong secret is refused", not r["ok"] and "secret" in r["error"], r.get("error"))
    r = H._conn(W1).call({"cmd": "exec", "secret": H._w(W1)["secret"], "token": b, "agent": "Z", "_agent": "A", "code": "result = agent"})
    check("'_' keys from outside are discarded", r["ok"] and r["result"]["result"] == "Z", r.get("result"))
    r = H.call(W1, "lease", mode="exclusive", label="grab")
    check("lease: exclusive refused while others are inside", not r["ok"], r.get("error"))
    res(H.call(W1, "release", token=a), "release A")
    r = ex(W1, b, "B", "result = len(coll.objects)")
    check("lease: releasing A does not take B down", r["ok"] and r["result"]["result"] == 3, r)
    r = H.call(W1, "exec", token=a, agent="A", code="result = 1")
    check("lease: a released token stops working", not r["ok"], r.get("error"))
    intr = res(H.call(W1, "lease", mode="shared", label="intruder"), "intruder lease")["token"]
    r = H.call(W1, "exec", token=intr, agent="A", code="result = 1")
    check("agent_key: another session cannot take name A after the release", not r["ok"] and "agent_key" in r["error"], r.get("error"))
    res(H.call(W1, "release", token=intr), "release intruder")
    a = res(H.call(W1, "lease", mode="shared", label="sessionA2"), "lease A2")["token"]
    r = ex(W1, a, "A", "result = len(coll.objects)")
    check("agent_key: A resumes its own agent with the key", r["ok"] and r["result"]["result"] == 3, r.get("error"))
    r = H.call(W1, "agent_key", token=a, agent="A")
    newk = (r.get("result") or {}).get("agent_key")
    old_k, KEYS["A"] = KEYS.get("A"), newk or KEYS.get("A")
    check("lost agent_key: hub_agent_key with the token using the agent returns a new key", r["ok"] and newk and newk != old_k, r.get("error"))
    r = H.call(W1, "agent_key", token=b, agent="A")
    check("agent_key: another token cannot ask for A's key", not r["ok"], r.get("error"))
    sL = res(H.call(W2, "lease", mode="shared", label="loses-key"), "lease L")["token"]
    r0 = H.call(W2, "exec", token=sL, agent="L", code="result = 1")
    oldL = r0.get("agent_key")
    H.call(W2, "release", token=sL)
    xL = res(H.call(W2, "lease", mode="exclusive", label="reissuer"), "lease xL")["token"]
    r = H.call(W2, "exec", token=xL, agent="L", code="result = 1")
    check("lost agent_key: an exclusive lease reissues it with a warning", r["ok"] and r.get("agent_key") and r.get("warning"), (r.get("error"), r.get("warning")))
    H.call(W2, "release", token=xL)
    sL2 = res(H.call(W2, "lease", mode="shared", label="owner-back"), "owner back lease")["token"]
    r = H.call(W2, "exec", token=sL2, agent="L", agent_key=oldL, code="result = 1")
    check("an owner returning with the old key learns who reissued it and when",
          not r["ok"] and "reissued" in r["error"] and "'reissuer'" in r["error"], r.get("error"))
    st = H.status(W2)
    check("status shows the agent's last key reissue", (st.get("key_reissues") or {}).get("L", {}).get("by") == "reissuer", st.get("key_reissues"))
    H.call(W2, "release", token=sL2)
    c = res(H.call(W1, "lease", mode="shared", label="short", ttl=2), "short lease")["token"]
    time.sleep(3.2)
    r = H.call(W1, "exec", token=c, agent="C", code="result = 1")
    check("lease: an expired lease says 'expired'", not r["ok"] and "expired" in r["error"], r.get("error"))
    q = res(H.call(W1, "lease", mode="shared", label="long-job", ttl=2), "long lease")["token"]
    r1 = ex(W1, q, "Q", "import time\nt = time.time()\nwhile time.time() - t < 3.5: pass\nresult = 1", timeout=10)
    r2 = ex(W1, q, "Q", "result = 2")
    check("lease: does not expire during its own job (ttl 2 s, job 3.5 s)", r1["ok"] and r2["ok"], (r1.get("error"), r2.get("error")))
    H.call(W1, "release", token=q)
    r = ex(W1, b, "B", "result = 1")
    check("lease: the reply carries the lease state", r.get("lease", {}).get("active") is True and "expires_in" in r["lease"], r.get("lease"))
    # modules: BLENDER_HUB_MODULES loads files into every hub_exec (as `KT` and by file stem), reloads on change
    r = ex(W1, b, "B", "result = (KT['VALUE'], KT['double'](21), 'broken_mod' in globals())")
    st_m = H.status(W1).get("modules") or {}
    check("modules: loaded into hub_exec under their name; a broken one is reported and does not block exec",
          r["ok"] and r["result"]["result"] == [1, 42, False] and st_m.get("KT") == "ok"
          and "broken on purpose" in (st_m.get("broken_mod") or ""), (r.get("result"), st_m))
    time.sleep(1.1)  # mtime resolution
    with open(MOD_OK, "w", encoding="utf-8") as f:
        f.write("VALUE = 2\ndef double(x):\n    return 2 * x\ndef get_value():\n    return VALUE\n")
    r = ex(W1, b, "B", "result = KT['VALUE']")
    check("modules: an edited module file is reloaded on the next hub_exec", r["ok"] and r["result"]["result"] == 2, r.get("result"))
    st_m = H.status(W1).get("modules") or {}
    check("modules: invalid and reserved names are reported (not silently dropped)",
          "invalid name" in (st_m.get("bad-name") or "") and "reserved" in (st_m.get("np") or ""), st_m)
    ex(W1, b, "B", "KT['leak'] = 1")
    r = ex(W1, a, "A", "result = 'leak' in KT")
    check("modules: each agent gets its own instance (one agent's KT['x'] does not leak)", r["ok"] and r["result"]["result"] is False, r.get("result"))
    r = ex(W1, b, "B", "KT['VALUE'] = 7; result = KT['get_value']()")
    r2 = ex(W1, a, "A", "result = KT['get_value']()")
    check("modules: configuring through K reaches the kit's own functions (K = {}; exec(src, K) pattern), per agent",
          r["ok"] and r["result"]["result"] == 7 and r2["ok"] and r2["result"]["result"] == 2,
          (r.get("result"), r2.get("result")))
    un = res(H.call(W1, "lease", mode="shared"), "unnamed lease")["token"]
    r = H.call(W1, "exec", token=un, agent="A", code="result = 1")  # A belongs to another lease: bind fails
    labels = [p["label"] for p in H.status(W1).get("participants", [])]
    check("unnamed lease is NOT renamed to an agent whose bind failed", not r["ok"] and "A" not in labels and "unnamed" in labels,
          (r.get("error"), labels))
    H.call(W1, "release", token=un)

    # ================= intrusion: every way of touching what belongs to someone else
    res(ex(W1, a, "A", """
ob = bpy.data.objects['rock_A_0']
ob.modifiers.new('sub', 'SUBSURF').levels = 1
m = bpy.data.materials['mat_A']; nt = m.node_tree
mix = nt.nodes.new('ShaderNodeMix'); mix.name = 'mix'
ng = bpy.data.node_groups.new('gnA', 'GeometryNodeTree')
ng.interface.new_socket('Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
ng.interface.new_socket('Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
ng.interface.new_socket('Size', in_out='INPUT', socket_type='NodeSocketFloat')
gi = ng.nodes.new('NodeGroupInput'); go = ng.nodes.new('NodeGroupOutput')
ng.links.new(gi.outputs[0], go.inputs[0])
o1 = bpy.data.objects['rock_A_1']
gm = o1.modifiers.new('gn', 'NODES'); gm.node_group = ng
o2 = bpy.data.objects['rock_A_2']
vg = o2.vertex_groups.new(name='weight'); vg.add([0, 1, 2], 1.0, 'REPLACE')
o2.keyframe_insert('location', frame=1)
bpy.ops.object.armature_add(location=(0, -10, 0)); bpy.context.object.name = 'rig_A'
bpy.ops.mesh.primitive_cube_add(location=(0, -20, 0)); nla = bpy.context.object; nla.name = 'nla_A'
nla.keyframe_insert('location', frame=1); nla.location.z = 3; nla.keyframe_insert('location', frame=20)
act = nla.animation_data.action; nla.animation_data.action = None
tr = nla.animation_data.nla_tracks.new(); tr.strips.new('st', 1, act)
result = [i.identifier for i in ng.interface.items_tree if i.name == 'Size']
"""), "setup A")
    gn_id = (res(ex(W1, a, "A", "result = [i.identifier for i in bpy.data.node_groups['gnA'].interface.items_tree if i.name == 'Size']"), "gn id") or {}).get("result", ["Socket_2"])[0]
    cases = {
        "transform": "bpy.data.objects['rock_A_0'].location.z += 1",
        "material (socket)": "m = bpy.data.materials['mat_A']; n = next(n for n in m.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'); n.inputs['Roughness'].default_value = 0.123",
        "material (node prop, not socket)": "bpy.data.materials['mat_A'].node_tree.nodes['mix'].blend_type = 'MULTIPLY'",
        "material (field outside the nodes)": "bpy.data.materials['mat_A'].roughness = 0.77",
        "modifier parameter": "bpy.data.objects['rock_A_0'].modifiers['sub'].levels = 2",
        "Geometry Nodes input": (f"m = bpy.data.objects['rock_A_1'].modifiers['gn']\n"
                                 f"inp = getattr(getattr(m, 'properties', None), 'inputs', None)\n"
                                 f"if inp is not None: getattr(inp, '{gn_id}').value = 5.0\n"
                                 f"else: m['{gn_id}'] = 5.0"),
        "UV": "me = bpy.data.objects['rock_A_1'].data; me.uv_layers[0].data[0].uv = (0.37, 0.91)",
        "vertex color": "me = bpy.data.objects['rock_A_1'].data; me.color_attributes[0].data[0].color = (1, 0, 0, 1)",
        "new generic attribute": "bpy.data.objects['rock_A_1'].data.attributes.new('foo', 'FLOAT', 'POINT')",
        "offsetting vertices": "me = bpy.data.objects['rock_A_2'].data; me.vertices[0].co.x += 0.25; me.vertices[1].co.x -= 0.25",
        "vertex group weight": "bpy.data.objects['rock_A_2'].vertex_groups['weight'].add([1], 0.3, 'REPLACE')",
        "animation keyframe": ("ad = bpy.data.objects['rock_A_2'].animation_data; act = ad.action\n"
                               "fcs = getattr(act, 'fcurves', None)\n"
                               "if fcs is None: fcs = [fc for l in act.layers for s in l.strips for cb in s.channelbags for fc in cb.fcurves]\n"
                               "fcs[0].keyframe_points[0].co.y += 1"),
        "scale of an animated object (non-animated channel)": "bpy.data.objects['rock_A_2'].scale.x = 1.7",
        "rig pose": "bpy.data.objects['rig_A'].pose.bones[0].location.x += 0.5",
        "scale of an object with NLA (non-animated channel)": "bpy.data.objects['nla_A'].scale.y = 1.9",
        "object custom prop": "bpy.data.objects['rock_A_0']['note'] = 1",
        "hide_render": "bpy.data.objects['rock_A_2'].hide_render = True",
        "owner hijack": "bpy.data.collections['Agent_A']['hub_owner'] = 'B'",
        "unlinking the collection from the scene": "bpy.context.scene.collection.children.unlink(bpy.data.collections['Agent_A'])",
    }
    for name, code in cases.items():
        r = ex(W1, b, "B", code)
        viol = r.get("violations") or []
        check(f"intrusion detected: {name}", r["ok"] and not (r["result"] or {}).get("error") and len(viol) > 0,
              r.get("violations") or (r.get("result") or {}).get("error") or r.get("error"))
        if name == "owner hijack":
            res(ex(W1, b, "B", "bpy.data.collections['Agent_A']['hub_owner'] = 'A'"), "restore owner")
    r = ex(W1, a, "A", "result = 1")  # also relinks Agent_A into the scene (collection outside the scene)
    notes = r.get("notices") or []
    check("intrusion: owner A receives the notices", len(notes) >= len(cases) - 1, f"{len(notes)} notices")
    # false positives: child of A under a parent of B; constraint of A targeting an object of B
    res(ex(W1, a, "A", """
bpy.data.objects['rock_A_1'].parent = bpy.data.objects['rock_B_50']
c = bpy.data.objects['rock_A_0'].constraints.new('COPY_LOCATION'); c.target = bpy.data.objects['rock_B_51']
"""), "parent+constraint")
    res(ex(W1, a, "A", "result = 1"), "clear notices")
    r = ex(W1, b, "B", "bpy.data.objects['rock_B_50'].location.x += 2; bpy.data.objects['rock_B_51'].location.y += 1")
    check("no false alarm: B's parent and constraint target move", r["ok"] and not r.get("violations"), r.get("violations"))
    r = ex(W1, b, "B", "bpy.context.scene.frame_set(15)")
    check("no false alarm: changing the frame with an object of A animated by NLA", r["ok"] and not any(v.get("object") == "nla_A" for v in r.get("violations") or []), r.get("violations"))
    # agent's collection outside the scene: bpy.ops lands in its collection, without blaming anyone
    res(ex(W1, b, "B", "bpy.context.scene.collection.children.unlink(coll)"), "B unlinks its own collection")
    r = ex(W1, b, "B", "bpy.ops.mesh.primitive_cube_add(location=(0, 30, 0)); result = [c.name for c in bpy.context.object.users_collection]")
    check("collection outside the scene: object lands in the agent's collection, no false alarm",
          r["ok"] and r["result"]["result"] == ["Agent_B"] and not r.get("violations"), (r.get("result"), r.get("violations")))
    # detection cost
    res(ex(W1, a, "A", "bpy.ops.mesh.primitive_uv_sphere_add(segments=160, ring_count=128, location=(0, 40, 0)); result = 1"), "typical mesh")
    lat = [ex(W1, b, "B", "x = 1")["ms"] for _ in range(5)]
    R["perf"]["exec_ms_with_20k_verts_of_other"] = round(statistics.median(lat), 1)
    check("detection cost < 60 ms with ~20k vertices of the other agent (typical asset)", statistics.median(lat) < 60, lat)
    res(ex(W1, a, "A", "bpy.ops.mesh.primitive_uv_sphere_add(segments=512, ring_count=256, location=(0, 60, 0)); result = 1"), "large mesh")
    R["perf"]["exec_ms_with_150k_verts_of_other"] = round(statistics.median([ex(W1, b, "B", "x = 1")["ms"] for _ in range(5)]), 1)

    # ================= exec: watchdog timeout, swallowing code, SystemExit, status during work
    r = ex(W1, b, "B", "while True: pass", timeout=2)
    check("exec: while True stops at the timeout", r["ok"] and "_Timeout" in (r["result"].get("error") or ""), (r.get("result") or {}).get("error", r.get("error")))
    r = ex(W1, b, "B", "result = sum(i for i in range(3_000_000))")
    R["perf"]["exec_python_loop_3M_ms"] = r.get("ms")
    check("exec: no per-line cost (3M loop in < 1 s)", r["ok"] and r["ms"] < 1000, r.get("ms"))
    r = ex(W1, b, "B", "raise SystemExit")
    check("exec: SystemExit does not take the worker down", r["ok"] and H.status(W1).get("name") == W1)
    r = ex(W1, b, "B", "x = []; x.append(x); result = x")
    check("exec: circular result comes back as an error", not r["ok"] and "JSON" in r["error"], r.get("error"))
    box = {}
    t = threading.Thread(target=lambda: box.update(r=Hub().call(W1, "exec", token=b, agent="B", agent_key=KEYS["B"],
                                                                 code="import time\nt=time.time()\nwhile time.time()-t<3: pass")))
    t.start()
    time.sleep(0.6)
    t0 = time.perf_counter()
    st = H.status(W1)
    st_ms = (time.perf_counter() - t0) * 1000
    t.join()
    check("status responds during a long exec (< 300 ms)", st.get("busy") == "exec" and st_ms < 300, {"busy": st.get("busy"), "ms": round(st_ms)})
    # watchdog race: jobs finishing right at the deadline and code that swallows and finishes inside the resend window
    stray = []
    for i in range(8):
        r = ex(W1, b, "B", "import time\nt = time.time()\nwhile time.time() - t < 0.97: pass", timeout=1)
        stray.append((r.get("result") or {}).get("error", "")[-60:] if r.get("ok") else r.get("error"))
    r = ex(W1, b, "B", ("import time\ntry:\n    while True: pass\nexcept BaseException:\n    pass\n"
                        "t = time.time()\nwhile time.time() - t < 0.5: pass\nresult = 'done'"), timeout=1)
    after = [ex(W1, a, "A", "result = 1") for _ in range(6)]
    check("watchdog: no stray _Timeout after jobs at the limit (worker stays alive and clean)",
          r["ok"] and (r["result"] or {}).get("result") == "done" and all(x["ok"] and not (x["result"] or {}).get("error") for x in after)
          and H.status(W1).get("name") == W1, {"swallow": (r.get("result") or {}).get("error"), "after": [x.get("error") for x in after]})

    # ================= measure
    res(ex(W1, a, "A", """
bpy.ops.mesh.primitive_cube_add(size=4, location=(80, 0, 0)); bpy.context.object.name = 'big'
bpy.ops.mesh.primitive_cube_add(size=1, location=(80, 0, 0)); bpy.context.object.name = 'small'
bpy.ops.mesh.primitive_cube_add(location=(100, 0, 0)); o = bpy.context.object; o.name = 'barX'; o.scale = (3, 0.1, 0.1)
bpy.ops.mesh.primitive_cube_add(location=(101.3, 0.7, 0.3)); o = bpy.context.object; o.name = 'barY'; o.scale = (0.1, 3, 0.1)
bpy.ops.mesh.primitive_cube_add(size=1, location=(120, 0, 0)); bpy.context.object.name = 'kitL'
bpy.ops.mesh.primitive_cube_add(size=1, location=(121, 0, 0)); bpy.context.object.name = 'kitR'
bpy.ops.mesh.primitive_plane_add(size=3, location=(140, 0, 0.2)); bpy.context.object.name = 'plane'
bpy.ops.mesh.primitive_cube_add(size=1, location=(140, 0, 0)); bpy.context.object.name = 'under_plane'
"""), "cubes")
    m = res(H.call(W1, "measure", token=a, a="big", b="small"), "inside")
    check("measure: object inside the other = penetrating, delta None", m and m["penetrating"] and not m["touching"] and m["delta_a_to_b"] is None, m)
    m = res(H.call(W1, "measure", token=a, a="barX", b="barY"), "crossed")
    check("measure: bars crossed off-center (edge-edge) ~0.1", m and abs(m["min_distance"] - 0.1) < 0.01, m and m["min_distance"])
    m = res(H.call(W1, "measure", token=a, a="kitL", b="kitR"), "side by side")
    check("measure: side-by-side cubes (kitbash) = touching, no false penetration", m and m["touching"] and not m["penetrating"], m)
    m = res(H.call(W1, "measure", token=a, a="plane", b="under_plane"), "plane")
    check("measure: open mesh reports manifold=false", m and m["manifold"]["a"] is False, m and m.get("manifold"))
    snaps = []
    for pa, pb in (("rock_A_0", "rock_B_51"), ("rock_B_51", "rock_A_0")):
        tk, ag = (b, "B") if pb.startswith("rock_B") else (a, "A")
        res(ex(W1, a, "A", "bpy.data.objects['rock_A_0'].constraints.clear(); bpy.data.objects['rock_A_0'].modifiers.clear()"), "detach")
        res(ex(W1, tk, ag, f"""
a_ = bpy.data.objects['{pa}']; b_ = bpy.data.objects['{pb}']
b_.location = a_.location.copy(); b_.location.x += 2.4; b_.location.y += 0.3
"""), "position")
        d = res(H.call(W1, "measure", token=tk, a=pa, b=pb), "m1")
        res(ex(W1, tk, ag, f"""
b_ = bpy.data.objects['{pb}']; dd = {tuple(d['delta_a_to_b'])}
b_.location.x -= dd[0]; b_.location.y -= dd[1]; b_.location.z -= dd[2]
"""), "snap")
        d2 = res(H.call(W1, "measure", token=tk, a=pa, b=pb), "m2")
        snaps.append((d["min_distance"], d2["min_distance"]))
    check("measure: delta snaps them together in both directions", all(after < 1e-3 for _, after in snaps), snaps)

    # ================= turntable (including a target in a hidden collection)
    res(ex(W1, a, "A", "coll.hide_render = True"), "hide collection")
    t0 = time.perf_counter()
    tt = res(H.call(W1, "turntable", token=a, agent="A", objects=["rock_A_0"], views=12, size=256, color="VERTEX", tag="turntable"), "turntable")
    R["perf"]["turntable_12_s"] = round(time.perf_counter() - t0, 2)
    check("turntable: 12 frames + contact sheet", tt and len(tt["frames"]) == 12 and os.path.exists(tt["sheet"]), tt)
    # tokens without losing quality: an identical turntable of an unchanged model is not re-rendered/re-sent,
    # but ANY change the sheet depends on (geometry, material, transform) renders again; force=true always renders
    base = res(H.call(W1, "turntable", token=a, agent="A", objects=["rock_A_0"], views=12, size=256, color="VERTEX"), "tt base")
    same = res(H.call(W1, "turntable", token=a, agent="A", objects=["rock_A_0"], views=12, size=256, color="VERTEX"), "tt same")
    res(ex(W1, a, "A", "m = bpy.data.objects['rock_A_0'].material_slots[0].material; m.diffuse_color = (0.9, 0.2, 0.2, 1)"), "mat color")
    after_mat = res(H.call(W1, "turntable", token=a, agent="A", objects=["rock_A_0"], views=12, size=256, color="VERTEX"), "tt mat")
    res(ex(W1, a, "A", "bpy.data.objects['rock_A_0'].data.vertices[0].co.z += 0.2"), "vertex edit")
    after_geo = res(H.call(W1, "turntable", token=a, agent="A", objects=["rock_A_0"], views=12, size=256, color="VERTEX"), "tt geo")
    forced = res(H.call(W1, "turntable", token=a, agent="A", objects=["rock_A_0"], views=12, size=256, color="VERTEX", force=True), "tt force")
    check("turntable reuse: unchanged -> no render; material or vertex change -> new render; force -> new render",
          same and same.get("unchanged") and after_mat and not after_mat.get("unchanged")
          and after_geo and not after_geo.get("unchanged") and forced and not forced.get("unchanged"),
          [bool(x and x.get("unchanged")) for x in (base, same, after_mat, after_geo, forced)])
    # what Workbench DRAWS: a boolean cutter OUTSIDE the target list, a curve bevel, and textures must never reuse
    res(ex(W1, a, "A", """
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, -50, 0)); wall = bpy.context.object; wall.name = 'wall'
bpy.ops.mesh.primitive_cube_add(size=0.8, location=(0, -50, 1)); cut = bpy.context.object; cut.name = 'cutter'
m = wall.modifiers.new('cut', 'BOOLEAN'); m.object = cut; m.operation = 'DIFFERENCE'
bpy.ops.curve.primitive_bezier_curve_add(location=(0, -60, 0)); bpy.context.object.name = 'pipe'
bpy.context.object.data.bevel_depth = 0.05
"""), "boolean + curve")
    tt_args = dict(token=a, agent="A", views=12, size=128, color="VERTEX")
    res(H.call(W1, "turntable", objects=["wall"], **tt_args), "wall 1")
    res(ex(W1, a, "A", "bpy.data.objects['cutter'].location.x += 0.6"), "move cutter")
    wall2 = res(H.call(W1, "turntable", objects=["wall"], **tt_args), "wall 2")
    res(H.call(W1, "turntable", objects=["pipe"], **tt_args), "pipe 1")
    res(ex(W1, a, "A", "bpy.data.objects['pipe'].data.bevel_depth = 0.2"), "bevel")
    pipe2 = res(H.call(W1, "turntable", objects=["pipe"], **tt_args), "pipe 2")
    # a self-contained mesh WITH a modifier still reuses; a render-only change (render_levels) renders again
    res(ex(W1, a, "A", "bpy.ops.mesh.primitive_cube_add(location=(0, -70, 0)); o = bpy.context.object; o.name = 'smooth'\n"
                       "s = o.modifiers.new('subd', 'SUBSURF'); s.levels = 0; s.render_levels = 1"), "subsurf cube")
    res(H.call(W1, "turntable", objects=["smooth"], **tt_args), "smooth 1")
    sm2 = res(H.call(W1, "turntable", objects=["smooth"], **tt_args), "smooth 2")
    res(ex(W1, a, "A", "bpy.data.objects['smooth'].modifiers['subd'].render_levels = 3"), "render levels only")
    sm3 = res(H.call(W1, "turntable", objects=["smooth"], **tt_args), "smooth 3")
    check("turntable reuse: self-contained mesh with a modifier reuses; a render-only change (render_levels) renders again",
          sm2 and sm2.get("unchanged") and sm3 and not sm3.get("unchanged"),
          [bool(x and x.get("unchanged")) for x in (sm2, sm3)])
    wall3 = res(H.call(W1, "turntable", objects=["wall"], **tt_args), "wall 3")
    pipe3 = res(H.call(W1, "turntable", objects=["pipe"], **tt_args), "pipe 3")
    check("turntable reuse is off for meshes depending on other objects (boolean) and for non-mesh targets (curve)",
          wall3 and not wall3.get("unchanged") and pipe3 and not pipe3.get("unchanged"),
          [bool(x and x.get("unchanged")) for x in (wall3, pipe3)])
    tex1 = res(H.call(W1, "turntable", objects=["pipe"], token=a, agent="A", views=12, size=128, color="TEXTURE"), "tex 1")
    tex2 = res(H.call(W1, "turntable", objects=["pipe"], token=a, agent="A", views=12, size=128, color="TEXTURE"), "tex 2")
    check("turntable reuse sees what is DRAWN: moved boolean cutter outside the list, curve bevel -> new render; TEXTURE never reuses",
          wall2 and not wall2.get("unchanged") and pipe2 and not pipe2.get("unchanged") and tex2 and not tex2.get("unchanged"),
          [bool(x and x.get("unchanged")) for x in (wall2, pipe2, tex1, tex2)])
    # shading="rendered": EEVEE sheet with the hub's own light rig (emissive visible), same reuse rules, scene restored
    before = res(ex(W1, a, "A", """
w0 = bpy.context.scene.world
bpy.ops.mesh.primitive_cube_add(location=(0, -80, 0)); o = bpy.context.object; o.name = 'glow'
m = bpy.data.materials.new('glow_mat'); o.data.materials.append(m)
if m.node_tree is None: m.use_nodes = True
p = m.node_tree.nodes.get('Principled BSDF')
p.inputs['Base Color'].default_value = (0.05, 0.05, 0.05, 1)
p.inputs['Emission Color'].default_value = (1, 0.1, 0.05, 1); p.inputs['Emission Strength'].default_value = 4.0
bpy.ops.mesh.primitive_cube_add(location=(0, -90, 0)); t = bpy.context.object; t.name = 'texd'
mt = bpy.data.materials.new('tex_mat'); t.data.materials.append(mt)
if mt.node_tree is None: mt.use_nodes = True
im = bpy.data.images.new('tex_img', 8, 8); n = mt.node_tree.nodes.new('ShaderNodeTexImage'); n.image = im
mt.node_tree.links.new(n.outputs[0], mt.node_tree.nodes['Principled BSDF'].inputs['Base Color'])
sc = bpy.context.scene
result = (sc.render.engine, w0.name if w0 else None, sc.eevee.taa_render_samples, sc.view_settings.look,
          sc.render.use_compositing)
"""), "emissive + textured cubes")
    rd_args = dict(token=a, agent="A", views=4, size=128, shading="rendered")
    rd1 = res(H.call(W1, "turntable", objects=["glow"], **rd_args), "rendered 1")
    rd2 = res(H.call(W1, "turntable", objects=["glow"], **rd_args), "rendered 2")
    sol = res(H.call(W1, "turntable", objects=["glow"], token=a, agent="A", views=4, size=128), "solid after rendered")
    rd2b = res(H.call(W1, "turntable", objects=["glow"], **rd_args), "rendered again after solid")
    # every setting EEVEE draws with must invalidate the reused sheet
    flips = {"eevee ray tracing (nested struct)": "sc.eevee.ray_tracing_options.resolution_scale = '4' if sc.eevee.ray_tracing_options.resolution_scale != '4' else '2'",
             "view layer material override": "vl.material_override = bpy.data.materials['tex_mat']",
             "object visible_shadow": "bpy.data.objects['glow'].visible_shadow = False",
             "render use_compositing": "sc.render.use_compositing = not sc.render.use_compositing"}
    flip_res = {}
    for label, code in flips.items():
        res(H.call(W1, "turntable", objects=["glow"], **rd_args), "prime " + label)
        ctl = res(H.call(W1, "turntable", objects=["glow"], **rd_args), "control " + label)  # stable before the change
        res(ex(W1, a, "A", "sc = bpy.context.scene; vl = bpy.context.view_layer; " + code), label)
        x = res(H.call(W1, "turntable", objects=["glow"], **rd_args), "after " + label)
        x2 = res(H.call(W1, "turntable", objects=["glow"], **rd_args), "after again " + label)  # stable after it
        flip_res[label] = (bool(ctl and ctl.get("unchanged")), bool(x and not x.get("unchanged")),
                           bool(x2 and x2.get("unchanged")))
    res(ex(W1, a, "A", "bpy.context.view_layer.material_override = None; bpy.data.objects['glow'].visible_shadow = True"), "undo flips")
    check("rendered reuse: a changed nested EEVEE setting, material override, visibility flag or compositing renders again",
          all(all(v) for v in flip_res.values()), flip_res)
    res(ex(W1, a, "A", "bpy.data.materials['glow_mat'].node_tree.nodes['Principled BSDF'].inputs['Emission Strength'].default_value = 0.0"), "emission off")
    rd3 = res(H.call(W1, "turntable", objects=["glow"], **rd_args), "rendered 3")
    tx1 = res(H.call(W1, "turntable", objects=["texd"], **rd_args), "rendered tex 1")
    tx2 = res(H.call(W1, "turntable", objects=["texd"], **rd_args), "rendered tex 2")
    bad = H.call(W1, "turntable", objects=["glow"], token=a, agent="A", agent_key=KEYS.get("A"), views=4, shading="cycles")
    check("turntable shading=rendered: EEVEE sheet; repeat reuses; solid is a separate review; emission change renders; image textures never reuse; bad shading refused",
          rd1 and rd1.get("shading") == "rendered" and rd1.get("sheet") and os.path.exists(rd1["sheet"])
          and rd2 and rd2.get("unchanged") and sol and not sol.get("unchanged") and rd2b and rd2b.get("unchanged")
          and rd3 and not rd3.get("unchanged") and tx1 and tx1.get("sheet") and tx2 and not tx2.get("unchanged") and not bad["ok"] and "shading" in (bad.get("error") or ""),
          [bool(x and x.get("unchanged")) for x in (rd1, rd2, sol, rd2b, rd3, tx1, tx2)] + [bad.get("error")])
    r = ex(W1, a, "A", f"""
def stats(path):
    img = bpy.data.images.load(path, check_existing=False)
    px = np.empty(len(img.pixels), dtype=np.float32); img.pixels.foreach_get(px); px = px.reshape(-1, 4)
    bpy.data.images.remove(img)
    return float((px[:, 0] - px[:, 1]).max())
sc = bpy.context.scene
left = [o.name for o in bpy.data.objects if o.name.startswith(('hub_key_', 'hub_fill_', 'hub_cam_'))]
left += [w.name for w in bpy.data.worlds if w.name.startswith('hub_world_')]
left += [l.name for l in bpy.data.lights if l.name.startswith(('hub_key_', 'hub_fill_'))]
result = (stats(r'{rd1['sheet'] if rd1 else ''}'), stats(r'{rd3['sheet'] if rd3 else ''}'),
          [sc.render.engine, sc.world.name if sc.world else None, sc.eevee.taa_render_samples, sc.view_settings.look,
           sc.render.use_compositing], left)
""")
    rr = r["result"]["result"] if r["ok"] and r["result"].get("result") else None
    exp = list(before["result"]) if before and before.get("result") else None
    if exp:
        exp[4] = not exp[4]  # the compositing flip above was left flipped on purpose
    check("rendered sheet shows the emissive glow (red >> green) and not after emission is off; engine/world/samples/look restored, no rig left",
          rr and rr[0] > 0.2 and rr[1] < 0.05 and rr[2] == exp and rr[3] == [], (rr, exp))
    # hub_exec with a saved script + args (edit the file instead of re-sending the code)
    spath = os.path.join(TMP, "build_step.py")
    with open(spath, "w", encoding="utf-8") as f:
        f.write("bpy.ops.mesh.primitive_cube_add(size=args.get('s', 1), location=(0, -30, 0))\n"
                "bpy.context.object.name = 'scripted'\nresult = round(bpy.context.object.dimensions.x, 3)\n")
    r = ex(W1, a, "A", None, script=spath, args={"s": 2.5})
    r_none = H.call(W1, "exec", token=a, agent="A", agent_key=KEYS.get("A"))
    with open(spath, "w", encoding="utf-8") as f:
        f.write("x = 1\nraise ValueError('script line 2')\n")
    r_err = ex(W1, a, "A", None, script=spath)
    check("hub_exec script=<file> with args runs the file; neither code nor script is refused; script errors show the line",
          r["ok"] and r["result"]["result"] == 2.5 and not r_none["ok"]
          and "raise ValueError('script line 2')" in ((r_err.get("result") or {}).get("error") or ""),
          (r.get("result"), r_none.get("error"), (r_err.get("result") or {}).get("error", "")[-200:]))
    with open(spath, "w", encoding="utf-8-sig") as f:  # BOM, as PowerShell 5.1 writes it
        f.write("result = (2 + 2, __file__.endswith('build_step.py'))\n")
    r_bom = ex(W1, a, "A", None, script=spath)
    r_rel = H.call(W1, "exec", token=a, agent="A", agent_key=KEYS.get("A"), script="build_step.py")
    r_both = H.call(W1, "exec", token=a, agent="A", agent_key=KEYS.get("A"), script=spath, code="result = 1")
    check("script: BOM file runs (with __file__); relative path and code+script together are refused",
          r_bom["ok"] and r_bom["result"].get("result") == [4, True] and not r_rel["ok"] and "absolute" in r_rel["error"]
          and not r_both["ok"] and "not both" in r_both["error"], (r_bom.get("result"), r_rel.get("error"), r_both.get("error")))
    r = ex(W1, a, "A", f"""
img = bpy.data.images.load(r'{tt['sheet'] if tt else ''}', check_existing=False)
px = np.empty(len(img.pixels), dtype=np.float32); img.pixels.foreach_get(px)
result = (float(px.std()), coll.hide_render, bpy.context.scene.render.engine, bpy.context.scene.display.shading.light)
bpy.data.images.remove(img)
""")
    rr = r["result"]["result"] if r["ok"] and r["result"].get("result") else None
    check("turntable: target in a hidden collection shows up in the render", rr and rr[0] > 0.02, rr)
    check("turntable: scene restored (collection hidden again, engine)", rr and rr[1] is True and rr[2] != "BLENDER_WORKBENCH", rr)
    res(ex(W1, a, "A", "coll.hide_render = False"), "show")

    # ================= export and merge
    res(ex(W1, a, "A", "ch = bpy.data.collections.new('A_details'); coll.children.link(ch); ch.objects.link(bpy.data.objects['small'])"), "child")
    pa, pb = os.path.join(TMP, "part_A.blend"), os.path.join(TMP, "part_B.blend")
    res(H.call(W1, "export_collection", token=a, agent="A", agent_key=KEYS["A"], collection="Agent_A", path=pa), "export A")
    r = H.call(W1, "export_collection", token=b, agent="B", agent_key=KEYS["B"], collection="Agent_A", path=pa + "x")
    check("export: B cannot export A's collection", not r["ok"], r.get("error"))
    res(H.call(W1, "export_collection", token=b, agent="B", agent_key=KEYS["B"], collection="Agent_B", path=pb), "export B")
    x = res(H.call(W2, "lease", mode="exclusive", label="merge"), "lease W2")["token"]
    res(H.call(W2, "merge", token=x, paths=[pa, pb]), "merge")
    r = ex(W2, x, "J", "result = sorted(c.name for c in bpy.context.scene.collection.children)")
    tops = r["result"]["result"] if r["ok"] else None
    check("merge: only top-level collections in the scene (child is not duplicated)", tops and "A_details" not in tops and "Agent_A" in tops, tops)
    final = os.path.join(TMP, "final.blend")
    res(H.call(W2, "save", token=x, path=final), "save")
    check("merge: final file saved", os.path.exists(final), final)
    H.stop_worker(W2, token=x)

    # ================= "base" owner: what came from the file
    H.start_worker("basew", blend=final)
    s1 = res(H.call("basew", "lease", mode="shared", label="s1"), "base lease")["token"]
    r = ex("basew", s1, "S", "bpy.data.objects['rock_A_0'].location.z += 3")
    check("base: a shared participant touching what came from the file is warned",
          r["ok"] and any(v["owner"] == "base" for v in r.get("violations") or []), r.get("violations"))
    H.call("basew", "release", token=s1)
    s2 = res(H.call("basew", "lease", mode="exclusive", label="s2"), "base x lease")["token"]
    r = ex("basew", s2, "S2", "bpy.data.objects['rock_A_0'].location.z -= 3")
    check("base: an exclusive lease edits the file without warnings", r["ok"] and not r.get("violations"), r.get("violations"))
    H.stop_worker("basew", token=s2)

    # ================= stuck worker: kill and restart
    t3 = res(H.call(W3, "lease", mode="exclusive", label="stuck"), "lease W3")["token"]
    stuck = {}
    th = threading.Thread(target=lambda: stuck.update(r=_safe(lambda: Hub().call(W3, "exec", token=t3, agent="T",
                                                                                code="import time\ntime.sleep(40)", timeout=2))))
    th.start()
    time.sleep(1.5)
    try:
        H.kill_worker(W1, token=a)
        check("kill: refused when not the only lease and not stuck", False, "killed")
    except PermissionError as e:
        check("kill: refused when not the only lease and not stuck", True, str(e))
    t0 = time.perf_counter()
    k = H.kill_worker(W3, token=t3)
    th.join(timeout=20)
    t4 = res(H.call(W3, "lease", mode="exclusive", label="after"), "lease W3 again")
    check("kill: worker stuck in a C call (sleep 40 s) killed and restarted",
          k.get("killed") and t4 and time.perf_counter() - t0 < 30, {"kill": k, "s": round(time.perf_counter() - t0, 1)})
    check("kill: the stuck call gets a connection error (does not hang)", "error" in (stuck.get("r") or {}), stuck)
    # legitimate long exec (timeout 20 s): a time-based kill must NOT kill it before its deadline
    lg = {}
    th = threading.Thread(target=lambda: lg.update(r=_safe(lambda: Hub().call(
        W3, "exec", token=t4["token"], agent="T1", timeout=20, code="import time\nt=time.time()\nwhile time.time()-t<5: pass\nresult='ok'"))))
    th.start()
    time.sleep(2)
    try:
        H.kill_worker(W3, token=None, grace=1)
        check("kill: does not kill a legitimate exec before its deadline", False, "killed")
    except PermissionError as e:
        check("kill: does not kill a legitimate exec before its deadline", True, str(e)[:120])
    th.join(timeout=20)
    check("kill: the legitimate exec finishes normally", (lg.get("r") or {}).get("ok"), lg)
    # code that swallows the timeout forever: it cannot be stopped from inside; it passes the deadline and the time-based kill handles it
    t5 = t4["token"]
    sw = {}
    th = threading.Thread(target=lambda: sw.update(r=_safe(lambda: Hub().call(
        W3, "exec", token=t5, agent="T2", timeout=2,
        code="while True:\n    try:\n        while True: pass\n    except BaseException:\n        pass"))))
    th.start()
    time.sleep(4)
    st = H.status(W3)
    k2 = _safe(lambda: H.kill_worker(W3, token=None, grace=1))
    th.join(timeout=20)
    check("kill: code that swallows the timeout -> stuck -> time-based kill (no token)",
          st.get("busy") == "exec" and isinstance(k2, dict) and k2.get("killed"), {"busy_s": st.get("busy_s"), "kill": k2})
    t6 = res(H.call(W3, "lease", mode="exclusive", label="end"), "lease W3 end")
    H.stop_worker(W3, token=t6["token"])

    # ================= safe stop
    try:
        H.stop_worker(W1)
        check("stop: refuses a worker with other sessions' leases", False, "closed")
    except PermissionError as e:
        check("stop: refuses a worker with other sessions' leases", True, str(e))
    check("stop: a PID that is not blender.exe is not treated as a worker", not hubmod._is_our_worker({"pid": os.getpid(), "created": None}))
    res(H.call(W1, "release", token=a), "release a")
    res(H.call(W1, "release", token=b), "release b")
    check("stop: closes when there are no leases", H.stop_worker(W1).get("stopped"))

    # ================= lifecycle: idleness, rotating autosave, restart
    hubmod.IDLE_S = "4"
    w = H.start_worker("idle1")
    t1 = res(H.call("idle1", "lease", mode="exclusive", label="life"), "idle lease")["token"]
    r = ex("idle1", t1, "V", "result = [o.name for o in bpy.data.objects]")
    check("worker without a .blend starts empty (no 'base' Cube/Camera/Light)", r["ok"] and r["result"]["result"] == [], r.get("result"))
    res(ex("idle1", t1, "V", "bpy.ops.mesh.primitive_monkey_add(); result = 1"), "monkey")
    res(H.call("idle1", "release", token=t1), "release idle")
    t0 = time.time()
    while time.time() - t0 < 20 and hubmod._is_our_worker(w):
        time.sleep(0.5)
    check("lifecycle: idle worker exits on its own", not hubmod._is_our_worker(w), round(time.time() - t0, 1))
    adir = os.path.join(os.environ["BLENDER_HUB_STATE"], "autosave", "idle1")
    check("lifecycle: autosave written on exit", os.path.isdir(adir) and len(os.listdir(adir)) >= 1, adir)
    hubmod.IDLE_S = "600"
    rs = H.restart_worker("idle1")
    t2 = res(H.call("idle1", "lease", mode="exclusive", label="back"), "back lease")["token"]
    r = ex("idle1", t2, "V", "result = [o.name for o in bpy.data.objects]")
    check("lifecycle: restart from autosave keeps the work (exclusive resumes the agent)",
          rs.get("from_autosave") and r["ok"] and "Suzanne" in r["result"]["result"], (rs, r.get("result"), r.get("error")))
    H.stop_worker("idle1", token=t2)
    check("lifecycle: rotating autosaves (more than one kept)", len([f for f in os.listdir(adir) if f.endswith(".blend")]) >= 2, os.listdir(adir))

    # ================= name reused after idleness: the previous work is archived, not deleted
    hubmod.IDLE_S = "4"
    wr = H.start_worker("reuse1")
    tr = res(H.call("reuse1", "lease", mode="shared", label="owner-ru"), "ru lease")["token"]
    res(ex("reuse1", tr, "RU", "bpy.ops.mesh.primitive_monkey_add(); bpy.ops.mesh.primitive_cube_add(location=(3,0,0)); result = 1"), "ru work")
    H.call("reuse1", "release", token=tr)
    t0 = time.time()
    while time.time() - t0 < 20 and hubmod._is_our_worker(wr):
        time.sleep(0.5)
    hubmod.IDLE_S = "600"
    H.start_worker("reuse1")  # another session starts a NEW worker with the same name
    recs = H.recycled("reuse1")
    rec = recs[-1] if recs else {}
    check("name reused: what the idle worker left goes into a listed archive",
          rec.get("reason", "").startswith("name reused") and hubmod._blends(rec.get("archived", "")) and rec.get("saved", {}).get("keys"), rec)
    st = H.status("reuse1")
    check("name reused: the new worker starts clean", not st.get("owners"), st.get("owners"))
    rs2 = _safe(lambda: H.restore_archive(rec["archived"], name="reuse1back"))
    ok_back = False
    if isinstance(rs2, dict) and rs2.get("name"):
        lt = res(H.call("reuse1back", "lease", mode="shared", label="ru-back"), "ru back lease")["token"]
        r = ex("reuse1back", lt, "RU", "result = len(coll.objects)")
        ok_back = r["ok"] and (r["result"] or {}).get("result") == 2
        H.call("reuse1back", "release", token=lt)
        H.stop_worker("reuse1back")
    check("name reused: restore brings the work back and the agent resumes with the old key", ok_back, rs2)
    H.stop_worker("reuse1")

    # ================= shared after restart: the keys survive
    H.start_worker("rs1")
    ra = res(H.call("rs1", "lease", mode="shared", label="RA"), "lease RA")["token"]
    rb = res(H.call("rs1", "lease", mode="shared", label="RB"), "lease RB")["token"]
    res(ex("rs1", ra, "RA", "bpy.ops.mesh.primitive_cube_add(); result = 1"), "RA cube")
    res(ex("rs1", rb, "RB", "bpy.ops.mesh.primitive_cone_add(location=(3,0,0)); result = 1"), "RB cone")
    H.call("rs1", "release", token=ra)
    H.call("rs1", "release", token=rb)
    H.stop_worker("rs1")
    rr = H.restart_worker("rs1")
    ra2 = res(H.call("rs1", "lease", mode="shared", label="RA2"), "lease RA2")["token"]
    rb2 = res(H.call("rs1", "lease", mode="shared", label="RB2"), "lease RB2")["token"]
    r1 = ex("rs1", ra2, "RA", "result = len(coll.objects)")
    r2 = ex("rs1", rb2, "RB", "result = len(coll.objects)")
    check("restart: A and B resume in shared with their own keys", rr.get("from_autosave") and r1["ok"] and r2["ok"]
          and r1["result"]["result"] == 1 and r2["result"]["result"] == 1, (r1.get("error"), r2.get("error")))
    H.call("rs1", "release", token=rb2)
    ri = res(H.call("rs1", "lease", mode="shared", label="intruder"), "intruder lease rs1")["token"]
    r = H.call("rs1", "exec", token=ri, agent="RB", code="result = 1")
    check("restart: without the key, another session cannot take the agent", not r["ok"] and "agent_key" in r["error"], r.get("error"))
    H.call("rs1", "release", token=ri)
    H.call("rs1", "release", token=ra2)
    H.stop_worker("rs1")

    # ================= window mode (live): timer-driven queue, live UI, same API
    gw = H.start_worker("window1", gui=True)
    tg = res(H.call("window1", "lease", mode="shared", label="gui"), "gui lease")["token"]
    stg = H.status("window1")
    r = ex("window1", tg, "G", "bpy.ops.mesh.primitive_monkey_add(); result = [o.name for o in coll.objects]")
    check("window: windowed worker accepts exec and the scene starts empty", stg.get("gui") is True and r["ok"]
          and r["result"]["result"] == ["Suzanne"], (stg.get("gui"), r.get("result"), r.get("error")))
    box = {}
    th = threading.Thread(target=lambda: box.update(r=ex("window1", tg, "G", "import time\nt=time.time()\nwhile time.time()-t<2: pass")))
    th.start()
    time.sleep(0.5)
    t0 = time.perf_counter()
    stb = H.status("window1")
    st_ms = (time.perf_counter() - t0) * 1000
    th.join()
    check("window: status responds during a long exec", stb.get("busy") == "exec" and st_ms < 300, round(st_ms))
    # live panel: how many / which agents work in this window; unnamed lease takes the agent's name
    tg2 = res(H.call("window1", "lease", mode="shared"), "unnamed lease")["token"]
    ex("window1", tg2, "G2", "result = 1")
    box2 = {}
    th = threading.Thread(target=lambda: box2.update(r=ex("window1", tg2, "G2", "import time\nt=time.time()\nwhile time.time()-t<2: pass")))
    th.start()
    time.sleep(0.6)
    ov = H.status("window1").get("overlay") or []
    th.join()
    texts = [t for t, _ in ov]
    kinds = {t.split()[0]: k for t, k in ov[1:]} if ov else {}
    check("live panel: title counts 2 agents, G2 shown running, G shown with its lease",
          ov and "2 agents working" in ov[0][0] and kinds.get("G2") == "busy" and any(t.startswith("G ") and "'gui'" in t for t in texts),
          texts)
    labels = [p["label"] for p in H.status("window1").get("participants", [])]
    check("unnamed lease is named after its first agent", "G2" in labels, labels)
    # what each agent is doing: declared task (hub_note) + step (note=) + changes the hub sees by itself
    rn = H.call("window1", "note", token=tg2, agent="G2", agent_key=KEYS.get("G2"), task="garden wall: first course")
    ex("window1", tg2, "G2", "bpy.ops.mesh.primitive_cube_add(location=(9, 0, 0)); bpy.context.object.name = 'brick_a'\n"
       "bpy.ops.mesh.primitive_cube_add(location=(11, 0, 0)); bpy.context.object.name = 'brick_b'", note="laying two bricks")
    st_n = H.status("window1")
    ovl = [t for t, _ in st_n.get("overlay") or []]
    g2 = (st_n.get("agents_info") or {}).get("G2") or {}
    check("panel shows the declared task and step of each agent",
          rn.get("ok") and any(t.startswith("G2") and "garden wall: first course" in t for t in ovl)
          and any("step: laying two bricks" in t for t in ovl), ovl)
    check("panel shows what the agent changed (seen by the hub, no note needed) and its totals",
          any("+2 (brick_a, brick_b)" in t for t in ovl) and g2.get("stats", {}).get("objects") == 2
          and g2.get("last_change", {}).get("n_added") == 2, (ovl, g2))
    H.call("window1", "release", token=tg2)
    vw = H.call("window1", "view", token=tg, collection="Agent_G", shading="MATERIAL")
    sh = H.call("window1", "screenshot", token=tg)
    sp = (sh.get("result") or {}).get("path")
    check("window: hub_view frames and hub_screenshot saves the capture", vw.get("ok") and sp and os.path.getsize(sp) > 10000,
          (vw.get("error"), sh.get("error")))
    shr = sh.get("result") or {}
    shf = (H.call("window1", "screenshot", token=tg, full=True).get("result") or {})
    check("tokens: screenshot = 3D viewport only, longest side <= 1024 by default; full=true gives the window",
          shr.get("area") == "viewport" and max(shr.get("size") or [9999]) <= 1024 and shf.get("area") == "window",
          (shr.get("size"), shr.get("area"), shf.get("area"), shf.get("size")))
    r = ex("window1", tg, "G", "while True: pass", timeout=2)
    check("window: the watchdog timeout also works with a window", r["ok"] and "_Timeout" in ((r.get("result") or {}).get("error") or ""),
          (r.get("result") or {}).get("error", r.get("error")))
    # user in Edit Mode: the agent edits the mesh and the change must not vanish when the user leaves the mode
    res(ex("window1", tg, "G", """
win = bpy.context.window_manager.windows[0]
area = next(a for a in win.screen.areas if a.type == 'VIEW_3D')
ob = bpy.data.objects['Suzanne']; bpy.context.view_layer.objects.active = ob; ob.select_set(True)
with bpy.context.temp_override(window=win, area=area, region=next(r for r in area.regions if r.type == 'WINDOW')):
    bpy.ops.object.mode_set(mode='EDIT')
result = bpy.context.mode
"""), "enter edit")
    r = ex("window1", tg, "G", "me = bpy.data.objects['Suzanne'].data; me.vertices[0].co.z += 5.0; result = me.vertices[0].co.z")
    z_set = (r.get("result") or {}).get("result")
    r2 = ex("window1", tg, "G", "result = (bpy.context.mode, bpy.data.objects['Suzanne'].data.vertices[0].co.z)")
    got = (r2.get("result") or {}).get("result")
    check("window: user in Edit Mode does not wipe the agent's change (leaves the mode first and warns)",
          r["ok"] and "left the user's" in (r.get("warning") or "") and got and got[0] == "OBJECT" and abs(got[1] - z_set) < 1e-4,
          (r.get("warning"), z_set, got))
    # autosave right after the change (closing via the X loses at most ~5 s)
    adir_g = os.path.join(os.environ["BLENDER_HUB_STATE"], "autosave", "window1")
    t0 = time.time()
    while time.time() - t0 < 12 and not hubmod._blends(adir_g):
        time.sleep(0.5)
    check("window: autosave written a few seconds after the change", hubmod._blends(adir_g), round(time.time() - t0, 1))
    # a "user" edit (outside any agent command: Blender's own timer) also goes into the autosave.
    # The edit fires 8 s after the agent's command, so the agent's own autosave is already out; then a NEWER
    # autosave must appear after the edit (only the user-edit handler can cause it).
    res(ex("window1", tg, "G", """
def _edit_like_user():
    bpy.data.objects['Suzanne'].location.x += 2.0
    return None
bpy.app.timers.register(_edit_like_user, first_interval=8.0)
"""), "schedule user edit")
    t_edit = time.time() + 8.0

    def newest():
        fs = [os.path.join(adir_g, f) for f in os.listdir(adir_g) if f.endswith(".blend")]
        return max((os.path.getmtime(f) for f in fs), default=0)
    t0 = time.time()
    while time.time() - t0 < 30 and newest() <= t_edit + 0.2:
        time.sleep(0.5)
    check("window: an edit made outside the agent's commands also goes into the autosave",
          newest() > t_edit + 0.2, round(newest() - t_edit, 1))
    # recycling never closes the window
    H.call("window1", "release", token=tg)
    rec = _safe(lambda: H._recycle_one({"window1": H.workers()["window1"]}))
    check("window: pool recycling never picks a windowed worker", rec is None and hubmod._is_our_worker(gw), rec)
    # window cap: with the cap reached, a new window is refused with the list of windows to close
    old_cap = hubmod.MAX_WINDOWS
    hubmod.MAX_WINDOWS = 1
    capped = _safe(lambda: Hub().acquire("exclusive", label="cap", gui=True))
    hubmod.MAX_WINDOWS = old_cap
    check("window cap: new window refused, error names the open windows and gui=false",
          isinstance(capped, dict) and "window limit" in capped.get("error", "") and "window1" in capped.get("error", "")
          and "gui=false" in capped.get("error", ""), capped)
    # kill of a windowed worker brings it back WITH a window
    tk = res(H.call("window1", "lease", mode="exclusive", label="kill-gui"), "lease kill gui")["token"]
    kg = _safe(lambda: H.kill_worker("window1", token=tk))
    st_k = H.status("window1") if isinstance(kg, dict) and kg.get("killed") else {}
    check("window: hub_kill_worker restarts it as a window", st_k.get("gui") is True, (kg, st_k.get("gui")))
    # the user closes the window (process gone, registry entry cleaned): restart keeps the window mode
    wk = H.workers().get("window1") or {}
    if wk.get("pid"):
        subprocess.run(["taskkill", "/PID", str(wk["pid"]), "/F"], capture_output=True)
        time.sleep(1.5)
    H.workers(clean=True)
    rr = _safe(lambda: H.restart_worker("window1"))
    st_r = H.status("window1") if isinstance(rr, dict) and rr.get("name") else {}
    check("window: restart after the user closed it keeps the window (meta.json next to the autosaves)",
          st_r.get("gui") is True, (rr, st_r.get("gui")))
    gw = H.workers().get("window1") or gw
    t0 = time.perf_counter()
    H.stop_worker("window1")
    stop_s = time.perf_counter() - t0
    check("window: stop closes the window (process exits)", not hubmod._is_our_worker(gw))
    check("window: closing via the hub does not hang on 'save?' (< 8 s, no taskkill)", stop_s < 8, round(stop_s, 1))
    # ================= refused bind returns exit code 3
    occ = socket.socket()
    occ.bind(("127.0.0.1", 0))
    occ.listen(1)
    port = occ.getsockname()[1]
    p = subprocess.run([hubmod.BLENDER, "-b", "--factory-startup", "--python", hubmod.WORKER, "--", "--port", str(port),
                        "--name", "bindtest", "--secret", "x"], capture_output=True, text=True, timeout=120)
    occ.close()
    check("refused bind: worker exits with code 3 (hub tries another port)", p.returncode == 3, p.returncode)

    # ================= MCP over stdio
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "hub.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8", env=dict(os.environ))
    replies, lock = {}, threading.Lock()

    def reader_():
        for line in p.stdout:
            m = json.loads(line)
            with lock:
                replies[m.get("id")] = (time.perf_counter(), m)
    threading.Thread(target=reader_, daemon=True).start()

    def send(obj):
        p.stdin.write((obj if isinstance(obj, str) else json.dumps(obj)) + "\n")
        p.stdin.flush()

    def wait(i, t=120):
        t0 = time.time()
        while time.time() - t0 < t:
            with lock:
                if i in replies:
                    return replies[i][1]
            time.sleep(0.02)
        return None
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01", "capabilities": {}, "clientInfo": {"name": "suite"}}})
    init = wait(1) or {}
    check("mcp: initialize negotiates a known version", init.get("result", {}).get("protocolVersion") in hubmod.SUPPORTED_PROTOCOLS
          and init["result"]["serverInfo"]["version"] == hubmod.VERSION, init.get("result"))
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send("[1, 2]")
    send("{broken")
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tl = wait(2)
    check("mcp: survives an invalid line and lists tools", tl and len(tl["result"]["tools"]) == len(hubmod.TOOLS))
    names = {t["name"] for t in tl["result"]["tools"]} if tl else set()
    check("mcp: hub_list_archives and hub_restore_archive exposed", {"hub_list_archives", "hub_restore_archive"} <= names, sorted(names))
    send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "hub_acquire", "arguments": {"mode": "exclusive", "label": "mcp"}}})
    acq = wait(3)
    data = json.loads(acq["result"]["content"][0]["text"]) if acq and not acq["result"].get("isError") else {}
    check("mcp: hub_acquire returns worker and token", data.get("token") and data.get("worker"), acq)
    mw, mt = data.get("worker"), data.get("token")
    # live window is the MCP default: hub_acquire without gui opens a windowed worker
    check("mcp: hub_acquire without gui opens a window by default (live)", mw and H.status(mw).get("gui") is True,
          H.status(mw).get("gui") if mw else None)
    send({"jsonrpc": "2.0", "id": 40, "method": "tools/call", "params": {"name": "hub_list_workers", "arguments": {}}})
    send({"jsonrpc": "2.0", "id": 41, "method": "tools/call", "params": {"name": "hub_list_workers", "arguments": {"verbose": True}}})
    t40, t41 = wait(40), wait(41)
    short_txt = t40["result"]["content"][0]["text"] if t40 else ""
    long_txt = t41["result"]["content"][0]["text"] if t41 else ""
    check("tokens: hub_list_workers is short by default (verbose=true has overlay/agents_info) and compact JSON",
          mw in short_txt and "overlay" not in short_txt and "overlay" in long_txt and '": ' not in short_txt
          and len(short_txt) * 3 < len(long_txt), (len(short_txt), len(long_txt)))
    check("gui default: omitted -> window, gui=false -> background, env 0 turns the default off",
          hubmod._gui_arg({}) is True and hubmod._gui_arg({"gui": False}) is False and hubmod._gui_arg({"gui": True}) is True
          and hubmod._gui_arg({"gui": "false"}) is False and hubmod._gui_arg({"gui": "true"}) is True
          and subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import hub; "
                              "print(hub._gui_arg({}))", ROOT], capture_output=True, text=True,
                             env=dict(os.environ, BLENDER_HUB_GUI_DEFAULT="0")).stdout.strip() == "False")
    send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "hub_exec", "arguments": {
        "worker": mw, "token": mt, "agent": "M", "code": "bpy.ops.mesh.primitive_monkey_add(); bpy.ops.object.shade_flat(); result = 'ç ã ok'"}}})
    e4 = wait(4)
    check("mcp: exec with UTF-8 and agent_key on first use", e4 and "ç ã ok" in e4["result"]["content"][0]["text"] and "agent_key" in e4["result"]["content"][0]["text"], e4)
    send({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "hub_turntable", "arguments": {
        "worker": mw, "token": mt, "agent": "M", "collection": "Agent_M", "views": 12, "size": 320}}})
    send({"jsonrpc": "2.0", "id": 6, "method": "ping"})
    t5, t6 = wait(5), wait(6)
    with lock:
        ping_first = replies[6][0] < replies[5][0]
    check("mcp: ping replies before the turntable finishes (concurrent)", ping_first)
    imgs = [cc for cc in (t5 or {}).get("result", {}).get("content", []) if cc.get("type") == "image"]
    check("mcp: turntable returns the contact sheet as an image", len(imgs) == 1 and len(imgs[0]["data"]) > 1000)
    send({"jsonrpc": "2.0", "id": 50, "method": "tools/call", "params": {"name": "hub_turntable", "arguments": {
        "worker": mw, "token": mt, "agent": "M", "collection": "Agent_M", "views": 12, "size": 320}}})
    t50 = wait(50)
    c50 = (t50 or {}).get("result", {}).get("content", [])
    txt50 = "".join(cc.get("text", "") for cc in c50)
    check("mcp: repeated unchanged turntable answers 'unchanged' with no image; no lease block while it is healthy",
          not any(cc.get("type") == "image" for cc in c50) and '"unchanged":true' in txt50 and '"lease"' not in txt50, txt50[:200])
    if imgs:
        with open(os.path.join(TMP, "mcp_sheet.png"), "wb") as f:
            f.write(base64.b64decode(imgs[0]["data"]))
    send({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "hub_exec", "arguments": {
        "worker": mw, "token": mt, "agent": "M", "code": "raise ValueError('test')"}}})
    e7 = wait(7)
    check("mcp: Python error becomes isError", e7 and e7["result"].get("isError") and "ValueError" in e7["result"]["content"][0]["text"])
    send({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "hub_exec", "arguments": {
        "worker": mw, "token": mt, "agent": "M", "code": "import time\nt=time.time()\nwhile time.time()-t<2: pass"}}})
    send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 9}})
    time.sleep(3)
    with lock:
        got9 = 9 in replies
    check("mcp: a cancelled request gets no reply", not got9)
    send({"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "hub_exec", "arguments": {
        "worker": mw, "token": mt, "agent": "M2", "code": "import time\nt=time.time()\nwhile time.time()-t<1: pass\nresult=1"}}})
    send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 10}})
    e10 = wait(10, 20)
    check("mcp: cancelling the first exec does not lose the agent_key (the reply with the key goes out anyway)",
          e10 and "agent_key" in e10["result"]["content"][0]["text"], e10)
    send({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "hub_stop_worker", "arguments": {"name": mw, "token": mt}}})
    check("mcp: stop with its own token", "stopped" in ((wait(8) or {}).get("result", {}).get("content", [{}])[0].get("text", "")))
    p.stdin.close()
    p.wait(timeout=15)

    # ================= wrap-up
    logs = {}
    for n, w in H.workers().items():
        with contextlib.suppress(OSError), open(w["log"], encoding="utf-8", errors="replace") as f:
            logs[n] = [ln for ln in f.read().splitlines() if "Traceback" in ln or "crash" in ln.lower()][:5]
    R["log_errors"] = logs
    H.kill_all_unsafe()
    time.sleep(1)
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq blender.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    R["blender_processes_end"] = out.count("blender.exe")
    R["free_mb_end"] = free_mb()
    R["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    n_ok = sum(1 for cc in R["checks"].values() if cc["ok"])
    R["summary"] = f"{n_ok}/{len(R['checks'])} checks, {len(R['errors'])} errors"
    path = os.path.join(OUT, f"suite-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(R, f, indent=1, ensure_ascii=False, default=str)
    print(json.dumps({"summary": R["summary"], "perf": {k: v for k, v in R["perf"].items() if k != "rss_idle_mb"},
                      "errors": R["errors"], "blender_processes_end": R["blender_processes_end"], "log_errors": logs},
                     indent=1, ensure_ascii=False))
    print("saved to", path)


def _safe(fn):
    try:
        return fn()
    except Exception as e:
        return {"error": repr(e)}


if __name__ == "__main__":
    try:
        main()
    finally:
        with contextlib.suppress(Exception):
            H.kill_all_unsafe()
