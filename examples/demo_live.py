"""Live demo: opens a Blender WITH a window and two agents work on the same scene,
each in its own collection, with pauses so you can follow along.

    python examples/demo_live.py [--close]

Leaves the window open at the end (close it yourself, or run again with --close).
"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hub import Hub  # noqa: E402

NAME = "live"
h = Hub()

if "--close" in sys.argv:
    print(h.stop_worker(NAME))
    sys.exit(0)

w = h.start_worker(NAME, gui=True)
print("window open:", w.get("port"), "pid", w.get("pid"), "boot", w.get("boot_s"), "s", flush=True)
time.sleep(2)

ta = h.call(NAME, "lease", mode="shared", label="agent-rocks")["result"]["token"]
tb = h.call(NAME, "lease", mode="shared", label="agent-tower")["result"]["token"]
keys = {}


def ex(tok, agent, code):
    r = Hub().call(NAME, "exec", token=tok, agent=agent, agent_key=keys.get(agent), code=code)
    if r.get("agent_key"):
        keys[agent] = r["agent_key"]
    if not r.get("ok") or (r.get("result") or {}).get("error"):
        print("ERROR", agent, r.get("error") or r["result"]["error"][-300:], flush=True)
    if r.get("violations"):
        print("INTRUSION WARNING", agent, r["violations"], flush=True)
    return r


# shared ground, created by A
ex(ta, "Rocks", """
bpy.ops.mesh.primitive_plane_add(size=14, location=(0, 0, 0)); bpy.context.object.name = 'ground'
m = bpy.data.materials.new('ground'); m.diffuse_color = (0.25, 0.45, 0.22, 1); bpy.context.object.data.materials.append(m)
""")
h.call(NAME, "view", token=ta, collection="Agent_Rocks", shading="MATERIAL")
time.sleep(1.5)

ROCK = """
import random
random.seed(SEED)
bm = bmesh.new(); bmesh.ops.create_icosphere(bm, subdivisions=2, radius=0.6)
for v in bm.verts: v.co *= 1 + random.uniform(-0.18, 0.18)
me = bpy.data.meshes.new(f'rock_{SEED}'); bm.to_mesh(me); bm.free()
ob = link(bpy.data.objects.new(me.name, me))
ob.location = (-4 + (SEED % 4) * 1.6, -3 + (SEED // 4) * 1.6, 0.4)
m = bpy.data.materials.get('rock') or bpy.data.materials.new('rock'); m.diffuse_color = (0.5, 0.5, 0.52, 1)
me.materials.append(m)
"""
TOWER = """
z = 0.25 + SEED * 0.5
bpy.ops.mesh.primitive_cylinder_add(vertices=8, radius=1.0 - SEED * 0.08, depth=0.5, location=(3.5, 1.5, z))
o = bpy.context.object; o.name = f'tower_{SEED}'
m = bpy.data.materials.get('brick') or bpy.data.materials.new('brick'); m.diffuse_color = (0.62, 0.3, 0.2, 1)
o.data.materials.append(m)
"""


def agent_a():
    for i in range(8):
        ex(ta, "Rocks", ROCK.replace("SEED", str(i)))
        time.sleep(0.9)


def agent_b():
    for i in range(8):
        ex(tb, "Tower", TOWER.replace("SEED", str(i)))
        time.sleep(1.1)


ts = [threading.Thread(target=agent_a), threading.Thread(target=agent_b)]
[t.start() for t in ts]
[t.join() for t in ts]

# measured fit: B adds a roof and measures it against the top of the tower
ex(tb, "Tower", """
bpy.ops.mesh.primitive_cone_add(vertices=8, radius1=0.7, depth=1.0, location=(3.5, 1.5, 6.0)); bpy.context.object.name = 'roof'
m = bpy.data.materials.get('roof_tile') or bpy.data.materials.new('roof_tile'); m.diffuse_color = (0.2, 0.2, 0.35, 1)
bpy.context.object.data.materials.append(m)
""")
time.sleep(1)
d = h.call(NAME, "measure", token=tb, a="tower_7", b="roof")["result"]
print("roof-tower gap before:", d["min_distance"], flush=True)
dd = d["delta_a_to_b"]
ex(tb, "Tower", f"o = bpy.data.objects['roof']; o.location.x -= {dd[0]}; o.location.y -= {dd[1]}; o.location.z -= {dd[2]}")
print("after:", h.call(NAME, "measure", token=tb, a="tower_7", b="roof")["result"]["min_distance"], flush=True)

# B tries to move one of A's rocks: the system warns
ex(tb, "Tower", "bpy.data.objects['rock_0'].location.z += 1.5")
ex(ta, "Rocks", "bpy.data.objects['rock_0'].location.z -= 1.5")  # A puts it back

h.call(NAME, "view", token=ta, objects=["ground"], shading="MATERIAL")
time.sleep(1)
shot = h.call(NAME, "screenshot", token=ta)
print("window screenshot:", shot.get("result", {}).get("path") or shot.get("error"), flush=True)
h.call(NAME, "release", token=ta)
h.call(NAME, "release", token=tb)
print("done: the window stays open. To close it: python examples/demo_live.py --close", flush=True)
