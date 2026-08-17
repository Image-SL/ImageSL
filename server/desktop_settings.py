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

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

import delta_update as delta
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

APP_NAME = "ImageSL"
TIMEOUT = 5.0

DEFAULTS: Dict[str, Any] = {
    "check_updates_on_launch": True,
    "auto_download_updates": False,
}

# Deliberately absent: any option to "use the online engine when online".
# Analysis always runs locally. Routing slides to a server when a connection
# happens to exist would silently break the one guarantee this app makes - that
# a slide never leaves the machine - and it would do so invisibly, varying with
# the network. Online/offline affects updates and nothing else.


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
    return (os.environ.get("IMAGESL_SITE") or "https://imagesl.com").rstrip("/")


def _platform_key() -> str:
    return "windows" if sys.platform.startswith("win") else "macos"


_online_cache: tuple[float, bool] | None = None
_online_lock = threading.Lock()
_ONLINE_TTL_OK = 30.0
_ONLINE_TTL_BAD = 10.0


def _online() -> bool:
    """Is the site reachable? Cached, because this blocks.

    Opening Settings used to pay a fresh TCP connect with a 2.5 s timeout, so
    on a machine with no network the panel sat empty for two and a half seconds
    before drawing - which reads as the app hanging. The answer barely changes
    within a few seconds, so it is cached; a negative result expires sooner so
    reconnecting is noticed quickly.
    """
    global _online_cache
    now = time.time()
    with _online_lock:
        if _online_cache is not None:
            checked_at, val = _online_cache
            if now - checked_at < (_ONLINE_TTL_OK if val else _ONLINE_TTL_BAD):
                return val
    val = _probe_online()
    with _online_lock:
        _online_cache = (time.time(), val)
    return val


def _probe_online() -> bool:
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
# `method` is how this update will be applied: "installer" re-runs the full
# ~76 MB setup, "delta" replaces only the files that actually changed. The UI
# reads it to say which is happening, and install-update branches on it.
_dl: Dict[str, Any] = {"state": "idle", "percent": 0, "file": None, "error": None,
                       "version": None, "sha256": None, "verified": False,
                       "method": "installer", "files": 0, "bytes": 0,
                       "staging": None}


def _dl_snapshot() -> Dict[str, Any]:
    with _dl_lock:
        return dict(_dl)


def _dl_set(**kw) -> None:
    with _dl_lock:
        _dl.update(kw)


# The plan that produced the staged files, kept so install-update applies
# exactly what was downloaded and verified rather than recomputing it against a
# tree that may have changed in between.
_delta_plans: Dict[str, Any] = {}


def _relaunch() -> None:
    """Start a fresh copy of the app and let this one go.

    The engine's .py files have just been replaced on disk, but this process
    imported the old ones and still holds them in memory, so the update only
    takes effect on the next launch. Exit is deferred a moment so this
    request's response reaches the page first — otherwise the user sees the
    window vanish with no confirmation that anything worked.
    """
    try:
        subprocess.Popen([sys.executable], close_fds=True)
    except Exception:
        return

    def bye() -> None:
        time.sleep(1.5)
        os._exit(0)

    threading.Thread(target=bye, name="imagesl-relaunch", daemon=True).start()


def _installer_path(version: str) -> Path:
    suffix = ".exe" if _platform_key() == "windows" else ".dmg"
    return _updates_dir() / f"ImageSL-{version}{suffix}"


# Versions this process has already tried to adopt, so the hash below runs once
# and not on every poll of /api/desktop/info.
_adopted: set = set()


def adopt_downloaded(version: str, expected_sha: str) -> bool:
    """Re-discover an installer that an EARLIER run already downloaded.

    The download state above lives in memory only. Closing the app between
    "Download" and "Install and restart" therefore stranded the file: the next
    launch reported `idle`, offered to fetch the same ~76 MB again, and
    /api/desktop/install-update answered "No downloaded update is ready to
    install" — while the verified installer sat in `updates/` the whole time.
    That is how a machine ends up holding a fixed build it can never reach, and
    it is worse than a plain failure because nothing on screen says so.

    Adoption is held to exactly the same bar as a fresh download rather than
    trusting the filename: the file becomes `ready` only if it hashes to the
    checksum the site publishes NOW. Anything else — a partial file from a
    killed run, a leftover from a build that has since been replaced, a file
    something else has written to — is deleted rather than offered, because the
    next thing that happens to it is being executed.
    """
    if not version or not expected_sha:
        return False                      # nothing to check it against
    key = (version, expected_sha.lower())
    with _dl_lock:
        if key in _adopted or _dl["state"] != "idle":
            return False
        _adopted.add(key)

    path = _installer_path(version)
    if not path.is_file():
        return False
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return False

    got = digest.hexdigest()
    if got.lower() != expected_sha.lower():
        try:
            path.unlink()
        except OSError:
            pass
        return False

    _dl_set(state="ready", percent=100, file=str(path), sha256=got,
            verified=True, version=version, error=None, method="installer")
    return True


def _abs_url(url: str) -> str:
    return url if url.startswith("http") else f"{_site()}{url}"


def _staging_dir(version: str) -> Path:
    return _updates_dir() / f"staging-{version}"


def plan_delta(version: str, remote: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """What a partial update would fetch, or None if it is not possible.

    Returns None — rather than raising — for every "we cannot patch this"
    case: running from source, no manifest published, a manifest describing a
    different release, or a release that touches a binary. The caller then uses
    the full installer, which must always remain the fallback. The one thing
    this does NOT do quietly is accept a manifest it could not verify.
    """
    root = delta.app_root()
    if root is None:
        return None                       # a source checkout has nothing to patch
    manifest_url = remote.get("manifest_url")
    manifest_sha = (remote.get("manifest_sha256") or "").lower()
    if not manifest_url or not manifest_sha:
        return None                       # this build published no update store

    raw = delta.fetch(_abs_url(manifest_url))
    got = hashlib.sha256(raw).hexdigest().lower()
    if got != manifest_sha:
        # The digest came from /api/downloads over HTTPS and the manifest did
        # not match it. That is not a reason to fall back quietly to the
        # installer - something is wrong with the channel - so say so.
        raise ValueError("the update manifest did not match its published checksum")

    manifest = delta.parse_manifest(raw)
    if manifest["version"] != version:
        # The store belongs to a different build than the one being offered;
        # patching to it would install a version nobody advertised.
        return None

    plan = delta.plan_update(root, manifest)
    return plan if plan["possible"] else None


def _delta_worker(version: str, plan: Dict[str, Any], remote: Dict[str, Any]) -> None:
    """Fetch only the changed files, into staging. Nothing is applied here."""
    staging = _staging_dir(version)
    try:
        _dl_set(state="downloading", percent=0, error=None, file=None,
                version=version, sha256=None, method="delta",
                files=plan["count"], bytes=plan["bytes"], staging=str(staging))
        base = _abs_url(remote.get("file_url") or f"/update/{_platform_key()}/file")

        def progress(done: int, total: int) -> None:
            _dl_set(percent=min(99, int(done * 100 / max(total, 1))))

        delta.download_blobs(plan, base, staging, progress=progress)
        _delta_plans[version] = plan
        _dl_set(state="ready", percent=100, verified=True,
                file=str(staging), method="delta")
    except Exception as exc:                                  # noqa: BLE001
        _dl_set(state="error", error=str(exc)[:300], file=None,
                method="delta")


def _download_worker(version: str, expected_sha: str) -> None:
    """Fetch the installer, then prove it is the one that was published.

    The file this writes is about to be EXECUTED. Downloading an executable and
    running it without checking it against a digest published by the server
    turns the update channel into a way to run arbitrary code on this machine -
    a hostile network, a broken proxy or a compromised bucket is all it takes.
    So the bytes are hashed as they arrive and the result must match; if it does
    not, the file is deleted rather than offered.
    """
    url = f"{_site()}/download/{_platform_key()}"
    target = _installer_path(version)
    part = target.with_suffix(target.suffix + ".part")
    try:
        _dl_set(state="downloading", percent=0, error=None, file=None,
                version=version, sha256=None)
        digest = hashlib.sha256()
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
                    digest.update(chunk)
                    done += len(chunk)
                    if total:
                        _dl_set(percent=min(99, int(done * 100 / total)))
        got = digest.hexdigest()

        if expected_sha and got.lower() != expected_sha.lower():
            part.unlink(missing_ok=True)
            _dl_set(state="error", file=None, sha256=got,
                    error="The downloaded file did not match the published "
                          "checksum and was discarded. Download it from the "
                          "website instead.")
            return

        # Rename only once the bytes are all here AND verified, so a partial or
        # unverified installer can never be presented as ready to run.
        part.replace(target)
        _dl_set(state="ready", percent=100, file=str(target), sha256=got,
                verified=bool(expected_sha))
    except Exception as exc:
        try:
            part.unlink(missing_ok=True)
        except Exception:
            pass
        _dl_set(state="error", error=str(exc)[:300], file=None)


def start_download(version: str, expected_sha: str = "",
                   remote: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Fetch the update — as a handful of files where that is possible.

    The partial path is tried first and silently declines whenever it cannot
    guarantee the same result as the installer, so the common release (a few
    hundred KB of Python and web assets) costs a few hundred KB instead of
    76 MB, while a release that changes the runtime still goes the long way.
    """
    snap = _dl_snapshot()
    if snap["state"] == "downloading":
        return snap

    if remote:
        try:
            plan = plan_delta(version, remote)
        except Exception as exc:                              # noqa: BLE001
            _dl_set(state="error", error=str(exc)[:300], file=None)
            return _dl_snapshot()
        if plan:
            threading.Thread(target=_delta_worker,
                             args=(version, plan, remote),
                             name="imagesl-update-delta", daemon=True).start()
            return _dl_snapshot()

    threading.Thread(target=_download_worker, args=(version, expected_sha),
                     name="imagesl-update-dl", daemon=True).start()
    return _dl_snapshot()


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
class SettingsBody(BaseModel):
    check_updates_on_launch: bool | None = None
    auto_download_updates: bool | None = None


def register(app, app_version: str, cache_dir: Path | None) -> None:
    """Attach the desktop-only routes to `app`."""

    def _update_state() -> Dict[str, Any]:
        online = _online()
        remote = fetch_remote() if online else {}
        latest = str(remote.get("version") or "") or None
        platforms = remote.get("platforms") or {}
        mine = platforms.get(_platform_key()) or {}
        can_get = bool(mine.get("available"))
        available = bool(latest and is_newer(latest, app_version) and can_get)
        # Before reporting "idle", check whether a previous run already fetched
        # this exact build — otherwise the UI offers to download a file that is
        # sitting on disk, and Install stays permanently unavailable.
        if available:
            adopt_downloaded(latest, mine.get("sha256") or "")
        return {
            "online": online,
            "checked": bool(remote),
            "latest_version": latest,
            "update_available": available,
            # The digest the server publishes for the build we would fetch. The
            # downloader checks against this before the file is offered.
            "sha256": mine.get("sha256") or "",
            # Carried through so the download can try the partial path; it
            # holds the manifest URL and the digest that authenticates it.
            "platform_info": mine,
            "download": _dl_snapshot(),
        }

    _cache_size_cache: Dict[str, Any] = {"at": 0.0, "bytes": 0}

    @app.get("/api/desktop/info")
    def desktop_info() -> JSONResponse:
        # Walking the cache is O(files) and this endpoint is polled while an
        # update downloads. A batch of slides leaves thousands of entries, and
        # re-summing them every poll made Settings stutter for a number that
        # changes slowly. Ten seconds is fresh enough for a size readout.
        cache_bytes = 0
        now = time.time()
        if cache_dir and Path(cache_dir).is_dir():
            if now - _cache_size_cache["at"] < 10.0:
                cache_bytes = _cache_size_cache["bytes"]
            else:
                try:
                    cache_bytes = sum(f.stat().st_size
                                      for f in Path(cache_dir).rglob("*") if f.is_file())
                except Exception:
                    cache_bytes = 0
                _cache_size_cache["at"] = now
                _cache_size_cache["bytes"] = cache_bytes
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
            start_download(state["latest_version"], state.get("sha256", ""),
                           state.get("platform_info"))
            state["download"] = _dl_snapshot()
        return JSONResponse(state)

    @app.post("/api/desktop/download-update")
    def desktop_download_update() -> JSONResponse:
        state = _update_state()
        if not state["update_available"]:
            raise HTTPException(status_code=409,
                                detail="There is no update to download.")
        return JSONResponse(start_download(state["latest_version"],
                                           state.get("sha256", ""),
                                           state.get("platform_info")))

    @app.get("/api/desktop/download-progress")
    def desktop_download_progress() -> JSONResponse:
        return JSONResponse(_dl_snapshot())

    @app.post("/api/desktop/install-update")
    def desktop_install_update() -> JSONResponse:
        snap = _dl_snapshot()
        path = snap.get("file")

        # A partial update is applied in place: the staged files are moved into
        # the app (all of them or none - see delta_update.apply_update) and the
        # app relaunches itself. No installer, no elevation, nothing to close.
        if snap.get("method") == "delta":
            if snap.get("state") != "ready" or not path:
                raise HTTPException(status_code=409,
                                    detail="No downloaded update is ready to install.")
            root = delta.app_root()
            plan = _delta_plans.get(snap.get("version") or "")
            if root is None or not plan:
                raise HTTPException(status_code=409,
                                    detail="This update can no longer be applied "
                                           "in place. Download it again.")
            result = delta.apply_update(root, plan, Path(path))
            if not result.get("applied"):
                _dl_set(state="error",
                        error=result.get("error") or "The update could not be applied.")
                raise HTTPException(
                    status_code=500,
                    detail="The update could not be applied and was rolled back: "
                           + str(result.get("error") or ""))
            _relaunch()
            return JSONResponse({"started": True, "method": "delta",
                                 "files": result.get("files", 0)})

        if snap.get("state") != "ready" or not path or not Path(path).is_file():
            raise HTTPException(status_code=409,
                                detail="No downloaded update is ready to install.")

        # Refuse to launch anything we could not prove. An update mechanism that
        # runs an unverified executable is a way to execute arbitrary code on
        # this machine, so "we could not check" is treated as "no", not as "go
        # ahead" - the safe default is the one that stops.
        if not snap.get("verified"):
            raise HTTPException(
                status_code=409,
                detail="This download could not be verified against a published "
                       "checksum, so it will not be run. Download the installer "
                       "from the website instead.")

        # Re-hash immediately before running it. The check at download time
        # proves what arrived; this proves what is about to execute, and the two
        # are not the same claim if anything touched the file in between.
        expected = (snap.get("sha256") or "").lower()
        actual = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                for block in iter(lambda: fh.read(1024 * 1024), b""):
                    actual.update(block)
        except OSError as exc:
            raise HTTPException(status_code=500,
                                detail=f"Could not read the installer: {exc}")
        if actual.hexdigest() != expected:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass
            _dl_set(state="error", file=None,
                    error="The installer changed after it was downloaded and "
                          "has been deleted.")
            raise HTTPException(status_code=409,
                                detail="The installer changed after it was "
                                       "downloaded. It has been deleted.")
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
