"""
ImageSL update check.

Deliberately conservative: it asks GitHub for the latest published release, and
if that release's version is newer than the running one, it reports where to get
it. It does NOT silently replace the running binary — a scientific tool should
not swap its own analysis code out from under a user mid-session without consent.
The launcher surfaces the result as a dismissible "Download" banner.

Every failure mode (offline, no releases yet, rate-limited, malformed) returns
`{"available": False}` and never raises, so a missing network can't break launch.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request

TIMEOUT = 4.0


def _norm(tag: str) -> tuple:
    """Parse 'v1.4.2', '1.4.2', '2.0.0-rc1' → comparable numeric tuple."""
    nums = re.findall(r"\d+", tag or "")
    return tuple(int(n) for n in nums[:4]) if nums else (0,)


def _is_newer(latest: str, current: str) -> bool:
    a, b = _norm(latest), _norm(current)
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return a > b


def check_for_update(current_version: str, site: str) -> dict:
    """Return {available, version, url} — never raises.

    Asks the ImageSL site what it is currently serving. This used to ask the
    GitHub releases API, which cannot work: the repository is private, so an
    anonymous request gets 404 and the check silently reported "no update"
    forever. The site's own /api/downloads is the authority on what a user can
    actually install, which is the question being asked.
    """
    base = (site or "").rstrip("/")
    try:
        req = urllib.request.Request(f"{base}/api/downloads", headers={
            "Accept": "application/json",
            "User-Agent": "ImageSL-Updater",
        })
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if r.status != 200:
                return {"available": False}
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return {"available": False}

    latest = str(data.get("version") or "")
    if not latest or not _is_newer(latest, current_version or "0"):
        return {"available": False}

    # Only offer it if there is genuinely a build for this platform to fetch.
    key = "windows" if sys.platform.startswith("win") else "macos"
    platform = (data.get("platforms") or {}).get(key) or {}
    if not platform.get("available"):
        return {"available": False}

    return {"available": True, "version": latest, "url": base}


if __name__ == "__main__":
    cur = sys.argv[1] if len(sys.argv) > 1 else "0.0.0"
    site = sys.argv[2] if len(sys.argv) > 2 else "https://imagesl.online"
    print(check_for_update(cur, site))
