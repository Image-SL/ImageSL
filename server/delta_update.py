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
DELTA_ROOTS = ("_internal/server/",)
DELTA_FILES = ("_internal/version.txt",)
_CHUNK = 1024 * 1024

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
    p = rel.replace("\\", "/")
    return p in DELTA_FILES or any(p.startswith(r) for r in DELTA_ROOTS)

def _safe_relpath(rel: str) -> Optional[str]:
    if not rel or not isinstance(rel, str):
        return None
    p = rel.replace("\\", "/").strip()
    if not p or p.startswith("/") or p.startswith("//"):
        return None
    if len(p) >= 2 and p[1] == ":":
        return None
    parts = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            return None
        parts.append(seg)
    return "/".join(parts) or None

def app_root() -> Optional[Path]:
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent

def local_files(root: Path) -> Dict[str, Path]:
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

def parse_manifest(raw: bytes) -> Dict[str, Any]:
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
            "delta": bool(entry.get("delta")) and is_delta_eligible(rel),
        })
    if not files:
        raise ValueError("manifest lists no files")
    return {"version": version, "files": files,
            "platform": str(data.get("platform") or "")}

def plan_update(root: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    changed: List[Dict[str, Any]] = []
    blocked: List[str] = []
    listed = set()

    for entry in manifest["files"]:
        rel = entry["path"]
        listed.add(rel)
        target = root / rel
        if target.is_file() and sha256_file(target) == entry["sha256"]:
            continue
        if not entry["delta"]:
            blocked.append(rel)
            continue
        changed.append(entry)

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

def fetch(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ImageSL-Desktop"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def download_blobs(plan: Dict[str, Any], base_url: str, staging: Path,
                   progress: Optional[Callable[[int, int], None]] = None) -> None:
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

def apply_update(root: Path, plan: Dict[str, Any], staging: Path) -> Dict[str, Any]:
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

    except Exception as exc:
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
