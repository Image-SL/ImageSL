"""Generate the update manifest and content store from a built app tree.

Run after PyInstaller, before packaging the installer:

    python desktop/make_manifest.py dist/ImageSL --out downloads/update/windows

It writes, under --out:

    manifest.json           every file in the build, with its sha256
    manifest.json.sha256    the digest /api/downloads publishes, so the client
                            can prove the manifest itself before trusting it
    files/<sha256>          one blob per distinct file, content-addressed

Content-addressing is what makes the store cheap to publish repeatedly: a file
that did not change between releases has the same name, so re-uploading a
build only adds the blobs that are genuinely new, and a client that already
has a blob never fetches it twice.

Publishing the whole build (not only the patchable part) is deliberate. The
manifest is what the client compares against to decide whether a delta is even
possible; if it only listed the files we hoped to patch, a release that also
changed a DLL would look patchable and produce a broken install.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from delta_update import is_delta_eligible, sha256_file   # noqa: E402


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
        # Only the patchable files are ever fetched individually, so only those
        # need a blob. Publishing the runtime here would add ~76 MB of objects
        # that no client can use — the full installer covers that case.
        if is_delta_eligible(rel) and not blob.exists():
            shutil.copy2(path, blob)

    manifest = {"app": "ImageSL", "version": version,
                "platform": platform, "files": files}
    text = json.dumps(manifest, indent=2, sort_keys=True)
    (out / "manifest.json").write_text(text, encoding="utf-8")
    (out / "manifest.json.sha256").write_text(
        hashlib.sha256(text.encode("utf-8")).hexdigest() + "\n", encoding="utf-8")
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
