"""
ImageSL desktop launcher — the offline app.

Unlike the old thin client, this bundles the ENTIRE analysis engine. It starts
the real FastAPI backend on a private localhost port, waits for it to come up,
and opens a native window pointed at the analyzer. No server, no account, no
license key, no network — a slide never leaves the machine. The only thing that
touches the network is a best-effort check for a newer release (see updater.py),
and even that fails silently offline.

Runs two ways, unchanged:
  * from source, for development:   python desktop/launcher.py
  * frozen by PyInstaller:          ImageSL.exe   (server/ bundled alongside)
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

APP_NAME = "ImageSL"
# Repo the auto-updater watches. Keep in step with landing.html's REPO.
REPO = os.environ.get("IMAGESL_REPO", "solvergent/ImageSL")


# --------------------------------------------------------------------------- #
# Locate the bundled server package, whether frozen or running from source.
# --------------------------------------------------------------------------- #
def _server_dir() -> Path:
    if getattr(sys, "frozen", False):
        # PyInstaller unpacks datas under sys._MEIPASS.
        return Path(sys._MEIPASS) / "server"          # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent / "server"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _configure_env(port: int) -> None:
    """Local, single-user, offline defaults — set BEFORE the app is imported."""
    os.environ["IMAGESL_DESKTOP"] = "1"          # "/" boots straight into the analyzer
    os.environ.pop("IMAGESL_ACCESS_TOKENS", None)  # no auth on a loopback socket
    os.environ.setdefault("IMAGESL_VERSION", _read_version())
    os.environ["IMAGESL_PORT"] = str(port)
    # Keep the disk cache inside the user's app-data, not a random /data volume.
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") \
        or os.path.expanduser("~/.imagesl")
    cache = Path(base) / APP_NAME / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("IMAGESL_CACHE_DIR", str(cache))


def _read_version() -> str:
    # version.txt is written at build time; falls back for source runs.
    for p in (_server_dir().parent / "version.txt",
              Path(__file__).resolve().parent / "version.txt"):
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return "0.0.0-dev"


# --------------------------------------------------------------------------- #
# The embedded server
# --------------------------------------------------------------------------- #
def _start_server(port: int):
    """Import the real app and serve it on the loopback interface, in a thread."""
    sys.path.insert(0, str(_server_dir()))
    import uvicorn
    from app import app  # the actual ImageSL FastAPI application

    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    t = threading.Thread(target=server.run, name="imagesl-server", daemon=True)
    t.start()
    return server


def _wait_healthy(port: int, timeout: float = 40.0) -> bool:
    import urllib.request
    url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


# --------------------------------------------------------------------------- #
# Update check (best-effort, non-blocking)
# --------------------------------------------------------------------------- #
def _check_update_async(window) -> None:
    def run():
        try:
            from updater import check_for_update
        except Exception:
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                from updater import check_for_update
            except Exception:
                return
        info = check_for_update(_read_version(), REPO)
        if info.get("available") and window is not None:
            _show_update_banner(window, info)
    threading.Thread(target=run, name="imagesl-update", daemon=True).start()


def _show_update_banner(window, info: dict) -> None:
    ver = str(info.get("version", "")).replace("'", "")
    url = str(info.get("url", "")).replace("'", "")
    js = """
    (function(){
      if (document.getElementById('imagesl-update')) return;
      var b=document.createElement('div'); b.id='imagesl-update';
      b.style.cssText='position:fixed;left:0;right:0;bottom:0;z-index:99999;'+
        'background:linear-gradient(135deg,#8b5cf6,#6d28d9);color:#fff;'+
        'font:14px -apple-system,Segoe UI,sans-serif;padding:11px 18px;'+
        'display:flex;align-items:center;gap:14px;justify-content:center;'+
        'box-shadow:0 -6px 24px rgba(76,42,140,.35)';
      b.innerHTML='A new version of ImageSL ('+%r+') is available. '+
        '<a href=\"'+%r+'\" target=\"_blank\" style=\"color:#fff;font-weight:700;text-decoration:underline\">Download</a>'+
        '<span onclick=\"this.parentNode.remove()\" style=\"cursor:pointer;opacity:.8;margin-left:6px\">✕</span>';
      document.body.appendChild(b);
    })();
    """ % (ver, url)
    try:
        window.evaluate_js(js)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _selftest() -> int:
    """Headless smoke test of the FROZEN bundle: start the engine, confirm it
    answers, exit. Used by CI and by `ImageSL --selftest` — proves the packaged
    app can actually run the analysis code without needing a display."""
    port = _free_port()
    _configure_env(port)
    _start_server(port)
    if not _wait_healthy(port):
        sys.stderr.write("SELFTEST FAIL: engine did not become healthy\n")
        return 1
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stains", timeout=4) as r:
            body = r.read().decode("utf-8", "ignore")
        assert '"dab"' in body, "stains endpoint missing dab"
    except Exception as exc:
        sys.stderr.write(f"SELFTEST FAIL: {exc}\n")
        return 1
    sys.stdout.write(f"SELFTEST OK  version={_read_version()}  port={port}\n")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()

    port = _free_port()
    _configure_env(port)
    _start_server(port)

    if not _wait_healthy(port):
        _fatal("ImageSL could not start its analysis engine.\n\n"
               "Please reinstall the app, or report this if it persists.")
        return 1

    url = f"http://127.0.0.1:{port}/"
    icon = _server_dir().parent / "ImageSL.ico"

    import webview  # pywebview
    window = webview.create_window(
        APP_NAME, url,
        width=1400, height=900, min_size=(1024, 720),
        background_color="#faf9fd",
    )
    _check_update_async(window)
    # gui="edgechromium" on Windows (WebView2); pywebview auto-selects per OS.
    webview.start(icon=str(icon) if icon.is_file() else None)
    return 0


def _fatal(msg: str) -> None:
    try:
        import webview
        webview.create_window(APP_NAME, html=(
            "<body style='font:15px sans-serif;padding:40px;color:#1c1630'>"
            "<h2>ImageSL</h2><p>" + msg.replace("\n", "<br>") + "</p></body>"))
        webview.start()
    except Exception:
        sys.stderr.write(msg + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
