#!/usr/bin/env python3
"""
rexauto desktop app — the .exe entry point.

Two modes in one binary:
  • default            open the native GUI window (WebView2) over the local server
  • --__pipeline ...   run the recompilation pipeline (the GUI re-invokes the exe
                       in this mode so it can stream the pipeline's output)
"""
import os
import sys
import threading
import time


def _base():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _setup_paths():
    base = _base()
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (here, base, os.path.join(base, "gui"), os.path.join(here, "gui")):
        if p and os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)


def run_pipeline():
    argv = [a for a in sys.argv[1:] if a != "--__pipeline"]
    sys.argv = [sys.argv[0]] + argv
    import rexauto  # noqa
    rexauto.main()


def run_gui():
    import server  # gui/server.py
    srv, port = server.make_server()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % port
    title = "rexauto · Recompilation Engine"
    try:
        import webview
        webview.create_window(title, url, width=1240, height=880, min_size=(900, 680),
                              background_color="#05060c")
        webview.start()
    except Exception as ex:
        sys.stderr.write("native window unavailable (%s); opening in browser\n" % ex)
        import webbrowser
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


def main():
    _setup_paths()
    # make sure these are pulled into a frozen build
    import extract, heal, rexauto, detect_setjmp  # noqa
    if "--__pipeline" in sys.argv:
        run_pipeline()
    else:
        run_gui()


if __name__ == "__main__":
    main()
