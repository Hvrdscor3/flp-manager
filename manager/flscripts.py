"""Scripts de FL Studio: los que trae FLP Manager y los que ya hay instalados en FL."""
import ast
import os
import re
import sys

import core

HERE = getattr(sys, "_MEIPASS", os.path.dirname(core.HERE))

# Catalogo de scripts que FLP Manager puede instalar.
CATALOG = [{
    "id": "autoorg",
    "name": "Auto Organizer",
    "kind": "Controlador MIDI",
    "file": "device_AutoOrganizer.py",
    "dest": ("Hardware", "AutoOrganizer", "device_AutoOrganizer.py"),
    "what": "Ordena las automatizaciones del proyecto abierto: las clasifica por tipo (Volumen, Filtro, Pan, FX, "
            "Pitch, Otros), las colorea y les pone prefijo ([VOL], [FLT]...). Además crea pistas en el Playlist "
            "(desde la 400) con el mismo nombre y color para cada clip.",
    "how": ["Instálalo aquí y reinicia FL Studio (o recarga los scripts).",
            "En FL: Options › MIDI Settings › elige un puerto de entrada y en Controller type selecciona "
            "«Auto Organizer».",
            "Ejecútalo con View › Script output › pestaña Auto Organizer, escribiendo organizar() — o con un "
            "teclado MIDI: C5 = organizar, C#5 = solo colorear, D5 = limpiar pistas AUTO.",
            "Arrastra cada clip a su pista (la API de FL no permite moverlos). Si prefieres que se muevan "
            "solos, usa «Ordenar pistas» en el detalle del proyecto."],
    "limits": "No puede mover clips ni crear pistas; necesita un puerto MIDI asignado.",
}]

KINDS = [("Hardware", "Controladores MIDI", "device_*.py"),
         ("Piano roll scripts", "Piano roll", "*.pyscript"),
         ("Audio scripts", "Audio (Edison)", "*.pyscript")]


def settings_dir():
    return os.path.join(core.FL["user"], "Settings")


def _src(item):
    return os.path.join(HERE, item["file"])


def _dst(item):
    return os.path.join(settings_dir(), *item["dest"])


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
    for it in CATALOG:
        new, cur = _read(_src(it)), _read(_dst(it))
        out.append({k: it[k] for k in ("id", "name", "kind", "what", "how", "limits")} | {
            "installed": cur is not None, "current": cur is not None and cur == new, "path": _dst(it),
            "available": new is not None})
    return out


def installed():
    ours = {os.path.normcase(_dst(it)) for it in CATALOG}
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
    for it in CATALOG:
        if it["id"] == sid:
            new = _read(_src(it))
            if new is None:
                raise OSError("El script no viene incluido en esta versión")
            d = _dst(it)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            with open(d, "wb") as f:
                f.write(new)
            return True
    raise OSError("Script desconocido")


def is_inside(path):
    """Solo se permite abrir rutas dentro de la carpeta Settings de FL."""
    base = os.path.normcase(os.path.abspath(settings_dir())) + os.sep
    return os.path.normcase(os.path.abspath(path)).startswith(base)
