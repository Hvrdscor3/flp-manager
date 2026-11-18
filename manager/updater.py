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
    if not (info["available"] and frozen()) or _cache.get("started"):
        return bool(_cache.get("started"))
    _cache["started"] = True
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
    # El .exe de PyInstaller son 2 procesos (lanzador + app): se espera a ambos y se
    # reintenta el reemplazo hasta que Windows suelte el archivo.
    pids = {os.getpid(), os.getppid()}
    wait = "".join('tasklist /FI "PID eq %d" 2>nul | find " %d " >nul && goto w\n' % (p, p)
                   for p in pids)
    bat = os.path.join(tempfile.gettempdir(), "flpm_update.bat")
    with open(bat, "w", encoding="mbcs", newline="\r\n") as f:
        f.write('@echo off\n'
                ':w\nping -n 2 127.0.0.1 >nul\n' + wait +
                'set n=0\n'
                ':m\nmove /y "%s" "%s" >nul 2>&1 && goto ok\n'
                'set /a n+=1\nif %%n%% gtr 30 goto ok\nping -n 2 127.0.0.1 >nul\ngoto m\n'
                ':ok\nstart "" "%s"\ndel "%%~f0"\n' % (new, exe, exe))
    # Entorno limpio: si el nuevo .exe hereda las variables de PyInstaller de este
    # proceso, busca la carpeta temporal vieja y falla ("Failed to load Python DLL").
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("_PYI", "_MEI"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen(["cmd", "/c", bat], creationflags=0x08000000 | 0x00000200, env=env,
                     close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    return True
