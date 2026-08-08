"""Settings, update checking and updating — desktop build only.

These routes are registered ONLY when IMAGESL_DESKTOP=1, so the public web
deployment never exposes them. The analyzer asks for /api/desktop/info on load;
a 404 simply means "not the desktop app" and the settings button stays hidden.

Design decisions worth keeping:

* **Updates download automatically (optionally), but never install themselves.**
  Installing means relaunching, and relaunching mid-session throws away the
  batch the user is working on. Worse, a quantification tool that swaps its own
  analysis code out from under a half-finished experiment produces two sets of
  numbers from one sitting with nothing on screen to say so. The download is the
  slow part and can be done quietly; pressing "Install" stays a decision.

* **Offline is normal, not an error.** Every network call here fails silently
  into "unknown" and the app carries on. The engine is local; the network is
  only ever consulted about updates.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any, Dict

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

APP_NAME = "ImageSL"
TIMEOUT = 5.0

DEFAULTS: Dict[str, Any] = {
    "check_updates_on_launch": True,
    "auto_download_updates": False,
    "prefer_online_engine": True,
}


# --------------------------------------------------------------------------- #
# Where our own data lives
# --------------------------------------------------------------------------- #
def _data_dir() -> Path:
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            or os.path.expanduser("~/.imagesl"))
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _settings_path() -> Path:
    return _data_dir() / "settings.json"


def _updates_dir() -> Path:
    d = _data_dir() / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_settings() -> Dict[str, Any]:
    """Defaults overlaid with whatever is on disk. A corrupt file is ignored
    rather than fatal — bad settings must never stop the app from opening."""
    out = dict(DEFAULTS)
    try:
        p = _settings_path()
        if p.is_file():
            stored = json.loads(p.read_text(encoding="utf-8"))
            for k in DEFAULTS:
                if k in stored:
                    out[k] = bool(stored[k])
    except Exception:
        pass
    return out


def save_settings(values: Dict[str, Any]) -> Dict[str, Any]:
    current = load_settings()
    for k in DEFAULTS:
        if k in values and values[k] is not None:
            current[k] = bool(values[k])
    try:
        _settings_path().write_text(json.dumps(current, indent=2), encoding="utf-8")
    except Exception:
        pass
    return current


# --------------------------------------------------------------------------- #
# Version comparison
# --------------------------------------------------------------------------- #
def _norm(v: str) -> tuple:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:4]) if nums else (0,)


def is_newer(candidate: str, current: str) -> bool:
    a, b = _norm(candidate), _norm(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


# --------------------------------------------------------------------------- #
# Talking to the site
# --------------------------------------------------------------------------- #
def _site() -> str:
    return (os.environ.get("IMAGESL_SITE") or "https://imagesl.online").rstrip("/")


def _platform_key() -> str:
    return "windows" if sys.platform.startswith("win") else "macos"


def _online() -> bool:
    """Cheap reachability probe: DNS + TCP only, no HTTP round trip.

    The port comes from the URL rather than being assumed to be 443, so a site
    on any other port (a staging host, or a local server during development)
    is not misreported as offline.
    """
    url = _site()
    scheme, _, rest = url.partition("://")
    hostport = rest.split("/", 1)[0]
    if hostport.startswith("["):                       # IPv6 literal
        host, _, tail = hostport[1:].partition("]")
        port_s = tail[1:] if tail.startswith(":") else ""
    else:
        host, _, port_s = hostport.partition(":")
    try:
        port = int(port_s) if port_s else (80 if scheme == "http" else 443)
    except ValueError:
        port = 443
    try:
        with socket.create_connection((host, port), timeout=2.5):
            return True
    except Exception:
        return False


def fetch_remote() -> Dict[str, Any]:
    """What the site is currently offering. Never raises."""
    try:
        req = urllib.request.Request(
            f"{_site()}/api/downloads",
            headers={"Accept": "application/json", "User-Agent": "ImageSL-Desktop"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if r.status != 200:
                return {}
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# Download state (one at a time, reported to the UI by polling)
# --------------------------------------------------------------------------- #
_dl_lock = threading.Lock()
_dl: Dict[str, Any] = {"state": "idle", "percent": 0, "file": None, "error": None,
                       "version": None}


def _dl_snapshot() -> Dict[str, Any]:
    with _dl_lock:
        return dict(_dl)


def _dl_set(**kw) -> None:
    with _dl_lock:
        _dl.update(kw)


def _download_worker(version: str) -> None:
    url = f"{_site()}/download/{_platform_key()}"
    suffix = ".exe" if _platform_key() == "windows" else ".dmg"
    target = _updates_dir() / f"ImageSL-{version}{suffix}"
    part = target.with_suffix(target.suffix + ".part")
    try:
        _dl_set(state="downloading", percent=0, error=None, file=None, version=version)
        req = urllib.request.Request(url, headers={"User-Agent": "ImageSL-Desktop"})
        with urllib.request.urlopen(req, timeout=30) as r:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(part, "wb") as fh:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        _dl_set(percent=min(99, int(done * 100 / total)))
        # Rename only once the bytes are all here, so a half-written installer
        # can never be presented as ready to run.
        part.replace(target)
        _dl_set(state="ready", percent=100, file=str(target))
    except Exception as exc:
        try:
            part.unlink(missing_ok=True)
        except Exception:
            pass
        _dl_set(state="error", error=str(exc)[:300], file=None)


def start_download(version: str) -> Dict[str, Any]:
    snap = _dl_snapshot()
    if snap["state"] == "downloading":
        return snap
    threading.Thread(target=_download_worker, args=(version,),
                     name="imagesl-update-dl", daemon=True).start()
    return _dl_snapshot()


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
class SettingsBody(BaseModel):
    check_updates_on_launch: bool | None = None
    auto_download_updates: bool | None = None
    prefer_online_engine: bool | None = None


def register(app, app_version: str, cache_dir: Path | None) -> None:
    """Attach the desktop-only routes to `app`."""

    def _update_state() -> Dict[str, Any]:
        online = _online()
        remote = fetch_remote() if online else {}
        latest = str(remote.get("version") or "") or None
        platforms = remote.get("platforms") or {}
        can_get = bool((platforms.get(_platform_key()) or {}).get("available"))
        available = bool(latest and is_newer(latest, app_version) and can_get)
        return {
            "online": online,
            "checked": bool(remote),
            "latest_version": latest,
            "update_available": available,
            "download": _dl_snapshot(),
        }

    @app.get("/api/desktop/info")
    def desktop_info() -> JSONResponse:
        cache_bytes = 0
        if cache_dir and Path(cache_dir).is_dir():
            try:
                cache_bytes = sum(f.stat().st_size
                                  for f in Path(cache_dir).rglob("*") if f.is_file())
            except Exception:
                cache_bytes = 0
        return JSONResponse({
            "app": APP_NAME,
            "version": app_version,
            "platform": _platform_key(),
            "python": sys.version.split()[0],
            "site": _site(),
            "data_dir": str(_data_dir()),
            "cache_dir": str(cache_dir) if cache_dir else None,
            "cache_bytes": cache_bytes,
            "log_file": str(_data_dir() / "last-error.log"),
            "settings": load_settings(),
            "update": _update_state(),
        })

    @app.post("/api/desktop/settings")
    def desktop_settings(body: SettingsBody) -> JSONResponse:
        return JSONResponse({"settings": save_settings(body.model_dump())})

    @app.post("/api/desktop/check-update")
    def desktop_check_update() -> JSONResponse:
        state = _update_state()
        # Honour the auto-download preference at the moment we learn there is
        # something to fetch, so the user does not have to come back and ask.
        if (state["update_available"]
                and load_settings().get("auto_download_updates")
                and _dl_snapshot()["state"] in ("idle", "error")):
            start_download(state["latest_version"])
            state["download"] = _dl_snapshot()
        return JSONResponse(state)

    @app.post("/api/desktop/download-update")
    def desktop_download_update() -> JSONResponse:
        state = _update_state()
        if not state["update_available"]:
            raise HTTPException(status_code=409,
                                detail="There is no update to download.")
        return JSONResponse(start_download(state["latest_version"]))

    @app.get("/api/desktop/download-progress")
    def desktop_download_progress() -> JSONResponse:
        return JSONResponse(_dl_snapshot())

    @app.post("/api/desktop/install-update")
    def desktop_install_update() -> JSONResponse:
        snap = _dl_snapshot()
        path = snap.get("file")
        if snap.get("state") != "ready" or not path or not Path(path).is_file():
            raise HTTPException(status_code=409,
                                detail="No downloaded update is ready to install.")
        try:
            if sys.platform.startswith("win"):
                # Hand off to the installer and let this process exit; it cannot
                # overwrite files it is still holding open.
                subprocess.Popen([path], close_fds=True)
            else:
                subprocess.Popen(["open", path], close_fds=True)
        except Exception as exc:
            raise HTTPException(status_code=500,
                                detail=f"Could not start the installer: {exc}")
        return JSONResponse({"started": True, "file": path})

    @app.post("/api/desktop/clear-cache")
    def desktop_clear_cache() -> JSONResponse:
        removed = 0
        if cache_dir and Path(cache_dir).is_dir():
            for f in Path(cache_dir).rglob("*"):
                if f.is_file():
                    try:
                        f.unlink()
                        removed += 1
                    except Exception:
                        pass
        return JSONResponse({"removed": removed})
