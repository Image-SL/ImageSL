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


def check_for_update(current_version: str, repo: str) -> dict:
    """Return {available, version, url, notes} — never raises."""
    api = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        req = urllib.request.Request(api, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ImageSL-Updater",
        })
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if r.status != 200:
                return {"available": False}
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return {"available": False}

    tag = data.get("tag_name") or ""
    if not tag or not _is_newer(tag, current_version or "0"):
        return {"available": False}

    return {
        "available": True,
        "version": tag,
        "url": data.get("html_url") or f"https://github.com/{repo}/releases/latest",
        "notes": (data.get("body") or "")[:2000],
    }


if __name__ == "__main__":
    import sys
    cur = sys.argv[1] if len(sys.argv) > 1 else "0.0.0"
    rp = sys.argv[2] if len(sys.argv) > 2 else "solvergent/ImageSL"
    print(check_for_update(cur, rp))
