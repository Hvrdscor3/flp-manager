"""Analisis rapido de .flp.

- Abre el archivo con mmap y recorre los eventos en una sola pasada: solo decodifica
  los pocos que interesan y SALTA los bloques grandes (estados de plugins, audio
  embebido) sin leerlos; Windows nunca trae esas paginas del disco.
- La existencia de samples se resuelve con una cache de listados de carpeta: cada
  carpeta se lista una vez por escaneo y cada unidad ausente se detecta una vez.
"""
import datetime as dt
import mmap
import os
import struct
import threading

WORD, DWORD, TEXT = 64, 128, 192
EV_CONTAINER, EV_FLVERSION = 254, 199
EV_TEMPO, EV_TITLE, EV_SAMPLE, EV_TIMESTAMP = 156, 194, 196, 237
EV_CH_NEW, EV_PATTERN_NEW = 64, 65
WANTED_TEXT = {EV_FLVERSION, EV_TITLE, EV_SAMPLE, EV_TIMESTAMP}
VARIANTS_NEW = ({172: 3}, {})   # FL 25+ escribe el evento 172 con 3 bytes
VARIANTS_OLD = ({}, {172: 3})


class FsCache:
    """Responde 'existe este archivo?' listando cada carpeta una sola vez."""

    def __init__(self):
        self._dirs = {}
        self._drives = {}
        self._lock = threading.Lock()

    def _drive_ok(self, path):
        drive = os.path.splitdrive(path)[0]
        if not drive:
            return True
        key = drive.lower()
        ok = self._drives.get(key)
        if ok is None:
            ok = os.path.exists(drive + "\\")
            self._drives[key] = ok
        return ok

    def _listing(self, folder):
        key = folder.lower()
        names = self._dirs.get(key)
        if names is None:
            try:
                with os.scandir(folder) as it:
                    names = frozenset(e.name.lower() for e in it)
            except OSError:
                names = frozenset()
            with self._lock:
                self._dirs[key] = names
        return names

    def exists(self, path):
        path = os.path.normpath(path)
        if not self._drive_ok(path):
            return False
        folder, name = os.path.split(path)
        return name.lower() in self._listing(folder)


def _fl_date(days):
    return (dt.datetime(1899, 12, 30) + dt.timedelta(days=days)).timestamp()


def _scan_events(mm, start, end, sizes):
    """Una pasada sobre los eventos. Devuelve dict con lo encontrado o lanza ValueError."""
    out = {"version": "", "tempo": None, "title": "", "created": None, "worked_h": None,
           "channels": 0, "patterns": 0, "samples": []}
    i = start
    stack = []  # fin de contenedores 254 abiertos
    while True:
        while stack and i >= stack[-1]:
            if i > stack[-1]:
                raise ValueError("contenedor desalineado")
            stack.pop()
        if i >= end:
            break
        eid = mm[i]
        i += 1
        if eid in sizes:
            i += sizes[eid]
            continue
        if eid < WORD:
            i += 1
        elif eid < DWORD:
            if eid == EV_PATTERN_NEW:
                n = mm[i] | (mm[i + 1] << 8)
                if n > out["patterns"]:
                    out["patterns"] = n
            elif eid == EV_CH_NEW:
                out["channels"] += 1
            i += 2
        elif eid < TEXT:
            if eid == EV_TEMPO and out["tempo"] is None:
                out["tempo"] = round(struct.unpack_from("<I", mm, i)[0] / 1000, 2)
            i += 4
        else:
            n = shift = 0
            while True:
                b = mm[i]
                i += 1
                n |= (b & 0x7F) << shift
                if not b & 0x80:
                    break
                shift += 7
                if shift > 35:
                    raise ValueError("longitud invalida")
            if eid == EV_CONTAINER:
                if i + n > end:
                    raise ValueError("contenedor truncado")
                stack.append(i + n)   # se entra al contenedor: sus eventos siguen aqui
                continue
            if i + n > end:
                raise ValueError("evento truncado")
            if eid in WANTED_TEXT:
                val = mm[i:i + n]
                if eid == EV_SAMPLE:
                    s = val.decode("utf-16-le", "replace").rstrip("\0")
                    if s:
                        out["samples"].append(s)
                elif eid == EV_TITLE:
                    out["title"] = val.decode("utf-16-le", "replace").rstrip("\0")
                elif eid == EV_FLVERSION:
                    out["version"] = val.split(b"\0")[0].decode("ascii", "replace")
                elif eid == EV_TIMESTAMP and n >= 16:
                    c, w = struct.unpack_from("<dd", val)
                    try:
                        if c > 1:
                            out["created"] = _fl_date(c)
                        out["worked_h"] = round(w * 24, 1)
                    except (OverflowError, OSError, ValueError):
                        pass
            i += n
    if i != end:
        raise ValueError("fin desalineado")
    return out


def _peek_version(mm, start):
    if start < len(mm) and mm[start] == EV_FLVERSION:
        n = mm[start + 1]
        return mm[start + 2:start + 2 + n].split(b"\0")[0].decode("ascii", "replace")
    return ""


def analyze(path, fs, placeholders, st=None):
    st = st or os.stat(path)
    info = {"path": path, "name": os.path.splitext(os.path.basename(path))[0],
            "dir": os.path.dirname(path), "size": st.st_size, "mtime": st.st_mtime,
            "version": "", "tempo": None, "title": "", "created": None, "worked_h": None,
            "channels": 0, "patterns": 0, "samples": 0, "missing": [], "error": ""}
    try:
        with open(path, "rb") as f:
            if st.st_size < 30:
                raise ValueError("Archivo vacio o incompleto")
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    except (OSError, ValueError) as e:
        info["error"] = str(e) or "No se pudo abrir"
        return info
    try:
        if mm[:4] != b"FLhd":
            raise ValueError("No es un proyecto FL Studio (.flp)")
        hlen = struct.unpack_from("<I", mm, 4)[0]
        i = 8 + hlen
        if mm[i:i + 4] != b"FLdt":
            raise ValueError("Bloque de datos no encontrado")
        start, end = i + 8, len(mm)
        ver = _peek_version(mm, start)
        major = ver.split(".")[0]
        variants = VARIANTS_NEW if major.isdigit() and int(major) >= 25 else VARIANTS_OLD
        res, err = None, None
        for sizes in variants:
            try:
                res = _scan_events(mm, start, end, sizes)
                break
            except (ValueError, IndexError, struct.error) as e:
                err = e
        if res is None:
            raise ValueError("Proyecto danado o formato desconocido: %s" % err)
    except (ValueError, IndexError, struct.error) as e:
        info["error"] = str(e)
        return info
    finally:
        mm.close()

    seen = set()
    for p in res.pop("samples"):
        k = p.lower()
        if k in seen:
            continue
        seen.add(k)
        info["samples"] += 1
        if not resolve_sample(p, info["dir"], fs, placeholders):
            info["missing"].append(p)
    info.update(res)
    return info


def resolve_sample(p, project_dir, fs, placeholders):
    q = p
    low = q.lower()
    for k, v in placeholders.items():
        if low.startswith(k) and v:
            q = v + q[len(k):]
            break
    if "%" in q:
        q = os.path.expandvars(q)
    q = q.replace("/", "\\")
    if fs.exists(q):
        return True
    # FL tambien busca el sample por nombre en la carpeta del proyecto
    return fs.exists(os.path.join(project_dir, os.path.basename(q)))
