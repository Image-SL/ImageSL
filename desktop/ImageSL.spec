# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the ImageSL offline desktop app.

Bundles the launcher + the ENTIRE server package (app.py, ihc/, web/) so the app
runs the real analysis engine locally. Because launcher.py imports the backend
dynamically (`from app import app`, from a data file), PyInstaller's static
analysis cannot see app.py's own dependencies — so every third-party library the
backend uses is pulled in explicitly below via collect_all / hiddenimports.

Build:   pyinstaller desktop/ImageSL.spec        (run from the repo root)
Output:  dist/ImageSL/ImageSL(.exe)   (onedir — faster start, simpler to sign)
"""

import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# Repo root = parent of this spec's directory.
ROOT = os.path.abspath(os.path.join(os.path.dirname(SPECPATH), "."))
SERVER = os.path.join(ROOT, "server")
ICON = os.path.join(ROOT, "client", "ImageSL.ico")

# --- pull in the backend's dependency trees in full ------------------------- #
datas, binaries, hiddenimports = [], [], []
for pkg in ("skimage", "scipy", "imagecodecs", "tifffile", "numpy", "PIL",
            "uvicorn", "webview"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# FastAPI / Starlette / multipart are imported inside app.py (a data file), so
# name them explicitly — collect_all is overkill for these but submodules matter.
for pkg in ("fastapi", "starlette", "anyio", "multipart"):
    hiddenimports += collect_submodules(pkg)
hiddenimports += ["python_multipart", "uvicorn.loops.auto",
                  "uvicorn.protocols.http.auto",
                  "uvicorn.protocols.websockets.auto",
                  "uvicorn.lifespan.on"]

# --- bundle the whole server package as data (real .py files on disk) -------- #
def tree(src, prefix):
    out = []
    for root, _dirs, files in os.walk(src):
        if "__pycache__" in root:
            continue
        for f in files:
            if f.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(root, f)
            rel = os.path.relpath(root, src)
            dest = prefix if rel == "." else os.path.join(prefix, rel)
            out.append((full, dest))
    return out

datas += tree(SERVER, "server")
if os.path.isfile(ICON):
    datas += [(ICON, ".")]
_ver = os.path.join(ROOT, "version.txt")
if os.path.isfile(_ver):
    datas += [(_ver, ".")]

a = Analysis(
    [os.path.join(ROOT, "desktop", "launcher.py")],
    pathex=[os.path.join(ROOT, "desktop"), SERVER],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "IPython", "notebook"],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="ImageSL",
    debug=False,
    strip=False,
    upx=False,                      # UPX packing trips antivirus heuristics — do NOT enable
    console=False,                  # windowed app, no console
    icon=ICON if os.path.isfile(ICON) else None,
)
coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, name="ImageSL",
)
