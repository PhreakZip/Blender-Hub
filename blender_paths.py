"""Find a Blender executable on Windows.

Order: BLENDER_EXE env var -> `blender` on PATH -> Program Files\\Blender Foundation (newest version)
-> Steam -> the .blend file association in the registry.
"""
import os
import re
import glob
import shutil


def _version_key(path):
    m = re.search(r"Blender\s*(\d+)\.(\d+)", path)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def candidates():
    out = []
    env = os.environ.get("BLENDER_EXE")
    if env:
        out.append(env)
    which = shutil.which("blender")
    if which:
        out.append(which)
    for root in filter(None, {os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), r"C:\Program Files"}):
        found = glob.glob(os.path.join(root, "Blender Foundation", "Blender*", "blender.exe"))
        out += sorted(found, key=_version_key, reverse=True)
    for steam in (r"C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe",
                  r"C:\Program Files\Steam\steamapps\common\Blender\blender.exe"):
        out.append(steam)
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"blendfile\shell\open\command") as k:
            cmd = winreg.QueryValue(k, None)
        m = re.match(r'\s*"([^"]+blender[^"]*\.exe)"', cmd, re.I)
        if m:
            out.append(m.group(1))
    except OSError:
        pass
    seen, uniq = set(), []
    for p in out:
        n = os.path.normcase(os.path.abspath(p))
        if n not in seen and os.path.isfile(p) and not os.path.basename(p).lower().startswith("blender-launcher"):
            seen.add(n)
            uniq.append(os.path.abspath(p))
    return uniq


def find_blender():
    c = candidates()
    return c[0] if c else None


if __name__ == "__main__":
    for p in candidates():
        print(p)
