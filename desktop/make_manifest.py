from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from delta_update import is_delta_eligible, sha256_file

def build(tree: Path, out: Path, version: str, platform: str) -> dict:
    files = []
    store = out / "files"
    store.mkdir(parents=True, exist_ok=True)

    for path in sorted(tree.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(tree).as_posix()
        digest = sha256_file(path)
        if not digest:
            raise SystemExit(f"could not read {path}")
        files.append({
            "path": rel,
            "sha256": digest,
            "bytes": path.stat().st_size,
            "delta": is_delta_eligible(rel),
        })
        blob = store / digest
        if is_delta_eligible(rel) and not blob.exists():
            shutil.copy2(path, blob)

    manifest = {"app": "ImageSL", "version": version,
                "platform": platform, "files": files}

    data = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    (out / "manifest.json").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    (out / "manifest.json.sha256").write_bytes(digest.encode("ascii") + b"\n")

    written = (out / "manifest.json").read_bytes()
    if hashlib.sha256(written).hexdigest() != digest:
        raise SystemExit("manifest.json on disk does not match its published "
                         "digest - refusing to publish an unusable manifest")
    return manifest

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tree", type=Path, help="built app dir, e.g. dist/ImageSL")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--version", default="")
    ap.add_argument("--platform", default="windows")
    args = ap.parse_args()

    if not args.tree.is_dir():
        raise SystemExit(f"not a directory: {args.tree}")
    version = args.version
    if not version:
        vf = args.tree / "_internal" / "version.txt"
        version = vf.read_text(encoding="utf-8").strip() if vf.is_file() else ""
    if not version:
        raise SystemExit("no version: pass --version or ship _internal/version.txt")

    m = build(args.tree, args.out, version, args.platform)
    patchable = [f for f in m["files"] if f["delta"]]
    print(f"manifest: {len(m['files'])} files, "
          f"{len(patchable)} patchable "
          f"({sum(f['bytes'] for f in patchable) / 1024:.0f} KB), "
          f"version {version}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
