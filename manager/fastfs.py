"""E/S de archivos al estilo FastCopy:
- Listado nativo FindFirstFileExW (info basica + LARGE_FETCH): menos llamadas al sistema.
- Hilos segun el disco: SSD en paralelo, HDD casi secuencial (evita saltos del cabezal).
- Copiar/mover con el motor nativo de Windows (CopyFileExW / MoveFileExW).
"""
import ctypes
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes

K32 = ctypes.WinDLL("kernel32", use_last_error=True)
INVALID = ctypes.c_void_p(-1).value
FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE = 0x10, 0x400


class WIN32_FIND_DATAW(ctypes.Structure):
    _fields_ = [("dwFileAttributes", wintypes.DWORD), ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME), ("ftLastWriteTime", wintypes.FILETIME),
                ("nFileSizeHigh", wintypes.DWORD), ("nFileSizeLow", wintypes.DWORD),
                ("dwReserved0", wintypes.DWORD), ("dwReserved1", wintypes.DWORD),
                ("cFileName", wintypes.WCHAR * 260), ("cAlternateFileName", wintypes.WCHAR * 14)]


K32.FindFirstFileExW.restype = wintypes.HANDLE
K32.FindFirstFileExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
                                 ctypes.c_void_p, wintypes.DWORD]
K32.FindNextFileW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
K32.FindClose.argtypes = [wintypes.HANDLE]
EPOCH = 116444736000000000


def listdir(d):
    """[(nombre, es_carpeta, tamano, mtime)] de una carpeta, sin seguir enlaces."""
    out = []
    data = WIN32_FIND_DATAW()
    h = K32.FindFirstFileExW("\\\\?\\" + os.path.abspath(d).rstrip("\\") + "\\*", 1, ctypes.byref(data), 0, None, 2)
    if h == INVALID or h is None:
        return out
    try:
        while True:
            n = data.cFileName
            if n not in (".", ".."):
                a = data.dwFileAttributes
                ft = data.ftLastWriteTime
                mt = ((ft.dwHighDateTime << 32 | ft.dwLowDateTime) - EPOCH) / 1e7
                out.append((n, bool(a & FILE_ATTRIBUTE_DIRECTORY) and not a & FILE_ATTRIBUTE_REPARSE,
                            data.nFileSizeHigh << 32 | data.nFileSizeLow, mt))
            if not K32.FindNextFileW(h, ctypes.byref(data)):
                break
    finally:
        K32.FindClose(h)
    return out


# ------------------------------------------------------------------ SSD o HDD
class _SPQ(ctypes.Structure):
    _fields_ = [("PropertyId", wintypes.DWORD), ("QueryType", wintypes.DWORD), ("Extra", ctypes.c_ubyte * 1)]


class _SEEK(ctypes.Structure):
    _fields_ = [("Version", wintypes.DWORD), ("Size", wintypes.DWORD), ("IncursSeekPenalty", wintypes.BOOLEAN)]


_SSD = {}


def is_ssd(path):
    """True si la unidad no tiene penalizacion de busqueda (SSD/NVMe). Ante la duda: False."""
    drive = os.path.splitdrive(os.path.abspath(path))[0].upper()
    if drive in _SSD:
        return _SSD[drive]
    res = False
    try:
        h = K32.CreateFileW("\\\\.\\" + drive, 0, 3, None, 3, 0, None)
        if h and h != INVALID:
            q, o, n = _SPQ(7, 0), _SEEK(), wintypes.DWORD()
            if K32.DeviceIoControl(h, 0x2D1400, ctypes.byref(q), ctypes.sizeof(q), ctypes.byref(o),
                                   ctypes.sizeof(o), ctypes.byref(n), None):
                res = not o.IncursSeekPenalty
            K32.CloseHandle(h)
    except OSError:
        pass
    _SSD[drive] = res
    return res


def threads_for(path, ssd_threads=16, hdd_threads=2):
    return ssd_threads if is_ssd(path) else hdd_threads


# ------------------------------------------------------------------ recorrido
def walk_files(roots, want, skip_dir=None, on_dir=None, stop=None):
    """Recorre varias raices en paralelo: cada disco con su propio numero de hilos.
    want(nombre_minusculas) -> bool. Devuelve [(ruta, tamano, mtime)]."""
    found, lock = [], threading.Lock()
    by_drive = {}
    for r in roots:
        by_drive.setdefault(os.path.splitdrive(os.path.abspath(r))[0].upper(), []).append(r)

    def drive_job(rs):
        n = threads_for(rs[0])
        pending, busy = list(rs), [0]
        cv = threading.Condition()

        def worker():
            while True:
                with cv:
                    while not pending and busy[0]:
                        cv.wait()
                    if not pending or (stop and stop.is_set()):
                        cv.notify_all()
                        return
                    d = pending.pop()
                    busy[0] += 1
                subs, files = [], []
                for name, isdir, size, mt in listdir(d):
                    low = name.lower()
                    if isdir:
                        if not (skip_dir and skip_dir(low, name)):
                            subs.append(os.path.join(d, name))
                    elif want(low):
                        files.append((os.path.join(d, name), size, mt))
                if on_dir:
                    on_dir(d, files)
                with cv:
                    pending.extend(subs)
                    busy[0] -= 1
                    cv.notify_all()
                if files:
                    with lock:
                        found.extend(files)
        ts = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

    jobs = [threading.Thread(target=drive_job, args=(rs,), daemon=True) for rs in by_drive.values()]
    for j in jobs:
        j.start()
    for j in jobs:
        j.join()
    return found


# ------------------------------------------------------------------ copiar / mover
K32.CopyFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                            ctypes.c_void_p, wintypes.DWORD]
K32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]


def copy(src, dst):
    """Copia nativa de Windows (conserva fechas/atributos). No sobrescribe."""
    if not K32.CopyFileExW(src, dst, None, None, None, 0x1):          # COPY_FILE_FAIL_IF_EXISTS
        raise ctypes.WinError(ctypes.get_last_error())


def move(src, dst):
    """Mismo disco: renombrado instantaneo. Otro disco: copia nativa + borrado."""
    if not K32.MoveFileExW(src, dst, 0x2 | 0x8):                       # COPY_ALLOWED | WRITE_THROUGH
        raise ctypes.WinError(ctypes.get_last_error())


def transfer_many(pairs, do_copy, on_done=None):
    """[(src, dst)] en paralelo segun el disco destino (SSD: 6 hilos, HDD: 2). -> (hechos, errores)"""
    done, errors = [], []
    if not pairs:
        return done, errors
    n = 6 if is_ssd(pairs[0][1]) else 2
    lock = threading.Lock()

    def one(pr):
        src, dst = pr
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            (copy if do_copy else move)(src, dst)
            with lock:
                done.append([src, dst])
            if on_done:
                on_done(src, dst)
        except OSError as e:
            with lock:
                errors.append("%s: %s" % (src, e))
    with ThreadPoolExecutor(n) as ex:
        list(ex.map(one, pairs))
    return done, errors
