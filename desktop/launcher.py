from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

APP_NAME = "ImageSL"
SITE = os.environ.get("IMAGESL_SITE", "https://imagesl.com")

def _server_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "server"
    return Path(__file__).resolve().parent.parent / "server"

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])

def _ensure_std_streams() -> None:
    for _name in ("stdout", "stderr"):
        if getattr(sys, _name, None) is None:
            try:
                setattr(sys, _name,
                        open(os.devnull, "w", encoding="utf-8", buffering=1))
            except Exception:
                pass

def _emit(msg: str, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    if stream is not None:
        try:
            stream.write(msg)
            stream.flush()
            return
        except Exception:
            pass
    try:
        os.write(2 if err else 1, msg.encode("utf-8", "replace"))
    except Exception:
        pass

def _configure_env(port: int) -> None:
    os.environ["IMAGESL_DESKTOP"] = "1"
    os.environ.pop("IMAGESL_ACCESS_TOKENS", None)
    os.environ.setdefault("IMAGESL_VERSION", _read_version())
    os.environ["IMAGESL_PORT"] = str(port)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") \
        or os.path.expanduser("~/.imagesl")
    cache = Path(base) / APP_NAME / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("IMAGESL_CACHE_DIR", str(cache))

def _read_version() -> str:
    for p in (_server_dir().parent / "version.txt",
              Path(__file__).resolve().parent / "version.txt"):
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return "0.0.0-dev"

def _start_server(port: int):
    sys.path.insert(0, str(_server_dir()))
    import uvicorn
    from app import app

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
        info = check_for_update(_read_version(), SITE)
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
        '<a href=\"javascript:void(0)\" onclick=\"if(window.pywebview && window.pywebview.api) window.pywebview.api.open_external('+%r+'); else window.open('+%r+', \\'_blank\\')\" style=\"color:#fff;font-weight:700;text-decoration:underline\">Download</a>'+
        '<span onclick=\"this.parentNode.remove()\" style=\"cursor:pointer;opacity:.8;margin-left:6px\">✕</span>';
      document.body.appendChild(b);
    })();
    """ % (ver, url, url)
    try:
        window.evaluate_js(js)
    except Exception:
        pass

def _enable_downloads() -> None:
    try:
        import webview
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    except Exception:
        pass

def _start_diagnostics_log() -> None:
    try:
        import logging
        path = _crash_log().with_name("imagesl.log")
        handler = logging.FileHandler(path, mode="w", encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        logging.getLogger("pywebview").setLevel(logging.DEBUG)
        logging.info("ImageSL %s starting (frozen=%s, platform=%s)",
                     _read_version(), getattr(sys, "frozen", False), sys.platform)
    except Exception:
        pass

def _selftest() -> int:
    port = _free_port()
    _configure_env(port)

    _real_out, _real_err = sys.stdout, sys.stderr
    try:
        sys.stdout = sys.stderr = None
        _ensure_std_streams()
        _start_server(port)
        healthy = _wait_healthy(port)
    finally:
        sys.stdout, sys.stderr = _real_out, _real_err

    if not healthy:
        _emit("SELFTEST FAIL: engine did not become healthy\n", err=True)
        return 1
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stains", timeout=4) as r:
            body = r.read().decode("utf-8", "ignore")
        assert '"dab"' in body, "stains endpoint missing dab"
    except Exception as exc:
        _emit(f"SELFTEST FAIL: {exc}\n", err=True)
        return 1
    _emit(f"SELFTEST OK  version={_read_version()}  port={port}\n")
    return 0

def main() -> int:
    _ensure_std_streams()

    if "--selftest" in sys.argv:
        return _selftest()

    if "--check-update" in sys.argv:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from updater import check_for_update, describe
        except Exception as exc:
            _emit(f"Update check unavailable: {exc}\n", err=True)
            return 20
        info = check_for_update(_read_version(), SITE)
        _emit(f"ImageSL {_read_version()}\n{describe(info)}\n")
        return (10 if info.get("available")
                else 20 if info.get("status") in ("offline", "unreadable")
                else 0)

    port = _free_port()
    _configure_env(port)
    _start_server(port)

    if not _wait_healthy(port):
        _fatal("ImageSL could not start its analysis engine.\n\n"
               "Please reinstall the app, or report this if it persists.")
        return 1

    url = f"http://127.0.0.1:{port}/"
    icon = _server_dir().parent / "ImageSL.ico"

    _start_diagnostics_log()
    import webview
    _enable_downloads()
    
    class Api:
        def open_external(self, link: str) -> None:
            import webbrowser
            webbrowser.open(link)

    window = webview.create_window(
        APP_NAME, url,
        js_api=Api(),
        width=1400, height=900, min_size=(1024, 720),
        background_color="#faf9fd",
    )
    _check_update_async(window)
    webview.start(icon=str(icon) if icon.is_file() else None)
    return 0

def _crash_log() -> Path:
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            or os.path.expanduser("~/.imagesl"))
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d / "last-error.log"

def _record_crash(text: str):
    try:
        p = _crash_log()
        p.write_text(f"ImageSL {_read_version()}\n"
                     f"frozen={getattr(sys, 'frozen', False)}\n"
                     f"python={sys.version}\n\n{text}\n", encoding="utf-8")
        return p
    except Exception:
        return None

def _message_box(msg: str) -> bool:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, APP_NAME, 0x10)
        return True
    except Exception:
        return False

def _fatal(msg: str) -> None:
    _emit(msg + "\n", err=True)
    try:
        import webview
        webview.create_window(APP_NAME, html=(
            "<body style='font:15px -apple-system,Segoe UI,sans-serif;padding:40px;color:#0d0f12'>"
            "<h2 style='font-weight:400'>ImageSL</h2><p style='color:#4d535c'>"
            + msg.replace("\n", "<br>") + "</p></body>"))
        webview.start()
        return
    except Exception:
        pass
    _message_box(msg)

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        import traceback
        _tb = traceback.format_exc()
        _where = _record_crash(_tb)
        _fatal("ImageSL could not start.\n\n"
               + (f"The details were written to:\n{_where}" if _where else _tb))
        raise SystemExit(1)
