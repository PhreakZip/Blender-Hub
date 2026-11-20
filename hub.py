"""Blender Hub: library + MCP server (stdio) for multiple Blender workers.

    python hub.py      -> MCP server on stdio (one process can serve the whole app)

Identity: there is NO per-process session. The agent is identified by the token that
hub_acquire / hub_lease returns; it goes into every tool call. That way several conversations
in the same MCP process (the Claude Desktop case) stay separate, and a server restart
locks nobody out: the token lives in the worker.

Registry: %LOCALAPPDATA%/blender-hub/registry.json (or BLENDER_HUB_STATE), guarded by an
msvcrt lock (Windows releases the lock by itself if the process dies).
"""
import os
import re
import sys
import json
import time
import base64
import glob
import shutil
import socket
import secrets
import subprocess
import threading
import contextlib

VERSION = "0.10.0"
HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "worker.py")
sys.path.insert(0, HERE)
from blender_paths import find_blender  # noqa: E402

BLENDER = os.environ.get("BLENDER_EXE") or find_blender() or "blender"
STATE_DIR = os.environ.get("BLENDER_HUB_STATE") or os.path.join(os.environ.get("LOCALAPPDATA", HERE), "blender-hub")
REGISTRY = os.path.join(STATE_DIR, "registry.json")
PORT_BASE = int(os.environ.get("BLENDER_HUB_PORT_BASE", "9881"))
PORT_MAX = PORT_BASE + 200
ADDON_PORT = 9876  # the user's windowed Blender (official addon): never use it
MAX_WORKERS = int(os.environ.get("BLENDER_HUB_MAX_WORKERS", "10"))
MIN_FREE_MB = int(os.environ.get("BLENDER_HUB_MIN_FREE_MB", "1500"))
IDLE_S = os.environ.get("BLENDER_HUB_IDLE", "1800")
AUTOSAVE_S = os.environ.get("BLENDER_HUB_AUTOSAVE", "300")
# MCP tools open a window by default (the user watches live; one window per session).
# The Python API keeps gui=False by default. Set BLENDER_HUB_GUI_DEFAULT=0 for background by default.
GUI_DEFAULT = os.environ.get("BLENDER_HUB_GUI_DEFAULT", "1").strip().lower() not in ("0", "false", "no", "off", "")


MAX_WINDOWS = int(os.environ.get("BLENDER_HUB_MAX_WINDOWS", "4"))  # windows never idle-exit: separate, lower cap
_FALSE = ("0", "false", "no", "off", "")


def _parse_bool(v):
    if isinstance(v, str):  # some clients send booleans as strings: "false" must stay false
        return v.strip().lower() not in _FALSE
    return bool(v)


def _gui_arg(args):
    """gui from the tool call; when omitted, the GUI_DEFAULT (live window unless configured otherwise)."""
    v = args.get("gui")
    return GUI_DEFAULT if v is None else _parse_bool(v)


def _meta_path(folder):
    return os.path.join(folder, "meta.json")


def _read_meta(folder):
    try:
        with open(_meta_path(folder), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_meta(name, **data):
    """Saved next to the autosaves (and moved with them into archives): restart/restore keep the window mode."""
    folder = os.path.join(STATE_DIR, "autosave", name)
    with contextlib.suppress(OSError):
        os.makedirs(folder, exist_ok=True)
        cur = _read_meta(folder)
        cur.update(data)
        tmp = _meta_path(folder) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f)
        os.replace(tmp, _meta_path(folder))
NAME_RE = re.compile(r"[A-Za-z0-9_\-]{1,32}")


# ---------------------------------------------------------------- Windows: lock and processes
@contextlib.contextmanager
def _locked(timeout=30):
    import msvcrt
    os.makedirs(STATE_DIR, exist_ok=True)
    f = open(REGISTRY + ".lock", "a+")
    t0 = time.time()
    while True:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            break
        except OSError:
            if time.time() - t0 > timeout:
                f.close()
                raise TimeoutError("registry locked")
            time.sleep(0.02)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        f.close()


def _read_json(path, empty):
    # Python opens without FILE_SHARE_DELETE: a read at the instant of os.replace raises PermissionError on Windows
    last = None
    for i in range(50):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return empty
        except (PermissionError, json.JSONDecodeError) as e:
            last = e
            time.sleep(0.01 * (i + 1))
    if isinstance(last, json.JSONDecodeError):  # truly corrupted: keep it aside and start empty
        with contextlib.suppress(OSError):
            os.replace(path, path + f".{int(time.time())}.bad")
        return empty
    raise TimeoutError(f"unreadable: {path}")


def _write_json(path, data):
    tmp = path + f".{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    for i in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # a reader has the file open: try again
            time.sleep(0.01 * (i + 1))
    raise TimeoutError(f"could not write {path}")


def _read_reg():
    return _read_json(REGISTRY, {})


def _write_reg(reg):
    _write_json(REGISTRY, reg)


RECYCLED = os.path.join(STATE_DIR, "recycled.json")


def _read_recycled():
    return _read_json(RECYCLED, [])


def _add_recycled(rec):
    with _locked():
        _write_json(RECYCLED, (_read_recycled() + [rec])[-200:])


def _write_recycled_update(rec):
    with _locked():
        recs = [rec if os.path.normcase(r["archived"]) == os.path.normcase(rec["archived"]) else r for r in _read_recycled()]
        _write_json(RECYCLED, recs)


def _retry(fn, tries=20):
    last = None
    for i in range(tries):
        try:
            return fn()
        except OSError as e:
            last = e
            time.sleep(0.05 * (i + 1))
    raise last


def _blends(d):
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "*.blend")))


def _archive_worker_files(name, arch):
    """Move autosave/<name> and keys/<name>.json into `arch` WITHOUT losing anything.

    1) rename with retries; 2) if Windows refuses (open handle: Defender, indexer, Explorer),
    copy, verify that every .blend arrived, and only then try to delete the source (if that fails,
    the copy is already safe). A real failure raises OSError: the caller does NOT recycle the worker.
    """
    src = os.path.join(STATE_DIR, "autosave", name)
    want = _blends(src) if os.path.isdir(src) else []
    if os.path.isdir(src):
        try:
            _retry(lambda: os.replace(src, arch), tries=10)
        except OSError:
            if os.path.exists(arch):
                shutil.rmtree(arch, ignore_errors=True)
            shutil.copytree(src, arch)
            if _blends(arch) != want:
                shutil.rmtree(arch, ignore_errors=True)
                raise OSError(f"autosave copy of {name} incomplete")
            for f in want:  # source: delete what we can; the replacement must not rotate over the old work
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(src, f))
    os.makedirs(arch, exist_ok=True)
    if want and _blends(arch) != want:
        raise OSError(f"archive of {name} is missing .blend files")
    ksrc = os.path.join(STATE_DIR, "keys", f"{name}.json")
    kdst = os.path.join(arch, "keys.json")
    if os.path.exists(ksrc):
        try:
            _retry(lambda: os.replace(ksrc, kdst), tries=10)
        except OSError:
            shutil.copyfile(ksrc, kdst)  # the replacement's start_worker deletes the source later
    return {"blends": want, "keys": os.path.exists(kdst)}


def _proc_identity(pid):
    """(executable path, creation time) of the PID, or None if it does not exist."""
    if not pid:
        return None
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(0x1000 | 0x00100000, False, int(pid))  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    if not h:
        return None
    try:
        code = wintypes.DWORD()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        if code.value != 259:  # STILL_ACTIVE
            return None
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        ft = [wintypes.FILETIME() for _ in range(4)]
        k32.GetProcessTimes(h, *[ctypes.byref(x) for x in ft])
        created = (ft[0].dwHighDateTime << 32) | ft[0].dwLowDateTime
        return buf.value, created
    finally:
        k32.CloseHandle(h)


def _is_our_worker(w):
    ident = _proc_identity(w.get("pid"))
    if not ident:
        return False
    exe, created = ident
    return os.path.basename(exe).lower() == "blender.exe" and (not w.get("created") or w["created"] == created)


def _port_free(port):
    # bind, not connect: on Windows a refused connect to localhost takes ~2 s
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def free_mb():
    import ctypes
    from ctypes import wintypes

    class MS(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MS()
    m.dwLength = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return round(m.ullAvailPhys / 1048576)


# ---------------------------------------------------------------- connection
class Conn:
    def __init__(self, port, timeout=1900):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.f = self.sock.makefile("rwb")
        self.lock = threading.Lock()
        self.n = 0

    def call(self, req):
        with self.lock:
            self.n += 1
            req = dict(req, id=self.n)
            self.f.write((json.dumps(req) + "\n").encode("utf-8"))
            self.f.flush()
            line = self.f.readline()
            if not line:
                raise ConnectionError("worker closed the connection")
            return json.loads(line)

    def close(self):
        with contextlib.suppress(Exception):
            self.sock.close()


class Hub:
    def __init__(self):
        self._conns = {}
        self._lock = threading.Lock()

    # ---- registry
    def workers(self, clean=False):
        reg = _read_reg()  # atomic write: read without the lock
        if clean:
            boot_grace = 180
            dead = [n for n, w in reg.items()
                    if (w.get("pid") and not _is_our_worker(w))
                    or (not w.get("pid") and time.time() - w.get("started", 0) > boot_grace)]  # orphan with pid 0
            if dead:
                with _locked():
                    reg = _read_reg()
                    for n in dead:
                        w = reg.get(n)
                        if w and ((w.get("pid") and not _is_our_worker(w))
                                  or (not w.get("pid") and time.time() - w.get("started", 0) > boot_grace)):
                            reg.pop(n)
                    _write_reg(reg)
        return reg

    def _w(self, name):
        w = self.workers().get(name)
        if not w:
            raise KeyError(f"worker does not exist: {name}")
        return w

    def status(self, name, timeout=3):
        """Answered on the worker's network thread: does not wait for long-running work."""
        w = self._w(name)
        c = Conn(w["port"], timeout=timeout)
        try:
            r = c.call({"cmd": "status", "secret": w["secret"]})
        finally:
            c.close()
        return r.get("result") if r.get("ok") else {"error": r.get("error")}

    # ---- lifecycle
    def start_worker(self, name, blend=None, wait=90, limit=None, check_ram=False, keep_keys=False,
                     keys_from=None, recycled_from=None, gui=False):
        """gui=True: windowed Blender (the user watches live). The queue is served by a timer,
        with no idle exit (it is closed by the user or by hub_stop_worker)."""
        if not NAME_RE.fullmatch(name or ""):
            raise ValueError("worker name: letters, digits, _ and - (max 32)")
        blend = os.path.abspath(blend) if blend else None
        if check_ram and free_mb() < MIN_FREE_MB:
            raise MemoryError(f"free RAM {free_mb()} MB < {MIN_FREE_MB} MB: not starting another Blender")
        if blend and not os.path.exists(blend):
            raise FileNotFoundError(blend)
        cur = self.workers(clean=True).get(name)
        if cur:
            try:
                st = self.status(name)
            except OSError:  # reserved by another process and still booting
                raise RuntimeError(f"{name} was just created by another process")
            if st.get("name") != name:
                raise RuntimeError(f"port of {name} answers as {st.get('name')}")
            if blend and os.path.normcase(st.get("file") or "") != os.path.normcase(blend):
                raise RuntimeError(f"{name} is already running with another file: {st.get('file')}")
            return dict(cur, reused=True)
        for attempt in range(5):
            with _locked():
                reg = _read_reg()
                if name in reg:
                    raise RuntimeError(f"{name} was just created by another process")
                if limit and len(reg) >= limit:  # inside the lock: two acquires cannot exceed the limit
                    raise RuntimeError(f"worker limit of {limit} reached; free one (hub_release/hub_stop_worker)")
                used = {w["port"] for w in reg.values()}
                port = PORT_BASE
                while port in used or port == ADDON_PORT or not _port_free(port):
                    port += 1
                    if port > PORT_MAX:
                        raise RuntimeError("no free port")
                secret = secrets.token_hex(16)
                log_path = os.path.join(STATE_DIR, f"{name}.log")
                keys_path = os.path.join(STATE_DIR, "keys", f"{name}.json")
                reg[name] = {"name": name, "port": port, "pid": 0, "secret": secret, "blend": blend,
                             "log": log_path, "started": time.time(), "gui": bool(gui)}
                if recycled_from:
                    reg[name]["recycled_from"] = recycled_from
                _write_reg(reg)  # name and port reserved; the rest happens outside the lock
            # Name reused (the previous one exited on idle, crashed, or this is a restore over it): what it left
            # in autosave/<name> and keys/<name>.json goes into an archive BEFORE booting, otherwise the autosave
            # rotation and the "fresh keys" step would delete the work. Restarting from its own autosave does not archive.
            own_dir = os.path.join(STATE_DIR, "autosave", name)
            from_own = bool(blend) and os.path.normcase(os.path.dirname(blend)) == os.path.normcase(own_dir)
            if not from_own and (_blends(own_dir) or (not keep_keys and os.path.exists(keys_path))):
                arch = os.path.join(STATE_DIR, "autosave", f"{name}-{time.strftime('%Y%m%d-%H%M%S')}")
                try:
                    saved = _archive_worker_files(name, arch)
                except OSError as e:
                    with _locked():
                        reg = _read_reg()
                        if reg.get(name, {}).get("pid") == 0 and reg[name].get("secret") == secret:
                            reg.pop(name)
                            _write_reg(reg)
                    raise RuntimeError(f"pending archive for {name}: not starting with this name, to avoid losing work ({e})")
                _add_recycled({"name": name, "archived": arch, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "saved": saved,
                               "agents": [], "file": None, "idle_s": None,
                               "reason": "name reused: the previous worker exited (idle or crash) or a restore over it"})
                with _locked():
                    reg = _read_reg()
                    if name in reg:
                        reg[name]["recycled_from"] = arch
                        _write_reg(reg)
                recycled_from = arch  # if the port fails and we retry, the mark goes along
            if not keep_keys:  # new worker: old keys are not valid (already archived above)
                with contextlib.suppress(OSError):
                    os.remove(keys_path)
            if keys_from and os.path.exists(keys_from):
                os.makedirs(os.path.dirname(keys_path), exist_ok=True)
                shutil.copyfile(keys_from, keys_path)
            cmd = [BLENDER] + ([] if gui else ["-b"]) + ["--factory-startup"] + ([blend] if blend else []) + [
                "--python", WORKER, "--", "--port", str(port), "--name", name, "--secret", secret,
                "--idle", "0" if gui else IDLE_S, "--autosave", AUTOSAVE_S, "--state", STATE_DIR, "--keys", keys_path,
                "--keep", "20" if gui else "5"]
            with open(log_path, "w", encoding="utf-8", errors="replace") as log:
                # stdin=DEVNULL: Blender must not inherit (and steal) the MCP stdio
                p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=0 if gui else getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ident = _proc_identity(p.pid)
            with _locked():
                reg = _read_reg()
                reg[name] = dict(reg.get(name, {}), pid=p.pid, created=ident[1] if ident else None)
                _write_reg(reg)
            t0 = time.time()
            while time.time() - t0 < wait:
                if p.poll() is not None:
                    break
                try:
                    c = Conn(port, timeout=5)
                    r = c.call({"cmd": "ping", "secret": secret})
                    c.close()
                    if r.get("ok") and r["result"].get("name") == name:
                        _write_meta(name, gui=bool(gui))
                        old = self._conns.pop(name, None)  # connection to the previous (dead) process
                        if old:
                            old.close()
                        return dict(self.workers()[name], boot_s=round(time.time() - t0, 2))
                except (OSError, ConnectionError, ValueError):
                    pass
                time.sleep(0.1)
            code = p.poll()
            with _locked():
                reg = _read_reg()
                reg.pop(name, None)
                _write_reg(reg)
            if code == 3:  # BIND_FAIL (port reserved by Windows, etc.): try another
                continue
            if code is None:
                p.kill()
                raise TimeoutError(f"worker {name} did not answer in {wait}s; see {log_path}")
            hint = (" | a Blender WINDOW could not be opened here (no desktop session / OpenGL?): pass gui=false, "
                    "or set BLENDER_HUB_GUI_DEFAULT=0 in the MCP server env" if gui else "")
            raise RuntimeError(f"Blender exited (code {code}); see {log_path}{hint}")
        raise RuntimeError("5 ports failed to bind")

    def stop_worker(self, name, token=None):
        """Stops only if: the given token is the only live lease, or there is no lease at all."""
        w = self._w(name)
        if not _is_our_worker(w):
            self.workers(clean=True)
            return {"stopped": True, "note": "was already dead"}
        tok = token
        if not tok:  # no token: stop only if nobody holds a lease (take an exclusive one to prove it)
            r = self.call(name, "lease", mode="exclusive", label="stop")
            if not r.get("ok"):
                raise PermissionError(f"did not stop {name}: {r.get('error')}")
            tok = r["result"]["token"]
        r = self.call(name, "quit", token=tok)
        if not r.get("ok"):
            raise PermissionError(f"did not stop {name}: {r.get('error')}")
        c = self._conns.pop(name, None)
        if c:
            c.close()
        t0 = time.time()
        while time.time() - t0 < 15 and _is_our_worker(w):
            time.sleep(0.1)
        if _is_our_worker(w):  # accepted the quit but did not exit: identity checked before killing
            subprocess.run(["taskkill", "/PID", str(w["pid"]), "/F"], capture_output=True)
        with _locked():
            reg = _read_reg()
            if reg.get(name, {}).get("pid") == w["pid"]:
                reg.pop(name)
            _write_reg(reg)
        return {"stopped": True}

    def kill_all_unsafe(self):
        """Benchmark only (its own registry): kills every worker in the registry."""
        for n, w in list(self.workers().items()):
            with contextlib.suppress(Exception):
                c = self._conns.pop(n, None)
                if c:
                    c.close()
            if _is_our_worker(w):
                subprocess.run(["taskkill", "/PID", str(w["pid"]), "/F"], capture_output=True)
        with _locked():
            _write_reg({})

    def restart_worker(self, name, gui=None, default_gui=False, _from_kill=False):
        """Dead worker (crash/idle/closed window): boots it again from its autosave.

        gui=None keeps the mode it had (registry entry, else meta.json next to its autosaves, else default_gui).
        """
        w = self.workers().get(name)
        if w and _is_our_worker(w):
            raise RuntimeError(f"{name} is alive")
        files = sorted(glob.glob(os.path.join(STATE_DIR, "autosave", name, "*.blend")))
        auto = files[-1] if files else None
        prev = (w or {}).get("recycled_from")
        self.workers(clean=True)
        if gui is None:
            gui = (w or {}).get("gui")
        if gui is None:
            gui = _read_meta(os.path.join(STATE_DIR, "autosave", name)).get("gui", default_gui)
        if gui and not _from_kill:
            self.check_window_cap()
        return dict(self.start_worker(name, blend=auto, limit=MAX_WORKERS, check_ram=True, keep_keys=bool(auto),
                                      recycled_from=prev, gui=bool(gui)),
                    from_autosave=auto, autosaves=len(files))

    def kill_worker(self, name, token=None, grace=None):
        """Hung worker (long C call, code that swallows the timeout): kill it and boot from autosave.

        Allowed if: the caller holds the only live lease, or the running job is past its OWN
        deadline (requested timeout) by more than `grace` s (default BLENDER_HUB_KILL_GRACE = 60).
        A legitimate exec with a long timeout is never killed before its deadline.
        """
        grace = float(os.environ.get("BLENDER_HUB_KILL_GRACE", "60") if grace is None else grace)
        w = self._w(name)
        st = self.status(name)
        sole = False
        if token:
            c = Conn(w["port"], timeout=3)
            try:
                chk = c.call({"cmd": "check_token", "secret": w["secret"], "token": token})
            finally:
                c.close()
            sole = bool(chk.get("ok") and chk["result"]["sole"])
        overdue = st.get("busy_overdue_s")
        busy_s = st.get("busy_s") or 0
        if not sole and not (overdue is not None and overdue > grace):
            raise PermissionError(f"did not kill {name}: you must hold the only live lease, or the job must be past "
                                  f"its deadline by > {grace:.0f}s (overdue {overdue}s)")
        if not _is_our_worker(w):
            raise RuntimeError("the registry PID is not the worker (not killing)")
        subprocess.run(["taskkill", "/PID", str(w["pid"]), "/F"], capture_output=True)
        t0 = time.time()
        while time.time() - t0 < 10 and _is_our_worker(w):
            time.sleep(0.1)
        c = self._conns.pop(name, None)
        if c:
            c.close()
        with _locked():
            reg = _read_reg()
            if reg.get(name, {}).get("pid") == w["pid"]:
                reg.pop(name)
            _write_reg(reg)
        return dict(self.restart_worker(name, gui=bool(w.get("gui")), _from_kill=True), killed=True, was_busy=st.get("busy"), busy_s=busy_s)

    def windows(self):
        """Open windows: name, leases, idle minutes (for actionable errors)."""
        out = []
        for n, w in self.workers(clean=True).items():
            if not w.get("gui"):
                continue
            try:
                st = self.status(n)
                out.append({"name": n, "leases": len(st.get("participants") or []),
                            "idle_min": round((st.get("idle_s") or 0) / 60, 1)})
            except Exception:
                out.append({"name": n, "leases": None, "idle_min": None})
        return out

    def _windows_hint(self):
        wins = self.windows()
        free = [w for w in wins if not w["leases"]]
        if not free:
            return ""
        return (" | open windows with no lease: " + ", ".join(f"{w['name']} (idle {w['idle_min']} min)" for w in free)
                + ": ask the user to close one (hub_stop_worker name=...), keep working in one with hub_lease(worker=name, label=..., mode=\"shared\" if it has leases), "
                  "or use gui=false")

    def check_window_cap(self):
        wins = self.windows()
        if len(wins) >= MAX_WINDOWS:
            raise RuntimeError(f"window limit of {MAX_WINDOWS} reached (BLENDER_HUB_MAX_WINDOWS): "
                               + ", ".join(f"{w['name']} (leases {w['leases']}, idle {w['idle_min']} min)" for w in wins)
                               + " | ask the user to close a window they no longer watch (hub_stop_worker name=...), "
                                 "keep working in an existing one with hub_lease(worker=name, label=..., mode=\"shared\" if it has leases), "
                                 "or pass gui=false to work in the background"
                               + " (the cap can be exceeded by one when two sessions open windows at the same instant)")

    def acquire(self, mode="exclusive", label=None, blend=None, ttl=900, gui=False):
        """Pool: takes a free worker (no lease) or boots a new one, and leases it."""
        # no label: the worker names the lease after its first agent
        reg = self.workers(clean=True)
        for name in sorted(reg):
            try:
                st = self.status(name)
            except Exception:
                continue
            if bool(st.get("gui")) != bool(gui):
                continue  # asked for a window and this one is background (or vice versa)
            if st.get("participants") or st.get("owners"):
                continue  # busy, or someone's work is in the scene ("dirty" worker): do not hand it out
            if blend and os.path.normcase(st.get("file") or "") != os.path.normcase(os.path.abspath(blend)):
                continue
            if not blend and st.get("file"):
                continue
            r = self.call(name, "lease", mode=mode, label=label, ttl=ttl)
            if r.get("ok"):
                return dict(r["result"], reused=True)
        if gui:  # before the pool-full branch: recycling may boot the replacement as a window
            self.check_window_cap()
        if len(reg) >= MAX_WORKERS:
            rec = self._recycle_one(reg, blend, gui=gui)
            if rec:
                r = self.call(rec["name"], "lease", mode=mode, label=label, ttl=ttl)
                if r.get("ok"):
                    return dict(r["result"], reused=False, recycled=rec)
            why = getattr(self, "_last_recycle_error", None)
            raise RuntimeError(f"worker limit of {MAX_WORKERS} reached and none is free; free one (hub_release/hub_stop_worker)"
                               + (f" [did not recycle, to avoid losing work: {why}]" if why else "") + self._windows_hint())
        if free_mb() < MIN_FREE_MB:
            raise MemoryError(f"free RAM {free_mb()} MB < {MIN_FREE_MB} MB: not starting another Blender")
        for i in range(MAX_WORKERS * 2):
            name = f"w{i}"
            reg = self.workers()
            if len(reg) >= MAX_WORKERS:
                raise RuntimeError(f"worker limit of {MAX_WORKERS} reached; free one (hub_release/hub_stop_worker)"
                                   + self._windows_hint())
            if name not in reg:
                try:
                    self.start_worker(name, blend=blend, limit=MAX_WORKERS, gui=gui)
                except RuntimeError as e:
                    if "was just created" in str(e) or "pending archive" in str(e):
                        continue
                    raise
                r = self.call(name, "lease", mode=mode, label=label, ttl=ttl)
                if not r.get("ok"):
                    raise RuntimeError(r.get("error"))
                return dict(r["result"], reused=False)
        raise RuntimeError("no free name")

    def _recycle_one(self, reg, blend=None, gui=False):
        """Limit reached: the unleased worker idle for the longest is stopped (autosave) and booted clean.

        The work it held is archived in autosave/<name>-<date>/ (keys included).
        """
        free = []
        for name in reg:
            try:
                st = self.status(name)
            except Exception:
                continue
            if st.get("gui"):
                continue  # open window: closing it is the user's decision, recycling never touches it
            if not st.get("participants") and not st.get("busy"):
                free.append((st.get("idle_s") or 0, name))
        for _, name in sorted(free, reverse=True):
            try:
                before = self.status(name)
                self.stop_worker(name)
            except (PermissionError, OSError, KeyError):
                continue  # someone just joined it, or it already exited
            stamp = time.strftime("%Y%m%d-%H%M%S")
            arch = os.path.join(STATE_DIR, "autosave", f"{name}-{stamp}")
            try:
                saved = _archive_worker_files(name, arch)
            except OSError as e:
                # could not save the work: do NOT recycle. If the folder already went into the archive,
                # move it back; then boot it again from its own autosave, with its keys.
                src = os.path.join(STATE_DIR, "autosave", name)
                if not os.path.isdir(src) and os.path.isdir(arch) and _blends(arch):
                    with contextlib.suppress(OSError):
                        _retry(lambda: os.replace(arch, src), tries=10)
                with contextlib.suppress(Exception):
                    self.restart_worker(name)
                self._last_recycle_error = f"{name}: {e}"
                continue
            rec = {"name": name, "archived": arch, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "saved": saved,
                   "agents": sorted({o for o in (before.get("owners") or {}).values() if o}),
                   "file": before.get("file"), "idle_s": before.get("idle_s")}
            _add_recycled(rec)  # whoever comes back is told and finds the archive
            try:
                self.start_worker(name, blend=blend, limit=MAX_WORKERS, gui=gui)
            except RuntimeError as e:
                if "was just created" in str(e):
                    continue  # another process took the name between the stop and the start
                raise
            with _locked():  # mark the replacement: only it gives the "RECYCLED" hint for an unknown token
                reg = _read_reg()
                if name in reg:
                    reg[name]["recycled_from"] = arch
                    _write_reg(reg)
            return rec
        return None

    def recycled(self, name=None):
        """Work archived by recycling (newest last)."""
        recs = _read_recycled()
        return [r for r in recs if name is None or r["name"] == name]

    def restore_archive(self, archive, name=None, gui=None, default_gui=False):
        """Boots a new worker with the newest autosave of a recycle archive and its keys.

        Agents resume as usual: hub_lease (shared) + agent_key.
        """
        archive = os.path.abspath(archive)
        known = {os.path.normcase(os.path.abspath(r["archived"])) for r in _read_recycled()}
        if os.path.normcase(archive) not in known or not os.path.isdir(archive):
            raise ValueError(f"is not a registered recycle archive (hub_list_archives): {archive}")
        files = sorted(glob.glob(os.path.join(archive, "*.blend")))
        if not files:
            raise FileNotFoundError(f"no .blend in {archive}")
        base = os.path.basename(archive)
        # default name differs from the archive folder: the restored worker writes autosave/<name>-r/ and the
        # archived snapshot stays intact
        name = name or (re.sub(r"[^A-Za-z0-9_\-]", "", base)[:30] + "-r")
        keys_src = os.path.join(archive, "keys.json")
        if name in self.workers(clean=True):
            raise RuntimeError(f"worker {name} already exists: choose another name")
        rec = next((r for r in _read_recycled() if os.path.normcase(r["archived"]) == os.path.normcase(archive)), {})
        own_dir = os.path.join(STATE_DIR, "autosave", name)
        if rec.get("restored_as") == name and (_blends(own_dir) or os.path.exists(os.path.join(STATE_DIR, "keys", f"{name}.json"))):
            # already restored under this name and the restored worker has a life of its own (newer autosave/keys)
            raise RuntimeError(f"this archive was already restored as {name}: use hub_restart_worker {name} "
                               "(keeps the newer autosave and keys)")
        # restore over a name already in use: start_worker archives what was there and only then copies the keys
        if gui is None:  # the mode the archived worker had, else the default
            gui = _read_meta(archive).get("gui", default_gui)
        if gui:
            self.check_window_cap()
        w = self.start_worker(name, blend=files[-1], limit=MAX_WORKERS, check_ram=True, keep_keys=False,
                              keys_from=keys_src, gui=bool(gui))
        for r in _read_recycled():
            if os.path.normcase(r["archived"]) == os.path.normcase(archive):
                r["restored_as"] = name
                _write_recycled_update(r)
        return dict(w, restored_from=files[-1], keys=os.path.exists(keys_src))

    # ---- commands
    def _conn(self, name, w=None):
        """Cached connection, checked against the registry: a worker recycled/restarted by ANOTHER process
        or another Hub instance has a new port and secret, so the stale connection is replaced."""
        w = w or self._w(name)
        ident = (w["port"], w["secret"])
        with self._lock:
            c = self._conns.get(name)
            if c is not None and getattr(c, "ident", None) != ident:
                self._conns.pop(name)
                c.close()
                c = None
            if c is None:
                c = Conn(w["port"])
                c.ident = ident
                self._conns[name] = c
            return c

    def call(self, name, cmd, **params):
        w = self._w(name)
        req = dict({k: v for k, v in params.items() if not k.startswith("_")}, cmd=cmd, secret=w["secret"])
        conn = self._conn(name, w)
        try:
            r = conn.call(req)
            err = r.get("error") or ""
            if not r.get("ok") and "requires a valid lease token" in err and w.get("recycled_from"):
                # unknown token on the REPLACEMENT of a recycled worker: it probably belonged to the old process
                rec = next((x for x in reversed(_read_recycled())
                            if os.path.normcase(x["archived"]) == os.path.normcase(w["recycled_from"])), None)
                if rec:
                    r["recycled"] = rec
                    r["error"] = (err + f" | worker {name} was RECYCLED at {rec['at']} (pool full) and the work of "
                                  f"{rec['agents']} was archived in {rec['archived']}: use hub_restore_archive"
                                  + (f" (already restored as {rec['restored_as']})" if rec.get("restored_as") else ""))
            return r
        except (OSError, ConnectionError):
            with self._lock:
                c = self._conns.get(name)
                if c is conn:  # close only the connection that failed (another thread may have opened a new one)
                    self._conns.pop(name)
            conn.close()
            raise


# ======================================================================= MCP
S = {"type": "string"}
TOKEN = {"type": "string", "description": "from hub_acquire/hub_lease"}
KEY = {"type": "string", "description": "only to resume an agent after its lease expired"}
NOTE = {"type": "string", "description": "short line: what this step does (shown live)"}
WIN = "(window workers)"
TOOLS = [
    ("hub_acquire", "Lease a free Blender worker (boots one if needed). Opens a window the user watches live by default; gui=false for background/batch. Returns worker + token (keep both). mode: exclusive (default) | shared. When done: hub_release, then ask the user before closing the window (hub_stop_worker).",
     {"mode": {"type": "string", "enum": ["exclusive", "shared"]},
      "label": {"type": "string", "description": "what you are doing (shown live)"},
      "blend": S, "ttl": {"type": "number"}, "gui": {"type": "boolean", "description": "default true"}}, ["label"]),
    ("hub_lease", "Join a specific worker; returns a token. mode exclusive (default) | shared (to work next to others).",
     {"worker": S, "mode": {"type": "string", "enum": ["exclusive", "shared"]},
      "label": {"type": "string", "description": "what you are doing (shown live)"}, "ttl": {"type": "number"}}, ["worker", "label"]),
    ("hub_renew", "Renew your lease (every command already renews it).", {"worker": S, "token": TOKEN}, ["worker", "token"]),
    ("hub_release", "Release your lease.", {"worker": S, "token": TOKEN}, ["worker", "token"]),
    ("hub_list_workers", "Short status of every worker (leases, agents and what they do, busy, RAM). verbose=true for everything.",
     {"verbose": {"type": "boolean"}}, []),
    ("hub_start_worker", "Boot a worker with a name/.blend (hub_acquire does it for you). Window by default; gui=false for background.",
     {"name": S, "blend": S, "gui": {"type": "boolean"}}, ["name"]),
    ("hub_view", f"Frame the user's viewport on a collection/objects {WIN}. Optional shading (VERTEX, MATERIAL).",
     {"worker": S, "token": TOKEN, "collection": S, "objects": {"type": "array", "items": S}, "shading": S}, ["worker", "token"]),
    ("hub_screenshot", f"What the user sees {WIN}: the 3D viewport, longest side `size` px (default 1024). full=true for the whole window. Prefer hub_turntable to review a model.",
     {"worker": S, "token": TOKEN, "size": {"type": "integer"}, "full": {"type": "boolean"}}, ["worker", "token"]),
    ("hub_stop_worker", "Stop a worker (refused while another session holds a lease).", {"name": S, "token": TOKEN}, ["name"]),
    ("hub_restart_worker", "Boot a dead/closed worker again from its newest autosave (keeps its window mode unless gui is given).",
     {"name": S, "gui": {"type": "boolean"}}, ["name"]),
    ("hub_agent_key", "New agent_key for an agent your token is still using (the old key stops working). No such token: an exclusive lease reissues it.",
     {"worker": S, "token": TOKEN, "agent": S}, ["worker", "token", "agent"]),
    ("hub_list_archives", "Work archived when a worker was recycled or its name reused.", {"name": S}, []),
    ("hub_restore_archive", "Boot a new worker from an archive (newest autosave + keys); then hub_lease(mode=\"shared\", label=...) and pass your agent_key.",
     {"archive": S, "name": S, "gui": {"type": "boolean"}}, ["archive"]),
    ("hub_kill_worker", "Kill a hung worker and boot it from autosave (only the sole lease holder, or when its job is past its own timeout + grace).",
     {"name": S, "token": TOKEN}, ["name"]),
    ("hub_exec", "Run bpy Python as `agent`. Objects created go to Agent_<agent> (link(ob) for bpy.data.objects.new); set `result` to return data; modules from BLENDER_HUB_MODULES (e.g. K) are preloaded. timeout s (default 120). The first call that names a new agent returns agent_key: keep it.",
     {"worker": S, "token": TOKEN, "agent": S, "agent_key": KEY, "code": S, "timeout": {"type": "number"}, "note": NOTE},
     ["worker", "token", "agent", "code"]),
    ("hub_note", "Declare the task you are working on (shown live; call at start and when it changes). Returns agent_key on the first call for a new agent.",
     {"worker": S, "token": TOKEN, "agent": S, "agent_key": KEY, "task": S}, ["worker", "token", "agent", "task"]),
    ("hub_measure", "Distance, intersection and penetration between objects a and b (world space). Move b by -delta_a_to_b to make it touch a (null if they penetrate).",
     {"worker": S, "token": TOKEN, "a": S, "b": S, "tol": {"type": "number"}}, ["worker", "token", "a", "b"]),
    ("hub_turntable", "Review sheet: N renders around a collection/objects (low to high), returned as one image. views default 12, size default 256 px per view; frames=true also lists the frame files.",
     {"worker": S, "token": TOKEN, "agent": S, "agent_key": KEY, "collection": S, "objects": {"type": "array", "items": S},
      "views": {"type": "integer"}, "size": {"type": "integer"}, "color": S, "note": NOTE, "frames": {"type": "boolean"}},
     ["worker", "token", "agent"]),
    ("hub_export_collection", "Save your collection to a .blend.",
     {"worker": S, "token": TOKEN, "agent": S, "agent_key": KEY, "collection": S, "path": S, "note": NOTE},
     ["worker", "token", "agent", "collection", "path"]),
    ("hub_merge", "Append collections from .blend files (exclusive lease).",
     {"worker": S, "token": TOKEN, "paths": {"type": "array", "items": S}}, ["worker", "token", "paths"]),
    ("hub_save", "Save the worker's .blend (a copy by default; in place needs an exclusive lease).",
     {"worker": S, "token": TOKEN, "path": S, "copy": {"type": "boolean"}}, ["worker", "token", "path"]),
]
_MAP = {"hub_lease": "lease", "hub_renew": "renew", "hub_release": "release", "hub_exec": "exec",
        "hub_measure": "measure", "hub_turntable": "turntable", "hub_export_collection": "export_collection",
        "hub_merge": "merge", "hub_save": "save", "hub_agent_key": "agent_key", "hub_view": "view",
        "hub_screenshot": "screenshot", "hub_note": "note"}


def _short_status(st):
    """What an agent needs to decide: who is there, doing what, and whether the worker is free."""
    info = st.get("agents_info") or {}
    agents = {}
    for a in st.get("agents") or []:
        i = info.get(a) or {}
        agents[a] = {k: v for k, v in (("task", i.get("task")), ("step", i.get("step")),
                                       ("objects", (i.get("stats") or {}).get("objects"))) if v}
    out = {"gui": st.get("gui"), "file": os.path.basename(st["file"]) if st.get("file") else None,
           "leases": [f"{p['label']} ({p['mode']}, {p['expires_in']}s)" for p in st.get("participants") or []],
           "agents": agents, "busy": st.get("busy"), "idle_s": st.get("idle_s"), "rss_mb": st.get("rss_mb")}
    bad = {k: v for k, v in (st.get("modules") or {}).items() if v != "ok"}
    if bad:
        out["module_errors"] = bad
    if st.get("note"):
        out["note"] = st["note"]
    return {k: v for k, v in out.items() if v not in (None, [], {})}


def _compact_lease(lease):
    if not lease:
        return None
    if not lease.get("active"):
        return {"active": False}
    out = {"mode": lease.get("mode"), "expires_in": lease.get("expires_in")}
    if lease.get("others"):
        out["others"] = len(lease["others"])
    return out


def _round_floats(v, nd=4):
    if isinstance(v, float):
        return round(v, nd)
    if isinstance(v, (list, tuple)):
        return [_round_floats(x, nd) for x in v]
    if isinstance(v, dict):
        return {k: _round_floats(x, nd) for k, x in v.items()}
    return v


def _tool_call(hub, name, args):
    """Returns (data, png_image_or_None); raises on error."""
    if name == "hub_acquire":
        return hub.acquire(args.get("mode", "exclusive"), args.get("label"), args.get("blend"), args.get("ttl", 900),
                           gui=_gui_arg(args)), None
    if name == "hub_start_worker":
        g = _gui_arg(args)
        if g:
            hub.check_window_cap()
        return hub.start_worker(args["name"], args.get("blend"), limit=MAX_WORKERS, check_ram=True, gui=g), None
    if name == "hub_stop_worker":
        return hub.stop_worker(args["name"], args.get("token")), None
    if name == "hub_restart_worker":
        g = _gui_arg(args) if args.get("gui") is not None else None
        return hub.restart_worker(args["name"], gui=g, default_gui=GUI_DEFAULT), None
    if name == "hub_list_archives":
        return hub.recycled(args.get("name")), None
    if name == "hub_restore_archive":
        g = _gui_arg(args) if args.get("gui") is not None else None
        return hub.restore_archive(args["archive"], args.get("name"), gui=g, default_gui=GUI_DEFAULT), None
    if name == "hub_kill_worker":
        return hub.kill_worker(args["name"], args.get("token")), None
    if name == "hub_list_workers":
        out = {}
        verbose = _parse_bool(args.get("verbose", False))
        for n in hub.workers(clean=True):
            try:
                st = hub.status(n)
                if st.get("gui") and not st.get("participants"):
                    st["note"] = (f"released window (no lease, idle {round((st.get('idle_s') or 0) / 60)} min): ask the user "
                                  "before closing it (hub_stop_worker)")
                out[n] = st if verbose else _short_status(st)
            except Exception as e:
                out[n] = {"error": f"no answer: {e}"}
        return out, None
    args = {k: v for k, v in args.items() if not str(k).startswith("_")}  # internal keys are not passed through
    worker = args.pop("worker")
    r = hub.call(worker, _MAP[name], **args)
    if not r.get("ok"):
        raise RuntimeError(r.get("error"))
    res = r["result"]
    shown = res
    if name == "hub_turntable" and isinstance(res, dict) and not _parse_bool(args.get("frames", False)):  # the image IS the answer
        shown = {k: v for k, v in res.items() if k != "frames"}
    elif name == "hub_measure":
        shown = _round_floats(res)
    data = {"result": shown, "lease": _compact_lease(r.get("lease"))}
    for k in ("violations", "notices", "agent_key", "warning", "recycled"):
        if r.get(k):
            data[k] = r[k]
    if isinstance(res, dict) and res.get("error"):
        raise RuntimeError(json.dumps(data, ensure_ascii=False, default=str))
    img = None
    shot = res.get("sheet") if name == "hub_turntable" else (res.get("path") if name == "hub_screenshot" else None)
    if shot and os.path.exists(shot):
        with open(shot, "rb") as f:
            img = base64.b64encode(f.read()).decode("ascii")
    return data, img


SUPPORTED_PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]


def serve_mcp():
    hub = Hub()
    cancelled = set()  # notifications/cancelled: the work in Blender does not stop, but the response is not sent
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    out_lock = threading.Lock()

    def send(msg):
        if msg.get("id") is not None and msg.get("id") in cancelled:
            cancelled.discard(msg["id"])
            if "agent_key" not in json.dumps(msg, default=str):  # a response carrying a new key is never dropped
                return
        with out_lock:
            sys.stdout.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def handle(msg):
        mid, method = msg.get("id"), msg.get("method")
        try:
            if method == "initialize":
                asked = (msg.get("params") or {}).get("protocolVersion")
                res = {"protocolVersion": asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
                       "capabilities": {"tools": {}},
                       "serverInfo": {"name": "blender-hub", "version": VERSION},
                       "instructions": "Multi-agent Blender. hub_acquire(label=...) -> keep worker+token (a window the user watches opens by default). "
                                       "Use one `agent` name; keep the agent_key from its first call. hub_note(task=) at start, note= on each hub_exec. "
                                       "Change only your Agent_<agent> collection; undo anything listed in `violations`; `notices` mean someone changed your objects. "
                                       "Review with hub_turntable (one image), fit parts with hub_measure; use hub_screenshot sparingly (images cost tokens). "
                                       "Done: hub_release, ask the user before closing the window."}
            elif method == "tools/list":
                res = {"tools": [{"name": n, "description": d,
                                  "inputSchema": {"type": "object", "properties": p, "required": r}}
                                 for n, d, p, r in TOOLS]}
            elif method == "tools/call":
                p = msg.get("params") or {}
                try:
                    data, img = _tool_call(hub, p.get("name"), p.get("arguments") or {})
                    content = [{"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))}]
                    if img:
                        content.append({"type": "image", "data": img, "mimeType": "image/png"})
                    res = {"content": content}
                except Exception as e:
                    res = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
            elif method == "ping":
                res = {}
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method: {method}"}})
                return
            send({"jsonrpc": "2.0", "id": mid, "result": res})
        except Exception as e:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}})

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"parse: {e}"}})
            continue
        if not isinstance(msg, dict):
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}})
            continue
        if msg.get("id") is None:  # notification: nothing to answer
            if msg.get("method") == "notifications/cancelled":
                rid = (msg.get("params") or {}).get("requestId")
                if rid is not None:
                    cancelled.add(rid)
            continue
        if msg.get("method") in ("initialize", "tools/list", "ping"):
            handle(msg)  # fast and in order
        else:
            threading.Thread(target=handle, args=(msg,), daemon=True).start()  # tools/call does not block ping


if __name__ == "__main__":
    serve_mcp()
