from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request

TIMEOUT = 4.0

def _norm(tag: str) -> tuple:
    nums = re.findall(r"\d+", tag or "")
    return tuple(int(n) for n in nums[:4]) if nums else (0,)

def _is_newer(latest: str, current: str) -> bool:
    a, b = _norm(latest), _norm(current)
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return a > b

def check_for_update(current_version: str, site: str) -> dict:
    base = (site or "").rstrip("/")
    try:
        req = urllib.request.Request(f"{base}/api/downloads", headers={
            "Accept": "application/json",
            "User-Agent": "ImageSL-Updater",
        })
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            status = getattr(r, "status", 200)
            raw = r.read(65536)
    except urllib.error.HTTPError:
        return {"available": False, "status": "unreadable"}
    except Exception:
        return {"available": False, "status": "offline"}

    if status != 200:
        return {"available": False, "status": "unreadable"}
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return {"available": False, "status": "unreadable"}
    if not isinstance(data, dict):
        return {"available": False, "status": "unreadable"}

    latest = str(data.get("version") or "")
    if not latest:
        return {"available": False, "status": "unreadable"}
    if not _is_newer(latest, current_version or "0"):
        return {"available": False, "status": "current", "version": latest}

    key = "windows" if sys.platform.startswith("win") else "macos"
    platform = (data.get("platforms") or {}).get(key) or {}
    if not platform.get("available"):
        return {"available": False, "status": "unbuilt", "version": latest}

    return {"available": True, "status": "available", "version": latest, "url": base}

STATUS_TEXT = {
    "offline":    "No connection — could not check for updates. ImageSL runs "
                  "fully offline; this only means the check was skipped.",
    "unreadable": "The update service answered with something unexpected. "
                  "Nothing was changed.",
    "current":    "ImageSL is up to date.",
    "unbuilt":    "A newer version exists, but no build for this platform yet.",
    "available":  "A newer version is available to download.",
}

def describe(info: dict) -> str:
    status = str(info.get("status") or "")
    text = STATUS_TEXT.get(status, "Update state unknown.")
    ver = info.get("version")
    if ver and status in ("current", "unbuilt", "available"):
        return f"{text} (latest: {ver})"
    return text

if __name__ == "__main__":
    cur = sys.argv[1] if len(sys.argv) > 1 else "0.0.0"
    site = sys.argv[2] if len(sys.argv) > 2 else "https://imagesl.com"
    result = check_for_update(cur, site)
    print(describe(result))
    print(result)
    raise SystemExit(10 if result.get("available")
                     else 20 if result.get("status") in ("offline", "unreadable")
                     else 0)
