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

TOKEN = secrets.token_urlsafe(24)
MGR = core.Manager()
UI = os.path.join(getattr(sys, "_MEIPASS", core.HERE), "ui.html")
HOOKS = {"pick_folder": core.pick_folder, "quit": lambda: os._exit(0)}


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
        if self.path.startswith("/preview/"):
            return self._audio()
        if self.headers.get("X-Token") != TOKEN:
            return self._send(403, {"error": "token"})
        if self.path == "/api/drives":
            return self._send(200, {"drives": core.list_drives(), "fl": core.FL})
        if self.path == "/api/version":
            return self._send(200, updater.check())
        if self.path == "/api/status":
            return self._send(200, MGR.status)
        if self.path == "/api/projects":
            return self._send(200, {"groups": MGR.listing(), "excluded": MGR.status["excluded"],
                                    "canUndo": bool(MGR.undo)})
        return self._send(404, {"error": "no encontrado"})

    def _audio(self):
        """Sirve un MP3 de vista previa (token en la URL; admite Range para poder adelantar)."""
        from urllib.parse import parse_qs, urlparse
        u = urlparse(self.path)
        if parse_qs(u.query).get("t", [""])[0] != TOKEN:
            return self._send(403, {"error": "token"})
        f = preview.file_for(os.path.basename(u.path).split(".")[0])
        if not f:
            return self._send(404, {"error": "no encontrado"})
        size = os.path.getsize(f)
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else max(0, size - int(b))
            end = int(b) if a and b else end
        end = min(end, size - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "audio/mpeg")
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
            if p == "/api/preview":
                for x in _paths(body)[:1]:
                    preview.request(x)
                return self._send(200, {"ok": True})
            if p == "/api/preview/state":
                return self._send(200, {x: preview.state(x) for x in _paths(body)})
            if p == "/api/reveal":
                for x in _paths(body)[:1]:
                    core.reveal(x)
                return self._send(200, {"ok": True})
            if p == "/api/mark":
                MGR.mark(_paths(body), body.get("status"), body.get("note"))
                return self._send(200, {"ok": True})
            if p == "/api/delete":
                return self._send(200, MGR.recycle(_paths(body)))
            if p == "/api/pickfolder":
                return self._send(200, {"path": HOOKS["pick_folder"]() or ""})
            if p == "/api/update":
                ok = updater.download_and_restart()
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
