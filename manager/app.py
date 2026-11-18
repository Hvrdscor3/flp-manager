"""Punto de entrada de FLP Manager.exe: ventana nativa (WebView2) sobre el servidor local.

Al abrirse busca una version nueva en GitHub; si la hay, la instala sola y se reinicia.
"""
import os
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
import updater  # noqa: E402
from version import VERSION  # noqa: E402

SPLASH = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#00203F;font:15px 'Segoe UI',system-ui,sans-serif;color:#E6E9EC}
.w{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px}
.bar{width:220px;height:5px;border-radius:9px;background:#ffffff22;overflow:hidden}
.bar div{width:40%;height:100%;background:#36ECDE;border-radius:9px;animation:m 1.1s ease-in-out infinite}
@keyframes m{0%{transform:translateX(-100%)}100%{transform:translateX(250%)}}
small{color:#8199b0}</style></head><body><div class="w">
<svg width="56" height="56" viewBox="0 0 32 32"><rect width="32" height="32" rx="7.5" fill="#36ECDE"/>
<rect x="4.5" y="5.4" width="5.8" height="21.2" rx="1.4" fill="#00203F"/><rect x="4.5" y="10.3" width="3.5" height="3" fill="#36ECDE"/><rect x="4.5" y="18.2" width="3.5" height="3" fill="#36ECDE"/>
<rect x="12.8" y="6.4" width="7" height="3" rx="1" fill="#00203F"/><rect x="17.9" y="10.3" width="9.6" height="3" rx="1" fill="#00203F"/>
<rect x="12.8" y="14.3" width="3.8" height="3" rx="1" fill="#fff"/><rect x="19.2" y="18.2" width="5.8" height="3" rx="1" fill="#00203F"/>
<rect x="14.1" y="22.1" width="13.4" height="3" rx="1" fill="#00203F"/></svg>
<b>Actualizando a la versi&oacute;n {V}&hellip;</b><div class="bar"><div></div></div>
<small>La app se reiniciar&aacute; sola en unos segundos</small></div></body></html>"""


def windows_dark():
    """True si Windows usa el tema oscuro para apps (evita destello blanco al abrir)."""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def main():
    url = server.serve(0)
    try:
        import webview
    except ImportError:
        webview = None
    if webview is None:
        webbrowser.open(url)
        threading.Event().wait()
        return

    # Buscar actualizacion antes de mostrar la app (si no hay internet, sigue normal).
    info = updater.check() if updater.frozen() else {"available": False}
    updating = bool(info.get("available"))

    if updating:
        win = webview.create_window("FLP Manager", html=SPLASH.replace("{V}", info["latest"]), width=460,
                                    height=300, resizable=False, background_color="#00203F")
    else:
        win = webview.create_window("FLP Manager %s" % VERSION, url, width=1320, height=860,
                                    min_size=(900, 600),
                                    background_color="#232323" if windows_dark() else "#f7f8f9")

    def pick_folder():
        r = win.create_file_dialog(webview.FOLDER_DIALOG)
        return r[0] if r else ""

    def quit_app():
        # destroy() desde otro hilo puede quedarse esperando a la UI: salida forzada igual.
        threading.Timer(1.5, lambda: os._exit(0)).start()
        try:
            win.destroy()
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)

    def auto_update():
        try:
            if updater.download_and_restart():
                quit_app()
                return
        except Exception:  # noqa: BLE001 - si falla, se abre la version actual
            pass
        win.set_title("FLP Manager %s" % VERSION)
        win.resize(1320, 860)
        win.load_url(url)

    server.HOOKS["pick_folder"] = pick_folder
    server.HOOKS["quit"] = quit_app
    webview.start(auto_update if updating else None, private_mode=False,
                  storage_path=os.path.join(os.environ.get("APPDATA", "."), "FLP Manager", "webview"))
    os._exit(0)


if __name__ == "__main__":
    main()
