"""Scripts de FL Studio: los que trae FLP Manager y los que ya hay instalados en FL."""
import ast
import json
import os
import re
import sys

import core

HERE = getattr(sys, "_MEIPASS", os.path.dirname(core.HERE))
SRC_DIR = os.path.join(HERE, "fl_scripts")     # un subdirectorio por script, con info.json (ver fl_scripts/README.md)
TYPES = {"hardware": ("Hardware", "Controlador MIDI"),
         "pianoroll": ("Piano roll scripts", "Script de Piano roll"),
         "audio": ("Audio scripts", "Script de Edison")}

KINDS = [("Hardware", "Controladores MIDI", "device_*.py"),
         ("Piano roll scripts", "Piano roll", "*.pyscript"),
         ("Audio scripts", "Audio (Edison)", "*.pyscript")]


def settings_dir():
    return os.path.join(core.FL["user"], "Settings")


def load_catalog():
    """Lee fl_scripts/<id>/info.json. Un script mal descrito se ignora (no rompe la app)."""
    out = []
    try:
        ids = sorted(os.listdir(SRC_DIR))
    except OSError:
        return out
    for sid in ids:
        try:
            with open(os.path.join(SRC_DIR, sid, "info.json"), encoding="utf-8") as f:
                it = json.load(f)
            if it.get("type") not in TYPES or not it.get("file") or os.sep in it["file"] or "/" in it["file"]:
                continue
            if it["type"] == "hardware" and not re.fullmatch(r"[\w .()-]+", it.get("folder", "")):
                continue
        except (OSError, ValueError):
            continue
        it["id"] = sid
        it["kind"] = TYPES[it["type"]][1]
        out.append(it)
    return out


def _src(item):
    return os.path.join(SRC_DIR, item["id"], item["file"])


def _dst(item):
    base = os.path.join(settings_dir(), TYPES[item["type"]][0])
    if item["type"] == "hardware":
        return os.path.join(base, item["folder"], item["file"])
    return os.path.join(base, item["file"])


def find(sid):
    for it in load_catalog():
        if it["id"] == sid:
            return it
    raise OSError("Script desconocido")


def _read(p):
    try:
        with open(p, "rb") as f:
            return f.read()
    except OSError:
        return None


def _good(ln):
    ln = ln.strip()
    return (len(re.findall(r"[A-Za-zÀ-ÿ]", ln)) >= 8 and not re.match(r"^[\w ]+\s*=", ln)
            and not ln.startswith(("[[", "]]", "http")))


def _meta(path):
    """Nombre (# name=) y primera linea de descripcion de un script."""
    raw = _read(path) or b""
    txt = raw[:20000].decode("utf-8", "replace")
    m = re.search(r"^#\s*name\s*=\s*(.+)$", txt, re.M)
    name = m.group(1).strip() if m else ""
    desc = ""
    try:
        doc = ast.get_docstring(ast.parse(txt if len(raw) <= 20000 else txt.rsplit("\n", 1)[0])) or ""
    except (SyntaxError, ValueError):
        doc = ""
    for ln in doc.splitlines():
        ln = ln.strip(" -")
        if _good(ln):
            desc = ln[:160]
            break
    if not desc:
        for ln in txt.splitlines()[:15]:
            ln = ln.strip()
            if ln.startswith("#") and _good(ln.lstrip("# ")):
                desc = ln.lstrip("# ").strip()[:160]
                break
    return name, desc


def catalog():
    out = []
    for it in load_catalog():
        new, cur = _read(_src(it)), _read(_dst(it))
        out.append({k: it.get(k, "" if k != "how" else []) for k in ("id", "name", "kind", "what", "how", "limits")} | {
            "installed": cur is not None, "current": cur is not None and cur == new, "path": _dst(it),
            "available": new is not None})
    return out


def installed():
    ours = {os.path.normcase(_dst(it)) for it in load_catalog()}
    res = []
    for folder, label, pat in KINDS:
        base = os.path.join(settings_dir(), folder)
        items = []
        try:
            entries = sorted(os.scandir(base), key=lambda e: e.name.lower())
        except OSError:
            entries = []
        rx = re.compile("^" + re.escape(pat).replace(r"\*", ".*") + "$", re.I)
        files = []
        for e in entries:
            if e.is_dir():
                try:
                    files += [os.path.join(e.path, f) for f in sorted(os.listdir(e.path)) if rx.match(f)][:1]
                except OSError:
                    pass
            elif e.is_file() and rx.match(e.name):
                files.append(e.path)
        for p in files:
            name, desc = _meta(p)
            rel = os.path.relpath(p, base)
            items.append({"name": name or os.path.splitext(os.path.basename(p))[0].replace("device_", ""),
                          "desc": desc, "path": p, "folder": rel.split(os.sep)[0] if os.sep in rel else "",
                          "ours": os.path.normcase(p) in ours})
        res.append({"kind": label, "dir": base, "items": items})
    return res


def listing():
    return {"catalog": catalog(), "installed": installed(), "settings": settings_dir()}


def install(sid):
    """Instala o actualiza (sobrescribe solo el archivo del script)."""
    it = find(sid)
    new = _read(_src(it))
    if new is None:
        raise OSError("El script no viene incluido en esta versión")
    d = _dst(it)
    os.makedirs(os.path.dirname(d), exist_ok=True)
    tmp = d + ".tmp"
    with open(tmp, "wb") as f:
        f.write(new)
    os.replace(tmp, d)
    return True


def uninstall(sid):
    """Envia el script a la Papelera. En controladores, tambien su carpeta si solo queda cache de Python."""
    it = find(sid)
    d = _dst(it)
    if not os.path.exists(d):
        raise OSError("No está instalado")
    if not core.recycle(d):
        raise OSError("No se pudo enviar a la Papelera (¿FL Studio lo tiene abierto?)")
    if it["type"] == "hardware":
        folder = os.path.dirname(d)
        try:
            rest = [x for x in os.listdir(folder) if x != "__pycache__"]
        except OSError:
            rest = None
        if rest == []:
            core.recycle(folder)
    return True


def is_inside(path):
    """Solo se permite abrir rutas dentro de la carpeta Settings de FL."""
    base = os.path.normcase(os.path.abspath(settings_dir())) + os.sep
    return os.path.normcase(os.path.abspath(path)).startswith(base)
