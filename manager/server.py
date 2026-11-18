"""FL Project Manager - servidor local (solo 127.0.0.1) + interfaz web."""
import json
import os
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402
import updater  # noqa: E402
import preview  # noqa: E402
import samples  # noqa: E402
import relink  # noqa: E402
import plugins  # noqa: E402
import flp_ordenar
import flscripts  # noqa: E402

TOKEN = secrets.token_urlsafe(24)
MGR = core.Manager()
samples._MGR[0] = MGR
SMP = samples.SampleManager()
PIX = plugins.PluginIndex(MGR)
UI = os.path.join(getattr(sys, "_MEIPASS", core.HERE), "ui.html")
HOOKS = {"pick_folder": core.pick_folder, "quit": lambda: os._exit(0)}


_PJ = [0, b""]


def _projects_json():
    """Listado ya serializado; solo se rehace cuando cambia la revision de datos."""
    rev = MGR.rev
    if _PJ[0] != rev:
        _PJ[1] = json.dumps({"groups": MGR.listing(), "canUndo": bool(MGR.undo), "rev": rev},
                            ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        _PJ[0] = rev
    return _PJ[1]




def _fl_script(install=False):
    """Estado del script Auto Organizer (lo muestra el pie del menu)."""
    if install:
        flscripts.install("autoorg")
    for c in flscripts.catalog():
        if c["id"] == "autoorg":
            return {"installed": c["installed"], "current": c["current"], "path": c["path"]}
    return {"installed": False, "current": True, "path": ""}


def _paths(body):
    """Solo se permite actuar sobre proyectos conocidos (escaneados)."""
    with MGR.lock:
        return [p for p in body.get("paths", []) if p in MGR.projects]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, ctype="application/json", raw=None):
        data = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _ok_host(self):
        host = self.headers.get("Host", "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._ok_host():
            return self._send(403, {"error": "host"})
        if self.path in ("/", "/index.html"):
            with open(UI, encoding="utf-8") as f:
                html = f.read().replace("__TOKEN__", TOKEN)
            return self._send(200, ctype="text/html", raw=html.encode("utf-8"))
        if self.path.startswith("/preview/") or self.path.startswith("/sample/"):
            return self._audio()
        if self.headers.get("X-Token") != TOKEN:
            return self._send(403, {"error": "token"})
        if self.path == "/api/drives":
            return self._send(200, {"drives": core.list_drives(), "fl": core.FL})
        if self.path.startswith("/api/version"):
            return self._send(200, updater.check(force="force=1" in self.path))
        if self.path == "/api/status":
            return self._send(200, MGR.status)
        if self.path.startswith("/api/projects"):
            # ?rev=N -> si nada cambio responde corto y el navegador no re-dibuja nada
            q = self.path.partition("rev=")[2]
            if q and q == str(MGR.rev):
                return self._send(200, {"same": True, "rev": MGR.rev})
            return self._send(200, raw=_projects_json(), ctype="application/json")
        if self.path == "/api/plugins":
            if not PIX.state["running"] and PIX.mgr_rev != MGR.rev:
                PIX.refresh()
            return self._send(200, PIX.summary())
        if self.path == "/api/relink/index":
            return self._send(200, relink.index_status())
        if self.path == "/api/samples/status":
            return self._send(200, SMP.status())
        if self.path == "/api/samples/summary":
            return self._send(200, SMP.summary())
        if self.path == "/api/backups":
            c = MGR.backup_candidates()
            return self._send(200, {"items": c, "size": sum(x["size"] for x in c)})
        return self._send(404, {"error": "no encontrado"})

    def _audio(self):
        """Sirve un MP3 de vista previa (token en la URL; admite Range para poder adelantar)."""
        from urllib.parse import parse_qs, urlparse
        u = urlparse(self.path)
        if parse_qs(u.query).get("t", [""])[0] != TOKEN:
            return self._send(403, {"error": "token"})
        k = os.path.basename(u.path).split(".")[0]
        f = preview.file_for(k) if u.path.startswith("/preview/") else SMP.path_of(k)
        if not f or not os.path.isfile(f):
            return self._send(404, {"error": "no encontrado"})
        ext = os.path.splitext(f)[1].lower()
        ctype = {".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg", ".aif": "audio/aiff",
                 ".aiff": "audio/aiff"}.get(ext, "audio/mpeg")
        size = os.path.getsize(f)
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else max(0, size - int(b))
            end = int(b) if a and b else end
        end = min(end, size - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        with open(f, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = fh.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (ConnectionError, OSError):
                pass

    def _samples(self, a, b):
        r = _samples_post(self, a, b)
        return self._send(200, r) if r is not None else self._send(404, {"error": "no encontrado"})

    def do_POST(self):
        if not self._ok_host() or self.headers.get("X-Token") != TOKEN:
            return self._send(403, {"error": "token"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "json"})
        p = self.path
        try:
            if p == "/api/scan":
                roots = [d["root"] for d in core.list_drives()]
                sel = [r for r in body.get("roots", []) if r in roots or os.path.isdir(r)]
                return self._send(200, {"started": MGR.start_scan(sel)})
            if p == "/api/scan/pause":
                for r in body.get("roots", []):
                    MGR.pause_scan(r)
                return self._send(200, {"ok": True})
            if p == "/api/scan/forget":
                for r in body.get("roots", []):
                    if r in MGR.jobs:
                        MGR.forget_scan(r)
                return self._send(200, {"ok": True})
            if p == "/api/open":
                for x in _paths(body)[:1]:
                    core.open_in_fl(x)
                return self._send(200, {"ok": True})
            if p == "/api/plugins/refresh":
                PIX.refresh()
                return self._send(200, {"ok": True})
            if p == "/api/plugins/projects":
                return self._send(200, {"items": PIX.projects_using(body.get("name", ""), body.get("fmt", ""))})
            if p == "/api/plugins/project":
                for x in _paths(body)[:1]:
                    return self._send(200, {"items": PIX.for_project(x)})
                return self._send(404, {"error": "Proyecto desconocido"})
            if p == "/api/plugins/export":
                if body.get("all"):
                    # con Serum segun el indice; los aun no leidos tambien se revisan
                    projs = [k for k in MGR.projects if not core.is_backup(k) and
                             (k not in PIX.cache or any(x["id"] == "XfsX" for x in PIX.cache[k][1]))]
                    return self._send(200, {"started": plugins.export_async(projs)})
                r = plugins.export_presets(_paths(body))
                if r["exported"] and body.get("open", True):
                    os.startfile(r["dest"])  # noqa: S606
                return self._send(200, r)
            if p == "/api/relink/index":
                return self._send(200, {"started": relink.build_names(SMP, force=True)})
            if p == "/api/relink/scan":
                for x in _paths(body)[:1]:
                    return self._send(200, relink.candidates(MGR, SMP, x))
                return self._send(404, {"error": "Proyecto desconocido"})
            if p == "/api/flscript":
                try:
                    return self._send(200, _fl_script(bool(body.get("install"))))
                except OSError as e:
                    return self._send(200, {"error": str(e)[:200]})
            if p == "/api/scripts":
                return self._send(200, flscripts.listing())
            if p == "/api/scripts/install":
                try:
                    flscripts.install(body.get("id"))
                except OSError as e:
                    return self._send(200, {"error": str(e)[:200]})
                return self._send(200, flscripts.listing())
            if p == "/api/scripts/uninstall":
                try:
                    flscripts.uninstall(body.get("id"))
                except OSError as e:
                    return self._send(200, {"error": str(e)[:200]})
                return self._send(200, flscripts.listing())
            if p == "/api/scripts/reveal":
                x = body.get("path", "")
                if flscripts.is_inside(x) and os.path.exists(x):
                    core.reveal(x)
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "Ruta no permitida"})
            if p == "/api/orden/check":
                for x in _paths(body)[:1]:
                    try:
                        return self._send(200, flp_ordenar.analizar(x))
                    except FileNotFoundError:
                        return self._send(200, {"error": "El archivo ya no existe"})
                    except Exception as e:  # noqa: BLE001
                        return self._send(200, {"error": str(e)[:200]})
                return self._send(404, {"error": "Proyecto desconocido"})
            if p == "/api/ordenar":
                for x in _paths(body)[:1]:
                    try:
                        r = flp_ordenar.ordenar(x, dry=bool(body.get("dry")))
                    except Exception as e:  # noqa: BLE001 - formato raro, sin arreglos, etc.
                        return self._send(200, {"error": str(e)[:200]})
                    if r["dst"]:
                        import fastscan
                        info = fastscan.analyze(r["dst"], fastscan.FsCache(), core.placeholders())
                        with MGR.lock:
                            MGR.projects[r["dst"]] = info
                        MGR.touch()
                        MGR.save()
                    return self._send(200, r)
                return self._send(404, {"error": "Proyecto desconocido"})
            if p == "/api/relink/apply":
                m = {k: v for k, v in (body.get("map") or {}).items()
                     if os.path.splitext(v or "")[1].lower() in samples.EXTS}
                for x in _paths(body)[:1]:
                    return self._send(200, relink.apply(MGR, x, m))
                return self._send(404, {"error": "Proyecto desconocido"})
            if p.startswith("/api/samples/"):
                return self._samples(p[13:], body)
            if p == "/api/preview":
                for x in _paths(body)[:1]:
                    preview.request(x)
                return self._send(200, {"ok": True})
            if p == "/api/preview/state":
                return self._send(200, {x: preview.state(x) for x in _paths(body)})
            if p == "/api/openurl":
                if body.get("url") in ("https://www.instagram.com/xidian_dj/",):
                    webbrowser.open(body["url"])
                    return self._send(200, {"ok": True})
                return self._send(403, {"error": "URL no permitida"})
            if p == "/api/reveal":
                for x in _paths(body)[:1]:
                    core.reveal(x)
                return self._send(200, {"ok": True})
            if p == "/api/mark":
                MGR.mark(_paths(body), body.get("status"), body.get("note"))
                return self._send(200, {"ok": True, "rev": MGR.rev})
            if p == "/api/backups/clean":
                ok = {x["path"] for x in MGR.backup_candidates()}
                return self._send(200, MGR.recycle([x for x in body.get("paths", []) if x in ok]))
            if p == "/api/delete":
                return self._send(200, MGR.recycle(_paths(body)))
            if p == "/api/pickfolder":
                return self._send(200, {"path": HOOKS["pick_folder"]() or ""})
            if p == "/api/update":
                try:
                    ok = updater.download_and_restart()
                except Exception as e:  # noqa: BLE001 - sin internet, descarga cortada
                    return self._send(200, {"restarting": False, "error": str(e)[:120]})
                if ok:
                    threading.Timer(0.8, HOOKS["quit"]).start()
                return self._send(200, {"restarting": ok})
            if p == "/api/plan":
                return self._send(200, {"plan": MGR.plan(body["dest"], body.get("keys"),
                                                         body.get("onlyUseful", False),
                                                         body.get("includeDiscarded", False))})
            if p == "/api/apply":
                plan = MGR.plan(body["dest"], body.get("keys"), body.get("onlyUseful", False),
                                body.get("includeDiscarded", False))
                return self._send(200, MGR.apply(plan, copy=body.get("copy", False)))
            if p == "/api/undo":
                return self._send(200, MGR.undo_last())
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)})
        return self._send(404, {"error": "no encontrado"})


def _samples_post(h, a, b):
    known = [x for x in b.get("paths", []) if x in SMP.files]
    if a == "scan":
        roots = [d["root"] for d in core.list_drives()]
        SMP.start([r for r in b.get("roots", []) if r in roots or os.path.isdir(r)])
        return {"ok": True}
    if a == "pause":
        for r in b.get("roots", []):
            SMP.pause(r)
        return {"ok": True}
    if a == "query":
        return SMP.query(b.get("cat"), b.get("q", ""), b.get("filter", "all"), b.get("sort", "name"),
                         int(b.get("offset", 0)), min(500, int(b.get("limit", 200))), b.get("group", ""))
    if a == "groups":
        return SMP.groups(b.get("kind", "exact"), int(b.get("offset", 0)), min(100, int(b.get("limit", 40))), b.get("group", ""))
    if a == "setcat":
        SMP.set_cat(known, b.get("cat"))
        return {"ok": True}
    if a == "recycle":
        return SMP.recycle(known)
    if a == "reveal":
        for x in known[:1]:
            core.reveal(x)
        return {"ok": True}
    if a == "plan":
        pl = SMP.plan(b["dest"], b.get("cats"), b.get("skipUsed", True))
        return {"total": len(pl), "plan": pl[:400]}
    if a == "apply":
        return SMP.apply(SMP.plan(b["dest"], b.get("cats"), b.get("skipUsed", True)), copy=b.get("copy", True))
    if a == "undo":
        return SMP.undo_last()
    if a == "dedupe":
        return SMP.dedupe()
    return None


def serve(port=0):
    """Arranca el servidor en un hilo y devuelve la URL."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d/" % srv.server_address[1]


def main():
    url = serve(int(os.environ.get("FLPM_PORT", "8765")))
    print("FL Project Manager en " + url + "  (cierra esta ventana para salir)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    threading.Event().wait()


if __name__ == "__main__":
    main()
