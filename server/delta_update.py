"""Partial ("delta") updates — replace only the files that actually changed.

A release is ~76 MB, and almost none of it moves between versions. The bulk is
the frozen CPython runtime plus numpy, scipy, scikit-image and imagecodecs;
what actually changes in a normal release is the interpreted payload —
`_internal/server/**` (the engine's .py files and the web assets) and
`version.txt`. Making a user re-download and re-run a 76 MB installer to
replace ~200 KB of Python is slow, wastes bandwidth on both ends, and turns
every fix into a full reinstall that needs the app closed.

So the updater asks what changed and fetches only that.

HOW IT DECIDES WHAT IS SAFE
---------------------------
A file is *delta-eligible* only if replacing it on disk while the app is
running is safe and takes effect on the next launch. That is true of the
interpreted payload: Python reads a .py at import and closes it, and the web
assets are read per request. It is NOT true of `ImageSL.exe`, the DLLs,
`base_library.zip`, or anything else Windows holds a lock on while the process
lives — those cannot be replaced under a running process at all.

So the manifest marks each file, and if ANY changed file is not delta-eligible
the whole update falls back to the full installer. A partial update is an
optimisation for the common case, never a second, weaker way to ship a release.

WHY EVERY LAYER IS VERIFIED
---------------------------
These files are executed. The chain of trust is therefore explicit and has no
weak link:

  1. `/api/downloads` (HTTPS) publishes the manifest's own sha256;
  2. the manifest is fetched and must hash to exactly that;
  3. every file listed carries its own sha256, and each downloaded blob must
     hash to it before it is written anywhere near the app directory.

Nothing is trusted because of where it came from or what it is called. A
manifest is remote input, so its paths are treated as hostile too — see
`_safe_relpath`, which is what stops a manifest entry called
`../../../Startup/evil.py` from being written outside the app.

APPLYING IT
-----------
Downloads land in a staging directory and are only moved into the app once all
of them have arrived and verified, so a connection that drops half way cannot
leave a half-updated install. The move keeps a backup of every file it
replaces and rolls the whole set back if any single one fails, because an app
that is partly one version and partly another is worse than one that did not
update.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

MANIFEST_NAME = "manifest.json"
# Roots whose contents may be replaced under a running app. Kept as a tuple of
# POSIX-style prefixes so it reads the same as the manifest paths.
DELTA_ROOTS = ("_internal/server/",)
DELTA_FILES = ("_internal/version.txt",)
_CHUNK = 1024 * 1024


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(_CHUNK), b""):
                h.update(block)
    except OSError:
        return ""
    return h.hexdigest()


def is_delta_eligible(rel: str) -> bool:
    """May this path be replaced while the app is running?"""
    p = rel.replace("\\", "/")
    return p in DELTA_FILES or any(p.startswith(r) for r in DELTA_ROOTS)


# --------------------------------------------------------------------------- #
# Paths from a manifest are hostile input
# --------------------------------------------------------------------------- #
def _safe_relpath(rel: str) -> Optional[str]:
    """Return a normalised relative path, or None if it escapes the app dir.

    The manifest arrives over the network, so a path in it is untrusted no
    matter how well the rest of the chain verified the bytes: a correctly
    signed manifest containing `..\\..\\Startup\\x.py` is still an attempt to
    write outside the application. Absolute paths, drive letters, UNC paths and
    any `..` component are refused outright rather than sanitised, because
    quietly rewriting a hostile path into a harmless one hides the attempt.
    """
    if not rel or not isinstance(rel, str):
        return None
    p = rel.replace("\\", "/").strip()
    if not p or p.startswith("/") or p.startswith("//"):
        return None
    if len(p) >= 2 and p[1] == ":":                 # C:/...
        return None
    parts = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            return None
        parts.append(seg)
    return "/".join(parts) or None


# --------------------------------------------------------------------------- #
# The application tree
# --------------------------------------------------------------------------- #
def app_root() -> Optional[Path]:
    """The installed application directory, or None when running from source.

    A delta update rewrites files inside a FROZEN install. Running from a
    source checkout there is nothing of that shape to patch — and silently
    writing manifest files over somebody's working tree would be a remarkable
    thing for an update checker to do — so this reports None and the caller
    falls back.
    """
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def local_files(root: Path) -> Dict[str, Path]:
    """Every delta-eligible file currently installed, keyed by relative path."""
    out: Dict[str, Path] = {}
    for prefix in DELTA_ROOTS:
        base = root / prefix.rstrip("/")
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if f.is_file():
                rel = f.relative_to(root).as_posix()
                out[rel] = f
    for rel in DELTA_FILES:
        f = root / rel
        if f.is_file():
            out[rel] = f
    return out


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #
def parse_manifest(raw: bytes) -> Dict[str, Any]:
    """Validate a manifest into a shape the rest of this module can trust."""
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest is not an object")
    version = str(data.get("version") or "").strip()
    if not version:
        raise ValueError("manifest has no version")
    files: List[Dict[str, Any]] = []
    for entry in data.get("files") or []:
        if not isinstance(entry, dict):
            raise ValueError("manifest file entry is not an object")
        rel = _safe_relpath(str(entry.get("path") or ""))
        if not rel:
            raise ValueError(f"unsafe path in manifest: {entry.get('path')!r}")
        digest = str(entry.get("sha256") or "").lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"bad sha256 for {rel}")
        files.append({
            "path": rel,
            "sha256": digest,
            "bytes": int(entry.get("bytes") or 0),
            # Trust the generator's judgement only where it AGREES with ours;
            # a manifest must not be able to declare ImageSL.exe patchable.
            "delta": bool(entry.get("delta")) and is_delta_eligible(rel),
        })
    if not files:
        raise ValueError("manifest lists no files")
    return {"version": version, "files": files,
            "platform": str(data.get("platform") or "")}


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def plan_update(root: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Work out what a delta would have to do, without doing any of it.

    Returned so the UI can say "3 files, 240 KB" instead of "76 MB", and so the
    decision to fall back to the full installer is made — and explainable —
    before anything is downloaded.
    """
    changed: List[Dict[str, Any]] = []
    blocked: List[str] = []
    listed = set()

    for entry in manifest["files"]:
        rel = entry["path"]
        listed.add(rel)
        target = root / rel
        if target.is_file() and sha256_file(target) == entry["sha256"]:
            continue                                  # already correct
        if not entry["delta"]:
            # A binary, the exe, or anything else locked by the running
            # process: this release cannot be applied file-by-file.
            blocked.append(rel)
            continue
        changed.append(entry)

    # Files we ship that the new version no longer has. Only ever inside the
    # delta roots, so this can never propose deleting the runtime.
    removals = [rel for rel in local_files(root) if rel not in listed]

    return {
        "version": manifest["version"],
        "possible": not blocked,
        "blocked_by": blocked[:20],
        "files": changed,
        "removals": removals,
        "count": len(changed),
        "bytes": sum(e["bytes"] for e in changed),
    }


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ImageSL-Desktop"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def download_blobs(plan: Dict[str, Any], base_url: str, staging: Path,
                   progress: Optional[Callable[[int, int], None]] = None) -> None:
    """Fetch each changed file into `staging`, keyed by its digest.

    Content-addressed on purpose: the URL names the hash, so a proxy or a CDN
    cannot serve a different file under the same name without the check below
    catching it, and an interrupted run can re-use whatever already verified.
    """
    staging.mkdir(parents=True, exist_ok=True)
    total = len(plan["files"])
    for i, entry in enumerate(plan["files"], 1):
        dest = staging / entry["sha256"]
        if dest.is_file() and sha256_file(dest) == entry["sha256"]:
            if progress:
                progress(i, total)
            continue
        blob = fetch(f"{base_url.rstrip('/')}/{entry['sha256']}")
        got = hashlib.sha256(blob).hexdigest()
        if got != entry["sha256"]:
            raise ValueError(
                f"{entry['path']} did not match its published checksum")
        dest.write_bytes(blob)
        if progress:
            progress(i, total)


# --------------------------------------------------------------------------- #
# Applying
# --------------------------------------------------------------------------- #
def apply_update(root: Path, plan: Dict[str, Any], staging: Path) -> Dict[str, Any]:
    """Move verified files into the app, all of them or none of them.

    Every replaced file is kept until the whole set has landed. If any single
    move fails — a lock, a permission, a full disk — everything already moved
    is put back, because a half-applied update leaves an install that is partly
    one version and partly another, which is harder to diagnose and to recover
    from than an update that simply did not happen.
    """
    backups: List[Tuple[Path, Optional[Path]]] = []
    moved: List[Path] = []
    backup_dir = staging / "_backup"
    backup_dir.mkdir(parents=True, exist_ok=True)

    try:
        for entry in plan["files"]:
            rel = entry["path"]
            target = root / rel
            source = staging / entry["sha256"]
            if sha256_file(source) != entry["sha256"]:
                raise ValueError(f"staged {rel} failed its final check")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                keep = backup_dir / entry["sha256"]
                shutil.copy2(target, keep)
                backups.append((target, keep))
            else:
                backups.append((target, None))
            shutil.copy2(source, target)
            moved.append(target)

        for rel in plan.get("removals") or []:
            safe = _safe_relpath(rel)
            if not safe or not is_delta_eligible(safe):
                continue
            victim = root / safe
            if victim.is_file():
                keep = backup_dir / ("rm-" + safe.replace("/", "_"))
                shutil.copy2(victim, keep)
                backups.append((victim, keep))
                victim.unlink()

    except Exception as exc:                            # noqa: BLE001
        for target, keep in reversed(backups):
            try:
                if keep is not None:
                    shutil.copy2(keep, target)
                elif target.exists():
                    target.unlink()
            except OSError:
                pass
        return {"applied": False, "error": str(exc)[:300],
                "rolled_back": len(backups)}

    return {"applied": True, "files": len(moved),
            "removed": len(plan.get("removals") or []),
            "version": plan["version"]}
