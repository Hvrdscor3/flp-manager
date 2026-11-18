"""Metadata VST: que plugins pide cada proyecto, si estan instalados, version y conflictos.
Tambien extrae presets VST2 (en especial Serum 1) de los proyectos como archivos .fxp.

FL guarda cada plugin en el evento 212/213: u32 tipo + registros (u32 id, u64 tamano, datos):
  51 = ID VST2 (fourcc, al reves)  52 = GUID VST3  54 = nombre  55 = ruta del plugin
  56 = fabricante  57 = version que reporta el plugin  53 = estado (chunk del plugin)
"""
import ctypes
import json
import os
import re
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import core
import flpraw as F

FILE = os.path.join(core.DATA_DIR, "plugins.json")
POOL = ThreadPoolExecutor(6, thread_name_prefix="plugins")
EV_NAME = 203
FL_PREFIX = 21   # bytes que FL antepone al chunk del plugin en el estado


def _records(val):
    if len(val) < 16 or struct.unpack_from("<I", val, 0)[0] not in (8, 10, 11, 12):
        return None
    rec, i = {}, 4
    while i + 12 <= len(val):
        cid, size = struct.unpack_from("<IQ", val, i)
        i += 12
        if size > len(val) - i:
            return None
        rec[cid] = val[i:i + size]
        i += size
    return rec if 54 in rec else None


def _txt(b):
    return bytes(b).split(b"\0")[0].decode("utf-8", "replace").strip()


def plugin_chunk(state):
    """Chunk original del plugin dentro del estado guardado por FL."""
    if len(state) > FL_PREFIX:
        n = struct.unpack_from("<I", state, 9)[0]
        if FL_PREFIX + n == len(state):
            return bytes(state[FL_PREFIX:])
    k = bytes(state).find(b"x\x01")       # respaldo: inicio del bloque zlib
    return bytes(state[k:]) if k >= 0 else b""


WANT = (64, 203, 212, 213)


def _lean(raw, sizes):
    """Una pasada rapida: solo guarda los eventos que interesan (canal, nombre, datos de plugin)."""
    hlen = struct.unpack_from("<I", raw, 4)[0]
    i = 8 + hlen + 8
    end, out, stack = len(raw), [], []
    while True:
        while stack and i >= stack[-1]:
            if i > stack[-1]:
                raise ValueError("desalineado")
            stack.pop()
        if i >= end:
            break
        eid = raw[i]
        i += 1
        if eid in sizes:
            i += sizes[eid]
            continue
        if eid < 64:
            i += 1
        elif eid < 128:
            if eid == 64:
                out.append((64, b""))
            i += 2
        elif eid < 192:
            i += 4
        else:
            n = shift = 0
            while True:
                b = raw[i]
                i += 1
                n |= (b & 0x7F) << shift
                if not b & 0x80:
                    break
                shift += 7
                if shift > 35:
                    raise ValueError("longitud invalida")
            if i + n > end:
                raise ValueError("truncado")
            if eid == 254:
                stack.append(i + n)
                continue
            if eid in WANT:
                out.append((eid, raw[i:i + n]))
            i += n
    if i != end:
        raise ValueError("fin desalineado")
    return out


def _events(path):
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:4] != b"FLhd":
        raise ValueError("No es .flp")
    major = F.fl_version(raw).split(".")[0]
    order = [{172: 3}, {}] if major.isdigit() and int(major) >= 25 else [{}, {172: 3}]
    for sizes in order:
        try:
            return _lean(raw, sizes)
        except (ValueError, IndexError, struct.error):
            pass
    _, events = F.load(path)                       # respaldo: lector completo
    return [(lst[k][0], lst[k][1]) for lst, k in F.walk(events) if lst[k][0] in WANT]


def scan_project(path, with_state=False):
    """Lista de plugins del proyecto (en orden), con el nombre del canal/slot si se conoce."""
    flat = _events(path)
    out = []
    for i, (eid, val) in enumerate(flat):
        if eid not in (212, 213) or isinstance(val, list):
            continue
        rec = _records(val)
        if not rec:
            continue
        slot = ""
        for j in range(i - 1, max(-1, i - 14), -1):          # canal: el nombre viene antes
            if flat[j][0] == EV_NAME and not isinstance(flat[j][1], list):
                slot = F.text(flat[j][1])
                break
            if flat[j][0] == 64 or (flat[j][0] == 213 and j != i) or j < i - 4:
                break
        if not slot:
            for j in range(i + 1, min(len(flat), i + 12)):   # mixer: a veces viene despues
                if flat[j][0] == EV_NAME and not isinstance(flat[j][1], list):
                    slot = F.text(flat[j][1])
                    break
                if flat[j][0] in (212, 213, 64):
                    break
        ppath = _txt(rec.get(55, b""))
        fourcc = bytes(rec[51][::-1]).decode("latin-1") if 51 in rec and len(rec[51]) == 4 else ""
        p = {"name": _txt(rec[54]), "vendor": _txt(rec.get(56, b"")), "path": ppath,
             "fmt": "VST3" if ppath.lower().endswith(".vst3") or 52 in rec and 51 not in rec else
             ("CLAP" if ppath.lower().endswith(".clap") else "VST2"),
             "id": fourcc or (rec[52].hex() if 52 in rec else ""),
             "sdk": (lambda v: "%d.%d" % (v // 1000, v % 1000 // 100))(struct.unpack("<I", rec[57])[0])
             if len(rec.get(57, b"")) == 4 else "",
             "slot": slot, "state": len(rec.get(53, b""))}
        if with_state:
            p["chunk"] = plugin_chunk(rec.get(53, b""))
        out.append(p)
    return out


# ------------------------------------------------------------------ plugins instalados
VST_DIRS = [r"C:\Program Files\Common Files\VST3", r"C:\Program Files\Common Files\VST2",
            r"C:\Program Files\VSTPlugins", r"C:\Program Files\Steinberg\VSTPlugins",
            r"C:\Program Files\Common Files\CLAP", r"C:\Program Files\Image-Line\FL Studio\Plugins\VST"]


def file_version(path):
    """Version del archivo (recurso de Windows). Para .vst3 de carpeta busca el binario interno."""
    if os.path.isdir(path):
        inner = os.path.join(path, "Contents", "x86_64-win", os.path.basename(path))
        path = inner if os.path.isfile(inner) else path
    try:
        v = ctypes.windll.version
        n = v.GetFileVersionInfoSizeW(path, None)
        if not n:
            return ""
        buf = ctypes.create_string_buffer(n)
        if not v.GetFileVersionInfoW(path, 0, n, buf):
            return ""
        p, ln = ctypes.c_void_p(), ctypes.c_uint()
        if not v.VerQueryValueW(buf, "\\", ctypes.byref(p), ctypes.byref(ln)):
            return ""
        ms, ls = struct.unpack_from("<II", ctypes.string_at(p.value, ln.value), 8)
        return "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except (OSError, AttributeError, struct.error):
        return ""


def pe_bits(path):
    try:
        with open(path, "rb") as f:
            h = f.read(4096)
        off = struct.unpack_from("<I", h, 0x3C)[0]
        m = struct.unpack_from("<H", h, off + 4)[0]
        return 64 if m == 0x8664 else 32 if m == 0x14C else None
    except (OSError, struct.error):
        return None


def _norm(n):
    n = re.sub(r"\.(dll|vst3|clap)$", "", n.lower())
    return re.sub(r"[\s_\-()]+|x64|64 ?bit|vst3?", "", n)


class PluginIndex:
    def __init__(self, mgr):
        self.mgr = mgr
        self.cache = {}      # flp -> [mtime, [plugins]]
        self.inst = None     # plugins instalados
        self.state = {"running": False, "done": 0, "total": 0}
        self.rev = 1
        self.mgr_rev = 0
        try:
            with open(FILE, encoding="utf-8") as f:
                self.cache = json.load(f).get("cache", {})
        except (OSError, ValueError):
            pass

    def save(self):
        tmp = FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"cache": self.cache}, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, FILE)

    # ---- escaneo (solo proyectos nuevos o cambiados)
    def refresh(self):
        if self.state["running"]:
            return
        self.state.update(running=True, done=0, total=0)
        self.mgr_rev = self.mgr.rev
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        st = self.state
        st.update(running=True, done=0)
        try:
            todo = []
            for p in list(self.mgr.projects):
                if core.is_backup(p):
                    continue                       # los autoguardados repiten los mismos plugins
                try:
                    mt = os.stat(p).st_mtime
                except OSError:
                    continue
                c = self.cache.get(p)
                if not c or c[0] != mt:
                    todo.append((p, mt))
            for p in [p for p in self.cache if p not in self.mgr.projects or core.is_backup(p)]:
                del self.cache[p]
            st["total"] = len(todo)

            def one(item):
                p, mt = item
                try:
                    return p, [mt, scan_project(p)]
                except Exception:  # noqa: BLE001
                    return p, [mt, []]
            last = time.time()
            for p, v in POOL.map(one, todo):
                self.cache[p] = v
                st["done"] += 1
                if time.time() - last > 20:          # guardar avance (se puede cerrar la app)
                    self.save()
                    last = time.time()
            self.inst = None
            self.rev += 1
            self.save()
        finally:
            st["running"] = False

    # ---- instalados
    def installed(self):
        if self.inst is not None:
            return self.inst
        dirs = set(VST_DIRS)
        for _, pl in list(self.cache.values()):     # tambien las carpetas que usan tus proyectos (E:\VSTS...)
            for x in pl:
                d = os.path.dirname(x["path"])
                if d:
                    dirs.add(d.split("\\Contents\\")[0] if "\\Contents\\" in d else d)
        files = {}
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for dp, dn, fn in os.walk(d):
                for n in dn + fn:
                    low = n.lower()
                    if low.endswith((".vst3", ".dll", ".clap")) and (low.endswith(".vst3") or n in fn):
                        files.setdefault(low, os.path.join(dp, n))
                dn[:] = [x for x in dn if not x.lower().endswith(".vst3") and x.lower() != "contents"]
        by_norm = {}
        for low, p in files.items():
            by_norm.setdefault(_norm(low), []).append(p)
        self.inst = {"files": files, "norm": by_norm}
        return self.inst

    def status(self, pl):
        """ok | movido | alternativa | falta (+ detalles)."""
        inst = self.installed()
        path = pl["path"]
        if path and os.path.exists(path):
            st, where = "ok", path
        else:
            where = inst["files"].get(os.path.basename(path).lower()) if path else None
            st = "movido" if where else None
            if not st:
                alts = inst["norm"].get(_norm(os.path.basename(path) or pl["name"]), []) or \
                    inst["norm"].get(_norm(pl["name"]), [])
                where = alts[0] if alts else None
                st = "alternativa" if where else "falta"
        info = {"status": st, "found": where or "", "fmtFound": "",
                "version": file_version(where) if where else ""}
        if where:
            info["fmtFound"] = "VST3" if where.lower().endswith(".vst3") else "CLAP" if where.lower().endswith(".clap") else "VST2"
            if info["fmtFound"] == "VST2" and os.path.isfile(where):
                info["bits"] = pe_bits(where)
        return info

    def summary(self):
        last = getattr(self, "_sum", (0, None))
        if self.state["running"]:          # mientras lee: resultados parciales cada 4 s
            if last[1] is None or time.time() - getattr(self, "_sum_t", 0) > 4:
                self._sum = (-1, self._summary())
                self._sum_t = time.time()
            return dict(self._sum[1], scan=dict(self.state), export=dict(EXPORT), rev=self.rev)
        if last[0] == self.rev:
            return dict(last[1], scan=dict(self.state), export=dict(EXPORT))
        r = self._summary()
        self._sum = (self.rev, r)
        return dict(r, export=dict(EXPORT))

    def _summary(self):
        agg = {}
        for proj, (_, pl) in list(self.cache.items()):
            for x in pl:
                k = (x["name"].lower(), x["fmt"])
                a = agg.setdefault(k, {"name": x["name"], "vendor": x["vendor"], "fmt": x["fmt"], "paths": set(),
                                       "sdk": set(), "projects": set(), "uses": 0, "serum": False})
                a["paths"].add(x["path"])
                if x.get("sdk"):
                    a["sdk"].add(x["sdk"])
                a["projects"].add(proj)
                a["uses"] += 1
                a["serum"] = a["serum"] or (x["id"] == "XfsX")
        out = []
        vst_cache = {}
        for a in agg.values():
            paths = sorted(a["paths"])
            st = vst_cache.get(paths[0]) or self.status({"name": a["name"], "path": paths[0]})
            vst_cache[paths[0]] = st
            other = [b["fmt"] for b in agg.values() if b["name"].lower() == a["name"].lower() and b is not a]
            out.append({"name": a["name"], "vendor": a["vendor"], "fmt": a["fmt"], "paths": paths,
                        "sdk": sorted(a["sdk"]), "projects": len(a["projects"]), "uses": a["uses"],
                        "serum": a["serum"], "otherFmt": other, **st,
                        "sample": [os.path.splitext(os.path.basename(p))[0] for p in sorted(a["projects"])[:6]]})
        out.sort(key=lambda x: (-x["projects"], x["name"].lower()))
        return {"plugins": out, "projects": len(self.cache), "scan": dict(self.state), "rev": self.rev,
                "missing": sum(1 for x in out if x["status"] == "falta"),
                "alt": sum(1 for x in out if x["status"] in ("alternativa", "movido")),
                "bits32": sum(1 for x in out if x.get("bits") == 32)}

    def projects_using(self, name, fmt):
        out = []
        for proj, (_, pl) in list(self.cache.items()):
            hits = [x for x in pl if x["name"].lower() == name.lower() and x["fmt"] == fmt]
            if hits:
                out.append({"path": proj, "name": os.path.splitext(os.path.basename(proj))[0],
                            "slots": [h["slot"] for h in hits][:8], "count": len(hits)})
        out.sort(key=lambda x: x["name"].lower())
        return out

    def for_project(self, proj):
        c = self.cache.get(proj)
        try:
            mt = os.stat(proj).st_mtime
        except OSError:
            mt = None
        if not c or c[0] != mt:
            c = [mt, scan_project(proj)]
            self.cache[proj] = c
        return [dict(x, **self.status(x)) for x in c[1]]


# ------------------------------------------------------------------ presets (.fxp)
def fxp(fourcc, chunk, name):
    nm = name.encode("ascii", "replace")[:27].ljust(28, b"\0")
    body = b"FPCh" + struct.pack(">I", 1) + fourcc.encode("latin-1") + struct.pack(">II", 1, 1) + nm + \
        struct.pack(">I", len(chunk)) + chunk
    return b"CcnK" + struct.pack(">I", len(body)) + body


def _safe(s):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .")[:80] or "Preset"


def serum2_dir():
    base = os.path.join(os.path.expanduser("~"), "Documents", "Xfer")
    for n in ("Serum 2 Presets", "Serum Presets"):
        d = os.path.join(base, n)
        if os.path.isdir(d):
            return os.path.join(d, "Presets", "User", "Desde proyectos FL")
    return os.path.join(base, "Serum 2 Presets", "Presets", "User", "Desde proyectos FL")


EXPORT = {"running": False, "done": 0, "total": 0, "exported": 0, "duplicates": 0, "dest": ""}


def export_async(projects, open_folder=True):
    if EXPORT["running"]:
        return False

    def run():
        EXPORT.update(running=True, done=0, total=len(projects), exported=0, duplicates=0)
        try:
            r = export_presets(projects, progress=EXPORT)
            EXPORT.update(exported=r["exported"], duplicates=r["duplicates"], dest=r["dest"])
            if r["exported"] and open_folder:
                os.startfile(r["dest"])  # noqa: S606
        finally:
            EXPORT["running"] = False
    threading.Thread(target=run, daemon=True).start()
    return True


def export_presets(projects, dest=None, only_serum=True, progress=None):
    """Extrae los presets VST2 (Serum 1 por defecto) de cada proyecto a .fxp. Omite duplicados exactos."""
    import hashlib
    dest = dest or serum2_dir()
    seen, n, skipped, errors = set(), 0, 0, []
    if os.path.isdir(dest):                    # lo que ya se exporto antes no se repite
        for dp, _, fn in os.walk(dest):
            for f in fn:
                if f.lower().endswith(".fxp"):
                    try:
                        with open(os.path.join(dp, f), "rb") as fh:
                            seen.add(hashlib.sha1(fh.read()[60:]).hexdigest())
                    except OSError:
                        pass
    for proj in projects:
        if progress is not None:
            progress["done"] += 1
            progress["exported"], progress["duplicates"] = n, skipped
        try:
            pls = scan_project(proj, with_state=True)
        except Exception as e:  # noqa: BLE001
            errors.append("%s: %s" % (proj, e))
            continue
        pname = os.path.splitext(os.path.basename(proj))[0]
        for k, x in enumerate(pls):
            if x["fmt"] != "VST2" or len(x["id"]) != 4 or not x.get("chunk"):
                continue
            if only_serum and x["id"] != "XfsX":
                continue
            h = hashlib.sha1(x["chunk"]).hexdigest()
            if h in seen:
                skipped += 1
                continue
            seen.add(h)
            sub = dest if only_serum else os.path.join(dest, _safe(x["name"]))
            os.makedirs(sub, exist_ok=True)
            label = x["slot"] if x["slot"] and x["slot"].lower() != x["name"].lower() else "%s %d" % (x["name"], k + 1)
            out = core._unique(os.path.join(sub, _safe("%s - %s" % (pname, label)) + ".fxp"))
            with open(out, "wb") as f:
                f.write(fxp(x["id"], x["chunk"], label))
            n += 1
    return {"exported": n, "duplicates": skipped, "errors": errors, "dest": dest}
