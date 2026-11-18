"""Bounce preview: exporta un .flp a MP3 con el motor de FL Studio (linea de comandos).

- Un render a la vez (FL no admite dos instancias de render).
- No toca la carpeta del proyecto: la salida va a %APPDATA%\\FLP Manager\\previews (/O).
- Cache por ruta+fecha+tamano: si el proyecto no cambio, la vista previa sale al instante.
- Cierra solo los avisos de FL (samples/plugins faltantes) para no bloquear el render.
"""
import ctypes
import glob
import hashlib
import os
import queue
import shutil
import subprocess
import threading
import time
from ctypes import wintypes

import core

PREV_DIR = os.path.join(core.DATA_DIR, "previews")
os.makedirs(PREV_DIR, exist_ok=True)
TIMEOUT = 600  # segundos maximos por render

_U = ctypes.windll.user32
_ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
WM_CLOSE, BM_CLICK = 0x0010, 0x00F5
DIALOG_CLASSES = ("#32770", "TMessageForm", "TMsgForm", "TTaskDialog")

STATE = {}  # path -> {state, t0, elapsed, error, warnings, pos}
_Q = queue.Queue()
_LOCK = threading.Lock()
_ORDER = []  # cola visible (paths en espera)


def key(path):
    st = os.stat(path)
    raw = "%s|%d|%d" % (os.path.normcase(path), int(st.st_mtime), st.st_size)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def file_for(k):
    if not k.isalnum():
        return ""
    f = os.path.join(PREV_DIR, k + ".mp3")
    return f if os.path.exists(f) else ""


def fl_running():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq FL64.exe", "/NH"], capture_output=True,
                         text=True, creationflags=0x08000000).stdout
    return "fl64.exe" in out.lower()


def state(path):
    with _LOCK:
        s = dict(STATE.get(path) or {})
        if s.get("state") == "queued" and path in _ORDER:
            s["pos"] = _ORDER.index(path) + 1
    try:
        k = key(path)
    except OSError:
        return {"state": "error", "error": "El archivo ya no existe"}
    if file_for(k):
        s.update(state="done", key=k)
    elif s.get("state") == "done":
        s = {}
    if s.get("state") == "rendering":
        s["elapsed"] = round(time.time() - s["t0"], 1)
    return s or {"state": "none"}


def request(path):
    with _LOCK:
        cur = (STATE.get(path) or {}).get("state")
        if cur in ("queued", "rendering"):
            return
        STATE[path] = {"state": "queued"}
        _ORDER.append(path)
    _Q.put(path)


def _dismiss_dialogs(pid, seen):
    """Cierra ventanas de dialogo (avisos) del proceso de render."""
    def cb(h, _):
        p = wintypes.DWORD()
        _U.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value != pid or not _U.IsWindowVisible(h):
            return True
        cls = ctypes.create_unicode_buffer(128)
        _U.GetClassNameW(h, cls, 128)
        if cls.value in DIALOG_CLASSES:
            t = ctypes.create_unicode_buffer(256)
            _U.GetWindowTextW(h, t, 256)
            seen.add(t.value or cls.value)
            _U.PostMessageW(h, WM_CLOSE, 0, 0)
        return True
    _U.EnumWindows(_ENUM(cb), 0)


def _render(path):
    exe = core.FL.get("exe")
    if not exe or not os.path.exists(exe):
        raise RuntimeError("No se encontró FL Studio instalado")
    if fl_running():
        raise RuntimeError("Cierra FL Studio para renderizar (FL no exporta por comandos si ya está abierto)")
    k = key(path)
    tmp = os.path.join(PREV_DIR, "tmp_" + k)
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    cmd = '"%s" /Emp3 /O"%s" /R "%s"' % (exe, tmp, path)
    si = subprocess.STARTUPINFO()
    si.dwFlags = 1  # STARTF_USESHOWWINDOW
    si.wShowWindow = 7  # minimizada sin robar el foco
    proc = subprocess.Popen(cmd, startupinfo=si, creationflags=0x00008000)  # ABOVE_NORMAL
    seen, t0 = set(), time.time()
    try:
        while proc.poll() is None:
            if time.time() - t0 > TIMEOUT:
                proc.kill()
                raise RuntimeError("El render tardó demasiado y se canceló")
            _dismiss_dialogs(proc.pid, seen)
            time.sleep(0.4)
        mp3 = glob.glob(os.path.join(tmp, "*.mp3"))
        if not mp3 or os.path.getsize(mp3[0]) < 1000:
            raise RuntimeError("FL no generó audio (¿proyecto vacío o dañado?)")
        os.replace(mp3[0], os.path.join(PREV_DIR, k + ".mp3"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return sorted(seen)


def _worker():
    while True:
        path = _Q.get()
        with _LOCK:
            if path in _ORDER:
                _ORDER.remove(path)
            STATE[path] = {"state": "rendering", "t0": time.time()}
        try:
            warns = _render(path)
            with _LOCK:
                STATE[path] = {"state": "done", "elapsed": round(time.time() - STATE[path]["t0"], 1),
                               "warnings": warns}
        except Exception as e:  # noqa: BLE001
            with _LOCK:
                STATE[path] = {"state": "error", "error": str(e)}


threading.Thread(target=_worker, daemon=True).start()
