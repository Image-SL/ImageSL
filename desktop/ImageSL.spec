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
import re
import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# Repo root = parent of this spec's directory.
ROOT = os.path.abspath(os.path.join(os.path.dirname(SPECPATH), "."))
SERVER = os.path.join(ROOT, "server")
ICON = os.path.join(ROOT, "desktop", "ImageSL.ico")
# macOS needs .icns, not .ico. CI generates this next to the spec (see the
# workflow); if it is absent the app simply builds with the default icon.
ICNS = os.path.join(ROOT, "desktop", "ImageSL.icns")
MACOS = sys.platform == "darwin"

# --- pull in the backend's dependency trees in full ------------------------- #
datas, binaries, hiddenimports = [], [], []
for pkg in ("skimage", "scipy", "imagecodecs", "tifffile", "numpy", "PIL",
            "uvicorn", "webview"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h


# --- drop what a user can never execute ------------------------------------- #
# collect_all takes the whole distribution, which for scipy and numpy means
# their test suites: ~24 MB of code that exists to be run by their maintainers
# and cannot be reached from this application. Shipping it costs download size,
# install time and disk, and widens the set of files an auditor has to account
# for, for no functional gain.
#
# This filters DATA and BINARIES only. Nothing here removes a module the engine
# imports - see _TEST_MARKERS - and the build is verified afterwards by running
# a real analysis, not just by starting up.
_TEST_MARKERS = (
    os.sep + "tests" + os.sep,
    os.sep + "test" + os.sep,
    os.sep + "testing" + os.sep,
    os.sep + "_pytesttester",
)


def _is_test_payload(dest: str) -> bool:
    d = os.sep + dest.replace("/", os.sep).strip(os.sep) + os.sep
    return any(m in d for m in _TEST_MARKERS)


def _strip_tests(entries):
    return [e for e in entries if not _is_test_payload(e[1])]


_before = len(datas) + len(binaries)
datas = _strip_tests(datas)
binaries = _strip_tests(binaries)
print(f"ImageSL.spec: dropped {_before - len(datas) - len(binaries)} test payload files")

# The same suites as importable modules, so PyInstaller's analysis does not pull
# them back in through a stray reference.
hiddenimports = [h for h in hiddenimports
                 if not (h.endswith(".tests") or ".tests." in h
                         or h.endswith("._pytesttester"))]

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
_ver_str = "0.0.0"
if os.path.isfile(_ver):
    datas += [(_ver, ".")]
    try:
        with open(_ver, encoding="utf-8") as _f:
            _raw = _f.read().strip()
        # CFBundleShortVersionString must be numeric dotted components; a manual
        # CI run writes "0.0.0-dev", which macOS rejects. Keep only the numbers.
        _m = re.match(r"^\d+(\.\d+){0,2}", _raw)
        if _m:
            _ver_str = _m.group(0)
    except OSError:
        pass

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
    # .ico is a Windows format; on macOS the icon rides on the BUNDLE below.
    icon=(None if MACOS else (ICON if os.path.isfile(ICON) else None)),
)
coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, name="ImageSL",
)

# --- macOS: wrap the onedir build in a real .app --------------------------- #
# Without this the dmg ships a bare Unix executable — double-clicking it opens
# Terminal instead of the app, and Finder shows no icon or name.
if MACOS:
    app = BUNDLE(
        coll,
        name="ImageSL.app",
        icon=ICNS if os.path.isfile(ICNS) else None,
        bundle_identifier="com.solvergent.imagesl",
        info_plist={
            "CFBundleName": "ImageSL",
            "CFBundleDisplayName": "ImageSL",
            "CFBundleShortVersionString": _ver_str,
            "CFBundleVersion": _ver_str,
            "NSHighResolutionCapable": True,
            # Nothing here talks to the camera, mic, or the user's files behind
            # their back; the app is a local server plus a web view.
            "LSApplicationCategoryType": "public.app-category.medical",
            "NSHumanReadableCopyright": "ImageSL",
        },
    )
