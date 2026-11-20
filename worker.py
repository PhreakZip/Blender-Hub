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

import bpy
import bmesh
import mathutils
from mathutils.bvhtree import BVHTree

try:
    import numpy as np
except Exception:
    np = None

VERSION = "0.10.0"
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


def _rna_props(struct, depth=0):
    """Editable props (not read-only ones, which change on evaluation and would raise false alarms)."""
    out = []
    for p in struct.bl_rna.properties:
        if p.identifier in {"rna_type", "name"} or p.is_readonly or p.type == "COLLECTION":
            continue
        try:
            v = getattr(struct, p.identifier)
        except Exception:
            continue
        if p.type == "POINTER":
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
            if (EXEC_STATE["active"] and EXEC_STATE["gen"] == EXEC_STATE["armed_gen"]
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
    win = _gui_window()
    out_dir = os.path.join(ARGS["state"], "renders", ARGS["name"])
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"screen_{int(time.time() * 1000)}.png")
    size = min(max(int(req.get("size") or 1024), 256), 2048)
    full = bool(req.get("full"))
    _redraw()
    area, region = (None, None) if full else _view3d(win)
    with bpy.context.temp_override(window=win):
        with contextlib.suppress(Exception):  # actually draw now (otherwise the screenshot may show the previous scene)
            bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)
    if area is not None:
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.screen.screenshot_area(filepath=path)
    else:
        with bpy.context.temp_override(window=win):
            bpy.ops.screen.screenshot(filepath=path)
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
RESERVED_NAMES = {"bpy", "bmesh", "mathutils", "np", "math", "agent", "coll", "link", "result"}


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
        return ob

    mods = {n: inst[1] for (n, a), inst in AGENT_MODS.items() if a == agent and n in MODULES and MODULES[n].get("code")}
    ns = {**mods, "bpy": bpy, "bmesh": bmesh, "mathutils": mathutils, "np": np, "math": math,
          "agent": agent, "coll": coll, "link": link, "result": None}
    buf = io.StringIO()
    err = None
    timeout = min(max(float(req.get("timeout", 120)), 1.0), 1800.0)
    code = compile(req["code"], f"<exec {agent}>", "exec")
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


def cmd_turntable(req):
    """Review turntable: N frames around the target, from low to high, + contact sheet."""
    obs = _targets(req)
    n = int(req.get("views", 12))
    if n < 1 or n > 48:
        raise ValueError("views must be between 1 and 48")
    size = min(max(int(req.get("size", 256)), 64), 1024)
    out_dir = req.get("out_dir") or os.path.join(ARGS["state"], "renders", ARGS["name"])
    os.makedirs(out_dir, exist_ok=True)
    tag = f"{req['_agent']}_{re.sub(r'[^A-Za-z0-9_-]', '', str(req.get('tag') or int(time.time() * 1000)))}"
    isolate = req.get("isolate", True)
    sc = bpy.context.scene
    r = sc.render
    shd = sc.display.shading
    vs = sc.view_settings
    world = sc.world
    saved = [(r, k, getattr(r, k)) for k in ("engine", "resolution_x", "resolution_y", "resolution_percentage",
                                             "filepath", "film_transparent")]
    saved += [(r.image_settings, "file_format", r.image_settings.file_format), (sc, "camera", sc.camera),
              (vs, "view_transform", vs.view_transform)]
    saved += [(shd, k, getattr(shd, k)) for k in ("light", "color_type", "show_cavity")]
    if world:
        saved.append((world, "color", tuple(world.color)))
    frames, sheet, cam, cam_data = [], None, None, None
    try:
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
        cam_data = bpy.data.cameras.new(f"hub_cam_{tag}")
        cam = bpy.data.objects.new(f"hub_cam_{tag}", cam_data)
        sc.collection.objects.link(cam)
        _set_workbench(r)
        shd.light = "STUDIO"
        want = req.get("color", "MATERIAL")
        try:
            shd.color_type = want
        except TypeError:  # 5.3: VERTEX; 4.x: ATTRIBUTE
            alt = {"ATTRIBUTE": "VERTEX", "VERTEX": "ATTRIBUTE"}.get(want)
            shd.color_type = alt if alt else "MATERIAL"
        shd.show_cavity = True
        r.resolution_x = r.resolution_y = size
        r.resolution_percentage = 100
        r.image_settings.file_format = "PNG"
        r.film_transparent = False
        with contextlib.suppress(TypeError):
            vs.view_transform = "Standard"
        if world:
            world.color = (0.55, 0.57, 0.6)
        sc.camera = cam
        lo, hi = _bbox(obs)
        center = (lo + hi) / 2
        radius = max((hi - lo).length / 2, 1e-3)
        cam_data.lens_unit = "FOV"
        dist = radius / math.sin(cam_data.angle / 2) * 1.08
        cam_data.clip_start = max(dist - radius * 2, 1e-3) * 0.5
        cam_data.clip_end = dist + radius * 4
        el_lo, el_hi = req.get("elev", (-15.0, 60.0))
        for i in range(n):
            az = math.radians(i * 360.0 / n + 30)
            el = math.radians(el_lo + (el_hi - el_lo) * (i / max(n - 1, 1)))
            pos = center + mathutils.Vector((math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el))) * dist
            cam.location = pos
            cam.rotation_euler = (center - pos).to_track_quat("-Z", "Y").to_euler()
            path = os.path.join(out_dir, f"{tag}_{i:02d}.png")
            r.filepath = path
            bpy.ops.render.render(write_still=True)
            frames.append(path)
        if np is not None and req.get("sheet", True):
            cols = min(int(req.get("cols", 4)), n)
            rows = math.ceil(n / cols)
            grid = np.zeros((rows * size, cols * size, 4), dtype=np.float32)
            for i, p in enumerate(frames):
                img = bpy.data.images.load(p, check_existing=False)
                img.colorspace_settings.is_data = True
                px = np.empty(img.size[0] * img.size[1] * 4, dtype=np.float32)
                img.pixels.foreach_get(px)
                px = px.reshape(img.size[1], img.size[0], 4)
                rr, cc = divmod(i, cols)
                y0 = (rows - 1 - rr) * size
                grid[y0:y0 + size, cc * size:(cc + 1) * size] = px[:size, :size]
                bpy.data.images.remove(img)
            sheet_img = bpy.data.images.new(f"hub_sheet_{tag}", cols * size, rows * size, alpha=True)
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
    return {"frames": frames, "sheet": sheet, "views": n, "size": size}


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
TOUCHES_SCENE = {"exec", "claim", "merge"}
NEEDS_AGENT = {"exec", "claim", "turntable", "export_collection", "note"}


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
    if cmd in TOUCHES_SCENE or cmd in {"measure", "turntable", "export_collection", "save", "view"}:
        warn = _ensure_object_mode()
        if warn:
            extra["warning"] = (extra.get("warning", "") + " | " if extra.get("warning") else "") + warn
    if cmd == "exec":  # (re)load modules BEFORE the ownership snapshot: their import is nobody's change
        _load_modules(req.get("_agent"))
    before = _snapshot(req.get("_agent"), exclusive) if cmd in TOUCHES_SCENE else None
    own_before = None
    if cmd in ("exec", "claim"):
        try:
            own_before = _own_sig(req.get("_agent"))
        except Exception:  # unusual object data must never fail the command itself
            own_before = None
    # job deadline, exposed in status: a timeout kill only applies after it (+ margin)
    limit = {"exec": min(max(float(req.get("timeout", 120)), 1.0), 1800.0), "turntable": 1800.0}.get(cmd, 600.0)
    STATUS["busy_deadline"] = time.time() + limit
    result = fn(req)
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
            state, kind = f"running {busy_cmd} {_ago(busy_since or now)}", "busy"
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
