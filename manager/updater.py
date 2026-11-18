"""Autoactualizacion desde GitHub Releases (solo cuando corre como .exe)."""
import json
import os
import subprocess
import sys
import tempfile
import urllib.request

from version import ASSET, REPO, VERSION

UA = {"User-Agent": "FLP-Manager-Updater", "Accept": "application/vnd.github+json"}
_cache = {}


def frozen():
    return getattr(sys, "frozen", False)


def _ver(tag):
    try:
        return tuple(int(x) for x in tag.lstrip("vV").split("."))
    except ValueError:
        return (0,)


def check(force=False):
    """{"current","latest","available","notes","url"}"""
    if _cache and not force:
        return _cache
    res = {"current": VERSION, "latest": VERSION, "available": False, "notes": "", "url": "",
           "frozen": frozen()}
    if REPO.startswith("OWNER/"):
        res["error"] = "Repositorio no configurado"
        return res
    try:
        req = urllib.request.Request("https://api.github.com/repos/%s/releases/latest" % REPO,
                                     headers=UA)
        with urllib.request.urlopen(req, timeout=8) as r:
            rel = json.load(r)
        asset = next((a for a in rel.get("assets", []) if a["name"] == ASSET), None)
        res.update(latest=rel.get("tag_name", VERSION).lstrip("vV"), notes=rel.get("body") or "",
                   url=asset["browser_download_url"] if asset else "")
        res["available"] = bool(asset) and _ver(res["latest"]) > _ver(VERSION)
    except Exception as e:  # noqa: BLE001 - sin internet, rate limit, etc.
        res["error"] = str(e)
    _cache.clear()
    _cache.update(res)
    return res


def download_and_restart():
    """Descarga el nuevo .exe, programa el reemplazo y devuelve True (la app debe cerrarse)."""
    info = check()
    if not (info["available"] and frozen()):
        return False
    exe = sys.executable
    new = exe + ".new"
    req = urllib.request.Request(info["url"], headers={"User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=120) as r, open(new, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    if os.path.getsize(new) < 1_000_000:  # descarga incompleta
        os.remove(new)
        raise IOError("Descarga incompleta")
    bat = os.path.join(tempfile.gettempdir(), "flpm_update.bat")
    with open(bat, "w", encoding="mbcs") as f:
        f.write('@echo off\r\n:w\r\ntasklist /FI "PID eq %d" | find "%d" >nul && '
                '(timeout /t 1 /nobreak >nul & goto w)\r\n'
                'move /y "%s" "%s" >nul\r\nstart "" "%s"\r\ndel "%%~f0"\r\n'
                % (os.getpid(), os.getpid(), new, exe, exe))
    subprocess.Popen(["cmd", "/c", bat], creationflags=0x08000000 | 0x00000008)
    return True
