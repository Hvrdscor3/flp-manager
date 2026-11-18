"""Nucleo del FL Project Manager: discos, escaneo, analisis, agrupado, organizar."""
import ctypes
import datetime as dt
import json
import os
import re
import shutil
import string
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import flpraw as F  # noqa: E402
import fastscan  # noqa: E402

DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "FLP Manager")
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "data.json")
_OLD = os.path.join(HERE, "data.json")  # ubicacion de la primera version
if not os.path.exists(DATA_FILE) and os.path.exists(_OLD):
    shutil.copy2(_OLD, DATA_FILE)
MESES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio",
         "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
SKIP_DIRS = {"windows", "$recycle.bin", "system volume information", "programdata",
             "appdata", "node_modules", ".git", "program files", "program files (x86)",
             "msocache", "recovery", "$windows.~bt", "$winreagent", "config.msi"}
EV_TEMPO, EV_TITLE, EV_SAMPLE, EV_TIMESTAMP, EV_DATAPATH = 156, 194, 196, 237, 202
EV_PATTERN_NEW = 65


# ------------------------------------------------------------------ FL Studio
def fl_paths():
    out = {"exe": "", "roots": [], "user": os.path.join(os.path.expanduser("~"), "Documents",
                                                          "Image-Line", "FL Studio")}
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Image-Line\Shared\Paths")
        for name in ("FL Studio", "Install path", "Image-Line", "Shared data"):
            try:
                v = winreg.QueryValueEx(k, name)[0]
            except OSError:
                continue
            if name == "FL Studio":
                out["exe"] = v
            elif name == "Shared data":
                out["user"] = os.path.join(v, "FL Studio")
            else:
                out["roots"].append(v)
    except OSError:
        pass
    return out


FL = fl_paths()


# Archivos que identifican una instalacion de FL Studio (aunque no este registrada).
FL_MARKERS = {"fl64.exe", "fl.exe", "flengine_x64.dll", "flengine.dll"}
_fl_root_cache = {}


def is_fl_install_dir(folder):
    """True si la carpeta es la raiz de una instalacion de FL Studio (con cache)."""
    key = folder.lower().rstrip("\\")
    r = _fl_root_cache.get(key)
    if r is None:
        r = any(os.path.isfile(os.path.join(folder, m)) for m in ("FL64.exe", "FL.exe",
                                                                  "FLEngine_x64.dll"))
        _fl_root_cache[key] = r
    return r


def is_program_file(path):
    """True si el .flp viene con FL Studio (demos, plantillas, render tests, datos de fabrica)."""
    low = path.lower().replace("/", "\\")
    for r in FL["roots"]:
        if r and low.startswith(r.lower().rstrip("\\") + "\\"):
            return True
    keys = ("\\data\\projects\\", "\\data\\templates\\", "\\image-line\\data\\",
            "\\fl studio\\templates\\", "\\templates\\", "\\program files")
    if "image-line" in low and any(k in low for k in keys) or \
            ("\\fl studio" in low and "\\data\\projects\\" in low):
        return True
    # Dentro de cualquier instalacion de FL (p. ej. A:\FL OG [25]\Data\System\Render tests)
    folder = os.path.dirname(path)
    for _ in range(8):
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        if is_fl_install_dir(folder):
            return True
        folder = parent
    return False


# ------------------------------------------------------------------ discos
def list_drives():
    res = []
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    types = {2: "Extraible", 3: "Disco local", 4: "Red", 5: "CD/DVD", 6: "RAM"}
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << i):
            continue
        root = letter + ":\\"
        t = ctypes.windll.kernel32.GetDriveTypeW(root)
        label = ctypes.create_unicode_buffer(261)
        ctypes.windll.kernel32.GetVolumeInformationW(root, label, 261, None, None, None, None, 0)
        try:
            u = shutil.disk_usage(root)
            total, free = u.total, u.free
        except OSError:
            total = free = 0
        res.append({"root": root, "label": label.value or types.get(t, "Unidad"),
                    "type": types.get(t, "Otro"), "total": total, "free": free})
    return res


# ------------------------------------------------------------------ analisis
def _resolve_sample(p, project_dir):
    rep = {"%flstudiofactorydata%": os.path.dirname(FL["exe"]) if FL["exe"] else "",
           "%flstudiouserdata%": FL["user"], "%userprofile%": os.path.expanduser("~")}
    q = p
    for k, v in rep.items():
        if q.lower().startswith(k) and v:
            q = v + q[len(k):]
    q = os.path.expandvars(q)
    if os.path.exists(q):
        return True
    return os.path.exists(os.path.join(project_dir, os.path.basename(q.replace("/", "\\"))))


def fl_date(days):
    return dt.datetime(1899, 12, 30) + dt.timedelta(days=days)


def analyze(path):
    st = os.stat(path)
    info = {"path": path, "name": os.path.splitext(os.path.basename(path))[0],
            "dir": os.path.dirname(path), "size": st.st_size, "mtime": st.st_mtime,
            "version": "", "tempo": None, "title": "", "created": None, "worked_h": None,
            "channels": 0, "patterns": 0, "samples": 0, "missing": [], "error": ""}
    try:
        with open(path, "rb") as f:
            raw = f.read()
        info["version"] = F.fl_version(raw)
        _, events = F.loads(raw)
    except Exception as e:  # noqa: BLE001
        info["error"] = str(e) or "No se pudo leer"
        return info
    seen = set()
    for lst, k in F.walk(events):
        eid, val = lst[k]
        if eid == EV_TEMPO and info["tempo"] is None and len(val) == 4:
            info["tempo"] = round(struct.unpack("<I", val)[0] / 1000, 2)
        elif eid == EV_TITLE:
            info["title"] = F.text(val)
        elif eid == F.EV_CH_NEW:
            info["channels"] += 1
        elif eid == EV_PATTERN_NEW:
            info["patterns"] = max(info["patterns"], struct.unpack("<H", val)[0])
        elif eid == EV_TIMESTAMP and len(val) >= 16:
            c, w = struct.unpack_from("<dd", val)
            try:
                if c > 1:
                    info["created"] = fl_date(c).timestamp()
                info["worked_h"] = round(w * 24, 1)
            except (OverflowError, OSError, ValueError):
                pass
        elif eid == EV_SAMPLE:
            p = F.text(val)
            if p and p.lower() not in seen:
                seen.add(p.lower())
                info["samples"] += 1
                if not _resolve_sample(p, info["dir"]):
                    info["missing"].append(p)
    return info


# ------------------------------------------------------------------ agrupar
_AUTOSAVE = re.compile(r"\s*\((autosaved|overwritten)[^)]*\)", re.I)
_TAIL = re.compile(r"[\s_\-.]*(ordenado|backup|copia|copy|final|v\d+|ver\d+|\(\d+\)|\d+)$", re.I)


def group_key(name):
    s = _AUTOSAVE.sub("", name).strip().lower()
    prev = None
    while prev != s:
        prev = s
        s = _TAIL.sub("", s).strip()
    return s or name.lower()


def is_backup(path):
    low = path.lower()
    return bool(_AUTOSAVE.search(os.path.basename(path))) or "\\backup\\" in low


# ------------------------------------------------------------------ estado
SCANS_FILE = os.path.join(DATA_DIR, "scans.json")
WALK_THREADS, ANALYZE_THREADS = 12, 8
_WALK_POOL = ThreadPoolExecutor(WALK_THREADS, thread_name_prefix="walk")
_AN_POOL = ThreadPoolExecutor(ANALYZE_THREADS, thread_name_prefix="analyze")


def placeholders():
    return {"%flstudiofactorydata%": os.path.dirname(FL["exe"]) if FL["exe"] else "",
            "%flstudiouserdata%": FL["user"], "%userprofile%": os.path.expanduser("~")}


def scan_dir(d):
    """Lista una carpeta: (subcarpetas, [(ruta .flp, mtime, size)], excluidos)."""
    subs, flps, excluded = [], [], 0
    try:
        with os.scandir(d) as it:
            entries = list(it)
        if any(e.name.lower() in FL_MARKERS for e in entries):
            _fl_root_cache[d.lower().rstrip("\\")] = True
            return [], [], 0   # instalacion de FL: no se entra (demos, render tests, plantillas)
        if True:
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.name.lower() not in SKIP_DIRS:
                            subs.append(e.path)
                    elif e.name.lower().endswith(".flp"):
                        if is_program_file(e.path):
                            excluded += 1
                        else:
                            st = e.stat()  # en Windows viene gratis con scandir
                            flps.append((e.path, st.st_mtime, st.st_size))
                except OSError:
                    pass
    except OSError:
        pass
    return subs, flps, excluded


class ScanJob:
    """Escaneo de una unidad/carpeta: se puede pausar y continuar (incluso tras cerrar la app)."""
    FIELDS = ("root", "state", "dirs", "files", "seen", "dirs_done", "found", "analyzed",
              "cached", "excluded", "elapsed", "error")

    def __init__(self, root):
        self.root = root
        self.state = "running"       # running | paused | done | error
        self.dirs = [root]           # carpetas pendientes de listar
        self.files = []              # .flp pendientes de analizar
        self.seen = []               # .flp vistos en este escaneo
        self.dirs_done = self.found = self.analyzed = self.cached = self.excluded = 0
        self.elapsed = 0.0
        self.error = ""
        self.current = ""
        self.pause = threading.Event()
        self.thread = None

    def to_dict(self, full=True):
        d = {k: getattr(self, k) for k in self.FIELDS}
        if not full:
            d.update(dirs=len(self.dirs), files=len(self.files), seen=len(self.seen),
                     current=self.current)
        return d

    @classmethod
    def from_dict(cls, d):
        j = cls(d["root"])
        for k in cls.FIELDS:
            if k in d:
                setattr(j, k, d[k])
        if j.state == "running":   # la app se cerro a mitad: queda en pausa
            j.state = "paused"
        return j


class Manager:
    def __init__(self):
        self.lock = threading.Lock()
        self.projects = {}     # path -> info
        self.marks = {}        # path -> {"status": util|descartar|pendiente, "note": ""}
        self.undo = []         # [[src, dst], ...] de la ultima organizacion
        self.jobs = {}         # root -> ScanJob
        self.fs = None         # cache de carpetas para samples (por sesion de escaneo)
        self._last_save = 0.0
        self.rev = 1           # sube con cada cambio de proyectos/marcas
        self._listing = (0, None)
        self._load()

    def _load(self):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                d = json.load(f)
            self.projects = d.get("projects", {})
            self.marks = d.get("marks", {})
            self.undo = d.get("undo", [])
        except (OSError, ValueError):
            pass
        # Quitar archivos de instalaciones de FL guardados por versiones anteriores.
        junk = [p for p in self.projects if is_program_file(p)]
        for p in junk:
            del self.projects[p]
        self.purged = len(junk)
        try:
            with open(SCANS_FILE, encoding="utf-8") as f:
                self.jobs = {d["root"]: ScanJob.from_dict(d) for d in json.load(f)}
        except (OSError, ValueError, KeyError):
            pass

    def touch(self):
        self.rev += 1

    def save(self):
        with self.lock:
            d = {"projects": self.projects, "marks": self.marks, "undo": self.undo}
            data = json.dumps(d, ensure_ascii=False)
            jobs = json.dumps([j.to_dict() for j in self.jobs.values()], ensure_ascii=False)
        for path, txt in ((DATA_FILE, data), (SCANS_FILE, jobs)):
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(txt)
            os.replace(tmp, path)
        self._last_save = time.time()

    @property
    def status(self):
        jobs = [j.to_dict(full=False) for j in self.jobs.values()]
        return {"running": any(j["state"] == "running" for j in jobs), "jobs": jobs,
                "excluded": sum(j["excluded"] for j in jobs)}

    # ---- escaneo
    def start_scan(self, roots):
        """Inicia o continua el escaneo de cada raiz (las ya terminadas empiezan de nuevo)."""
        for root in roots:
            j = self.jobs.get(root)
            if j and j.state == "running":
                continue
            if not j or j.state in ("done", "error"):
                j = ScanJob(root)
                self.jobs[root] = j
            self._run(j)
        return True

    def pause_scan(self, root):
        j = self.jobs.get(root)
        if j and j.state == "running":
            j.pause.set()
        return bool(j)

    def forget_scan(self, root):
        j = self.jobs.get(root)
        if j and j.state == "running":
            j.pause.set()
            if j.thread:
                j.thread.join(10)
        self.jobs.pop(root, None)
        self.save()

    def _run(self, j):
        if self.fs is None or not any(x.state == "running" for x in self.jobs.values()):
            self.fs = fastscan.FsCache()   # cache fresca: refleja samples movidos/borrados
        j.state, j.error = "running", ""
        j.pause.clear()
        j.thread = threading.Thread(target=self._worker, args=(j,), daemon=True)
        j.thread.start()

    def _worker(self, j):
        ph, fs = placeholders(), self.fs
        seen = set(j.seen)
        inflight = {}            # future -> ("dir", ruta) | ("flp", ruta)
        t0 = time.time() - j.elapsed
        try:
            while True:
                pausing = j.pause.is_set()
                if not pausing:
                    while j.files and sum(1 for k in inflight.values() if k[0] == "flp") < ANALYZE_THREADS * 2:
                        path = j.files.pop()
                        j.current = path
                        inflight[_AN_POOL.submit(fastscan.analyze, path, fs, ph)] = ("flp", path)
                    while j.dirs and sum(1 for k in inflight.values() if k[0] == "dir") < WALK_THREADS * 2:
                        d = j.dirs.pop()
                        j.current = d
                        inflight[_WALK_POOL.submit(scan_dir, d)] = ("dir", d)
                if not inflight:
                    break
                done, _ = wait(list(inflight), timeout=0.5, return_when=FIRST_COMPLETED)
                for fut in done:
                    kind, path = inflight.pop(fut)
                    try:
                        res = fut.result()
                    except Exception as e:  # noqa: BLE001
                        if kind == "flp":
                            res = {"path": path, "name": os.path.splitext(os.path.basename(path))[0],
                                   "dir": os.path.dirname(path), "size": 0, "mtime": 0,
                                   "version": "", "tempo": None, "title": "", "created": None,
                                   "worked_h": None, "channels": 0, "patterns": 0, "samples": 0,
                                   "missing": [], "error": str(e)}
                        else:
                            res = ([], [], 0)
                    if kind == "dir":
                        subs, flps, excl = res
                        j.dirs.extend(subs)
                        j.dirs_done += 1
                        j.excluded += excl
                        for p, mt, sz in flps:
                            if p in seen:
                                continue
                            seen.add(p)
                            j.seen.append(p)
                            j.found += 1
                            old = self.projects.get(p)
                            if old and old.get("mtime") == mt and old.get("size") == sz:
                                j.cached += 1        # ya analizado y sin cambios: se omite
                            else:
                                j.files.append(p)
                    else:
                        with self.lock:
                            self.projects[path] = res
                            self.rev += 1
                        j.analyzed += 1
                j.elapsed = time.time() - t0
                if time.time() - self._last_save > 15:
                    self.save()
            if j.pause.is_set():
                j.state = "paused"
            else:
                rootl = j.root.lower().rstrip("\\") + "\\"
                with self.lock:  # quitar proyectos que ya no existen bajo esta raiz
                    for p in [p for p in self.projects if p.lower().startswith(rootl) and p not in seen]:
                        del self.projects[p]
                    self.rev += 1
                j.state, j.dirs, j.files = "done", [], []
        except Exception as e:  # noqa: BLE001
            j.state, j.error = "error", str(e)
        finally:
            j.current = ""
            j.elapsed = time.time() - t0
            self.save()

    # ---- vista
    def listing(self):
        rev = self.rev
        if self._listing[0] == rev:
            return self._listing[1]
        out = self._build_listing()
        self._listing = (rev, out)
        return out

    def _build_listing(self):
        with self.lock:
            items = [dict(v) for v in self.projects.values()]
        groups = {}
        for it in items:
            it["backup"] = is_backup(it["path"])
            it["key"] = group_key(it["name"])
            it["mark"] = self.marks.get(it["path"], {}).get("status", "pendiente")
            it["note"] = self.marks.get(it["path"], {}).get("note", "")
            groups.setdefault(it["key"], []).append(it)
        out = []
        for key, members in groups.items():
            members.sort(key=lambda x: x["mtime"], reverse=True)
            primary = next((m for m in members if not m["backup"]), members[0])
            for m in members:
                m["latest"] = m is members[0]
                m["primary"] = m is primary
            out.append({"key": key, "name": _AUTOSAVE.sub("", primary["name"]).strip() or primary["name"], "latest": members[0]["path"],
                        "mtime": members[0]["mtime"], "count": len(members),
                        "backups": sum(m["backup"] for m in members),
                        "missing": max(len(m["missing"]) for m in members),
                        "error": any(m["error"] for m in members),
                        "version": members[0]["version"], "members": members,
                        "mark": primary["mark"]})
        out.sort(key=lambda g: g["mtime"], reverse=True)
        return out

    def mark(self, paths, status, note=None):
        with self.lock:
            for p in paths:
                m = self.marks.setdefault(p, {})
                if status:
                    m["status"] = status
                if note is not None:
                    m["note"] = note
            self.rev += 1
        self.save()

    # ---- organizar
    def plan(self, dest_root, keys=None, only_useful=False, include_discarded=False):
        plan = []
        for g in self.listing():
            if keys and g["key"] not in keys:
                continue
            if only_useful and g["mark"] != "util":
                continue
            if g["mark"] == "descartar" and not include_discarded:
                continue
            d = dt.datetime.fromtimestamp(g["mtime"])
            folder = os.path.join(dest_root, str(d.year), "%02d - %s" % (d.month, MESES[d.month - 1]),
                                  _safe(g["name"]))
            for m in g["members"]:
                sub = folder if m["primary"] else os.path.join(
                    folder, "Backups" if m["backup"] else "Versiones")
                dst = os.path.join(sub, os.path.basename(m["path"]))
                if os.path.normcase(dst) != os.path.normcase(m["path"]):
                    plan.append({"src": m["path"], "dst": dst, "group": g["name"]})
        return plan

    def apply(self, plan, copy=False):
        done, errors = [], []
        for step in plan:
            src, dst = step["src"], _unique(step["dst"])
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                (shutil.copy2 if copy else shutil.move)(src, dst)
                done.append([src, dst])
                with self.lock:
                    info = self.projects.pop(src, None) if not copy else dict(self.projects.get(src, {}))
                    if info:
                        info["path"], info["dir"] = dst, os.path.dirname(dst)
                        self.projects[dst] = info
                    if not copy and src in self.marks:
                        self.marks[dst] = self.marks.pop(src)
            except OSError as e:
                errors.append("%s: %s" % (src, e))
        self.undo = [] if copy else done
        self.touch()
        self.save()
        return {"moved": len(done), "errors": errors}

    def undo_last(self):
        errors, n = [], 0
        for src, dst in reversed(self.undo):
            try:
                os.makedirs(os.path.dirname(src), exist_ok=True)
                shutil.move(dst, src)
                n += 1
                try:
                    os.removedirs(os.path.dirname(dst))  # quita carpetas que quedaron vacias
                except OSError:
                    pass
                with self.lock:
                    info = self.projects.pop(dst, None)
                    if info:
                        info["path"], info["dir"] = src, os.path.dirname(src)
                        self.projects[src] = info
                    if dst in self.marks:
                        self.marks[src] = self.marks.pop(dst)
            except OSError as e:
                errors.append("%s: %s" % (dst, e))
        self.undo = []
        self.touch()
        self.save()
        return {"restored": n, "errors": errors}

    # ---- acciones
    def recycle(self, paths):
        ok, errors = [], []
        for p in paths:
            if recycle(p):
                ok.append(p)
                with self.lock:
                    self.projects.pop(p, None)
                    self.marks.pop(p, None)
            else:
                errors.append(p)
        self.touch()
        self.save()
        return {"deleted": len(ok), "errors": errors}

    # ---- backups innecesarios
    def backup_candidates(self):
        """Backups que sobran: copia automatica (autosave/overwritten o carpeta Backup) MAS ANTIGUA
        que el archivo principal del mismo proyecto, en su misma carpeta o en su subcarpeta Backup.
        Nunca se proponen si el principal no se puede leer o si el backup es mas nuevo."""
        out = []
        for g in self.listing():
            mains = [m for m in g["members"] if not m["backup"] and not m["error"]]
            for b in g["members"]:
                if not b["backup"]:
                    continue
                base = _AUTOSAVE.sub("", b["name"]).strip().lower()
                bdir = b["dir"].lower()
                for m in mains:
                    mdir = m["dir"].lower()
                    if m["name"].strip().lower() != base or bdir not in (mdir, mdir + "\\backup"):
                        continue
                    if b["mtime"] < m["mtime"]:
                        out.append({"path": b["path"], "size": b["size"], "group": g["name"],
                                    "main": m["path"]})
                        break
        return out


def _safe(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .") or "Proyecto"


def _unique(path):
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    n = 2
    while os.path.exists("%s (%d)%s" % (root, n, ext)):
        n += 1
    return "%s (%d)%s" % (root, n, ext)


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16),
                ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR)]


def recycle(path):
    """Envia a la Papelera de reciclaje (recuperable)."""
    if not os.path.exists(path):
        return False
    op = _SHFILEOPSTRUCTW(None, 3, os.path.abspath(path) + "\0", None,
                          0x40 | 0x10 | 0x04 | 0x400, False, None, None)
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not os.path.exists(path)


def open_in_fl(path):
    if FL["exe"] and os.path.exists(FL["exe"]):
        subprocess.Popen([FL["exe"], path])
    else:
        os.startfile(path)  # noqa: S606


def reveal(path):
    subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])


def pick_folder():
    ps = ("Add-Type -AssemblyName System.Windows.Forms;"
          "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
          "$d.Description='Carpeta destino para tus proyectos';"
          "if($d.ShowDialog((New-Object System.Windows.Forms.Form -Property @{TopMost=$true}))"
          " -eq 'OK'){[Console]::OutputEncoding=[Text.Encoding]::UTF8;$d.SelectedPath}")
    r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                       capture_output=True, text=True, encoding="utf-8", timeout=600,
                       creationflags=0x08000000)
    return r.stdout.strip()


def now():
    return time.time()
