"""Reparar samples faltantes de UN proyecto: busca candidatos y reescribe la ruta en el .flp.

- Candidatos exactos: mismo nombre de archivo en la libreria escaneada (Samples) o cerca del proyecto.
- Si no hay, sugiere parecidos por nombre (tokens + similitud).
- Antes de escribir guarda una copia en <carpeta>\\Backup\\<nombre> (antes de reparar samples).flp
- Solo se cambia el texto de la ruta (evento 196); el resto del archivo queda byte a byte igual.
"""
import difflib
import json
import os
import re
import shutil
import struct
import threading
import time

import core
import fastfs
import fastscan
import flpraw as F
import samples

EV_SAMPLE = 196
_TOK = re.compile(r"[a-z0-9#]+")
_IDX = [None, None]
NAMES_FILE = os.path.join(core.DATA_DIR, "names.json")
CLOUD = re.compile(r"drive|dropbox|cloud|onedrive|shared|icloud|mega", re.I)
# Indice propio de nombres de audio en todos los discos (independiente de la seccion Samples):
# solo lista carpetas, no abre archivos. Se hace una vez y se guarda.
NIDX = {"state": "none", "dirs": 0, "files": 0, "t": 0, "paths": []}
try:
    with open(NAMES_FILE, encoding="utf-8") as _f:
        _d = json.load(_f)
    _p = [p for p in _d.get("paths", []) if not core.is_fl_content(p)]
    NIDX.update(state="done", t=_d.get("t", 0), paths=_p, files=len(_p))
except (OSError, ValueError):
    pass


def local_roots():
    return [d["root"] for d in core.list_drives()
            if d["type"] in ("Disco local", "Extraible") and not CLOUD.search(d["label"] or "")]


def build_names(smp, force=False):
    if NIDX["state"] == "running" or (not force and NIDX["state"] == "done" and time.time() - NIDX["t"] < 7 * 86400):
        return False
    NIDX.update(state="running", dirs=0, files=0)

    def run():
        try:
            def on_dir(d, files):
                NIDX["dirs"] += 1
                NIDX["files"] += len(files)
            found = [p for p, _, _ in fastfs.walk_files(local_roots(), samples.want_audio, samples.skip_dir, on_dir)
                     if not core.is_fl_content(p)]
            NIDX.update(paths=found, t=time.time(), state="done", files=len(found))
            tmp = NAMES_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"t": NIDX["t"], "paths": found}, fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, NAMES_FILE)
        except Exception:  # noqa: BLE001
            NIDX["state"] = "done" if NIDX["paths"] else "none"
    threading.Thread(target=run, daemon=True).start()
    return True


def index_status():
    return {k: v for k, v in NIDX.items() if k != "paths"}


def _index(smp):
    """Indices por nombre y por palabra (libreria de Samples + indice de discos)."""
    key = (smp.rev, NIDX["t"], NIDX["state"])
    if _IDX[0] == key and _IDX[1]:
        return _IDX[1]
    by_name, by_tok, norm = {}, {}, {}
    allp = dict.fromkeys(list(smp.files) + (NIDX["paths"] if NIDX["state"] == "done" else []))
    for p in allp:
        b = os.path.basename(p).lower()
        by_name.setdefault(b, []).append(p)
        n = samples.norm_name(p)
        norm[p] = n
        for t in set(_TOK.findall(n)):
            if len(t) > 1 and not t.isdigit():
                by_tok.setdefault(t, []).append(p)
    _IDX[:] = [key, {"name": by_name, "tok": by_tok, "norm": norm}]
    return _IDX[1]


def _other_drives(q):
    """Misma ruta en otra letra de unidad (disco que cambio de letra)."""
    if len(q) < 3 or q[1] != ":":
        return []
    rest = q[2:]
    return [r[:2] + rest for r in local_roots() if r[0].lower() != q[0].lower() and os.path.isfile(r[:2] + rest)]


def _expand(raw):
    q = raw.replace("/", "\\")
    low = q.lower()
    for k, v in core.placeholders().items():
        if low.startswith(k) and v:
            return v + q[len(k):]
    return os.path.expandvars(q)


def _near(project_dir, base, limit=3000):
    """Busca el mismo nombre de archivo en la carpeta del proyecto y su carpeta padre."""
    out, n = [], 0
    for root in {project_dir, os.path.dirname(project_dir)}:
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if d.lower() not in core.SKIP_DIRS and not d.startswith(".")]
            n += len(fn)
            out += [os.path.join(dp, f) for f in fn if f.lower() == base]
            if n > limit:
                break
    return out


def _item(smp, p, idx_s):
    if p in smp.files:
        return smp.item(p, idx_s)
    st = os.stat(p)
    smp.ids[samples.sid(p)] = p
    return {"id": samples.sid(p), "path": p, "name": os.path.basename(p), "dir": os.path.dirname(p),
            "size": st.st_size, "mtime": st.st_mtime, "ext": os.path.splitext(p)[1].lower()[1:],
            "dur": None, "used": 0, "usedBy": [], "cat": samples.categorize(p), "bad": False}


def candidates(mgr, smp, project):
    info = mgr.projects.get(project)
    if not info:
        raise ValueError("Proyecto desconocido")
    build_names(smp)                    # primera vez: indexa los discos en segundo plano
    idx, idx_s = _index(smp), smp.index()
    out = []
    for raw in info.get("missing", []):
        q = _expand(raw)
        base = os.path.basename(q).lower()
        exact = list(dict.fromkeys(_other_drives(q) + idx["name"].get(base, []) + _near(info["dir"], base)))
        exact = [p for p in exact if os.path.isfile(p)]
        similar = []
        if len(exact) < 3:
            n = samples.norm_name(q)
            pool = {}
            for t in set(_TOK.findall(n)):
                for p in idx["tok"].get(t, [])[:4000]:
                    pool[p] = pool.get(p, 0) + 1
            ext = os.path.splitext(base)[1]
            best = sorted(pool, key=lambda p: -pool[p])[:400]
            scored = []
            for p in best:
                if p in exact:
                    continue
                r = difflib.SequenceMatcher(None, n, idx["norm"].get(p, "")).ratio()
                if os.path.splitext(p)[1].lower() == ext:
                    r += 0.03
                if r >= 0.55:
                    scored.append((r, p))
            scored.sort(reverse=True)
            similar = [dict(_item(smp, p, idx_s), score=round(min(r, 1) * 100)) for r, p in scored[:6]]
        out.append({"raw": raw, "name": os.path.basename(q), "dir": os.path.dirname(q),
                    "exact": [_item(smp, p, idx_s) for p in exact[:12]], "similar": similar})
    return {"project": project, "name": info["name"], "missing": out,
            "library": len(smp.files) + (len(NIDX["paths"]) if NIDX["state"] == "done" else 0),
            "index": index_status()}


def apply(mgr, project, mapping):
    """mapping: {ruta original (tal cual en el .flp): ruta nueva}."""
    if project not in mgr.projects:
        raise ValueError("Proyecto desconocido")
    mapping = {k: v for k, v in mapping.items() if v and os.path.isfile(v)}
    if not mapping:
        return {"changed": 0}
    with open(project, "rb") as f:
        raw = f.read()
    header, events = F.loads(raw)
    body = F.dump_events(events)
    if header + b"FLdt" + struct.pack("<I", len(body)) + body != raw:
        raise ValueError("Este proyecto no se puede editar de forma segura (formato inesperado)")
    changed = 0
    for lst, k in F.walk(events):
        eid, val = lst[k]
        if eid == EV_SAMPLE:
            s = F.text(val)
            if s in mapping:
                lst[k] = [eid, F.enc_text(os.path.normpath(mapping[s]))]
                changed += 1
    if not changed:
        return {"changed": 0}
    bdir = os.path.join(os.path.dirname(project), "Backup")
    os.makedirs(bdir, exist_ok=True)
    name = os.path.splitext(os.path.basename(project))[0]
    backup = core._unique(os.path.join(bdir, name + " (antes de reparar samples).flp"))
    shutil.copy2(project, backup)
    tmp = project + ".tmp"
    F.save(tmp, header, events)
    os.replace(tmp, project)
    info = fastscan.analyze(project, fastscan.FsCache(), core.placeholders())
    with mgr.lock:
        mgr.projects[project] = info
    mgr.touch()
    mgr.save()
    return {"changed": changed, "backup": backup, "missing": len(info["missing"])}
