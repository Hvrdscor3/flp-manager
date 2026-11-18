"""Punto de entrada de FLP Manager.exe: ventana nativa (WebView2) sobre el servidor local."""
import os
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
from version import VERSION  # noqa: E402


def main():
    url = server.serve(0)
    try:
        import webview
    except ImportError:
        webview = None
    if webview is None:
        webbrowser.open(url)
        import threading
        threading.Event().wait()
        return

    win = webview.create_window("FLP Manager %s" % VERSION, url, width=1320, height=860,
                                min_size=(900, 600), background_color="#0f0d14")

    def pick_folder():
        r = win.create_file_dialog(webview.FOLDER_DIALOG)
        return r[0] if r else ""

    def quit_app():
        try:
            win.destroy()
        finally:
            os._exit(0)

    server.HOOKS["pick_folder"] = pick_folder
    server.HOOKS["quit"] = quit_app
    webview.start(private_mode=False, storage_path=os.path.join(
        os.environ.get("APPDATA", "."), "FLP Manager", "webview"))
    os._exit(0)


if __name__ == "__main__":
    main()
