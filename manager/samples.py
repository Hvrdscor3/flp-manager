"""Administrar samples: escaneo por unidad, duplicados, uso en proyectos, categorias y organizacion.

Disenado para usar poca CPU:
- Solo se abre cada audio una vez (cabecera WAV/AIFF); despues se usa la cache (ruta+fecha+tamano).
- Las huellas (hash) solo se calculan para archivos con el MISMO tamano que otro: rapido y exacto.
"""
import hashlib
import json
import os
import re
import shutil
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import core
import fastfs
import fastscan


def want_audio(low):
    return not low.startswith("._") and os.path.splitext(low)[1] in EXTS


def skip_dir(low, name):
    return low in core.SKIP_DIRS or low == "__macosx" or low.startswith(".")

FILE = os.path.join(core.DATA_DIR, "samples.json")
EXTS = {".wav", ".mp3", ".flac", ".aif", ".aiff", ".ogg"}
POOL = ThreadPoolExecutor(8, thread_name_prefix="samples")

# Orden importa: lo mas especifico primero.
CATS = [
    ("Loops/Drums", r"(drum|perc|top|hat|break|groove)s?[ _\-]*loop|loop[ _\-]*(drum|perc|top)|break ?beat"),
    ("Loops/Melodías", r"loop|melod|chord|progression|\d{2,3} ?bpm"),
    ("Drums/Open Hats", r"open[ _\-]?(h(i|igh)[ _\-]?)?hat|\boh\b"),
    ("Drums/Hi-Hats", r"h(i|igh)[ _\-]?hat|hihat|\bhh\b|\bhat"),
    ("Drums/Kicks", r"kick|\bkck\b|\bbd\b|bassdrum"),
    ("808 & Bass", r"808|\bsub\b|bass"),
    ("Drums/Snares", r"snare|\bsnr\b|\bsd\b|rimshot|\brim\b"),
    ("Drums/Claps", r"clap|\bclp\b|snap"),
    ("Drums/Cymbals", r"crash|ride|cymbal|splash"),
    ("Drums/Percs", r"perc|conga|bongo|\btoms?\b|shaker|tamb|cowbell|clave|wood ?block|triangle"),
    ("Vocals", r"vocal|\bvox\b|acapella|a cappella|chant|adlib|voice|shout"),
    ("FX", r"\bfx\b|sfx|riser|sweep|impact|transition|whoosh|noise|uplifter|downlifter|reverse|siren|scratch|vinyl|foley"),
    ("Melódicos", r"piano|keys|guitar|synth|pad|arp|lead|bell|pluck|string|brass|flute|organ|choir"),
]
CATS_RE = [(c, re.compile(p, re.I)) for c, p in CATS]
BPM_RE = re.compile(r"(\d{2,3}) ?bpm|bpm ?(\d{2,3})", re.I)
KEY_RE = re.compile(r"(?:^|[ _\-(\[])([A-G](?:#|b)?) ?(maj(?:or)?|min(?:or)?|m)(?=$|[ _\-)\].])", re.I)
COPY_RE = re.compile(r"(\s*\(\d+\)|\s*-\s*cop(y|ia)(\s*\(\d+\))?|\s*cop(y|ia)\s*\d*|[ _\-]+\d+)$", re.I)


def categorize(path):
    name = os.path.splitext(os.path.basename(path))[0]
    for c, rx in CATS_RE:            # primero el nombre del archivo
        if rx.search(name):
            return c
    parent = " ".join(path.split("\\")[-3:-1])
    for c, rx in CATS_RE:            # despues las carpetas que lo contienen
        if rx.search(parent):
            return c
    return "Sin clasificar"


def tags(path):
    name = os.path.splitext(os.path.basename(path))[0]
    m = BPM_RE.search(name)
    bpm = int(m.group(1) or m.group(2)) if m else None
    if bpm and not 50 <= bpm <= 220:
        bpm = None
    k = KEY_RE.search(name)
    key = None
    if k:
        suf = k.group(2).lower()
        key = k.group(1)[0].upper() + k.group(1)[1:] + ("m" if suf == "m" or suf.startswith("min") else "")
    return bpm, key


def norm_name(path):
    n = os.path.splitext(os.path.basename(path))[0].lower().strip()
    prev = None
    while prev != n:
        prev = n
        n = COPY_RE.sub("", n).strip()
    return n


def audio_info(path):
    """Duracion/formato leyendo solo la cabecera (WAV y AIFF). None si no se puede."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
                fmt, data = None, None
                while True:
                    h = f.read(8)
                    if len(h) < 8:
                        break
                    cid, size = h[:4], struct.unpack("<I", h[4:])[0]
                    if cid == b"fmt ":
                        fmt = struct.unpack("<HHIIHH", f.read(16))
                        f.seek(size - 16 + (size & 1), 1)
                    elif cid == b"data":
                        data = size
                        break
                    else:
                        f.seek(size + (size & 1), 1)
                if fmt and data is not None:
                    ch, sr, bits = fmt[1], fmt[2], fmt[5]
                    bps = fmt[3] or 1
                    return {"dur": round(data / bps, 3), "sr": sr, "ch": ch, "bits": bits}
                return {"bad": True}
            if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
                while True:
                    h = f.read(8)
                    if len(h) < 8:
                        break
                    cid, size = h[:4], struct.unpack(">I", h[4:])[0]
                    if cid == b"COMM":
                        c = f.read(18)
                        ch, frames, bits = struct.unpack(">hIh", c[:8])
                        e, mant = struct.unpack(">HQ", c[8:18])
                        sr = mant * 2.0 ** ((e & 0x7FFF) - 16383 - 63) if mant else 0
                        return {"dur": round(frames / sr, 3) if sr else None, "sr": int(sr), "ch": ch, "bits": bits}
                    f.seek(size + (size & 1), 1)
                return {"bad": True}
    except (OSError, struct.error):
        return {"bad": True}
    return {}


def _hash(path, quick):
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        if quick:
            h.update(f.read(65536))
            size = os.fstat(f.fileno()).st_size
            if size > 131072:
                f.seek(-65536, 2)
                h.update(f.read(65536))
        else:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


class _Rec:
    """Falso FsCache: registra las rutas de samples que pide el analizador de .flp."""
    def __init__(self):
        self.paths = []

    def exists(self, p):
        self.paths.append(p)
        return True


def sample_refs(flp, ph):
    rec = _Rec()
    fastscan.analyze(flp, rec, ph)
    return sorted({os.path.normcase(p) for p in rec.paths})


def sid(path):
    return hashlib.sha1(os.path.normcase(path).encode("utf-8")).hexdigest()[:16]


class SampleManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.files = {}     # ruta -> info
        self.cats = {}      # ruta -> categoria elegida a mano
        self.refs = {}      # flp -> [mtime, [samples]]
        self.undo = []
        self.jobs = {}      # raiz -> {state, stage, dirs, found, analyzed, cached, hashed, elapsed}
        self.ids = {}
        self.rev = 1
        self._cache = (0, None)
        self._catc = {}             # categoria automatica por ruta (regex: se calcula una vez)
        self._use = (None, None)    # (firma de refs, uso)
        self._save_cost = 0.0
        self._idx_t = self._idx_cost = 0.0
        self._stop = {}
        self._last_save = 0
        try:
            with open(FILE, encoding="utf-8") as f:
                d = json.load(f)
            self.files, self.cats = d.get("files", {}), d.get("cats", {})
            self.refs, self.undo = d.get("refs", {}), d.get("undo", [])
            for r, j in d.get("jobs", {}).items():
                j["state"] = "paused" if j.get("state") == "running" else j.get("state", "done")
                self.jobs[r] = j
        except (OSError, ValueError):
            pass
        for p in [p for p in self.files if core.is_fl_content(p)]:
            del self.files[p]
            self.cats.pop(p, None)
        self.ids = {sid(p): p for p in self.files}

    def save(self):
        t = time.time()
        with self.lock:             # copia rapida; serializar fuera del candado
            snap = {"files": dict(self.files), "cats": dict(self.cats), "refs": dict(self.refs),
                    "undo": list(self.undo), "jobs": {k: dict(v) for k, v in self.jobs.items()}}
        tmp = FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, FILE)
        self._last_save = time.time()
        self._save_cost = self._last_save - t

    def _save_due(self):
        # guardar cuesta CPU: con librerias grandes se guarda menos seguido (max ~5% del tiempo)
        return time.time() - self._last_save > max(15, self._save_cost * 20)

    # ---------------------------------------------------------------- escaneo
    def status(self):
        return {"running": any(j["state"] == "running" for j in self.jobs.values()),
                "jobs": list(self.jobs.values()), "rev": self.rev}

    def start(self, roots):
        for r in roots:
            if self.jobs.get(r, {}).get("state") == "running":
                continue
            self.jobs[r] = {"root": r, "state": "running", "stage": "Buscando archivos", "dirs": 0,
                            "found": 0, "analyzed": 0, "cached": 0, "hashed": 0, "elapsed": 0}
            self._stop[r] = threading.Event()
            threading.Thread(target=self._run, args=(r,), daemon=True).start()

    def pause(self, r):
        if r in self._stop:
            self._stop[r].set()

    def _walk(self, d):
        subs, out = [], []
        try:
            with os.scandir(d) as it:
                entries = list(it)
            if any(e.name.lower() in core.FL_MARKERS for e in entries):
                return [], []          # instalacion de FL: sus samples de fabrica no se tocan
            for e in entries:
                try:
                    if e.name.startswith("._"):
                        continue                  # metadatos de macOS, no son audio
                    if e.is_dir(follow_symlinks=False):
                        if e.name.lower() not in core.SKIP_DIRS and e.name.lower() != "__macosx":
                            subs.append(e.path)
                    elif os.path.splitext(e.name)[1].lower() in EXTS:
                        st = e.stat()
                        out.append((e.path, st.st_mtime, st.st_size))
                except OSError:
                    pass
        except OSError:
            pass
        return subs, out

    def _run(self, root):
        j, stop, t0 = self.jobs[root], self._stop[root], time.time()
        seen = set()
        try:
            # 1) recorrer carpetas: API nativa, hilos segun SSD/HDD (estilo FastCopy)
            new = []

            def on_dir(d, files):
                j["dirs"] += 1
                j["found"] += len(files)
                j["elapsed"] = time.time() - t0
            found = fastfs.walk_files([root], want_audio, skip_dir, on_dir, stop)
            for p, sz, mt in found:
                if core.is_fl_content(p):          # FL (salvo renders y sliced): no se toca
                    j["found"] -= 1
                    continue
                seen.add(p)
                old = self.files.get(p)
                if old and abs(old["mtime"] - mt) < 2 and old["size"] == sz:
                    j["cached"] += 1
                else:
                    new.append((p, mt, sz))
            # 2) leer cabeceras de los nuevos
            j["stage"] = "Leyendo audio"

            def info(item):
                p, mt, sz = item
                d = {"size": sz, "mtime": mt, "ext": os.path.splitext(p)[1].lower()[1:]}
                d.update(audio_info(p) if sz else {"bad": True})
                d["bpm"], d["key"] = tags(p)
                return p, d
            for i in range(0, len(new), 256):
                if stop.is_set():
                    break
                for p, d in POOL.map(info, new[i:i + 256]):
                    with self.lock:
                        self.files[p] = d
                        self.ids[sid(p)] = p
                    j["analyzed"] += 1
                self.rev += 1
                j["elapsed"] = time.time() - t0
                if self._save_due():
                    self.save()
            if stop.is_set():
                j["state"] = "paused"
                return
            # quitar los que ya no existen bajo esta raiz
            rl = root.lower().rstrip("\\") + "\\"
            with self.lock:
                for p in [p for p in self.files if p.lower().startswith(rl) and p not in seen]:
                    del self.files[p]
            # 3) duplicados: hash solo de tamanos repetidos
            j["stage"] = "Buscando duplicados"
            self._hash_dups(j, stop)
            # 4) que samples usa cada proyecto
            j["stage"] = "Revisando uso en proyectos"
            self._update_refs(stop)
            j["state"] = "paused" if stop.is_set() else "done"
            j["stage"] = ""
        except Exception as e:  # noqa: BLE001
            j["state"], j["stage"] = "error", str(e)
        finally:
            j["elapsed"] = time.time() - t0
            self.rev += 1
            self.save()

    def _hash_dups(self, j, stop):
        by_size = {}
        for p, d in self.files.items():
            if d["size"] > 0:
                by_size.setdefault(d["size"], []).append(p)
        cand = [p for ps in by_size.values() if len(ps) > 1 for p in ps]

        def qh(p):
            d = self.files.get(p)
            if d and not d.get("qh"):
                try:
                    d["qh"] = _hash(p, True)
                except OSError:
                    d["qh"] = "x" + p
            return p
        for i in range(0, len(cand), 256):
            if stop.is_set():
                return
            list(POOL.map(qh, cand[i:i + 256]))
            j["hashed"] = min(len(cand), i + 256)
        by_q = {}
        for p in cand:
            d = self.files.get(p)
            if d and d.get("qh"):
                by_q.setdefault((d["size"], d["qh"]), []).append(p)
        full = [p for ps in by_q.values() if len(ps) > 1 for p in ps if not self.files[p].get("fh")]

        def fh(p):
            try:
                self.files[p]["fh"] = _hash(p, False)
            except (OSError, KeyError):
                pass
        for i in range(0, len(full), 64):
            if stop.is_set():
                return
            list(POOL.map(fh, full[i:i + 64]))

    def _update_refs(self, stop):
        ph = core.placeholders()
        projects = list(core_mgr().projects)
        todo = []
        for flp in projects:
            try:
                mt = os.stat(flp).st_mtime
            except OSError:
                continue
            r = self.refs.get(flp)
            if not r or r[0] != mt:
                todo.append((flp, mt))
        for flp in [k for k in self.refs if k not in core_mgr().projects]:
            del self.refs[flp]

        def one(item):
            flp, mt = item
            try:
                return flp, [mt, sample_refs(flp, ph)]
            except Exception:  # noqa: BLE001
                return flp, [mt, []]
        for i in range(0, len(todo), 64):
            if stop.is_set():
                return
            for flp, v in POOL.map(one, todo[i:i + 64]):
                self.refs[flp] = v

    # ---------------------------------------------------------------- consultas
    def _usage(self):
        sig = (len(self.refs), sum(v[0] for v in self.refs.values()))
        if self._use[0] == sig:
            return self._use[1]
        use = {}
        for flp, (_, paths) in list(self.refs.items()):
            for p in paths:
                use.setdefault(p, []).append(flp)
        self._use = (sig, use)
        return use

    def index(self):
        """Indice calculado una vez por revision: categorias, uso y grupos de duplicados."""
        if self._cache[0] == self.rev:
            return self._cache[1]
        # mientras escanea, reutiliza el indice reciente (recalcularlo cuesta CPU con librerias grandes)
        if self._cache[1] is not None and self.status()["running"] and \
                time.time() - self._idx_t < max(5, self._idx_cost * 15):
            return self._cache[1]
        rev, t0 = self.rev, time.time()
        with self.lock:
            files = dict(self.files)
        use = self._usage()
        cat, exact, names, cc = {}, {}, {}, self._catc
        for p, d in files.items():
            k = cc.get(p)
            if k is None:
                k = cc[p] = (categorize(p), norm_name(p))
            c = self.cats.get(p) or k[0]
            if c == "Sin clasificar" and ((d.get("dur") or 0) > 45 or (d.get("ext") == "mp3" and d["size"] > 1500000)):
                c = "Canciones y renders"   # audios largos: temas, bounces, referencias
            cat[p] = c
            if d.get("fh"):
                exact.setdefault((d["size"], d["fh"]), []).append(p)
            names.setdefault(k[1], []).append(p)
        dups = [sorted(ps, key=lambda x: (-len(use.get(os.path.normcase(x), [])), len(x)))
                for ps in exact.values() if len(ps) > 1]
        dups.sort(key=lambda ps: -files[ps[0]]["size"] * (len(ps) - 1))
        in_dup = {p for ps in dups for p in ps}
        similar = []
        for n, ps in names.items():
            if len(ps) > 1 and len(n) > 2 and len(ps) <= 12:
                hashes = {files[p].get("fh") or files[p].get("qh") or str(files[p]["size"]) for p in ps}
                if len(hashes) > 1 and not all(p in in_dup for p in ps):
                    similar.append(sorted(ps, key=lambda x: -files[x]["mtime"]))
        similar.sort(key=lambda ps: -len(ps))
        idx = {"files": files, "cat": cat, "use": use, "dups": dups, "similar": similar}
        self._cache = (rev, idx)
        self._idx_t, self._idx_cost = time.time(), time.time() - t0
        return idx

    def item(self, p, idx):
        d = idx["files"][p]
        u = idx["use"].get(os.path.normcase(p), [])
        return {"id": sid(p), "path": p, "name": os.path.basename(p), "dir": os.path.dirname(p),
                "size": d["size"], "mtime": d["mtime"], "ext": d.get("ext"), "dur": d.get("dur"),
                "sr": d.get("sr"), "ch": d.get("ch"), "bits": d.get("bits"), "bad": d.get("bad", False),
                "bpm": d.get("bpm"), "key": d.get("key"), "cat": idx["cat"][p], "used": len(u),
                "usedBy": [os.path.splitext(os.path.basename(x))[0] for x in u[:5]]}

    def summary(self):
        # Escaneando: solo totales (baratos). Duplicados/categorias se recalculan al terminar.
        last = getattr(self, "_sum", None)
        if last and self.status()["running"]:
            with self.lock:
                vals = list(self.files.values())
            return dict(last, rev=self.rev, total=len(vals), size=sum(d["size"] for d in vals))
        self._sum = s = self._summary()
        return s

    def _summary(self):
        idx = self.index()
        cats = {}
        for p, c in idx["cat"].items():
            x = cats.setdefault(c, [0, 0])
            x[0] += 1
            x[1] += idx["files"][p]["size"]
        wasted = sum(idx["files"][ps[0]]["size"] * (len(ps) - 1) for ps in idx["dups"])
        files = idx["files"]
        return {"rev": self.rev, "total": len(files), "size": sum(d["size"] for d in files.values()),
                "cats": sorted(([c, n, s] for c, (n, s) in cats.items()), key=lambda x: -x[1]),
                "dups": len(idx["dups"]), "dupFiles": sum(len(ps) - 1 for ps in idx["dups"]),
                "wasted": wasted, "similar": len(idx["similar"]),
                "unused": sum(1 for p in files if os.path.normcase(p) not in idx["use"]),
                "bad": sum(1 for d in files.values() if d.get("bad")),
                "projects": len(self.refs), "canUndo": bool(self.undo)}

    def query(self, cat=None, q="", flt="all", sort="name", offset=0, limit=200):
        idx = self.index()
        q = (q or "").lower().strip()
        out = []
        for p in idx["files"]:
            if cat and idx["cat"][p] != cat:
                continue
            if q and q not in p.lower():
                continue
            if flt == "unused" and os.path.normcase(p) in idx["use"]:
                continue
            if flt == "used" and os.path.normcase(p) not in idx["use"]:
                continue
            if flt == "bad" and not idx["files"][p].get("bad"):
                continue
            out.append(p)
        key = {"name": lambda p: os.path.basename(p).lower(), "size": lambda p: -idx["files"][p]["size"],
               "date": lambda p: -idx["files"][p]["mtime"],
               "used": lambda p: -len(idx["use"].get(os.path.normcase(p), []))}.get(sort)
        out.sort(key=key)
        return {"total": len(out), "items": [self.item(p, idx) for p in out[offset:offset + limit]]}

    def groups(self, kind="exact", offset=0, limit=40):
        idx = self.index()
        g = idx["dups"] if kind == "exact" else idx["similar"]
        g = [ps for ps in g if all(p in idx["files"] for p in ps)]
        return {"total": len(g), "items": [[self.item(p, idx) for p in ps] for ps in g[offset:offset + limit]]}

    def path_of(self, i):
        return self.ids.get(i)

    # ---------------------------------------------------------------- acciones
    def set_cat(self, paths, cat):
        with self.lock:
            for p in paths:
                if p in self.files:
                    if cat:
                        self.cats[p] = cat
                    else:
                        self.cats.pop(p, None)
        self.rev += 1
        self._idx_t = 0          # accion del usuario: refrescar ya
        self.save()

    def recycle(self, paths):
        ok, err = 0, []
        for p in paths:
            if p in self.files and core.recycle(p):
                ok += 1
                with self.lock:
                    self.files.pop(p, None)
                    self.cats.pop(p, None)
            else:
                err.append(p)
        self.rev += 1
        self._idx_t = 0          # accion del usuario: refrescar ya
        self.save()
        return {"deleted": ok, "errors": err}

    def dedupe(self):
        """Envia a la Papelera las copias sobrantes de cada grupo identico (conserva la sugerida).
        Nunca toca copias que use algun proyecto."""
        idx = self.index()
        dels, kept = [], 0
        for ps in idx["dups"]:
            for p in ps[1:]:
                if os.path.normcase(p) in idx["use"]:
                    kept += 1
                else:
                    dels.append(p)
        r = self.recycle(dels)
        r["kept"] = kept
        return r

    def plan(self, dest, cats=None, skip_used=True):
        idx = self.index()
        out = []
        dl = os.path.normcase(os.path.abspath(dest)) + os.sep
        for p in idx["files"]:
            c = idx["cat"][p]
            if cats and c not in cats:
                continue
            if skip_used and os.path.normcase(p) in idx["use"]:
                continue
            if os.path.normcase(p).startswith(dl):
                continue
            out.append({"src": p, "dst": os.path.join(dest, *c.split("/"), os.path.basename(p)), "cat": c})
        return out

    def apply(self, plan, copy=True):
        plan = [s for s in plan if s["src"] in self.files]
        pairs = list(zip([s["src"] for s in plan], core.unique_many([s["dst"] for s in plan])))

        def moved(src, dst):
            with self.lock:
                d = dict(self.files[src]) if copy else self.files.pop(src)
                self.files[dst] = d
                self.ids[sid(dst)] = dst
                if not copy and src in self.cats:
                    self.cats[dst] = self.cats.pop(src)
        done, errors = fastfs.transfer_many(pairs, copy, moved)
        self.undo = [] if copy else done
        self.rev += 1
        self._idx_t = 0          # accion del usuario: refrescar ya
        self.save()
        return {"moved": len(done), "errors": errors}

    def undo_last(self):
        n, errors = 0, []
        for src, dst in reversed(self.undo):
            try:
                os.makedirs(os.path.dirname(src), exist_ok=True)
                shutil.move(dst, src)
                n += 1
                with self.lock:
                    d = self.files.pop(dst, None)
                    if d:
                        self.files[src] = d
                        self.ids[sid(src)] = src
                try:
                    os.removedirs(os.path.dirname(dst))
                except OSError:
                    pass
            except OSError as e:
                errors.append("%s: %s" % (dst, e))
        self.undo = []
        self.rev += 1
        self._idx_t = 0          # accion del usuario: refrescar ya
        self.save()
        return {"restored": n, "errors": errors}


_MGR = [None]


def core_mgr():
    return _MGR[0]
