"""Blender Hub worker: runs INSIDE a background Blender.

    blender -b [file.blend] --python worker.py -- --port 9881 --name a --secret XYZ
            [--idle 1800] [--autosave 300] [--keep 5] [--state DIR]

Protocol: TCP 127.0.0.1, one JSON line per request and per response.
Request:  {"id": 1, "secret": "...", "cmd": "exec", "token": "...", "agent": "A", "agent_key": "...", ...}
Response: {"id": 1, "ok": true, "result": {...}, "ms": 12.3, "wait_ms": 0.4,
           "lease": {...}, "notices": [...], "agent_key": "..." (only on the agent's first use)}

Rules (see docs/ARCHITECTURE.md):
- Every request carries the worker's `secret` (stored in the hub registry). Keys starting with
  "_" that come from outside are dropped.
- `lease` returns a token. Commands that touch the scene require a live token. exclusive = alone;
  shared = several, each with its own expiry. A token with a request queued or running does not expire.
- Agent: the first use returns an `agent_key`. After the lease expires, only the key holder
  can resume the name (and its collections). The name is never released to someone else.
- Owner "base": whatever came from the opened file (outside Agent_*). Only an exclusive lease
  may change it without a notice.
- Intrusion: hash of everything owned by someone else (object, mesh, attributes, weights, shape keys,
  pose, animation, curves, modifiers incl. Geometry Nodes, constraints, custom props, materials with
  all nodes and node groups, collections). Notice goes to the intruder and to the owner. Not undone
  (no undo in -b): the rotating autosaves are the way back.
- `ping`/`status`/`check_token` are answered on the network thread, without waiting for the queue.
- exec has a watchdog timeout (async exception on the main thread, resent every second).
  A long C call only stops once it returns to Python: that is what hub_kill_worker is for.
"""
import sys
import os
import re
import json
import time
import math
import glob
import queue
import socket
import ctypes
import secrets
import hashlib
import threading
import traceback
import io
import contextlib
import linecache
import shutil

import bpy
import bmesh
import mathutils
from mathutils.bvhtree import BVHTree

try:
    import numpy as np
except Exception:
    np = None

VERSION = "0.14.0"
LEASE_TTL_DEFAULT = 900.0
LEASE_TTL_MAX = 3600.0
NET_CMDS = {"ping", "status", "check_token"}   # answered outside the queue, without bpy
NO_TOKEN_CMDS = {"lease"}
MESH_TYPES = {"MESH", "CURVE", "FONT", "SURFACE", "META"}
BASE = "base"


# ---------------------------------------------------------------- arguments
def _args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    out = {"port": "9881", "name": "worker", "host": "127.0.0.1", "secret": "", "idle": "1800",
           "autosave": "300", "keep": "5", "state": os.path.join(os.environ.get("TEMP", "."), "blender-hub"),
           "keys": ""}
    i = 0
    while i < len(argv):
        k = argv[i].lstrip("-")
        if k in out and i + 1 < len(argv):
            out[k] = argv[i + 1]
            i += 2
        else:
            i += 1
    for k in ("port", "keep"):
        out[k] = int(out[k])
    for k in ("idle", "autosave"):
        out[k] = float(out[k])
    return out


ARGS = _args()
LOCK = threading.RLock()       # PARTS/AGENTS/KEYS/NOTICES/PENDING/STATUS between network and main thread
PARTS = {}                     # token -> {"label", "mode", "ttl", "expires"}
EXPIRED = {}                   # token -> when it expired
AGENTS = {}                    # agent -> live token currently using it
KEYS = {}                      # agent -> agent_key hash (persisted to --keys: survives restarts)
REISSUES = {}                  # agent -> [{"by": lease label, "at": date}] (the original owner gets told)
NOTICES = {}                   # owner -> [notices]
PENDING = {}                   # token -> requests queued/running (does not expire while > 0)
STATUS = {"busy": None, "busy_since": None, "busy_token": None, "busy_deadline": None, "file": None, "commands": 0, "dirty": False,
          "last_activity": time.time(), "last_autosave": None, "autosave_path": None,
          "started": time.time(), "objects": 0, "owners": {}, "gui": not bpy.app.background}
JOBS = queue.Queue()
AGENT_LAST = {}                # agent -> time of its last command (live overlay)
AGENT_INFO = {}                # agent -> {"task", "step", "step_at", "last_change", "stats"} (what it is doing)
MODULES = {}                   # name -> {"path", "mtime", "ns" (dict) or None, "error"}
RUNNING = True
MAIN_TID = threading.get_ident()


# ---------------------------------------------------------------- agent keys (only the hash goes to disk)
def _khash(key):
    return hashlib.blake2b((key or "").encode(), digest_size=16).hexdigest()


def _load_keys():
    if ARGS["keys"] and os.path.exists(ARGS["keys"]):
        with contextlib.suppress(Exception):
            with open(ARGS["keys"], encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "keys" in data:
                KEYS.update(data.get("keys") or {})
                REISSUES.update(data.get("reissues") or {})
            else:  # older format: just the key dictionary
                KEYS.update(data)


def _save_keys():
    if not ARGS["keys"]:
        return
    with contextlib.suppress(Exception):
        os.makedirs(os.path.dirname(ARGS["keys"]), exist_ok=True)
        tmp = ARGS["keys"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"keys": KEYS, "reissues": REISSUES}, f)
        os.replace(tmp, ARGS["keys"])


# ---------------------------------------------------------------- lease
def _now():
    return time.time()


def _gc_leases():
    with LOCK:
        t = _now()
        for tok in [k for k, p in PARTS.items() if p["expires"] <= t and PENDING.get(k, 0) == 0]:
            EXPIRED[tok] = t
            PARTS.pop(tok)
            for ag in [a for a, o in AGENTS.items() if o == tok]:
                AGENTS.pop(ag)  # the name stays bound to the agent_key
        for tok in [k for k, v in EXPIRED.items() if t - v > 86400]:
            EXPIRED.pop(tok)


def _lease_view(token):
    with LOCK:
        p = PARTS.get(token)
        others = [{"label": q["label"], "mode": q["mode"], "expires_in": round(q["expires"] - _now())}
                  for k, q in PARTS.items() if k != token]
        if not p:
            return {"active": False, "others": others}
        return {"active": True, "mode": p["mode"], "label": p["label"],
                "expires_in": round(p["expires"] - _now()), "others": others}


def _renew(tok):
    with LOCK:
        p = PARTS.get(tok)
        if p:
            p["expires"] = _now() + p["ttl"]


def _check_token(req):
    _gc_leases()
    tok = req.get("token")
    with LOCK:
        if tok in PARTS:
            _renew(tok)
            return tok
        if tok in EXPIRED:
            raise PermissionError("your lease expired: call hub_lease(worker, mode=<your original mode>, label=<what you are doing>) for a new token, "
                                  "then pass your agent_key in the next hub_exec/hub_turntable/hub_export_collection "
                                  "(another session may have made changes)")
    raise PermissionError("command requires a valid lease token (hub_acquire/hub_lease)")


def _autoname(token, agent):
    """An unnamed lease is named after its first agent (called only after the bind succeeded)."""
    part = PARTS.get(token)
    if part and part.get("auto_label"):
        part["label"], part["auto_label"] = agent, False


def _bind_agent(req, token):
    """Returns (agent, new_key_or_None, warning_or_None)."""
    agent = req.get("agent") or f"p_{token[:6]}"
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", agent) or agent == BASE:
        raise ValueError("agent: letters, digits, _ and - only (up to 40), and not 'base'")
    with LOCK:
        cur = AGENTS.get(agent)
        exclusive = PARTS.get(token, {}).get("mode") == "exclusive"
        if cur and cur != token:
            raise PermissionError(f"agent {agent} is in use by another lease")
        if agent in KEYS:
            if cur == token or _khash(req.get("agent_key")) == KEYS[agent]:
                AGENTS[agent] = token
                _autoname(token, agent)
                return agent, None, None
            if exclusive:  # lost key: an exclusive lease reissues it, with a recorded notice
                key = secrets.token_hex(8)
                KEYS[agent] = _khash(key)
                AGENTS[agent] = token
                REISSUES.setdefault(agent, []).append({"by": PARTS[token]["label"], "at": time.strftime("%Y-%m-%d %H:%M:%S")})
                REISSUES[agent] = REISSUES[agent][-10:]
                _save_keys()
                NOTICES.setdefault(agent, []).append({"owner": agent, "what": "agent_key reissued by an exclusive lease",
                                                      "at": time.strftime("%H:%M:%S")})
                _autoname(token, agent)
                return agent, key, "agent_key reissued (the old one is no longer valid)"
            last = (REISSUES.get(agent) or [None])[-1]
            if last:  # whoever comes back with the old key learns who reissued it and when
                raise PermissionError(f"the agent_key of {agent} was reissued on {last['at']} by lease "
                                      f"'{last['by']}': your old key is no longer valid "
                                      "(talk to whoever reissued it, or request an exclusive lease to reissue it again)")
            raise PermissionError(f"agent {agent} already exists: resuming it requires its agent_key "
                                  "(lost it? hub_agent_key with the token still using the agent, or an exclusive lease reissues it)")
        coll = bpy.data.collections.get(f"Agent_{agent}")
        if coll is not None and coll.get("hub_owner") == agent and not exclusive:
            raise PermissionError(f"Agent_{agent} came from the file without a registered key: resume it with an exclusive lease")
        key = secrets.token_hex(8)
        KEYS[agent] = _khash(key)
        AGENTS[agent] = token
        _save_keys()
        _autoname(token, agent)
        return agent, key, None


# ---------------------------------------------------------------- utilities
def _rss_mb():
    try:
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return None, None
        return round(pmc.WorkingSetSize / 1048576, 1), round(pmc.PeakWorkingSetSize / 1048576, 1)
    except Exception:
        return None, None


def _owner(coll):
    return coll.get("hub_owner") if coll else None


def _agent_coll(agent):
    name = f"Agent_{agent}"
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
        coll["hub_owner"] = agent
    own = _owner(coll)
    if own and own != agent:
        raise PermissionError(f"{name} is owned by {own}")
    coll["hub_owner"] = agent
    if coll.name not in bpy.context.scene.collection.children:
        bpy.context.scene.collection.children.link(coll)  # collection outside the scene: relink before use
    return coll


def _obj_owner(ob):
    for c in ob.users_collection:
        o = _owner(c)
        if o:
            return o
    # nested collections: owner of the parent collection
    for coll in bpy.data.collections:
        o = _owner(coll)
        if o and ob.name in coll.all_objects:
            return o
    return BASE


def _refresh_owners():
    STATUS["owners"] = {c.name: _owner(c) for c in bpy.data.collections if _owner(c)}


def _h(*parts):
    h = hashlib.blake2b(digest_size=16)
    for p in parts:
        if isinstance(p, (bytes, bytearray, memoryview)):
            h.update(bytes(p))
        else:
            h.update(repr(p).encode("utf-8", "replace"))
        h.update(b"|")
    return h.hexdigest()


def _arr(coll, attr, n, width, dtype="f"):
    if np is None or n == 0:
        return b""
    a = np.empty(n * width, dtype={"f": np.float32, "i": np.int32, "b": np.bool_}[dtype])
    try:
        coll.foreach_get(attr, a)
    except Exception:
        return b"?"
    return a.tobytes()


def _rna_props(struct, depth=0, deep=False):
    """Editable props (not read-only ones, which change on evaluation and would raise false alarms).
    deep=True also follows non-ID pointer structs (e.g. eevee.ray_tracing_options), two levels down."""
    out = []
    for p in struct.bl_rna.properties:
        if p.identifier in {"rna_type", "name", "interop_id"} or p.type == "COLLECTION":  # interop_id: filled lazily on first render
            continue
        # read-only props change on evaluation (skip them), except nested setting structs in deep mode
        # (eevee.ray_tracing_options is a read-only pointer to an editable struct)
        if p.is_readonly and not (deep and depth < 2 and p.type == "POINTER"):
            continue
        try:
            v = getattr(struct, p.identifier)
        except Exception:
            continue
        if p.type == "POINTER":
            if deep and depth < 2 and v is not None and not isinstance(v, bpy.types.ID):
                v = _rna_props(v, depth + 1, True)
            elif p.is_readonly:
                continue
            else:
                v = getattr(v, "name", None) if v is not None else None
        elif hasattr(v, "__len__") and not isinstance(v, str):
            v = tuple(v)
        out.append((p.identifier, v))
    return tuple(out)


def _idprops(idb):
    try:
        return tuple((k, repr(idb[k])) for k in idb.keys() if not k.startswith("hub_"))
    except Exception:
        return ()


_ATTR_FIELD = {"FLOAT": ("value", 1), "INT": ("value", 1), "BOOLEAN": ("value", 1), "FLOAT2": ("vector", 2),
               "FLOAT_VECTOR": ("vector", 3), "FLOAT_COLOR": ("color", 4), "BYTE_COLOR": ("color", 4),
               "INT8": ("value", 1), "INT32_2D": ("value", 2), "QUATERNION": ("value", 4)}


def _mesh_hash(me, ob=None):
    nv, nl = len(me.vertices), len(me.loops)
    parts = [nv, len(me.edges), len(me.polygons), tuple(m.name if m else None for m in me.materials), _idprops(me)]
    for at in me.attributes:  # position, topology, UV, color, sharp_face, crease... all generic
        if at.name.startswith(".") and at.name not in (".corner_vert", ".edge_verts"):
            continue  # selection (.select_*, .uv_select_*): nobody's work, and it cost half the hash time
        field, w = _ATTR_FIELD.get(at.data_type, ("value", 1))
        dt = "i" if at.data_type in ("INT", "INT8", "INT32_2D") else ("b" if at.data_type == "BOOLEAN" else "f")
        parts += [at.name, at.domain, at.data_type, _arr(at.data, field, len(at.data), w, dt)]
    if me.shape_keys:
        for kb in me.shape_keys.key_blocks:
            parts += [kb.name, kb.value, _arr(kb.data, "co", len(kb.data), 3)]
    if ob is not None and ob.vertex_groups:
        parts.append(tuple(vg.name for vg in ob.vertex_groups))
        parts.append(tuple((v.index, tuple((g.group, round(g.weight, 6)) for g in v.groups)) for v in me.vertices if v.groups))
    return _h(*parts)


def _tree_hash(tree, seen):
    if tree is None or tree.name in seen:
        return None
    seen.add(tree.name)
    parts = [tree.name]
    for n in tree.nodes:
        parts += [n.name, n.bl_idname, _rna_props(n)]
        for s in list(n.inputs) + list(n.outputs):
            dv = getattr(s, "default_value", None)
            if dv is not None and hasattr(dv, "__len__") and not isinstance(dv, str):
                dv = tuple(dv)
            parts.append((s.identifier, dv))
        if getattr(n, "color_ramp", None):
            parts.append(tuple((e.position, tuple(e.color)) for e in n.color_ramp.elements))
        if getattr(n, "node_tree", None):
            parts.append(_tree_hash(n.node_tree, seen))
        img = getattr(n, "image", None)
        if img is not None:
            parts.append((img.name, img.filepath, tuple(img.size), img.is_dirty))
    parts.append(tuple((l.from_node.name, l.from_socket.identifier, l.to_node.name, l.to_socket.identifier)
                       for l in tree.links))
    return _h(*parts)


def _gn_inputs(m):
    """Geometry Nodes inputs: 4.x stores them in the modifier's IDProperties; 5.3 in properties.inputs.Socket_N."""
    out = list(_idprops(m))
    props = getattr(m, "properties", None)
    inputs = getattr(props, "inputs", None) if props is not None else None
    if inputs is not None:
        for p in inputs.bl_rna.properties:
            if p.type == "POINTER" and p.identifier != "rna_type":
                sub = getattr(inputs, p.identifier, None)
                if sub is not None:
                    out.append((p.identifier, _rna_props(sub)))
    return tuple(out)


def _material_hash(mat):
    return _h(mat.name, _rna_props(mat), _idprops(mat), _tree_hash(mat.node_tree, set()) if mat.node_tree else None)


def _anim_hash(idb):
    ad = getattr(idb, "animation_data", None)
    if not ad or not ad.action:
        return None
    parts = [ad.action.name]
    fcs = getattr(ad.action, "fcurves", None)
    if fcs is None:  # 5.x: layered actions
        with contextlib.suppress(Exception):
            fcs = [fc for layer in ad.action.layers for strip in layer.strips
                   for cb in strip.channelbags for fc in cb.fcurves]
    for fc in fcs or []:
        parts += [fc.data_path, fc.array_index, _arr(fc.keyframe_points, "co", len(fc.keyframe_points), 2)]
    return _h(*parts)


def _animated_paths(ob):
    """Animated data_paths (fcurves and drivers): evaluation reapplies them on every command."""
    ad = ob.animation_data
    if not ad:
        return set()
    paths = {d.data_path for d in ad.drivers}
    act = ad.action
    if act:
        fcs = getattr(act, "fcurves", None)
        if fcs is None:
            with contextlib.suppress(Exception):
                fcs = [fc for layer in act.layers for strip in layer.strips for cb in strip.channelbags for fc in cb.fcurves]
        paths |= {fc.data_path for fc in fcs or []}
    for track in getattr(ad, "nla_tracks", []):  # actions on NLA tracks are also reapplied on evaluation
        for strip in track.strips:
            sa = strip.action
            if sa is None:
                continue
            sfc = getattr(sa, "fcurves", None)
            if sfc is None:
                with contextlib.suppress(Exception):
                    sfc = [fc for layer in sa.layers for st in layer.strips for cb in st.channelbags for fc in cb.fcurves]
            paths |= {fc.data_path for fc in sfc or []}
    return paths


_TF = ("location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale",
       "delta_location", "delta_rotation_euler", "delta_rotation_quaternion", "delta_scale")
_BONE_TF = ("location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale")


def _object_hash(ob):
    # Local transform per channel (not matrix_world: world only updates in the depsgraph and changes with a
    # parent owned by someone else). Animated channels are skipped (evaluation reapplies them); the rest stays watched.
    anim = _animated_paths(ob)
    tf = tuple((k, tuple(getattr(ob, k))) for k in _TF if k not in anim)
    parts = [ob.type, ob.rotation_mode, tf, tuple(tuple(r) for r in ob.matrix_parent_inverse), ob.parent.name if ob.parent else None, ob.parent_type,
             ob.hide_viewport, ob.hide_render, ob.data.name if ob.data else None, _idprops(ob),
             tuple(s.material.name if s.material else None for s in ob.material_slots),
             tuple(sorted(c.name for c in ob.users_collection)), _anim_hash(ob)]
    for m in ob.modifiers:
        parts += [m.name, m.type, _rna_props(m)]
        if m.type == "NODES":
            parts.append(_gn_inputs(m))
            if m.node_group:
                parts.append(_tree_hash(m.node_group, set()))
    for c in ob.constraints:
        parts += [c.name, c.type, _rna_props(c)]
    if ob.pose:
        parts.append(tuple((b.name, b.rotation_mode,
                            tuple((k, tuple(getattr(b, k))) for k in _BONE_TF if 'pose.bones["%s"].%s' % (b.name, k) not in anim))
                           for b in ob.pose.bones))
    if ob.type == "MESH" and ob.data:
        parts.append(_mesh_hash(ob.data, ob))
    elif ob.type in {"CURVE", "SURFACE"} and ob.data:
        for sp in ob.data.splines:
            parts += [_arr(sp.points, "co", len(sp.points), 4), _arr(sp.bezier_points, "co", len(sp.bezier_points), 3)]
    elif ob.type == "ARMATURE" and ob.data:
        parts.append(tuple((b.name, tuple(b.head_local), tuple(b.tail_local)) for b in ob.data.bones))
    elif ob.data is not None:
        parts.append(_rna_props(ob.data))
    return _h(*parts)


def _owner_map():
    """object -> owner, in a single pass (previously scanned every collection per object)."""
    m = {}
    for coll in bpy.data.collections:
        own = _owner(coll)
        if own:
            for ob in coll.all_objects:
                m.setdefault(ob.name, own)
    return m


def _snapshot(agent, exclusive):
    """Everything owned by someone else. With an exclusive lease, 'base' is yours."""
    objs, mats, colls = {}, {}, {}
    omap = _owner_map()
    for ob in bpy.data.objects:
        if not ob.users_collection and not ob.users_scene:
            continue
        own = omap.get(ob.name, BASE)
        if own == agent or (own == BASE and exclusive):
            continue
        objs[ob.name] = (own, _object_hash(ob))
        for s in ob.material_slots:
            if s.material and s.material.name not in mats:
                mats[s.material.name] = (own, _material_hash(s.material))
    for coll in bpy.data.collections:
        own = _owner(coll)
        if not own or own == agent:
            continue
        colls[coll.name] = (own, coll.name in bpy.context.scene.collection.children,
                            tuple(sorted(o.name for o in coll.objects)), tuple(sorted(c.name for c in coll.children)),
                            coll.hide_render)
    return {"objs": objs, "mats": mats, "colls": colls}


def _diff(agent, before):
    out = []
    for name, (own, h) in before["objs"].items():
        ob = bpy.data.objects.get(name)
        if ob is None:
            out.append({"object": name, "owner": own, "what": "deleted"})
        elif _object_hash(ob) != h:
            out.append({"object": name, "owner": own, "what": "modified"})
    for name, (own, h) in before["mats"].items():
        mat = bpy.data.materials.get(name)
        if mat is None:
            out.append({"material": name, "owner": own, "what": "deleted"})
        elif _material_hash(mat) != h:
            out.append({"material": name, "owner": own, "what": "material modified (used by owner)"})
    for name, (own, linked, obs, kids, hide) in before["colls"].items():
        c = bpy.data.collections.get(name)
        if c is None:
            out.append({"collection": name, "owner": own, "what": "collection deleted"})
            continue
        if _owner(c) != own:
            out.append({"collection": name, "owner": own, "what": f"owner changed to {_owner(c)}"})
        if (name in bpy.context.scene.collection.children) != linked:
            out.append({"collection": name, "owner": own, "what": "collection unlinked/linked in scene"})
        if tuple(sorted(o.name for o in c.objects)) != obs or tuple(sorted(k.name for k in c.children)) != kids:
            out.append({"collection": name, "owner": own, "what": "objects added/removed from collection"})
        if c.hide_render != hide:
            out.append({"collection": name, "owner": own, "what": "collection visibility changed"})
    return out


def _deliver(violations, by_agent):
    with LOCK:
        for v in violations:
            NOTICES.setdefault(v["owner"], []).append(dict(v, by=by_agent, at=time.strftime("%H:%M:%S")))
            NOTICES[v["owner"]] = NOTICES[v["owner"]][-50:]


def _pop_notices(token):
    with LOCK:
        mine = [a for a, t in AGENTS.items() if t == token]
        if PARTS.get(token, {}).get("mode") == "exclusive":
            mine.append(BASE)
        out = []
        for a in mine:
            out += NOTICES.pop(a, [])
        return out


def _world_bmesh(ob, depsgraph):
    bm = bmesh.new()
    ev = ob.evaluated_get(depsgraph)
    bm.from_object(ev, depsgraph)
    bm.transform(ob.matrix_world)
    return bm


def _targets(req):
    names = req.get("objects")
    coll_name = req.get("collection")
    if names:
        obs = [bpy.data.objects[n] for n in names if n in bpy.data.objects]
    elif coll_name:
        coll = bpy.data.collections.get(coll_name)
        if coll is None:
            raise ValueError(f"collection does not exist: {coll_name}")
        obs = list(coll.all_objects)
    else:
        obs = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    obs = [o for o in obs if o.type in MESH_TYPES]
    if not obs:
        raise ValueError("no mesh objects in target")
    return obs


def _bbox(obs):
    lo = mathutils.Vector((1e18,) * 3)
    hi = mathutils.Vector((-1e18,) * 3)
    for ob in obs:
        for c in ob.bound_box:
            w = ob.matrix_world @ mathutils.Vector(c)
            lo = mathutils.Vector(map(min, lo, w))
            hi = mathutils.Vector(map(max, hi, w))
    return lo, hi


def _set_workbench(render):
    """bl_rna lists only some engines (in 5.3, only EEVEE): try it and parse the error."""
    try:
        render.engine = "BLENDER_WORKBENCH"
        return render.engine
    except TypeError as e:
        for ident in re.findall(r"'([A-Z_]+)'", str(e)):
            if "WORKBENCH" in ident:
                render.engine = ident
                return ident
        raise RuntimeError(f"no Workbench engine in this Blender: {e}")


def _set_eevee(render):
    """EEVEE is 'BLENDER_EEVEE' in 5.x and 'BLENDER_EEVEE_NEXT' in 4.2-4.x."""
    for ident in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
        try:
            render.engine = ident
            return ident
        except TypeError:
            continue
    raise RuntimeError("no EEVEE engine in this Blender")


SHADINGS = ("solid", "rendered", "cycles", "wireframe", "xray")
LIT = ("rendered", "cycles")  # real render engines: the hub's light rig, materials, emissive


def _shading_arg(req):
    sh = str(req.get("shading") or "solid").lower()
    if sh not in SHADINGS:
        raise ValueError("shading must be one of " + ", ".join(SHADINGS))
    return sh


# ---------------------------------------------------------------- watchdog timeout
class _Timeout(BaseException):
    pass


EXEC_GUARD = threading.Lock()
EXEC_STATE = {"active": False, "deadline": 0.0, "gen": 0, "armed_gen": None}


def _async_raise(exc):
    ctypes.pythonapi.PyThreadState_SetAsyncExc.argtypes = [ctypes.c_ulong, ctypes.py_object]
    ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(MAIN_TID), ctypes.py_object(exc) if exc else ctypes.py_object())


def _clear_async():
    ctypes.pythonapi.PyThreadState_SetAsyncExc.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
    ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(MAIN_TID), None)


def _in_agent_code():
    """Is the main thread inside the agent's code right now? (never hit the worker loop)."""
    f = sys._current_frames().get(MAIN_TID)
    while f is not None:
        if f.f_code.co_filename.startswith("<exec "):
            return True
        f = f.f_back
    return False


def _watchdog():
    while True:
        time.sleep(0.2)
        with EXEC_GUARD:
            if (EXEC_STATE["active"] and EXEC_STATE["gen"] == EXEC_STATE["armed_gen"] and not LIVE["in"]
                    and time.perf_counter() > EXEC_STATE["deadline"] and _in_agent_code()):
                _async_raise(_Timeout)
                EXEC_STATE["deadline"] = time.perf_counter() + 1.0  # resend if the code swallows it


def _disarm():
    """After every job: watchdog off and no pending exception."""
    with EXEC_GUARD:
        EXEC_STATE["active"] = False
        EXEC_STATE["armed_gen"] = None
        _clear_async()


def _run_guarded(fn, seconds):
    """Runs fn() on the main thread with a time limit. No per-line cost (no settrace)."""
    try:
        try:
            with EXEC_GUARD:
                EXEC_STATE["gen"] += 1
                EXEC_STATE.update(active=True, deadline=time.perf_counter() + seconds, armed_gen=EXEC_STATE["gen"])
            fn()
        finally:
            with EXEC_GUARD:
                EXEC_STATE["active"] = False
                _clear_async()  # exception sent but not yet delivered: cancel it
    except _Timeout:
        raise _Timeout(f"exec exceeded {seconds}s")


# ---------------------------------------------------------------- commands (main thread)
def cmd_lease(req):
    _gc_leases()
    mode = req.get("mode", "exclusive")
    if mode not in ("exclusive", "shared"):
        raise ValueError("mode = exclusive | shared")
    ttl = min(max(float(req.get("ttl", LEASE_TTL_DEFAULT)), 2.0), LEASE_TTL_MAX)
    auto_label = not req.get("label")
    label = str(req.get("label") or "unnamed")[:60]
    with LOCK:
        live = list(PARTS.values())
        if mode == "exclusive" and live:
            raise PermissionError(f"worker in use by {len(live)} lease(s): {[p['label'] for p in live]}")
        if mode == "shared" and any(p["mode"] == "exclusive" for p in live):
            raise PermissionError("worker reserved in exclusive mode")
        tok = secrets.token_hex(12)
        PARTS[tok] = {"label": label, "mode": mode, "ttl": ttl, "expires": _now() + ttl, "auto_label": auto_label}
    return {"token": tok, "mode": mode, "ttl": ttl, "worker": ARGS["name"]}


def cmd_renew(req):
    return {"renewed": True}


def cmd_release(req):
    tok = req["token"]
    with LOCK:
        PARTS.pop(tok, None)
        for ag in [a for a, o in AGENTS.items() if o == tok]:
            AGENTS.pop(ag)
    return {"released": True}


def cmd_info(req):
    return {"name": ARGS["name"], "blender": bpy.app.version_string, "file": bpy.data.filepath or None,
            "objects": len(bpy.data.objects), "owners": dict(STATUS["owners"]),
            "agents_mine": [a for a, t in AGENTS.items() if t == req.get("token")]}


def cmd_agent_key(req):
    """Lost key: the token that is STILL using the agent requests a new one (the old one stops working)."""
    agent = req.get("agent")
    with LOCK:
        if not agent or AGENTS.get(agent) != req["token"]:
            raise PermissionError(f"your token is not using agent {agent} right now")
        key = secrets.token_hex(8)
        KEYS[agent] = _khash(key)
        REISSUES.setdefault(agent, []).append({"by": PARTS[req["token"]]["label"] + " (hub_agent_key)",
                                               "at": time.strftime("%Y-%m-%d %H:%M:%S")})
        REISSUES[agent] = REISSUES[agent][-10:]
        _save_keys()
    return {"agent": agent, "agent_key": key}


def _gui_window():
    wm = bpy.context.window_manager
    if bpy.app.background or not wm.windows:
        raise RuntimeError("only works on a worker with a window (hub_start_worker gui=true)")
    return wm.windows[0]


def _view3d(win):
    """The LARGEST 3D view of the window (a split layout may have a small one first)."""
    areas = [a for a in win.screen.areas if a.type == "VIEW_3D"]
    if not areas:
        return None, None
    area = max(areas, key=lambda a: a.width * a.height)
    return area, next((r for r in area.regions if r.type == "WINDOW"), None)


def _redraw():
    if bpy.app.background:
        return
    with contextlib.suppress(Exception):
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                area.tag_redraw()


LIVE = {"last": 0.0, "cost": 0.0, "count": 0, "progress": None, "in": False, "rendering": False,
        "loading": False, "op_depth": 0, "skip_turntable": False}


def _live_every():
    try:
        return max(float(os.environ.get("BLENDER_HUB_LIVE_EVERY", "0.25")), 0.0)
    except ValueError:
        return 0.25


LIVE_EVERY = _live_every()  # s between redraws while a command runs; 0 = off


def _live_safe():
    """Only at safe points: a command is running, no operator is executing (op_depth 0), no render / bake /
    composite job, no file load or undo in progress, and Object Mode (drawing a stale edit/sculpt mesh can crash)."""
    if bpy.app.background or LIVE_EVERY <= 0 or not STATUS.get("busy"):
        return False
    if LIVE["in"] or LIVE["rendering"] or LIVE["loading"] or LIVE["op_depth"] > 0 or LIVE["skip_turntable"]:
        return False
    with contextlib.suppress(Exception):
        for job in ("RENDER", "RENDER_PREVIEW", "OBJECT_BAKE", "COMPOSITE", "SEQ_BUILD_PROXY"):
            if bpy.app.is_job_running(job):
                return False
    try:
        return bpy.context.mode == "OBJECT"
    except Exception:
        return False


def _live_refresh(force=False):
    """Window mode, WHILE a command runs (the main thread is busy, so Blender would not redraw until it ends):
    draw the 3D view now. Throttled: at least LIVE_EVERY s apart, and never more than ~25% of the time
    (interval >= 4x the last draw's cost); force (refresh(), turntable frames) only skips LIVE_EVERY.
    Called after each top-level bpy.ops call, on frame changes, after turntable frames, from link() and
    refresh(). Costs no tokens: nothing goes back to the agent."""
    if not _live_safe():
        return
    now = time.time()
    floor = max(4.0 * LIVE["cost"], 1.0 / 30.0)
    if now - LIVE["last"] < (floor if force else max(LIVE_EVERY, floor)):
        return
    # context.mode only reflects the active object: any object left in Edit/Sculpt mode keeps a stale draw mesh
    with contextlib.suppress(Exception):
        if any(o.mode != "OBJECT" for o in bpy.context.view_layer.objects):
            return
    t0 = time.perf_counter()
    try:
        LIVE["in"] = True  # drawing may evaluate the depsgraph, which calls the handlers again
        wm = bpy.context.window_manager
        if not wm.windows:
            return
        win = wm.windows[0]
        area, region = _view3d(win)
        if area is None or region is None:
            return
        area.tag_redraw()  # only the 3D view (with the live panel), not the outliner/properties of a big scene
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.wm.redraw_timer(type="DRAW_SWAP", iterations=1)
        LIVE["count"] += 1
    except Exception:
        pass
    finally:
        LIVE["cost"] = time.perf_counter() - t0
        LIVE["last"] = time.time()
        LIVE["in"] = False


class _LiveOp:
    """Wraps a bpy.ops callable: counts nesting, and after a TOP-LEVEL operator returns (a safe point: nothing
    is being evaluated or rendered) lets the live window catch up. Everything else is delegated untouched."""
    __slots__ = ("_op",)

    def __init__(self, op):
        self._op = op

    def __call__(self, *a, **k):
        LIVE["op_depth"] += 1
        try:
            return self._op(*a, **k)
        finally:
            LIVE["op_depth"] -= 1
            if LIVE["op_depth"] == 0 and STATUS.get("busy"):
                _live_refresh()

    def __getattr__(self, name):
        return getattr(self._op, name)

    def __repr__(self):
        return repr(self._op)


def _install_live_ops():
    """bpy.ops.<module>.<op> objects come from bpy.ops._bpy_ops_submodule__getattr__ (5.x): wrap what it returns."""
    import bpy.ops as _ops
    orig = getattr(_ops, "_bpy_ops_submodule__getattr__", None)
    if orig is None or getattr(orig, "_hub_live", False):
        return False

    def wrapped(module, func):
        return _LiveOp(orig(module, func))
    wrapped._hub_live = True
    _ops._bpy_ops_submodule__getattr__ = wrapped
    return True


def _ensure_object_mode():
    """Window mode: the user may be in Edit/Sculpt/Weight Paint. When leaving that mode, Blender writes
    the edit mesh back over the data and would wipe what the agent did: switch to Object Mode FIRST."""
    if bpy.app.background or bpy.context.mode == "OBJECT":
        return None
    prev = bpy.context.mode
    win = bpy.context.window_manager.windows[0]
    area, region = _view3d(win)
    kw = {"window": win}
    if area:
        kw.update(area=area, region=region)
    with bpy.context.temp_override(**kw):
        bpy.ops.object.mode_set(mode="OBJECT")
    return f"left the user's {prev} mode (so the agent's change is not lost)"


def cmd_view(req):
    """Window mode: frames the viewport camera on the target (collection/objects) for whoever is watching."""
    win = _gui_window()
    area, region = _view3d(win)
    if area is None:
        raise RuntimeError("window has no 3D viewport")
    obs = _targets(req)
    vl = bpy.context.view_layer
    sel = [o for o in vl.objects if o.select_get()]
    active = vl.objects.active
    with bpy.context.temp_override(window=win, area=area, region=region):
        for o in vl.objects:
            o.select_set(False)
        for o in obs:
            with contextlib.suppress(Exception):
                o.select_set(True)
        bpy.ops.view3d.view_selected()
        for o in vl.objects:  # restore the user's selection and active object
            with contextlib.suppress(Exception):
                o.select_set(o in sel)
        with contextlib.suppress(Exception):
            vl.objects.active = active
    sp = area.spaces.active
    shading = req.get("shading")
    if shading:
        with contextlib.suppress(Exception):
            sp.shading.type = "SOLID"
            sp.shading.color_type = shading
    return {"framed": [o.name for o in obs]}


def cmd_screenshot(req):
    """Window mode: what the user is seeing. By default only the 3D viewport, scaled so its longest side is
    `size` px (default 1024): image tokens grow with pixels, and the panels around the viewport rarely matter.
    full=true captures the whole window."""
    out_dir = os.path.join(ARGS["state"], "renders", ARGS["name"])
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"screen_{int(time.time() * 1000)}.png")
    size = min(max(int(req.get("size") or 1024), 256), 2048)
    if req.get("image"):  # an image instead of the screen: "Render Result", an image in the file, or a file path
        return _image_to_png(str(req["image"]), path, size)
    win = _gui_window()
    full = bool(req.get("full"))
    _redraw()
    area, region = (None, None) if full else _view3d(win)
    if area is None:
        area_v, _r = _view3d(win)
    else:
        area_v = area
    sp = area_v.spaces.active if area_v is not None else None
    want = str(req.get("shading") or "").upper()
    if want and want not in ("SOLID", "MATERIAL", "RENDERED", "WIREFRAME"):
        raise ValueError("shading must be solid, material, rendered or wireframe")
    prev = sp.shading.type if (sp is not None and want) else None
    try:
        if prev is not None and want != prev:
            sp.shading.type = want
        with bpy.context.temp_override(window=win):
            with contextlib.suppress(Exception):  # actually draw now (otherwise the screenshot may show the previous scene)
                # rendered viewport converges over several redraws
                bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=24 if want == "RENDERED" else 1)
        if area is not None:
            with bpy.context.temp_override(window=win, area=area, region=region):
                bpy.ops.screen.screenshot_area(filepath=path)
        else:
            with bpy.context.temp_override(window=win):
                bpy.ops.screen.screenshot(filepath=path)
    finally:
        if prev is not None and sp.shading.type != prev:
            with contextlib.suppress(Exception):
                sp.shading.type = prev
    w = h = None
    img = None
    try:
        img = bpy.data.images.load(path, check_existing=False)
        w, h = img.size
        scale = size / max(w, h)
        if scale < 1.0:
            w, h = max(1, round(w * scale)), max(1, round(h * scale))
            img.scale(w, h)
            img.filepath_raw = path
            img.file_format = "PNG"
            img.save()
    except Exception:
        pass  # the unscaled capture is still valid
    finally:
        if img is not None:  # never leave the capture inside the user's file (Image Editor, autosaves)
            with contextlib.suppress(Exception):
                bpy.data.images.remove(img)
    return {"path": path, "size": [w, h], "area": "window" if area is None else "viewport"}


def _clean_text(v, n=120):
    """One line for the panel: newlines and control characters become spaces (blf draws them badly)."""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in str(v or "")).split())[:n]


def cmd_note(req):
    """hub_note: the agent declares its current task (shown in the live panel and in status)."""
    task = _clean_text(req.get("task"))
    with LOCK:
        AGENT_INFO.setdefault(req["_agent"], {})["task"] = task or None
    return {"agent": req["_agent"], "task": task or None}


def _own_sig(agent):
    """Cheap signature of the agent's own objects: what it added, removed or changed in a command."""
    coll = bpy.data.collections.get(f"Agent_{agent}") if agent else None
    out = {}
    if coll is None:
        return out
    for ob in coll.all_objects:
        me = ob.data if ob.type == "MESH" else None
        out[ob.name] = (ob.type, len(me.vertices) if me else 0, len(me.polygons) if me else 0,
                        tuple(round(v, 4) for row in ob.matrix_basis for v in row), ob.data.name if ob.data else None,
                        len(ob.modifiers))
    return out


def _record_change(agent, before):
    after = _own_sig(agent)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(n for n in set(after) & set(before) if after[n] != before[n])
    faces = sum(v[2] for v in after.values())
    with LOCK:
        info = AGENT_INFO.setdefault(agent, {})
        if added or removed or changed:
            info["last_change"] = {"added": added[:20], "removed": removed[:20], "changed": changed[:20],
                                   "n_added": len(added), "n_removed": len(removed), "n_changed": len(changed),
                                   "at": time.time()}
        info["stats"] = {"objects": len(after), "faces": faces}


def cmd_claim(req):
    return {"collection": _agent_coll(req["_agent"]).name}


def _module_specs():
    """BLENDER_HUB_MODULES="K=C:\\path\\kit.py;other=C:\\x.py" (name defaults to the file stem)."""
    out = []
    for item in os.environ.get("BLENDER_HUB_MODULES", "").split(";"):
        item = item.strip()
        if not item:
            continue
        name, path = item.split("=", 1) if "=" in item and not re.match(r"^[A-Za-z]:[\\/]", item) else (None, item)
        path = os.path.abspath(path.strip().strip('"'))
        name = (name or os.path.splitext(os.path.basename(path))[0]).strip()
        out.append((name, path))
    return out


MODULE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
RESERVED_NAMES = {"bpy", "bmesh", "mathutils", "np", "math", "agent", "coll", "link", "result", "args", "refresh"}


AGENT_MODS = {}  # (module name, agent) -> (mtime, globals dict): every agent gets its own instance


def _load_modules(agent=None):
    """Each module is exec'd into its own dict (same as `K = {}; exec(open(path).read(), K)`),
    re-loaded when its file changes. A broken module never blocks hub_exec: its error is reported.

    MODULES holds the checked, compiled code. With `agent`, that agent gets ITS OWN instance (exec'd once per
    agent and file version), so `K` is the kit functions' own globals: `K["SCALE"] = 2; K["build"]()` works as in
    the plain pattern, and nothing an agent sets in its K reaches the other agents."""
    def put(name, value):
        with LOCK:  # status() reads MODULES from the network thread
            MODULES[name] = value
    for name, path in _module_specs():
        if not MODULE_NAME_RE.fullmatch(name):
            put(name, {"path": path, "mtime": None, "ns": None, "error": "invalid name (use letters, digits and _)"})
            continue
        if name in RESERVED_NAMES:
            put(name, {"path": path, "mtime": None, "ns": None, "error": f"reserved name: {sorted(RESERVED_NAMES)} are hub_exec built-ins"})
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError as e:
            put(name, {"path": path, "mtime": None, "ns": None, "error": f"{type(e).__name__}: {e}"})
            continue
        cur = MODULES.get(name)
        if cur and cur["mtime"] == mtime and cur["path"] == path:
            continue
        ns = {"__file__": path, "__name__": f"hub_module_{name}"}
        try:
            with open(path, encoding="utf-8") as f:
                src = f.read()
            # "<exec module ...>" filename: the exec watchdog can interrupt a module that hangs at load
            fname = f"<exec module {name}: {path}>"
            code = compile(src, fname, "exec")
            linecache.cache[fname] = (len(src), None, src.splitlines(True), fname)  # tracebacks show the kit's lines
            _run_guarded(lambda: exec(code, ns), 60)  # probe: a broken module is reported once, not per agent
            put(name, {"path": path, "mtime": mtime, "ns": ns, "code": code, "error": None})
        except BaseException:
            put(name, {"path": path, "mtime": mtime, "ns": None, "code": None, "error": traceback.format_exc(limit=3)[-600:]})
    if not agent:
        return
    with LOCK:
        ready = [(n, m) for n, m in MODULES.items() if m.get("code") is not None]
    for name, m in ready:
        key = (name, agent)
        cur = AGENT_MODS.get(key)
        if cur and cur[0] == m["mtime"]:
            continue
        ns = {"__file__": m["path"], "__name__": f"hub_module_{name}"}
        try:
            _run_guarded(lambda: exec(m["code"], ns), 60)
            AGENT_MODS[key] = (m["mtime"], ns)
        except BaseException:
            AGENT_MODS.pop(key, None)


def cmd_exec(req):
    agent = req["_agent"]
    coll = _agent_coll(agent)
    vl = bpy.context.view_layer

    def find_lc(lc):
        if lc.collection == coll:
            return lc
        for ch in lc.children:
            r = find_lc(ch)
            if r:
                return r
    lc = find_lc(vl.layer_collection)
    if lc:
        if lc.exclude:
            lc.exclude = False
        vl.active_layer_collection = lc

    def link(ob):
        if ob.name not in coll.objects:
            coll.objects.link(ob)
        _live_refresh()  # bpy.data-style building (no bpy.ops) shows up live too
        return ob

    mods = {n: inst[1] for (n, a), inst in AGENT_MODS.items() if a == agent and n in MODULES and MODULES[n].get("code")}
    ns = {**mods, "bpy": bpy, "bmesh": bmesh, "mathutils": mathutils, "np": np, "math": math,
          "agent": agent, "coll": coll, "link": link, "result": None,
          "refresh": lambda: _live_refresh(force=True)}  # long scripts: show the work in the window now
    buf = io.StringIO()
    err = None
    timeout = min(max(float(req.get("timeout", 120)), 1.0), 1800.0)
    if req.get("script") and req.get("code"):
        raise ValueError("pass either `code` or `script`, not both")
    if req.get("script"):  # a saved .py file: agents edit the file instead of re-sending the whole code
        raw = str(req["script"])
        if not os.path.isabs(raw):
            raise ValueError(f"script must be an absolute path (relative paths resolve inside the Blender process): {raw}")
        spath = os.path.abspath(raw)
        if not spath.lower().endswith(".py") or not os.path.isfile(spath):
            raise ValueError(f"script must be an existing .py file: {spath}")
        with open(spath, encoding="utf-8-sig") as f:  # -sig: files saved by PowerShell 5.1 / some editors carry a BOM
            src = f.read()
        fname = f"<exec {agent}: {spath}>"
        linecache.cache[fname] = (len(src), None, src.splitlines(True), fname)  # tracebacks show the script's lines
        ns["__file__"] = spath
    elif req.get("code"):
        src, fname = req["code"], f"<exec {agent}>"
    else:
        raise ValueError("hub_exec needs `code` or `script`")
    ns["args"] = req.get("args") or {}
    code = compile(src, fname, "exec")
    with contextlib.redirect_stdout(buf):
        try:
            _run_guarded(lambda: exec(code, ns), timeout)
        except BaseException:  # SystemExit, timeout etc. do not bring the worker down
            err = traceback.format_exc(limit=6)
    out = {"stdout": buf.getvalue()[-4000:], "result": ns.get("result")}
    if err:
        out["error"] = err
    return out


def _count_hits(tree, origin, direction, limit=64):
    n, o = 0, origin.copy()
    for _ in range(limit):
        hit = tree.ray_cast(o, direction)
        if hit[0] is None:
            break
        n += 1
        o = hit[0] + direction * 1e-4
    return n


_RAY_DIRS = [mathutils.Vector(d).normalized() for d in ((0.577, 0.211, 0.789), (-0.613, 0.702, -0.362), (0.102, -0.894, 0.436))]


def _is_inside(tree, p):
    """Ray parity in 3 directions: inside if most of them cross an odd number of faces."""
    return sum(_count_hits(tree, p, d) % 2 for d in _RAY_DIRS) >= 2


def _samples(bm, tol, max_samples):
    """Vertices + points along edges spaced by `tol` (long edges get more points)."""
    edges = [(e.verts[0].co, e.verts[1].co, (e.verts[1].co - e.verts[0].co).length) for e in bm.edges]
    total = len(bm.verts) + sum(max(0, math.ceil(L / tol) - 1) for _, _, L in edges)
    if total > max_samples:  # widen the spacing before building the list
        tol *= total / max_samples
    pts = [v.co.copy() for v in bm.verts]
    for p0, p1, L in edges:
        k = min(max(0, math.ceil(L / tol) - 1), 256)
        for i in range(1, k + 1):
            pts.append(p0.lerp(p1, i / (k + 1)))
    return pts, tol


def cmd_measure(req):
    """Minimum distance, intersection and penetration between two objects (evaluated mesh, world space).

    delta_a_to_b = point on B - point on A: moving B by -delta makes B touch A.
    With penetration the delta separates nothing: it is None. An open mesh (manifold=false) has
    no 'inside': penetration then comes only from face intersection.
    """
    dg = bpy.context.evaluated_depsgraph_get()
    a, b = bpy.data.objects.get(req["a"]), bpy.data.objects.get(req["b"])
    for name, ob in ((req["a"], a), (req["b"], b)):
        if ob is None or ob.type not in MESH_TYPES:
            raise ValueError(f"object has no mesh or does not exist: {name}")
    bma, bmb = _world_bmesh(a, dg), _world_bmesh(b, dg)
    man = {"a": all(e.is_manifold for e in bma.edges), "b": all(e.is_manifold for e in bmb.edges)}
    ta, tb = BVHTree.FromBMesh(bma), BVHTree.FromBMesh(bmb)
    pairs = ta.overlap(tb)
    tol = float(req.get("tol", 0.02))
    max_samples = int(req.get("max_samples", 60000))
    best = (1e18, None, None)
    inside = {"a_in_b": 0, "b_in_a": 0}
    candidates = {"a": [], "b": []}
    used_tol = tol
    for side, src, tree, other_closed in (("a", bma, tb, man["b"]), ("b", bmb, ta, man["a"])):
        pts, used_tol = _samples(src, tol, max_samples)
        for p in pts:
            hit = tree.find_nearest(p)
            if hit[0] is None:
                continue
            if other_closed and (p - hit[0]).dot(hit[1]) < -1e-5 and len(candidates[side]) < 50000:
                candidates[side].append(p)
            if hit[3] < best[0]:
                pa, pb = (tuple(p), tuple(hit[0])) if side == "a" else (tuple(hit[0]), tuple(p))
                best = (hit[3], pa, pb)
    # normal-sign candidates are wrong near corners: confirm by ray parity
    import random
    rng = random.Random(7)  # the first candidates may all be corner false positives: sample randomly
    for side, tree in (("a", tb), ("b", ta)):
        cand = candidates[side]
        pick = cand if len(cand) <= 600 else rng.sample(cand, 600)
        inside["a_in_b" if side == "a" else "b_in_a"] = sum(1 for p in pick if _is_inside(tree, p))
    bma.free()
    bmb.free()
    d, pa, pb = best
    penetrating = inside["a_in_b"] > 0 or inside["b_in_a"] > 0 or (len(pairs) > 0 and not (man["a"] and man["b"]) and d > 1e-4)
    delta = tuple(round(pb[i] - pa[i], 5) for i in range(3)) if (pa and pb and not penetrating) else None
    return {"min_distance": round(d, 5), "intersecting_faces": len(pairs), "vertices_inside": inside,
            "penetrating": penetrating, "touching": (len(pairs) > 0 or d < 1e-4) and not penetrating,
            "manifold": man, "closest_a": pa, "closest_b": pb, "delta_a_to_b": delta,
            "sample_spacing": round(used_tol, 4)}


TT_CACHE = {}  # (agent, target, params) -> (fingerprint, result): a repeated turntable of an unchanged model


TT_CACHE_MAX = 50


def _tt_fingerprint(obs, color, shading="solid"):
    """Fingerprint of WHAT WORKBENCH DRAWS for self-contained meshes: the evaluated mesh of every target, all its
    modifier settings (render-only too), world matrix, mesh and object-linked materials, plus the scene display
    settings. Returns None (= always render) unless every target is a MESH with no particles, no Geometry Nodes
    and no modifier pointing at another object/collection (curves, text, metaballs, booleans with outside cutters
    always render), and also for TEXTURE color or when a target is instanced.
    shading='rendered' (EEVEE, isolated, with the hub's own fixed light rig and world) also hashes the EEVEE
    settings, and never reuses when a target material uses an image (its pixels are not hashed)."""
    if shading == "solid" and str(color).upper() == "TEXTURE":
        return None
    # Reuse only for SELF-CONTAINED meshes: anything whose render can depend on data outside the target's own
    # mesh/modifiers (curves/text/metaballs with render resolutions, particle hair, Geometry Nodes, modifiers
    # pointing at other objects or collections) always renders. Quality first: never a stale sheet.
    for ob in obs:
        if ob.type != "MESH" or len(ob.particle_systems):
            return None
        for m in ob.modifiers:
            if m.type == "NODES":
                return None
            for p in m.bl_rna.properties:
                if p.type == "POINTER" and p.identifier != "rna_type":
                    v = getattr(m, p.identifier, None)
                    if isinstance(v, (bpy.types.Object, bpy.types.Collection)):
                        return None
    dg = bpy.context.evaluated_depsgraph_get()
    dg.update()
    names = {o.name for o in obs}
    for inst in dg.object_instances:
        if inst.is_instance and inst.parent is not None and inst.parent.original.name in names:
            return None
    sc = bpy.context.scene
    vl = bpy.context.view_layer
    # scene-level settings that change the pixels (whatever the turntable does not force to a fixed value)
    parts = [shading, _rna_props(sc.display.shading), _rna_props(sc.display), _rna_props(sc.view_settings, deep=True),
             _rna_props(sc.display_settings), _rna_props(sc.render, deep=True), _rna_props(vl)]
    if shading in LIT:
        parts.append(_rna_props(sc.eevee, deep=True))
        if shading == "cycles" and hasattr(sc, "cycles"):
            parts.append(_rna_props(sc.cycles, deep=True))
        for ob in obs:
            for s in ob.material_slots:
                mt = s.material
                if mt and mt.node_tree and any(_node_reads_outside(nd) for nd in _all_nodes(mt.node_tree, set())):
                    return None  # image pixels, other objects, object/scene/view-layer attributes: not hashed
    for ob in sorted(obs, key=lambda o: o.name):
        ev = ob.evaluated_get(dg)
        me = ev.to_mesh()
        try:
            if me is None:
                return None
            nv, nl, npoly = len(me.vertices), len(me.loops), len(me.polygons)
            mesh_parts = [nv, nl, npoly, _arr(me.vertices, "co", nv, 3), _arr(me.loops, "vertex_index", nl, 1, "i"),
                          _arr(me.polygons, "material_index", npoly, 1, "i")]
            for at in me.attributes:  # vertex colors / UVs / sharp flags as drawn
                if at.name.startswith(".") and at.name not in (".corner_vert", ".edge_verts"):
                    continue
                field, w = _ATTR_FIELD.get(at.data_type, ("value", 1))
                dt = "i" if at.data_type in ("INT", "INT8", "INT32_2D") else ("b" if at.data_type == "BOOLEAN" else "f")
                mesh_parts += [at.name, at.domain, at.data_type, _arr(at.data, field, len(at.data), w, dt)]
            # mesh materials AND object-linked slots (link='OBJECT' overrides the mesh at draw time)
            mats = (tuple(_material_hash(m) if m else None for m in me.materials),
                    tuple((s.link, _material_hash(s.material) if s.material else None) for s in ev.material_slots))
            # render-only settings (render_levels, show_render, GN "Is Viewport" branches) do not change the
            # viewport-evaluated mesh above, but they change what render.render() draws: hash them too
            src = ob.original if hasattr(ob, "original") else ob
            mods = []
            for m in src.modifiers:
                mods.append((m.name, m.type, _rna_props(m)))
                if m.type == "NODES":
                    mods.append(_gn_inputs(m))
                    if m.node_group:
                        mods.append(_tree_hash(m.node_group, set()))
            vis = tuple(getattr(src, k, None) for k in _VIS_FLAGS)
            md = src.data
            texspace = (getattr(md, "use_auto_texspace", None), tuple(getattr(md, "texspace_location", ()) or ()),
                        tuple(getattr(md, "texspace_size", ()) or ()))
            parts.append((ob.name, _h(*mesh_parts), tuple(round(v, 5) for row in ev.matrix_world for v in row), mats,
                          ev.hide_render, tuple(ev.color), _h(*mods), vis, texspace, src.pass_index, _idprops(src)))
        finally:
            with contextlib.suppress(Exception):
                ev.to_mesh_clear()
    return _h(*parts)


_VIS_FLAGS = ("visible_camera", "visible_shadow", "visible_diffuse", "visible_glossy", "visible_transmission",
              "visible_volume_scatter", "hide_probe_volume", "hide_probe_sphere", "hide_probe_plane", "is_holdout",
              "is_shadow_catcher")


def _node_reads_outside(nd):
    """A shader node whose output depends on data the fingerprint does not hash."""
    if nd.bl_idname == "ShaderNodeAttribute" and getattr(nd, "attribute_type", "GEOMETRY") != "GEOMETRY":
        return True
    for p in nd.bl_rna.properties:
        if p.type == "POINTER" and p.identifier != "rna_type":
            v = getattr(nd, p.identifier, None)
            if isinstance(v, bpy.types.ID) and not isinstance(v, bpy.types.NodeTree):
                return True  # image, object (Texture Coordinate), text, ...
    return False


def _all_nodes(tree, seen):
    if tree is None or tree.name in seen:
        return
    seen.add(tree.name)
    for nd in tree.nodes:
        yield nd
        if getattr(nd, "node_tree", None):
            yield from _all_nodes(nd.node_tree, seen)


def cmd_turntable(req):
    """Review turntable: N frames around the target, from low to high, + contact sheet.

    If nothing the sheet depends on changed since this agent's last identical turntable, it returns
    {"unchanged": true, ...} without rendering (force=true renders anyway)."""
    obs = _targets(req)
    n = int(req.get("views", 12))
    if n < 1 or n > 48:
        raise ValueError("views must be between 1 and 48")
    size = min(max(int(req.get("size", 256)), 64), 1024)
    shading = _shading_arg(req)
    scene_cam = str(req.get("camera") or "").lower() == "scene"
    if req.get("camera") and not scene_cam:
        raise ValueError("camera must be 'scene' (the scene camera) or omitted (orbit)")
    if scene_cam and bpy.context.scene.camera is None:
        raise ValueError("camera='scene' but the scene has no camera")
    timeline = _timeline_frames(req.get("timeline"))
    warning = None
    if timeline:
        n = len(timeline)
        sims = _unbaked_sims()
        if sims and not req.get("force"):
            raise RuntimeError("a timeline jumps frames and would reset unbaked simulations (" + ", ".join(sims[:6])
                               + "): bake them first, or pass force=true to accept that their caches are reset")
        if sims:
            warning = "unbaked simulation caches were reset by the timeline: " + ", ".join(sims[:6])
    elif scene_cam:
        n = 1
    isolate_req = req.get("isolate", True)
    key = (req["_agent"], tuple(sorted(o.name for o in obs)), n, size, shading, str(req.get("color", "MATERIAL")),
           str(req.get("elev")), bpy.context.scene.frame_current)
    # no reuse without isolation (other objects show) or with a caller-chosen tag/out_dir (the file may be overwritten)
    # never with the scene camera or a timeline: the fingerprint covers the targets at this frame, from the orbit
    reusable = isolate_req and not req.get("tag") and not req.get("out_dir") and not scene_cam and not timeline
    fp = None
    if reusable:
        try:
            fp = _tt_fingerprint(obs, req.get("color", "MATERIAL"), shading)
        except Exception:
            fp = None  # cannot prove it unchanged: render
    cached = TT_CACHE.get(key)
    if (fp and not req.get("force") and cached and cached[0] == fp and cached[1].get("sheet")
            and os.path.exists(cached[1]["sheet"]) and os.path.getmtime(cached[1]["sheet"]) == cached[2]):
        return dict(cached[1], unchanged=True)
    result = _turntable_render(req, obs, n, size, shading, timeline, scene_cam)
    if warning:
        result["warning"] = warning
    if fp and result.get("sheet") and os.path.exists(result["sheet"]):
        TT_CACHE[key] = (fp, result, os.path.getmtime(result["sheet"]))
        while len(TT_CACHE) > TT_CACHE_MAX:
            TT_CACHE.pop(next(iter(TT_CACHE)))
    return result


def _timeline_frames(v):
    """timeline: a count (2-48, spread over the scene frame range) or an explicit list of frame numbers."""
    if v in (None, False, 0, "", []):
        return None
    sc = bpy.context.scene
    if isinstance(v, (list, tuple)):
        fr = [int(x) for x in v][:48]
    else:
        k = int(v)
        if k < 2 or k > 48:
            raise ValueError("timeline must be 2-48 frames or a list of frame numbers")
        a, b = sc.frame_start, sc.frame_end
        fr = sorted({round(a + (b - a) * i / (k - 1)) for i in range(k)})
    if not fr:
        raise ValueError("timeline has no frames")
    return fr


def _wire_copies(obs, tag, size, rig, scene_cam=False):
    """World-space copies of the evaluated meshes with a Wireframe modifier about 1.5 px thick at this tile size."""
    dg = bpy.context.evaluated_depsgraph_get()
    lo, hi = _bbox(obs)
    thick = max((hi - lo).length, 1e-3) * 1.08 * 1.5 / size  # orbit: the target fills the tile
    cam = bpy.context.scene.camera if scene_cam else None
    if cam is not None:  # the user's camera decides how big the model looks
        cd = cam.data
        if cd.type == "ORTHO":
            world_px = cd.ortho_scale / size
        else:
            d = max((cam.matrix_world.translation - (lo + hi) / 2).length, 1e-3)
            world_px = 2 * d * math.tan(cd.angle / 2) / size
        thick = 1.5 * world_px
    out = []
    sc = bpy.context.scene
    for i, o in enumerate(obs):
        me = bpy.data.meshes.new_from_object(o.evaluated_get(dg))
        rig.append(("mesh", me))
        me.transform(o.matrix_world)  # world space: the thickness is in metres whatever the object scale
        wo = bpy.data.objects.new(f"hub_wire_{tag}_{i}", me)
        rig.append(("object", wo))
        wo.color = o.color
        for j, slot in enumerate(o.material_slots):  # object-linked materials too
            if j < len(wo.material_slots) and slot.link == "OBJECT":
                wo.material_slots[j].link = "OBJECT"
                wo.material_slots[j].material = slot.material
        md = wo.modifiers.new("wire", "WIREFRAME")
        md.thickness = thick
        md.use_relative_offset = False
        sc.collection.objects.link(wo)
        out.append((wo, me))
    return out


def _drop_wires(wires, rig):
    for wo, me in wires:
        with contextlib.suppress(Exception):
            bpy.data.objects.remove(wo)
        with contextlib.suppress(Exception):
            bpy.data.meshes.remove(me)
        rig[:] = [x for x in rig if x[1] is not wo and x[1] is not me]


_ANIM_COLLS = ("objects", "meshes", "materials", "shape_keys", "cameras", "lights", "worlds", "node_groups",
               "armatures", "curves", "hair_curves", "scenes", "grease_pencils", "grease_pencils_v3", "lattices",
               "particles", "textures", "speakers", "metaballs", "volumes", "pointclouds", "linestyles",
               "cache_files", "masks", "movieclips")
_CUSTOM_KEY = re.compile(r"""^(.*)\[(["'])(.*)\2\]$""")  # ...["key"]: a custom property


def _fcurves(action):
    fcs = getattr(action, "fcurves", None)
    if fcs is None:  # 5.x layered actions
        fcs = []
        with contextlib.suppress(Exception):
            fcs = [fc for layer in action.layers for strip in layer.strips for cb in strip.channelbags for fc in cb.fcurves]
    return list(fcs or [])


def _unbaked_sims():
    """Simulation caches a frame jump would reset (and returning to the frame would not rebuild)."""
    sc = bpy.context.scene
    out = []
    rbw = sc.rigidbody_world
    if rbw is not None and rbw.enabled and rbw.collection and len(rbw.collection.objects) \
            and not (rbw.point_cache and rbw.point_cache.is_baked):
        out.append("rigid body world")
    for o in sc.objects:
        for m in o.modifiers:
            pc = getattr(m, "point_cache", None)
            if pc is not None and not pc.is_baked:
                out.append(f"{o.name}: {m.type.lower()}")
            elif m.type == "FLUID" and getattr(m, "fluid_type", "") == "DOMAIN":
                out.append(f"{o.name}: fluid domain")
            elif m.type == "NODES" and m.node_group and any(
                    n.bl_idname == "GeometryNodeSimulationOutput" for n in _all_nodes(m.node_group, set())):
                out.append(f"{o.name}: geometry nodes simulation")  # bake state is not exposed reliably: assume unbaked
        for ps in getattr(o, "particle_systems", []):
            if ps.settings.type == "EMITTER" or getattr(ps.settings, "use_hair_dynamics", False) or ps.use_hair_dynamics:
                if not ps.point_cache.is_baked:
                    out.append(f"{o.name}: particles {ps.name}")
    return out


def _animated_values():
    """Current values of every animated property, so a temporary frame change does not lose unkeyed edits."""
    out, seen = [], set()
    for cn in _ANIM_COLLS:
        for idb in getattr(bpy.data, cn, []) or []:
            ad = getattr(idb, "animation_data", None)
            if not ad:
                continue
            actions = [ad.action] if ad.action else []
            with contextlib.suppress(Exception):  # NLA-only animation too
                actions += [st.action for tr in ad.nla_tracks for st in tr.strips if st.action]
            for act in actions:
                for fc in _fcurves(act):
                    k = (idb.as_pointer(), fc.data_path, fc.array_index)
                    if k in seen:
                        continue
                    seen.add(k)
                    with contextlib.suppress(Exception):
                        prop = idb.path_resolve(fc.data_path)
                        if hasattr(prop, "__len__") and not isinstance(prop, str):
                            out.append((idb, fc.data_path, fc.array_index, prop[fc.array_index]))
                        else:
                            out.append((idb, fc.data_path, -1, prop))
    return out


def _restore_animated(vals):
    for idb, path, idx, v in vals:
        with contextlib.suppress(Exception):
            m = _CUSTOM_KEY.match(path)
            if m:  # custom property: ["key"] or pose.bones["Arm"]["ik"]
                owner = idb.path_resolve(m.group(1)) if m.group(1) else idb
                key = m.group(3).replace('\\"', '"').replace("\\'", "'")
                if idx < 0:
                    owner[key] = v
                else:
                    owner[key][idx] = v
            elif idx < 0:
                if "." in path:
                    owner_path, attr = path.rsplit(".", 1)
                    setattr(idb.path_resolve(owner_path), attr, v)
                else:
                    setattr(idb, path, v)
            else:
                idb.path_resolve(path)[idx] = v
    if vals:
        with contextlib.suppress(Exception):
            bpy.context.view_layer.update()


def _turntable_render(req, obs, n, size, shading="solid", timeline=None, scene_cam=False):
    out_dir = req.get("out_dir") or os.path.join(ARGS["state"], "renders", ARGS["name"])
    os.makedirs(out_dir, exist_ok=True)
    tag = f"{req['_agent']}_{re.sub(r'[^A-Za-z0-9_-]', '', str(req.get('tag') or int(time.time() * 1000)))}"
    isolate = req.get("isolate", True)
    sc = bpy.context.scene
    r = sc.render
    shd = sc.display.shading
    vs = sc.view_settings
    world = sc.world
    lit = shading in LIT
    # scene camera: tiles keep the render aspect (before resolution and pixel aspect are forced below)
    aspect = (r.resolution_y * r.pixel_aspect_y) / max(r.resolution_x * r.pixel_aspect_x, 1e-6)
    if scene_cam:  # longest side = size, the other keeps the camera's aspect (same framing as the user's camera)
        tile_w, tile_h = (size, max(16, round(size * aspect))) if aspect <= 1 else (max(16, round(size / aspect)), size)
    else:
        tile_w, tile_h = size, size
    frame0, subframe0 = sc.frame_current, sc.frame_subframe
    anim_vals = _animated_values() if timeline else []  # frame_set below re-evaluates fcurves: keep unkeyed edits
    wires = []
    saved = [(r, k, getattr(r, k)) for k in ("engine", "resolution_x", "resolution_y", "resolution_percentage",
                                             "filepath", "film_transparent")]
    # look first: restored in reverse order, so the look is set back AFTER its view transform
    saved += [(r.image_settings, "file_format", r.image_settings.file_format), (sc, "camera", sc.camera),
              (vs, "look", vs.look), (vs, "view_transform", vs.view_transform)]
    # anything that would make the review differ from the model itself: forced off for the turntable, restored after
    neutral = [(r, "use_compositing", False), (r, "use_sequencer", False), (r, "use_border", False),
               (r, "pixel_aspect_x", 1.0), (r, "pixel_aspect_y", 1.0), (r, "use_multiview", False),
               (r, "use_freestyle", False), (r, "use_motion_blur", False), (r, "use_simplify", False),
               (vs, "exposure", 0.0), (vs, "gamma", 1.0), (vs, "use_curve_mapping", False),
               (sc, "background_set", None)]
    vl = bpy.context.view_layer
    if hasattr(vl, "material_override"):
        neutral.append((vl, "material_override", None))
    if hasattr(vl, "samples"):
        neutral.append((vl, "samples", 0))
    for obj, attr, _v in neutral:
        if hasattr(obj, attr):
            saved.append((obj, attr, getattr(obj, attr)))
    saved += [(shd, k, getattr(shd, k)) for k in ("light", "color_type", "show_cavity", "show_xray", "xray_alpha")]
    cy = getattr(sc, "cycles", None)
    if shading == "cycles" and cy is not None:
        saved += [(cy, k, getattr(cy, k)) for k in ("samples", "use_denoising", "use_adaptive_sampling") if hasattr(cy, k)]
    if world:
        saved.append((world, "color", tuple(world.color)))
    saved.append((sc, "world", world))
    ee = getattr(sc, "eevee", None)
    if ee is not None and hasattr(ee, "taa_render_samples"):
        saved.append((ee, "taa_render_samples", ee.taa_render_samples))
    frames, sheet, cam, cam_data = [], None, None, None
    rig = []  # temporary lights/world/wire copies, removed in finally
    try:
        for obj, attr, v in neutral:
            with contextlib.suppress(Exception):
                setattr(obj, attr, v)
        if isolate:
            keep = {o.name for o in obs}
            for o in sc.objects:
                if o.name not in keep and not o.hide_render:
                    saved.append((o, "hide_render", False))
                    o.hide_render = True
        for o in obs:  # hidden target (object, collection or view layer exclusion): show it during the turntable
            if o.hide_render:
                saved.append((o, "hide_render", True))
                o.hide_render = False
            for c in o.users_collection:
                if c.hide_render:
                    saved.append((c, "hide_render", True))
                    c.hide_render = False

        def walk(lc):
            yield lc
            for ch in lc.children:
                yield from walk(ch)
        names = {o.name for o in obs}
        for lc in walk(bpy.context.view_layer.layer_collection):
            if lc.exclude and any(o.name in names for o in lc.collection.all_objects):
                saved.append((lc, "exclude", True))
                lc.exclude = False
        if scene_cam:
            cam = None  # the user's camera, untouched
        else:
            cam_data = bpy.data.cameras.new(f"hub_cam_{tag}")
            cam = bpy.data.objects.new(f"hub_cam_{tag}", cam_data)
            sc.collection.objects.link(cam)
        if lit:
            if shading == "cycles":
                try:
                    r.engine = "CYCLES"
                except TypeError as e:
                    raise RuntimeError("the Cycles add-on is disabled in this Blender: enable it, or use "
                                       "shading='rendered' (EEVEE) or 'solid'") from e
                if cy is not None:
                    cy.samples = 16  # + denoising: clean at <= 1024 px per view
                    with contextlib.suppress(Exception):
                        cy.use_denoising = True
                    with contextlib.suppress(Exception):
                        cy.use_adaptive_sampling = True
            else:
                _set_eevee(r)
                if ee is not None and hasattr(ee, "taa_render_samples"):
                    ee.taa_render_samples = 16  # at <= 1024 px per view more samples change nothing visible
            no_light = sc.world is None and not any(
                o.type == "LIGHT" and not o.hide_render and not any(c.hide_render for c in o.users_collection)
                for o in bpy.context.view_layer.objects)  # lights the render can actually see
            if isolate or no_light:  # same light for every review: the hub's rig, not whatever the scene has
                tw = bpy.data.worlds.new(f"hub_world_{tag}")
                rig.append(("world", tw))
                tw.color = (0.25, 0.26, 0.28)
                if tw.node_tree is None and hasattr(tw, "use_nodes"):
                    with contextlib.suppress(Exception):
                        tw.use_nodes = True
                bg = tw.node_tree.nodes.get("Background") if tw.node_tree else None
                if bg is not None:
                    bg.inputs[0].default_value = (0.25, 0.26, 0.28, 1.0)
                    bg.inputs[1].default_value = 1.0
                sc.world = tw
                for nm, energy, rot in (("key", 4.0, (math.radians(50), 0, math.radians(35))),
                                        ("fill", 1.0, (math.radians(65), 0, math.radians(215)))):
                    ld = bpy.data.lights.new(f"hub_{nm}_{tag}", "SUN")
                    rig.append(("light", ld))
                    ld.energy = energy
                    lo_ = bpy.data.objects.new(f"hub_{nm}_{tag}", ld)
                    rig.append(("object", lo_))
                    lo_.rotation_euler = rot
                    sc.collection.objects.link(lo_)
        else:
            _set_workbench(r)
            shd.light = "STUDIO"
            want = req.get("color", "MATERIAL")
            try:
                shd.color_type = want
            except TypeError:  # 5.3: VERTEX; 4.x: ATTRIBUTE
                alt = {"ATTRIBUTE": "VERTEX", "VERTEX": "ATTRIBUTE"}.get(want)
                shd.color_type = alt if alt else "MATERIAL"
            shd.show_cavity = True
            if shading == "xray":
                shd.show_xray = True
                shd.xray_alpha = 0.35
            if shading == "wireframe":  # Workbench ignores display_type WIRE: wire copies of the evaluated meshes
                for o in obs:
                    if not o.hide_render:
                        saved.append((o, "hide_render", False))
                        o.hide_render = True
                wires = _wire_copies(obs, tag, size, rig, scene_cam)
        r.resolution_x, r.resolution_y = tile_w, tile_h
        r.resolution_percentage = 100
        r.image_settings.file_format = "PNG"
        r.film_transparent = False
        with contextlib.suppress(TypeError):
            vs.view_transform = "Standard"
        with contextlib.suppress(TypeError):
            vs.look = "None"
        if not lit:
            if world is None:  # Workbench takes the background from the world: without one the sheet is blank
                tw = bpy.data.worlds.new(f"hub_world_{tag}")
                rig.append(("world", tw))
                sc.world = tw
            sc.world.color = (0.55, 0.57, 0.6)
        if cam is not None:
            sc.camera = cam
            if timeline:  # frame the whole motion, not only the current pose
                lo, hi = None, None
                for f in timeline:
                    sc.frame_set(f)
                    a_, b_ = _bbox(obs)
                    lo = a_ if lo is None else mathutils.Vector(map(min, lo, a_))
                    hi = b_ if hi is None else mathutils.Vector(map(max, hi, b_))
            else:
                lo, hi = _bbox(obs)
            center = (lo + hi) / 2
            radius = max((hi - lo).length / 2, 1e-3)
            cam_data.lens_unit = "FOV"
            dist = radius / math.sin(cam_data.angle / 2) * 1.08
            cam_data.clip_start = max(dist - radius * 2, 1e-3) * 0.5
            cam_data.clip_end = dist + radius * 4
        el_lo, el_hi = req.get("elev", (-15.0, 60.0))
        with contextlib.suppress(Exception):  # a Material Preview / Rendered viewport would start its own GPU render
            LIVE["skip_turntable"] = any(sp.type == "VIEW_3D" and sp.shading.type in ("MATERIAL", "RENDERED")
                                         for w_ in bpy.context.window_manager.windows for a_ in w_.screen.areas
                                         for sp in a_.spaces)
        for i in range(n):
            LIVE["progress"] = f"{'timeline' if timeline else 'turntable'} {i + 1}/{n}"
            if timeline:
                sc.frame_set(timeline[i])
                if shading == "wireframe":  # the copies are meshes of one pose: rebuild them for this frame
                    _drop_wires(wires, rig)
                    wires = _wire_copies(obs, tag, size, rig, scene_cam)
            if cam is not None:
                if timeline:  # one fixed three-quarter view for the whole strip
                    az, el = math.radians(30), math.radians(20)
                else:
                    az = math.radians(i * 360.0 / n + 30)
                    el = math.radians(el_lo + (el_hi - el_lo) * (i / max(n - 1, 1)))
                pos = center + mathutils.Vector((math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el))) * dist
                cam.location = pos
                cam.rotation_euler = (center - pos).to_track_quat("-Z", "Y").to_euler()
            path = os.path.join(out_dir, f"{tag}_{i:02d}.png")
            r.filepath = path
            try:
                bpy.ops.render.render(write_still=True)
            except Exception as e:
                if shading == "rendered":
                    raise RuntimeError(f"shading='rendered' failed: {e}. EEVEE needs a Blender that can use the GPU "
                                       "(a window, or -b with a GPU); use shading='cycles' (CPU) or 'solid'") from e
                if shading == "cycles":
                    raise RuntimeError(f"shading='cycles' failed: {e}") from e
                raise
            frames.append(path)
            LIVE["rendering"] = False  # render.render returned: safe to draw
            _live_refresh(force=True)  # (_live_safe skips it while a viewport renders on its own)
        if np is not None and req.get("sheet", True):
            cols = min(int(req.get("cols", 4)), n)
            rows = math.ceil(n / cols)
            grid = np.zeros((rows * tile_h, cols * tile_w, 4), dtype=np.float32)
            for i, p in enumerate(frames):
                img = bpy.data.images.load(p, check_existing=False)
                img.colorspace_settings.is_data = True
                px = np.empty(img.size[0] * img.size[1] * 4, dtype=np.float32)
                img.pixels.foreach_get(px)
                px = px.reshape(img.size[1], img.size[0], 4)
                rr, cc = divmod(i, cols)
                y0 = (rows - 1 - rr) * tile_h
                ph, pw = min(tile_h, px.shape[0]), min(tile_w, px.shape[1])
                grid[y0:y0 + ph, cc * tile_w:cc * tile_w + pw] = px[:ph, :pw]
                bpy.data.images.remove(img)
            sheet_img = bpy.data.images.new(f"hub_sheet_{tag}", cols * tile_w, rows * tile_h, alpha=True)
            sheet_img.colorspace_settings.is_data = True
            sheet_img.pixels.foreach_set(grid.ravel())
            sheet = os.path.join(out_dir, f"{tag}_sheet.png")
            sheet_img.filepath_raw = sheet
            sheet_img.file_format = "PNG"
            sheet_img.save()
            bpy.data.images.remove(sheet_img)
    finally:
        if cam is not None:
            with contextlib.suppress(Exception):
                bpy.data.objects.remove(cam)
        if cam_data is not None:
            with contextlib.suppress(Exception):
                bpy.data.cameras.remove(cam_data)
        for obj, attr, val in reversed(saved):  # field by field, in reverse order
            with contextlib.suppress(Exception):
                setattr(obj, attr, val)
        LIVE["progress"] = None
        if timeline or sc.frame_current != frame0 or sc.frame_subframe != subframe0:
            with contextlib.suppress(Exception):
                sc.frame_set(frame0, subframe=subframe0)
            _restore_animated(anim_vals)
        _drop_wires(wires, rig)
        for want in ("object", "light", "mesh", "world"):  # objects before the data they use
            for kind, idb in rig:
                if kind == want:
                    with contextlib.suppress(Exception):
                        {"object": bpy.data.objects, "light": bpy.data.lights, "world": bpy.data.worlds,
                         "mesh": bpy.data.meshes}[kind].remove(idb)
    out = {"frames": frames, "sheet": sheet, "views": n, "size": size, "shading": shading}
    if timeline:
        out["timeline"] = timeline
    if scene_cam:
        out["camera"] = "scene"
    return out


def _image_to_png(src, path, size):
    """'Render Result', an image datablock name, or an image file path -> PNG scaled to `size` px."""
    img, tmp = bpy.data.images.get(src), None
    try:
        if img is None:
            if not os.path.isfile(src):
                raise ValueError(f"no image named {src!r} in the file and no such file")
            img = tmp = bpy.data.images.load(src, check_existing=False)
        if img.type == "RENDER_RESULT":
            img.save_render(filepath=path)  # with the scene's color management, like the render window
            img2 = tmp = bpy.data.images.load(path, check_existing=False)
        else:
            img2 = img if tmp is not None else img.copy()
            tmp = img2
        w, h = img2.size
        if not w or not h:
            raise ValueError(f"image {src!r} has no pixels (render first?)")
        sc_ = size / max(w, h)
        if sc_ < 1.0:
            w, h = max(1, round(w * sc_)), max(1, round(h * sc_))
            img2.scale(w, h)
        img2.filepath_raw = path
        img2.file_format = "PNG"
        img2.save()
        return {"path": path, "size": [w, h], "area": "image", "image": src}
    finally:
        if tmp is not None:
            with contextlib.suppress(Exception):
                bpy.data.images.remove(tmp)


# ---------------------------------------------------------------- scene facts (text, no image)
SCENE_FIELDS = {"location", "size", "ground", "rotation", "scale", "parent", "children", "hidden", "details",
                "materials", "modifiers", "animation", "topology", "weights", "settings"}


def _fmt(v, nd=2):
    return "(" + ", ".join(f"{x:.{nd}f}" for x in v) + ")"


def _world_bbox(ob):
    pts = [ob.matrix_world @ mathutils.Vector(c) for c in ob.bound_box]
    return (mathutils.Vector(map(min, *pts)), mathutils.Vector(map(max, *pts)))


def _topology(ob):
    """Base mesh (before modifiers); in Edit Mode the live edit mesh."""
    me = ob.data
    edit = ob.mode == "EDIT"
    bm = bmesh.from_edit_mesh(me) if edit else bmesh.new()
    try:
        if not edit:
            bm.from_mesh(me)
        q = t = ng = 0
        for f in bm.faces:
            k = len(f.verts)
            q, t, ng = q + (k == 4), t + (k == 3), ng + (k > 4)
        nonman = sum(1 for e in bm.edges if not e.is_manifold and not e.is_boundary)
        bound = sum(1 for e in bm.edges if e.is_boundary)
        loose = sum(1 for v in bm.verts if not v.link_edges)
        poles = sum(1 for v in bm.verts if len(v.link_edges) >= 6)
        return f"base mesh: quads {q} tris {t} ngons {ng} non-manifold {nonman} boundary {bound} loose {loose} poles {poles}"
    finally:
        if not edit:
            bm.free()


def _weights(ob):
    arm = ob.find_armature()
    if arm is None:
        return "no armature"
    deform = {b.name for b in arm.data.bones if b.use_deform}
    groups = {g.index: g.name for g in ob.vertex_groups if g.name in deform}
    free = sum(1 for v in ob.data.vertices if not any(g.group in groups and g.weight > 0 for g in v.groups))
    missing = sorted(deform - {g.name for g in ob.vertex_groups})
    return f"verts no bone moves {free}; deform bones without group {len(missing)}" + (
        f" ({', '.join(missing[:6])}{'...' if len(missing) > 6 else ''})" if missing else "")


def _obj_line(ob, fields):
    parts = [f"{ob.name} [{ob.type.lower()}]"]
    lo, hi = _world_bbox(ob)
    for f in fields:
        if f == "location":
            parts.append("loc " + _fmt(ob.matrix_world.translation))
        elif f == "size":
            parts.append("size " + _fmt(hi - lo))
        elif f == "ground":
            z = lo.z
            parts.append("on ground" if abs(z) < 1e-3 else (f"floating {z:.3f}" if z > 0 else f"below {-z:.3f}"))
        elif f == "rotation":
            parts.append("rot " + _fmt([math.degrees(a) for a in ob.matrix_world.to_euler()], 1))
        elif f == "scale":
            parts.append("scale " + _fmt(ob.matrix_world.to_scale(), 3))
        elif f == "parent" and ob.parent:
            parts.append("parent " + ob.parent.name)
        elif f == "children" and ob.children:
            parts.append(f"children {len(ob.children)}")
        elif f == "hidden":
            if ob.name not in bpy.context.view_layer.objects:
                parts.append("excluded")
            elif ob.hide_get() or ob.hide_render:
                parts.append("hidden" + (" (render)" if ob.hide_render else ""))
        elif f == "details":
            if ob.type == "MESH":
                parts.append(f"faces {len(ob.data.polygons)} verts {len(ob.data.vertices)}")
            elif ob.type == "ARMATURE":
                parts.append(f"bones {len(ob.data.bones)}")
            elif ob.type == "LIGHT":
                parts.append(f"{ob.data.type.lower()} light {ob.data.energy:g} W")
        elif f == "materials" and getattr(ob, "material_slots", None):
            parts.append("mats " + ",".join(s.material.name if s.material else "-" for s in ob.material_slots))
        elif f == "modifiers" and ob.modifiers:
            parts.append("mods " + ",".join(m.type.lower() for m in ob.modifiers))
        elif f == "animation":
            ad = ob.animation_data
            if ad and ad.action:
                fr = ad.action.frame_range
                parts.append(f"anim {ad.action.name} {int(fr[0])}-{int(fr[1])}")
        elif f == "topology" and ob.type == "MESH":
            parts.append(_topology(ob))
        elif f == "weights" and ob.type == "MESH":
            parts.append(_weights(ob))
    return " | ".join(parts)


def cmd_scene(req):
    """Scene facts as text: header + one line per object. Top-level objects by default; root=<name> lists its
    hierarchy; query=<text> every object whose name contains it."""
    sc = bpy.context.scene
    vl = bpy.context.view_layer
    fields = [f for f in (req.get("fields") or ["location", "size", "children", "hidden"]) if f in SCENE_FIELDS]
    bad = [f for f in (req.get("fields") or []) if f not in SCENE_FIELDS]
    if bad:
        raise ValueError(f"unknown fields {bad}; use: {', '.join(sorted(SCENE_FIELDS))}")
    limit = min(max(int(req.get("limit") or 20), 1), 500)
    counts = {}
    for o in sc.objects:
        counts[o.type.lower()] = counts.get(o.type.lower(), 0) + 1
    act = vl.objects.active
    sel = [o.name for o in vl.objects if o.select_get()]
    lines = [", ".join(f"{v} {k}" for k, v in sorted(counts.items())) +
             f" | active {act.name if act else '-'} | selected {', '.join(sel[:5]) or '-'}"
             f"{'...' if len(sel) > 5 else ''} | mode {bpy.context.mode.lower()}"]
    if "settings" in fields:
        env = None
        if sc.world and sc.world.node_tree:
            env = next((n.image.name for n in sc.world.node_tree.nodes if n.bl_idname == "ShaderNodeTexEnvironment"
                        and n.image), None)
        r = sc.render
        lines.append(f"file {os.path.basename(bpy.data.filepath) or '(unsaved)'} | engine {r.engine} | frames "
                     f"{sc.frame_start}-{sc.frame_end} @{sc.frame_current} | {r.resolution_x}x{r.resolution_y} "
                     f"{r.resolution_percentage}% | camera {sc.camera.name if sc.camera else '-'} | hdri {env or '-'} "
                     f"| unit scale {sc.unit_settings.scale_length:g}")
    if req.get("root"):
        root = bpy.data.objects.get(req["root"])
        if root is None:
            raise ValueError(f"no object named {req['root']!r}")
        obs = [root] + list(root.children_recursive)
    elif req.get("query"):
        q = str(req["query"]).lower()
        obs = [o for o in sc.objects if q in o.name.lower()]
    else:
        obs = [o for o in sc.objects if o.parent is None]
    obs.sort(key=lambda o: o.name)
    obj_fields = [f for f in fields if f != "settings"]
    lines.append(f"showing {min(len(obs), limit)} of {len(obs)}")
    for o in obs[:limit]:
        lines.append(_obj_line(o, obj_fields))
    return {"text": "\n".join(lines)}


# ---------------------------------------------------------------- asset libraries (search + import)
# Network work (API calls, downloads, unzip) runs in the connection thread BEFORE the job is queued
# (_assets_net): Blender's main thread, the window and the other agents never wait on the internet.
# Only the bpy part (preview sheet, import, material) runs as the queued command (cmd_assets).
ASSET_UA = {"User-Agent": "blender-hub"}
ASSET_SOURCES = ("polyhaven", "sketchfab", "polypizza")
PH_TYPES = {"hdris": 0, "textures": 1, "models": 2}
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_AUTH_HEADERS = ("Authorization", "X-auth-token")
ZIP_MAX_BYTES = 2 * 1024 ** 3


def _check_id(v, what="asset_id"):
    v = str(v or "")
    if not _SAFE_ID.match(v):
        raise ValueError(f"{what} must be letters, digits, _ or - (got {v[:40]!r})")
    return v


def _opener():
    import urllib.request
    import urllib.parse

    class _Redirect(urllib.request.HTTPRedirectHandler):
        """https only; an API key never follows a redirect to another host."""
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if urllib.parse.urlparse(newurl).scheme != "https":
                raise ValueError(f"refusing a non-https redirect: {newurl[:80]}")
            new = super().redirect_request(req, fp, code, msg, headers, newurl)
            if new is not None and urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc:
                for h in _AUTH_HEADERS:
                    new.remove_header(h)
            return new
    return urllib.request.build_opener(_Redirect)


def _https(url):
    import urllib.parse
    if urllib.parse.urlparse(str(url)).scheme != "https":
        raise ValueError(f"refusing a non-https url: {str(url)[:80]}")
    return str(url)


def _http_json(url, headers=None, timeout=30):
    import urllib.request
    rq = urllib.request.Request(_https(url), headers={**ASSET_UA, **(headers or {})})
    with _opener().open(rq, timeout=timeout) as f:
        return json.loads(f.read().decode("utf-8"))


_PATH_LOCKS = {}


def _path_lock(path):
    """Downloads and unzips run in connection threads: two agents fetching the same asset take turns."""
    with LOCK:
        return _PATH_LOCKS.setdefault(os.path.normcase(os.path.abspath(path)), threading.Lock())


def _http_file(url, path, headers=None, timeout=120):
    import urllib.request
    with _path_lock(path):
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path  # cached (or just fetched by the other thread)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rq = urllib.request.Request(_https(url), headers={**ASSET_UA, **(headers or {})})
        tmp = path + ".part"
        with _opener().open(rq, timeout=timeout) as f, open(tmp, "wb") as out:
            shutil.copyfileobj(f, out)
        os.replace(tmp, path)
        return path


def _asset_dir(*parts):
    return os.path.join(ARGS["state"], "assets", *[_check_id(p, "path part") if i else p for i, p in enumerate(parts)])


def _safe_join(base, rel):
    """A relative path from an API response or a zip, kept inside `base`."""
    rel = str(rel).replace("\\", "/")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or any(p in ("..", "") for p in rel.split("/")):
        raise ValueError(f"unsafe path from the download: {rel[:80]!r}")
    p = os.path.realpath(os.path.join(base, *rel.split("/")))
    root_ = os.path.realpath(base)
    if os.path.commonpath([root_, p]) != root_:
        raise ValueError(f"unsafe path from the download: {rel[:80]!r}")
    return p


def _extract_zip(z, ex):
    """Extract into ex.part, check every member and the total size first, then rename: never a half model."""
    with _path_lock(ex):
        return _extract_zip_locked(z, ex)


def _extract_zip_locked(z, ex):
    import zipfile
    if os.path.isdir(ex):
        return ex
    part = ex + ".part"
    shutil.rmtree(part, ignore_errors=True)
    with zipfile.ZipFile(z) as zf:
        total = 0
        for info in zf.infolist():
            if not info.is_dir():
                _safe_join(part, info.filename)
            total += info.file_size
        if total > ZIP_MAX_BYTES:
            raise ValueError(f"download unpacks to {total // 1024 ** 2} MB (limit {ZIP_MAX_BYTES // 1024 ** 2} MB)")
        zf.extractall(part)
    os.replace(part, ex)
    return ex


def _key(env):
    k = os.environ.get(env)
    if not k:
        raise PermissionError(f"this source needs an API key: set {env} in the hub's environment (MCP server env)")
    return k


def _limit(req):
    return min(max(int(req.get("limit") or 10), 1), 24)


def _ph_search(req):
    t = str(req.get("type") or "all").lower()
    if t != "all" and t not in PH_TYPES:
        raise ValueError("type must be hdris, textures, models or all")
    data = _http_json("https://api.polyhaven.com/assets" + ("" if t == "all" else f"?t={t}"))
    words = [w for w in re.split(r"\W+", str(req.get("query") or "").lower()) if w]
    cat = str(req.get("category") or "").lower()
    min_m = float(req.get("min_size_m") or 0)
    hits = []
    for aid, a in data.items():
        cats = [c.lower() for c in a.get("categories", [])]
        tags = [x.lower() for x in a.get("tags", [])]
        name = a.get("name", aid).lower()
        if cat and not any(cat in c for c in cats):
            continue
        dims = a.get("dimensions")
        size_m = round(max(dims) / 1000.0, 2) if dims else None
        if min_m and (size_m is None or size_m < min_m):
            continue
        score = sum(3 * (w in name) + 2 * any(w in x for x in tags) + any(w in c for c in cats) for w in words)
        if words and score == 0:
            continue
        hits.append((score, a.get("download_count", 0), aid, a, size_m))
    hits.sort(key=lambda h: (-h[0], -h[1]))
    inv = {v: k for k, v in PH_TYPES.items()}
    out = []
    for _s, _d, aid, a, size_m in hits[:_limit(req)]:
        row = {"id": aid, "name": a.get("name"), "type": inv.get(a.get("type")), "categories": a.get("categories", [])[:3]}
        if size_m:
            row["size_m"] = size_m
        if a.get("polycount"):
            row["polys"] = a["polycount"]
        row["thumb"] = f"https://cdn.polyhaven.com/asset_img/thumbs/{aid}.png?width=256&height=256"
        out.append(row)
    return out


def _sf_search(req):
    import urllib.parse
    lic = {"CC0": "cc0", "CC-BY": "by"}.get(str(req.get("licence") or "").upper())
    if req.get("licence") and not lic:
        raise ValueError("licence must be CC0 or CC-BY")
    q = urllib.parse.urlencode({k: v for k, v in {"type": "models", "q": req.get("query") or "", "downloadable": "true",
                                                    "count": _limit(req), "categories": req.get("category"),
                                                    "license": lic,
                                                    "animated": "true" if req.get("animated") else None}.items() if v})
    data = _http_json("https://api.sketchfab.com/v3/search?" + q)
    out = []
    for m in data.get("results", [])[:_limit(req)]:
        imgs = sorted((m.get("thumbnails") or {}).get("images", []), key=lambda i: abs((i.get("width") or 0) - 256))
        lic_ = m.get("license")
        out.append({"id": m.get("uid"), "name": m.get("name"), "author": (m.get("user") or {}).get("username"),
                    "license": lic_.get("label") if isinstance(lic_, dict) else lic_, "faces": m.get("faceCount"),
                    "animated": bool(m.get("animationCount")), "thumb": imgs[0]["url"] if imgs else None})
    return out


def _pp_search(req):
    import urllib.parse
    key = _key("POLYPIZZA_API_KEY")
    params = {k: v for k, v in {"Limit": _limit(req), "Category": req.get("category"),
                                "License": {"CC0": 1, "CC-BY": 0}.get(str(req.get("licence") or "").upper()),
                                "Animated": "true" if req.get("animated") else None}.items() if v is not None}
    data = _http_json("https://api.poly.pizza/v1.1/search/" + urllib.parse.quote(str(req.get("query") or ""))
                      + "?" + urllib.parse.urlencode(params), {"x-auth-token": key})
    out = []
    for m in (data.get("results") or [])[:_limit(req)]:
        out.append({"id": m.get("ID"), "name": m.get("Title"), "author": (m.get("Creator") or {}).get("Username"),
                    "license": m.get("Licence"), "tris": m.get("Tri Count"), "animated": bool(m.get("Animated")),
                    "thumb": m.get("Thumbnail")})
    return out


def _fetch_thumbs(src, rows, n):
    """Thumbnail files of the first n results, cached per asset (reused by later searches)."""
    paths = []
    for row in rows[:min(max(n, 0), 6)]:
        url, rid = row.get("thumb"), str(row.get("id") or "")
        if not url or not _SAFE_ID.match(rid):
            continue
        ext = os.path.splitext(url.split("?")[0])[1].lower()
        p = os.path.join(_asset_dir("thumbs", src), rid + (ext if ext in (".png", ".jpg", ".jpeg", ".webp") else ".png"))
        with contextlib.suppress(Exception):
            paths.append(_http_file(url, p, timeout=30))
    return paths


def _preview_sheet(paths, tag):
    """One image with the thumbnails, each letterboxed into a 256 px tile (no distortion)."""
    if np is None or not paths:
        return None
    tiles = []
    for p in paths:
        img = None
        try:
            img = bpy.data.images.load(p, check_existing=False)
            img.colorspace_settings.is_data = True
            w, h = img.size
            if not w or not h:
                continue
            f = 256.0 / max(w, h)
            nw, nh = max(1, round(w * f)), max(1, round(h * f))
            img.scale(nw, nh)
            px = np.empty(nw * nh * 4, dtype=np.float32)
            img.pixels.foreach_get(px)
            tile = np.zeros((256, 256, 4), dtype=np.float32)
            tile[..., 3] = 1.0
            y0, x0 = (256 - nh) // 2, (256 - nw) // 2
            tile[y0:y0 + nh, x0:x0 + nw] = px.reshape(nh, nw, 4)
            tiles.append(tile)
        except Exception:
            continue
        finally:
            if img is not None:
                with contextlib.suppress(Exception):
                    bpy.data.images.remove(img)
    if not tiles:
        return None
    cols = min(3, len(tiles))
    rws = math.ceil(len(tiles) / cols)
    grid = np.zeros((rws * 256, cols * 256, 4), dtype=np.float32)
    for i, t in enumerate(tiles):
        r_, c_ = divmod(i, cols)
        grid[(rws - 1 - r_) * 256:(rws - r_) * 256, c_ * 256:(c_ + 1) * 256] = t
    out_dir = os.path.join(ARGS["state"], "renders", ARGS["name"])
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"assets_{tag}.png")
    im = bpy.data.images.new(f"hub_prev_{tag}", cols * 256, rws * 256, alpha=True)
    try:
        im.colorspace_settings.is_data = True
        im.pixels.foreach_set(grid.ravel())
        im.filepath_raw = path
        im.file_format = "PNG"
        im.save()
    finally:
        bpy.data.images.remove(im)
    return path


def _ph_net(req, exclusive):
    aid = _check_id(req.get("asset_id"))
    res = _check_id(req.get("resolution") or "2k", "resolution")
    t = str(req.get("type") or "").lower()
    if not t:  # not given: ask Poly Haven what it is
        code = _http_json(f"https://api.polyhaven.com/info/{aid}").get("type")
        t = {v: k for k, v in PH_TYPES.items()}.get(code, "")
    if t not in PH_TYPES:
        raise ValueError("polyhaven type must be hdris, textures or models")
    if t == "hdris" and not exclusive:
        raise PermissionError("an HDRI replaces the world lighting for everyone: needs an exclusive lease")
    files = _http_json(f"https://api.polyhaven.com/files/{aid}")
    att = {"source": "polyhaven", "id": aid, "license": "CC0"}
    if t == "models":
        g = (files.get("gltf") or {}).get(res) or next(iter((files.get("gltf") or {}).values()), None)
        if not g:
            raise ValueError(f"{aid}: no glTF download")
        main = g["gltf"]
        d = _asset_dir("polyhaven", aid, res)
        path = _http_file(main["url"], _safe_join(d, os.path.basename(main["url"].split("?")[0])))
        for rel, inc in (main.get("include") or {}).items():
            _http_file(inc["url"], _safe_join(d, rel))
        return {"kind": "model", "path": path, "att": att}
    if t == "hdris":
        fmt = _check_id(req.get("file_format") or "hdr", "file_format")
        h = ((files.get("hdri") or {}).get(res) or {}).get(fmt)
        if not h:
            raise ValueError(f"{aid}: no {res} {fmt} HDRI")
        p = _http_file(h["url"], _safe_join(_asset_dir("polyhaven", aid), os.path.basename(h["url"].split("?")[0])))
        return {"kind": "hdri", "path": p, "att": att}
    fmt = _check_id(req.get("file_format") or "jpg", "file_format")
    maps = {}
    for key in ("Diffuse", "nor_gl", "Rough", "Metal", "arm"):  # only the maps the material uses
        if key == "arm" and "Rough" in maps:
            continue
        m = ((files.get(key) or {}).get(res) or {}).get(fmt)
        if m:
            maps[key] = _http_file(m["url"], _safe_join(_asset_dir("polyhaven", aid, res),
                                                        os.path.basename(m["url"].split("?")[0])))
    if "Diffuse" not in maps:
        raise ValueError(f"{aid}: no {res} {fmt} texture maps")
    return {"kind": "texture", "maps": maps, "att": att}


def _sf_net(req):
    uid = _check_id(req.get("asset_id"))
    if not req.get("target_size"):
        raise ValueError("sketchfab models come at arbitrary scale: pass target_size (metres of the largest side)")
    key = _key("SKETCHFAB_API_KEY")
    meta = _http_json(f"https://api.sketchfab.com/v3/models/{uid}")
    dl = _http_json(f"https://api.sketchfab.com/v3/models/{uid}/download", {"Authorization": f"Token {key}"})
    url = (dl.get("gltf") or {}).get("url")
    if not url:
        raise ValueError(f"{uid}: no glTF download (not downloadable with this account?)")
    d = _asset_dir("sketchfab", uid)
    z = _http_file(url, os.path.join(d, "model.zip"), timeout=300)
    ex = _extract_zip(z, os.path.join(d, "x"))
    gl = next((os.path.join(a, f) for a, _b, fs in os.walk(ex) for f in sorted(fs) if f.lower().endswith((".gltf", ".glb"))), None)
    if not gl:
        raise ValueError(f"{uid}: no glTF in the download")
    lic = meta.get("license")
    att = {"source": "sketchfab", "id": uid, "author": (meta.get("user") or {}).get("username"),
           "license": lic.get("label") if isinstance(lic, dict) else lic, "url": meta.get("viewerUrl")}
    return {"kind": "model", "path": gl, "att": att}


def _pp_net(req):
    key = _key("POLYPIZZA_API_KEY")
    mid = _check_id(req.get("asset_id"))
    m = _http_json(f"https://api.poly.pizza/v1.1/model/{mid}", {"x-auth-token": key})
    url = m.get("Download")
    if not url:
        raise ValueError(f"{mid}: no download url")
    name = os.path.basename(url.split("?")[0]) or "model.glb"
    if not name.lower().endswith((".glb", ".gltf")):
        name = "model.glb"
    p = _http_file(url, _safe_join(_asset_dir("polypizza", mid), name))
    att = {"source": "polypizza", "id": mid, "author": (m.get("Creator") or {}).get("Username"),
           "license": m.get("Licence")}
    return {"kind": "model", "path": p, "att": att}


def _assets_net(req):
    """Connection thread, before queuing: everything that waits on the internet. No bpy here."""
    action = str(req.get("action") or "search").lower()
    src = str(req.get("source") or "polyhaven").lower()
    if src not in ASSET_SOURCES:
        raise ValueError("source must be " + ", ".join(ASSET_SOURCES))
    if action == "search":
        rows = {"polyhaven": _ph_search, "sketchfab": _sf_search, "polypizza": _pp_search}[src](req)
        thumbs = _fetch_thumbs(src, rows, int(req.get("previews") or 0))
        return {"rows": rows, "thumbs": thumbs}
    if action != "import":
        raise ValueError("action must be search or import")
    with LOCK:
        exclusive = PARTS.get(req.get("token"), {}).get("mode") == "exclusive"
    if src == "polyhaven":
        return _ph_net(req, exclusive)
    return _sf_net(req) if src == "sketchfab" else _pp_net(req)


def _activate_agent_coll(agent):
    coll = _agent_coll(agent)
    vl = bpy.context.view_layer

    def find_lc(lc):
        if lc.collection == coll:
            return lc
        for ch in lc.children:
            r_ = find_lc(ch)
            if r_:
                return r_
    lc = find_lc(vl.layer_collection)
    if lc:
        if lc.exclude:
            lc.exclude = False
        vl.active_layer_collection = lc
    return coll


def _import_model(path, coll, target_size, attribution):
    before = set(bpy.data.objects)
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".gltf", ".glb"):
        raise ValueError(f"unsupported model file: {path}")
    bpy.ops.import_scene.gltf(filepath=path)
    new = [o for o in bpy.data.objects if o not in before]
    for o in new:  # always in the agent's collection, whatever the importer did
        if o.name not in coll.objects:
            coll.objects.link(o)
        for c in list(o.users_collection):
            if c != coll:
                c.objects.unlink(o)
        for k, v in attribution.items():
            o[f"asset_{k}"] = str(v)
    roots = [o for o in new if o.parent is None or o.parent not in new]
    meshes = [o for o in new if o.type in MESH_TYPES]
    if target_size and meshes:
        bpy.context.view_layer.update()
        lo, hi = _bbox(meshes)
        big = max(hi - lo)
        if big > 1e-9:
            f = float(target_size) / big
            for o in roots:
                o.scale = o.scale * f
                o.location = o.location * f
    bpy.context.view_layer.update()
    lo, hi = _bbox(meshes) if meshes else (mathutils.Vector(), mathutils.Vector())
    return {"objects": sorted(o.name for o in new), "size_m": [round(v, 3) for v in (hi - lo)],
            "min_z": round(lo.z, 3), **attribution}


def _texture_material(net, req, coll, exclusive):
    # validate every target before changing anything (no partial application)
    targets = []
    for name in req.get("apply_to") or []:
        ob = bpy.data.objects.get(name)
        if ob is None:
            raise ValueError(f"no object named {name!r}")
        if ob.type not in MESH_TYPES or ob.data is None or not hasattr(ob.data, "materials"):
            raise ValueError(f"{name} cannot take a material ({ob.type})")
        if not exclusive:
            if ob.name not in coll.all_objects:
                raise PermissionError(f"{name} is not in your collection {coll.name}")
            sharing = [o.name for o in bpy.data.objects if o.data == ob.data and o.name not in coll.all_objects]
            if sharing:
                raise PermissionError(f"{name}'s mesh is shared with objects outside your collection: {sharing[:5]}")
        targets.append(ob)
    maps = net["maps"]
    mat = bpy.data.materials.new(f"PH_{net['att']['id']}")
    if mat.node_tree is None and hasattr(mat, "use_nodes"):
        mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")

    def tex(key, color):
        n = nt.nodes.new("ShaderNodeTexImage")
        n.image = bpy.data.images.load(maps[key], check_existing=True)
        if not color:
            n.image.colorspace_settings.is_data = True
        return n
    nt.links.new(tex("Diffuse", True).outputs["Color"], bsdf.inputs["Base Color"])
    if "Rough" in maps:
        nt.links.new(tex("Rough", False).outputs["Color"], bsdf.inputs["Roughness"])
    elif "arm" in maps:  # AO / Roughness / Metal packed in R / G / B
        sep = nt.nodes.new("ShaderNodeSeparateColor")
        nt.links.new(tex("arm", False).outputs["Color"], sep.inputs["Color"])
        nt.links.new(sep.outputs[1], bsdf.inputs["Roughness"])
        if "Metal" not in maps:
            nt.links.new(sep.outputs[2], bsdf.inputs["Metallic"])
    if "Metal" in maps:
        nt.links.new(tex("Metal", False).outputs["Color"], bsdf.inputs["Metallic"])
    if "nor_gl" in maps:
        nm = nt.nodes.new("ShaderNodeNormalMap")
        nt.links.new(tex("nor_gl", False).outputs["Color"], nm.inputs["Color"])
        nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
    for ob in targets:
        ob.data.materials.clear()
        ob.data.materials.append(mat)
    return {"material": mat.name, "applied_to": [o.name for o in targets], "maps": sorted(maps), **net["att"]}


def cmd_assets(req):
    """bpy part of hub_assets (the network part already ran in the connection thread, see _assets_net)."""
    if req.get("_net_exc") is not None:
        raise req["_net_exc"]
    net = req.get("_net") or {}
    action = str(req.get("action") or "search").lower()
    if action == "search":
        out = {"results": net.get("rows", [])}
        sheet = _preview_sheet(net.get("thumbs") or [], f"{req.get('source', 'x')}_{int(time.time() * 1000)}")
        if sheet:
            out["sheet"] = sheet
        for row in out["results"]:
            row.pop("thumb", None)  # urls cost tokens; the previews show them
        return out
    with LOCK:
        exclusive = PARTS.get(req["token"], {}).get("mode") == "exclusive"
    vl = bpy.context.view_layer
    sel = {o.name for o in vl.objects if o.select_get()}
    active, active_lc = vl.objects.active, vl.active_layer_collection
    try:
        if net.get("kind") == "hdri":
            if not exclusive:
                raise PermissionError("an HDRI replaces the world lighting for everyone: needs an exclusive lease")
            w = bpy.data.worlds.new(f"PH_{net['att']['id']}")
            if w.node_tree is None and hasattr(w, "use_nodes"):
                w.use_nodes = True
            nt = w.node_tree
            env = nt.nodes.new("ShaderNodeTexEnvironment")
            env.image = bpy.data.images.load(net["path"], check_existing=True)
            nt.links.new(env.outputs["Color"], nt.nodes["Background"].inputs["Color"])
            bpy.context.scene.world = w
            return {"world": w.name, **net["att"]}
        coll = _activate_agent_coll(req["_agent"])
        if net.get("kind") == "texture":
            return _texture_material(net, req, coll, exclusive)
        return _import_model(net["path"], coll, req.get("target_size"), net["att"])
    finally:  # importers change selection, the active object and the active collection: give them back
        for o in vl.objects:
            with contextlib.suppress(Exception):
                o.select_set(o.name in sel)
        with contextlib.suppress(Exception):
            vl.objects.active = active
        with contextlib.suppress(Exception):
            vl.active_layer_collection = active_lc


def _require_owner_coll(req, name):
    coll = bpy.data.collections.get(name)
    if coll is None:
        raise ValueError(f"collection does not exist: {name}")
    own = _owner(coll)
    with LOCK:
        exclusive = PARTS.get(req["token"], {}).get("mode") == "exclusive"
    if own and own != req["_agent"] and not exclusive:
        raise PermissionError(f"{name} belongs to {own}")
    return coll


def cmd_export_collection(req):
    coll = _require_owner_coll(req, req["collection"])
    path = os.path.abspath(req["path"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Never write a Scene datablock (crashed Blender 5.2.3); write the collection only.
    bpy.data.libraries.write(path, {coll}, fake_user=True, path_remap="ABSOLUTE")
    return {"path": path, "objects": len(coll.all_objects)}


def cmd_merge(req):
    with LOCK:
        if PARTS.get(req["token"], {}).get("mode") != "exclusive":
            raise PermissionError("merge requires an exclusive lease (it is the step that assembles the final file)")
    got = []
    for path in req["paths"]:
        path = os.path.abspath(path)
        with bpy.data.libraries.load(path, link=False) as (src, dst):
            dst.collections = list(src.collections)
        loaded = [c for c in dst.collections if c]
        kids = {ch.name for c in loaded for ch in c.children}
        for c in loaded:
            if "hub_owner" in c:
                del c["hub_owner"]  # the final file carries no session owner
            if c.name in kids:
                continue  # child: already comes along with its parent
            bpy.context.scene.collection.children.link(c)
            got.append({"collection": c.name, "objects": len(c.all_objects), "from": path})
    return {"merged": got}


def cmd_save(req):
    path = os.path.abspath(req["path"])
    copy = bool(req.get("copy", True))
    with LOCK:
        exclusive = PARTS.get(req["token"], {}).get("mode") == "exclusive"
    if not copy and not exclusive:
        raise PermissionError("save without copy changes everyone's file: requires an exclusive lease")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=path, copy=copy)
    return {"path": path}


def cmd_quit(req):
    global RUNNING
    with LOCK:
        others = [p["label"] for t, p in PARTS.items() if t != req.get("token")]
    if others:
        raise PermissionError(f"other live leases: {others}")
    _autosave(force=True)
    RUNNING = False
    return {"bye": True}


CMDS = {k[4:]: v for k, v in list(globals().items()) if k.startswith("cmd_")}
TOUCHES_SCENE = {"exec", "claim", "merge", "assets"}
NEEDS_AGENT = {"exec", "claim", "turntable", "export_collection", "note", "assets"}


def dispatch(req):
    cmd = req.get("cmd")
    fn = CMDS.get(cmd)
    if fn is None:
        raise ValueError(f"unknown command: {cmd}")
    token, extra = None, {}
    if cmd not in NO_TOKEN_CMDS:
        token = _check_token(req)
        if cmd in NEEDS_AGENT:
            req["_agent"], key, warn = _bind_agent(req, token)
            STATUS["busy_agent"] = req["_agent"]
            AGENT_LAST[req["_agent"]] = time.time()
            step = _clean_text(req.get("note"))
            if step and cmd != "note":  # what this command does, declared by the agent
                with LOCK:
                    AGENT_INFO.setdefault(req["_agent"], {}).update(step=step, step_at=time.time())
            if key:
                extra["agent_key"] = key
            if warn:
                extra["warning"] = warn
    with LOCK:
        exclusive = PARTS.get(token, {}).get("mode") == "exclusive"
    touches = cmd in TOUCHES_SCENE and not (cmd == "assets" and str(req.get("action") or "search") != "import")
    if touches or cmd in {"measure", "turntable", "export_collection", "save", "view"}:
        warn = _ensure_object_mode()
        if warn:
            extra["warning"] = (extra.get("warning", "") + " | " if extra.get("warning") else "") + warn
    if cmd == "exec":  # (re)load modules BEFORE the ownership snapshot: their import is nobody's change
        _load_modules(req.get("_agent"))
    before = _snapshot(req.get("_agent"), exclusive) if touches else None
    own_before = None
    if cmd in ("exec", "claim"):
        try:
            own_before = _own_sig(req.get("_agent"))
        except Exception:  # unusual object data must never fail the command itself
            own_before = None
    # job deadline, exposed in status: a timeout kill only applies after it (+ margin)
    limit = {"exec": min(max(float(req.get("timeout", 120)), 1.0), 1800.0), "turntable": 1800.0}.get(cmd, 600.0)
    STATUS["busy_deadline"] = time.time() + limit
    try:
        result = fn(req)
    finally:
        # a render/load that failed before its *_complete/_post handler must not block live redraws
        LIVE.update(rendering=False, loading=False, op_depth=0, skip_turntable=False)
        LIVE["in"] = False
    if own_before is not None:  # seen by the hub itself, even if the agent never says what it does
        with contextlib.suppress(Exception):
            _record_change(req["_agent"], own_before)
    if before is not None:
        viol = _diff(req.get("_agent"), before)
        if viol:
            _deliver(viol, req.get("_agent"))
            extra["violations"] = viol
        STATUS["dirty"] = True
    if cmd == "lease":
        token = result["token"]
    return result, extra, token


# ---------------------------------------------------------------- autosave / idle
def _autosave(force=False):
    if not STATUS["dirty"] and not force:
        return
    try:
        d = os.path.join(ARGS["state"], "autosave", ARGS["name"])
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}.blend")
        t_save = time.perf_counter()
        STATUS["saving"] = True
        try:
            bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
        finally:
            STATUS["saving"] = False
        STATUS["last_save_s"] = time.perf_counter() - t_save
        old = sorted(glob.glob(os.path.join(d, "*.blend")))[:-ARGS["keep"]]  # rotate: keep the last N
        for f in old:
            with contextlib.suppress(OSError):
                os.remove(f)
        STATUS.update(dirty=False, last_autosave=time.time(), autosave_path=path)
    except Exception as e:
        print(f"[hub-worker] autosave failed: {e}", flush=True)


# ---------------------------------------------------------------- network
def _modules_view():
    with LOCK:
        items = list(MODULES.items())
    return {n: ("ok" if m.get("ns") is not None else (m.get("error") or "not loaded")) for n, m in items}


def _status():
    rss, peak = _rss_mb()
    _gc_leases()
    with LOCK:
        parts = [{"label": p["label"], "mode": p["mode"], "expires_in": round(p["expires"] - _now())}
                 for p in PARTS.values()]
        agents = sorted(AGENTS)
        owners = dict(STATUS["owners"])
        reissues = {a: v[-1] for a, v in REISSUES.items() if v}
    return {"name": ARGS["name"], "pid": os.getpid(), "port": ARGS["port"], "worker": VERSION,
            "file": STATUS["file"], "gui": STATUS["gui"], "rss_mb": rss, "peak_mb": peak, "busy": STATUS["busy"],
            "busy_s": round(time.time() - STATUS["busy_since"], 1) if STATUS["busy_since"] else None,
            "busy_overdue_s": round(time.time() - STATUS["busy_deadline"], 1) if STATUS["busy"] and STATUS["busy_deadline"] else None,
            "queue": JOBS.qsize(), "participants": parts, "agents": agents, "owners": owners, "key_reissues": reissues,
            "modules": _modules_view(),
            "agents_info": _agents_info_view(),
            "overlay": _overlay_lines(),
            "commands": STATUS["commands"], "objects": STATUS["objects"], "autosave": STATUS["autosave_path"],
            "live_redraws": LIVE["count"], "live_ops_hooked": bool(LIVE.get("ops_hooked")),
            "idle_s": round(time.time() - STATUS["last_activity"], 1), "uptime_s": round(time.time() - STATUS["started"], 1)}


def _check_token_net(req):
    _gc_leases()
    with LOCK:
        tok = req.get("token")
        live = tok in PARTS
        return {"live": live, "sole": live and len(PARTS) == 1,
                "busy_token_is_you": STATUS["busy_token"] == tok and tok is not None}


def _encode(resp):
    try:
        return json.dumps(resp, default=str)
    except (ValueError, TypeError) as e:
        return json.dumps({"id": resp.get("id"), "ok": False, "error": f"response is not JSON-serializable: {e}"})


def _client(conn):
    f = conn.makefile("rwb")
    try:
        for line in f:
            if not line.strip():
                continue
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    raise ValueError("request is not an object")
            except Exception as e:
                f.write((json.dumps({"ok": False, "error": f"json: {e}"}) + "\n").encode())
                f.flush()
                continue
            req = {k: v for k, v in req.items() if not str(k).startswith("_")}  # internal keys never come from outside
            if not ARGS["secret"] or req.get("secret") != ARGS["secret"]:
                resp = {"id": req.get("id"), "ok": False, "error": "PermissionError: wrong worker secret"}
            elif req.get("cmd") in NET_CMDS:
                c = req["cmd"]
                res = {"pong": True, "name": ARGS["name"]} if c == "ping" else (_status() if c == "status" else _check_token_net(req))
                resp = {"id": req.get("id"), "ok": True, "result": res}
            else:
                tok = req.get("token")
                with LOCK:
                    if tok:
                        PENDING[tok] = PENDING.get(tok, 0) + 1  # queued: does not expire
                if req.get("cmd") == "assets" and _check_token_net(req)["live"]:  # internet work here, not on the main thread
                    try:
                        req["_net"] = _assets_net(req)
                    except Exception as e:  # raised by the queued command, after the token/agent checks
                        req["_net_exc"] = e
                box = queue.Queue(1)
                JOBS.put((req, box, time.perf_counter()))
                resp = box.get()
            f.write((_encode(resp) + "\n").encode("utf-8"))
            f.flush()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _serve(sock):
    while RUNNING:
        try:
            conn, _ = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(target=_client, args=(conn,), daemon=True).start()


def main():
    global RUNNING
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((ARGS["host"], ARGS["port"]))
    except OSError as e:
        print(f"[hub-worker] BIND_FAIL port {ARGS['port']}: {e}", flush=True)
        sys.stdout.flush()
        os._exit(3)  # guaranteed exit code (the hub tries the next port)
    sock.listen(32)
    sock.settimeout(0.5)
    threading.Thread(target=_serve, args=(sock,), daemon=True).start()
    threading.Thread(target=_watchdog, daemon=True).start()
    STATUS["file"] = bpy.data.filepath or None
    if not bpy.data.filepath:  # no .blend: empty scene (the factory Cube/Camera/Light would become "base")
        for coll in (bpy.data.objects, bpy.data.meshes, bpy.data.cameras, bpy.data.lights):
            for idb in list(coll):
                with contextlib.suppress(Exception):
                    coll.remove(idb)
    _load_keys()
    _refresh_owners()
    print(f"[hub-worker] {ARGS['name']} ready on {ARGS['host']}:{ARGS['port']} (Blender {bpy.app.version_string}, "
          f"{'window' if STATUS['gui'] else 'background'})", flush=True)
    if STATUS["gui"]:
        _gui_start(sock)
        return  # the script ends and Blender keeps running with its UI; the timer serves the queue
    while RUNNING:
        try:
            _loop_once()
        except _Timeout:  # late watchdog exception outside the agent's code: ignore and continue
            _disarm()
    time.sleep(0.3)
    sock.close()


GUI_AUTOSAVE_S = 5.0  # window mode: the user may close it with the X at any time
OVERLAY = os.environ.get("BLENDER_HUB_OVERLAY", "1").strip().lower() not in ("0", "false", "no", "off", "")


def _ago(t):
    s = max(0, int(time.time() - t))
    return f"{s}s" if s < 60 else (f"{s // 60}m" if s < 3600 else f"{s // 3600}h{(s % 3600) // 60:02d}")


def _agents_info_view():
    with LOCK:
        return {a: dict(v) for a, v in AGENT_INFO.items()}


def _cut(text, n):
    return text if len(text) <= n else text[:n - 1] + "…"


def _detail_line(info):
    """'step: ... · last: +2 (roof, gutter) ~3 -1 · 14 objects, 8,240 faces' (empty if nothing known)."""
    bits = []
    if info.get("step"):
        bits.append(f"step: {_cut(info['step'], 60)} ({_ago(info.get('step_at') or time.time())} ago)")
    ch = info.get("last_change")
    if ch:
        parts = []
        if ch["n_added"]:
            names = ", ".join(ch["added"][:3]) + ("…" if ch["n_added"] > 3 else "")
            parts.append(f"+{ch['n_added']} ({_cut(names, 40)})")
        if ch["n_changed"]:
            parts.append(f"~{ch['n_changed']}")
        if ch["n_removed"]:
            parts.append(f"-{ch['n_removed']}")
        bits.append("last: " + " ".join(parts) + f" {_ago(ch['at'])} ago")
    st = info.get("stats")
    if st:
        bits.append(f"{st['objects']} objects, {st['faces']:,} faces")
    return "  ·  ".join(bits)


def _overlay_lines():
    """[(text, kind)] for the live panel: kind = title | busy | active | idle | dim. Works without bpy."""
    _gc_leases()
    now = time.time()
    with LOCK:
        parts = dict(PARTS)
        agents = dict(AGENTS)
        busy_tok, busy_cmd, busy_agent = STATUS.get("busy_token"), STATUS.get("busy"), STATUS.get("busy_agent")
        busy_since = STATUS.get("busy_since")
        last = dict(AGENT_LAST)
        infos = {a: dict(v) for a, v in AGENT_INFO.items()}
    by_tok = {}
    for a, t in agents.items():
        by_tok.setdefault(t, []).append(a)
    working = sorted(agents)
    head = f"Blender Hub  {ARGS['name']}  |  {len(working)} agent{'s' if len(working) != 1 else ''} working"
    if len(parts) != len(by_tok):
        head += f", {len(parts)} lease{'s' if len(parts) != 1 else ''}"
    lines = [(head, "title")]
    MAX_SHOWN = 8  # the panel must fit the viewport: busy agents first, then the most recent
    order = sorted(working, key=lambda a: (not (busy_tok == agents[a] and busy_cmd and busy_agent in (None, a)),
                                           -(last.get(a) or 0)))
    hidden = order[MAX_SHOWN:]
    for a in order[:MAX_SHOWN]:
        t = agents[a]
        p = parts.get(t, {})
        lease = f"'{p.get('label', '?')}' {p.get('mode', '')}".strip()
        if busy_tok == t and busy_cmd and (busy_agent in (None, a)):
            prog = LIVE.get("progress")
            state, kind = f"running {prog or busy_cmd} {_ago(busy_since or now)}", "busy"
        elif a in last and now - last[a] < 60:
            state, kind = f"active, last command {_ago(last[a])} ago", "active"
        else:
            state, kind = (f"idle {_ago(last[a])}" if a in last else "idle"), "idle"
        info = infos.get(a, {})
        task = f"  ·  {_cut(info['task'], 50)}" if info.get("task") else ""
        lines.append((_cut(f"{a}{task}  ·  {state}  ·  lease {lease}", 140), kind))
        detail = _detail_line(info)
        if detail:
            lines.append((_cut("      " + detail, 140), "detail"))
    if hidden:
        lines.append((f"+{len(hidden)} more: " + ", ".join(sorted(hidden)[:10]), "dim"))
    for t, p in parts.items():
        if t not in by_tok:
            lines.append((f"lease '{p['label']}' ({p['mode']}) with no agent yet", "dim"))
    if not parts:
        lines.append(("no lease: nobody is working here", "dim"))
    return lines


_OVERLAY_COLORS = {"title": (1.0, 0.82, 0.35, 1.0), "busy": (0.45, 1.0, 0.45, 1.0), "active": (0.9, 0.95, 1.0, 1.0),
                   "idle": (0.62, 0.66, 0.72, 1.0), "dim": (0.55, 0.57, 0.6, 1.0),
                   "detail": (0.78, 0.82, 0.88, 1.0)}


def _draw_overlay():
    try:
        import blf
        region = bpy.context.region
        if region is None or region.type != "WINDOW":
            return
        ui = bpy.context.preferences.system.ui_scale
        size = int(12 * ui)
        font = 0
        blf.size(font, size)
        lines = _overlay_lines()
        pad, step = int(14 * ui), int(17 * ui)
        tools = next((r.width for r in bpy.context.area.regions if r.type == "TOOLS"), 0)
        x = pad + tools
        y = pad + step * (len(lines) - 1)
        blf.enable(font, blf.SHADOW)
        blf.shadow(font, 3, 0.0, 0.0, 0.0, 0.85)
        blf.shadow_offset(font, 1, -1)
        for text, kind in lines:
            blf.color(font, *_OVERLAY_COLORS.get(kind, (1, 1, 1, 1)))
            blf.position(font, x, y, 0)
            blf.draw(font, text)
            y -= step
        blf.disable(font, blf.SHADOW)
    except Exception:
        pass


def _redraw_views():
    with contextlib.suppress(Exception):
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()


def _gui_start(sock):
    def boot():
        # Reloading the empty scene closes the welcome screen (splash), which covered the viewport.
        # The queue is only served after that.
        if not bpy.data.filepath:
            with contextlib.suppress(Exception):
                with bpy.context.temp_override(window=bpy.context.window_manager.windows[0]):
                    bpy.ops.wm.read_homefile(use_empty=True)
        with contextlib.suppress(Exception):  # object/face count in the viewport
            for win in bpy.context.window_manager.windows:
                area, _ = _view3d(win)
                if area:
                    area.spaces.active.overlay.show_stats = True
        _refresh_owners()

        @bpy.app.handlers.persistent
        def _user_edit(scene, depsgraph):
            # Manual edits in the window go into the next autosave. Not counted: animation playback (fires
            # every frame), selection changes (not geometry/transform) and the autosave itself (thumbnail).
            if STATUS.get("saving"):
                return
            with contextlib.suppress(Exception):
                if bpy.context.screen and bpy.context.screen.is_animation_playing:
                    return
            for u in depsgraph.updates:
                if (u.is_updated_geometry or u.is_updated_transform
                        or isinstance(u.id, (bpy.types.Material, bpy.types.Collection))):
                    STATUS["dirty"] = True
                    return
        bpy.app.handlers.depsgraph_update_post.append(_user_edit)

        @bpy.app.handlers.persistent
        def _live(*_a):  # agent work shows up in the window while the command is still running
            _live_refresh()

        @bpy.app.handlers.persistent
        def _render_on(*_a):
            LIVE["rendering"] = True

        @bpy.app.handlers.persistent
        def _render_off(*_a):
            LIVE["rendering"] = False
        @bpy.app.handlers.persistent
        def _load_on(*_a):
            LIVE["loading"] = True

        @bpy.app.handlers.persistent
        def _load_off(*_a):
            LIVE["loading"] = False
        # first in line: handlers of other add-ons that run before ours must already see the flag
        bpy.app.handlers.render_init.insert(0, _render_on)
        bpy.app.handlers.render_pre.insert(0, _render_on)
        for h in (bpy.app.handlers.render_complete, bpy.app.handlers.render_cancel):
            h.append(_render_off)
        for name in ("load_pre", "undo_pre", "redo_pre"):
            getattr(bpy.app.handlers, name).insert(0, _load_on)
        for name in ("load_post", "load_post_fail", "undo_post", "redo_post"):
            if hasattr(bpy.app.handlers, name):
                getattr(bpy.app.handlers, name).append(_load_off)
        # depsgraph updates inside an operator are not safe points (_live_safe checks op_depth); frame changes
        # from the agent's own frame_set are
        for h in (bpy.app.handlers.depsgraph_update_post, bpy.app.handlers.frame_change_post):
            h.append(_live)
        LIVE["ops_hooked"] = _install_live_ops()
        if OVERLAY:  # live panel: worker, how many / which agents are working (bottom-left of every 3D view)
            bpy.types.SpaceView3D.draw_handler_add(_draw_overlay, (), "WINDOW", "POST_PIXEL")
        bpy.app.timers.register(tick, first_interval=0.05, persistent=True)
        return None

    def tick():
        global RUNNING
        t_end = time.perf_counter() + 0.03  # up to ~30 ms of queue work per tick: the UI does not stutter
        try:
            while RUNNING and time.perf_counter() < t_end and not JOBS.empty():
                _loop_once(block=False)
            if JOBS.empty():
                _loop_once(block=False)  # idle tasks (autosave, leases)
        except _Timeout:
            _disarm()
        except Exception:
            traceback.print_exc()
        gap = max(GUI_AUTOSAVE_S, 5.0 * STATUS.get("last_save_s", 0.0))  # heavy scene: save less often
        if ARGS["autosave"] > 0 and STATUS["dirty"] and time.time() - (STATUS["last_autosave"] or 0) > gap:
            with contextlib.suppress(Exception):
                _autosave()
        if OVERLAY and time.time() - STATUS.get("overlay_drawn", 0) > 1.0:  # clocks in the panel tick once a second
            STATUS["overlay_drawn"] = time.time()
            _redraw_views()
        if not RUNNING:
            with contextlib.suppress(Exception):
                sock.close()
            bpy.app.timers.register(_gui_quit, first_interval=0.3)
            return None
        return 0.01 if not JOBS.empty() else 0.05
    bpy.app.timers.register(boot, first_interval=0.3, persistent=True)


def _gui_quit():
    wm = bpy.context.window_manager
    with contextlib.suppress(Exception):  # only here: the hub quits without a "save?" prompt (the autosave already saved)
        bpy.context.preferences.view.use_save_prompt = False
    with contextlib.suppress(Exception):
        with bpy.context.temp_override(window=wm.windows[0]):
            bpy.ops.wm.quit_blender()
    return None


def _loop_once(block=True):
    global RUNNING
    try:
        req, box, t_in = JOBS.get(timeout=0.25) if block else JOBS.get_nowait()
    except queue.Empty:
        now = time.time()
        if ARGS["autosave"] > 0 and STATUS["dirty"] and now - (STATUS["last_autosave"] or STATUS["started"]) > ARGS["autosave"]:
            _autosave()
        _gc_leases()
        with LOCK:
            nobody = not PARTS
        if ARGS["idle"] > 0 and nobody and now - STATUS["last_activity"] > ARGS["idle"]:
            print(f"[hub-worker] {ARGS['name']} idle for {ARGS['idle']}s: saving and exiting", flush=True)
            _autosave()
            RUNNING = False
        return
    t0 = time.perf_counter()
    token = req.get("token")
    STATUS.update(busy=req.get("cmd"), busy_since=time.time(), busy_token=token, last_activity=time.time())
    try:
        result, extra, token = dispatch(req)
        resp = dict({"id": req.get("id"), "ok": True, "result": result}, **extra)
    except BaseException as e:  # nothing brings down the main loop
        resp = {"id": req.get("id"), "ok": False, "error": f"{type(e).__name__}: {e}"}
    _disarm()
    with contextlib.suppress(Exception):
        _refresh_owners()
    STATUS.update(busy=None, busy_since=None, busy_token=None, busy_deadline=None, busy_agent=None, commands=STATUS["commands"] + 1,
                  file=bpy.data.filepath or None, objects=len(bpy.data.objects), last_activity=time.time())
    with LOCK:
        tin = req.get("token")
        if tin and tin in PENDING:
            PENDING[tin] -= 1
            if PENDING[tin] <= 0:
                PENDING.pop(tin)
    if token:
        _renew(token)  # long work does not let the lease expire right after
        resp["lease"] = _lease_view(token)
        notes = _pop_notices(token)
        if notes:
            resp["notices"] = notes
    resp["ms"] = round((time.perf_counter() - t0) * 1000, 2)
    resp["wait_ms"] = round((t0 - t_in) * 1000, 2)
    box.put(resp)
    _redraw()  # window mode: viewers see the change immediately


main()
