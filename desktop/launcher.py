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
# Site the auto-updater asks what the current build is, and where the download
# lives. Not a GitHub repo: the repository is private, so the releases API 404s
# for everyone and the check never fires.
SITE = os.environ.get("IMAGESL_SITE", "https://imagesl.online")


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


def _ensure_std_streams() -> None:
    """Give a windowed build the stdout/stderr that third-party code assumes.

    PyInstaller sets both to None when console=False, and libraries do not
    expect that. uvicorn's default logging config calls sys.stdout.isatty()
    while constructing its formatter; against None that raises AttributeError,
    dictConfig turns it into "Unable to configure formatter 'default'", and the
    server dies before it ever binds. The app then exits with no window and no
    message.

    This is worth understanding rather than patching narrowly, because the
    failure is invisible to testing: run the frozen exe from a shell, or with
    its output redirected - which is what CI does - and stdout is a real handle,
    so everything passes. It only breaks when launched from Explorer, which is
    how every user launches it.

    Only substitutes when the stream is genuinely absent, so a redirected or
    console run keeps its real stream and its output.
    """
    for _name in ("stdout", "stderr"):
        if getattr(sys, _name, None) is None:
            try:
                setattr(sys, _name,
                        open(os.devnull, "w", encoding="utf-8", buffering=1))
            except Exception:
                pass


def _emit(msg: str, err: bool = False) -> None:
    """Write a line without assuming a console exists.

    A windowed PyInstaller build (console=False) leaves sys.stdout/sys.stderr as
    None, so a bare sys.stdout.write() raises AttributeError — which would fail
    --selftest on exactly the builds CI ships. Fall back to the raw fd, and give
    up quietly if that is closed too; the exit code carries the result.
    """
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

    # Start the engine under the conditions a double-clicked windowed build
    # actually runs in: no stdout, no stderr. CI invokes this exe from a shell
    # with its output captured, so both streams are real handles here and the
    # no-console path would never otherwise be exercised - which is precisely
    # how a crash that killed every real launch passed every smoke test.
    _real_out, _real_err = sys.stdout, sys.stderr
    try:
        sys.stdout = sys.stderr = None          # type: ignore[assignment]
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
    # Must run before anything imports uvicorn or touches logging.
    _ensure_std_streams()

    if "--selftest" in sys.argv:
        return _selftest()

    if "--check-update" in sys.argv:
        # Ask once, on demand, and say what came back — including "offline",
        # which the launch-time check deliberately keeps silent about. An
        # offline install otherwise has no way to distinguish "up to date" from
        # "has not been able to ask since it was installed".
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


def _crash_log() -> Path:
    """Where a windowed build leaves its traceback.

    A console build prints and the user can read it. A windowed build has no
    stdout at all, so an unhandled exception is simply an app that vanishes -
    which is exactly what happened here, and is unsupportable: there is nothing
    to send us and nothing to search for. The traceback goes to a file instead.
    """
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
    """Win32 message box - the one way to reach the user with no window toolkit
    and no console. Used when the GUI stack itself is what failed."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, APP_NAME, 0x10)  # MB_ICONERROR
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
        # The window toolkit is a plausible cause of the failure we are
        # reporting, so never let it swallow the report.
        pass
    _message_box(msg)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:                      # noqa: BLE001 - last line of defence
        import traceback
        _tb = traceback.format_exc()
        _where = _record_crash(_tb)
        _fatal("ImageSL could not start.\n\n"
               + (f"The details were written to:\n{_where}" if _where else _tb))
        raise SystemExit(1)
