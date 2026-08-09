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
import urllib.error
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
    """Return {available, status, version, url} — never raises.

    Asks the ImageSL site what it is currently serving. This used to ask the
    GitHub releases API, which cannot work: the repository is private, so an
    anonymous request gets 404 and the check silently reported "no update"
    forever. The site's own /api/downloads is the authority on what a user can
    actually install, which is the question being asked.

    `status` says WHY, because `available: False` on its own answers two
    completely different questions with the same word:

        offline     the network could not be reached at all. Says nothing about
                    whether an update exists - this is the normal, expected
                    answer on the offline machines this app is built for.
        unreadable  the site answered, but not with something we understood.
        current     genuinely up to date. We asked and there is nothing newer.
        unbuilt     something newer exists, but not for this platform yet.
        available   there is a newer build and it can be installed.

    Collapsing `offline` into `current` is what makes an air-gapped install look
    like a maintained one: both render as silence, and a machine that has not
    reached the network in a year reports exactly what a machine that checked a
    second ago does. Callers that want to tell a user "last checked: never" need
    to be able to see the difference.

    `available` stays a plain bool with the same meaning it always had, so
    existing callers keep working.
    """
    base = (site or "").rstrip("/")
    # Reaching the site and understanding it are separate failures, so they are
    # caught separately. Parsing inside the network try-block made a site that
    # answered with an error page report "offline", which is a lie in the one
    # direction that matters: it says the machine cannot reach the internet when
    # the machine is online and the service is broken.
    try:
        req = urllib.request.Request(f"{base}/api/downloads", headers={
            "Accept": "application/json",
            "User-Agent": "ImageSL-Updater",
        })
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            status = getattr(r, "status", 200)
            # Bounded: this is a small JSON document, and an unbounded read of
            # whatever is at a configured URL is not something to do on launch.
            raw = r.read(65536)
    except urllib.error.HTTPError:
        # The site answered, with an error. We reached the network, so this is
        # emphatically not "offline" -- reporting it as such would tell a user
        # on a working connection that they have no connection, and send them
        # to debug the wrong machine.
        return {"available": False, "status": "unreadable"}
    except Exception:
        # Offline, DNS, TLS, no route, timeout. Deliberately not distinguished
        # further: to this app they all mean "could not ask", and guessing at
        # the reason from an exception type would be dressing up a shrug.
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

    # Only offer it if there is genuinely a build for this platform to fetch.
    key = "windows" if sys.platform.startswith("win") else "macos"
    platform = (data.get("platforms") or {}).get(key) or {}
    if not platform.get("available"):
        return {"available": False, "status": "unbuilt", "version": latest}

    return {"available": True, "status": "available", "version": latest, "url": base}


# What each status means to a person, so the app and the CLI say the same thing
# rather than each inventing its own wording.
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
    """One line a human can read, for whichever status came back."""
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
    # Exit code so a script can act on it without parsing: 0 nothing to do,
    # 10 update available, 20 could not check.
    raise SystemExit(10 if result.get("available")
                     else 20 if result.get("status") in ("offline", "unreadable")
                     else 0)
