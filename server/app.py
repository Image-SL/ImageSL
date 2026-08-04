"""
ImageSL backend — FastAPI application (fully online, single-page web tool).

All analysis lives here; the browser is the only client. Upload a slide to get
IHC stain quantification (color deconvolution + per-slide background
segmentation + Otsu), then adjust the analysis parameters to re-run the
measurement and re-render the preview.

Currently a DAB-only build — both Auto-detect and Select-stain still work, they
just resolve to DAB. See ihc/stains.py ENABLED_KEYS and ihc/engine.py
ENABLED_FAMILIES to bring further stains online.
"""

from __future__ import annotations

import hashlib
import os
import time
import threading
import uuid
from pathlib import Path
from typing import Optional
import tempfile
import pickle
import json
import csv
import zipfile
import re

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
import io
from PIL import Image
import numpy as np

from ihc import detect as detect_mod
from ihc import engine
from ihc import regions as region_tools
from ihc import stains as stain_registry

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
ASSETS_DIR = WEB_DIR / "assets"

MAX_UPLOAD_BYTES = int(os.environ.get("IMAGESL_MAX_UPLOAD_MB", "256")) * 1024 * 1024
APP_VERSION = os.environ.get("IMAGESL_VERSION", "2.0.0")

# Set by the offline desktop launcher. When true, "/" boots straight into the
# analyzer instead of the public landing page — the download button only makes
# sense on the web site, not inside an app the user has already installed.
IMAGESL_DESKTOP = os.environ.get("IMAGESL_DESKTOP") == "1"

# Optional access control. If IMAGESL_ACCESS_TOKENS is set (comma-separated),
# every /api/* call must carry a matching X-ImageSL-Key header. Unset => open.
_TOKENS = {t.strip() for t in os.environ.get("IMAGESL_ACCESS_TOKENS", "").split(",") if t.strip()}

app = FastAPI(title="ImageSL", version=APP_VERSION, docs_url=None, redoc_url=None)

if ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")


# --------------------------------------------------------------------------- #
# Persistent disk analysis cache (survives restarts if /data is mounted)
# --------------------------------------------------------------------------- #

CACHE_DIR = Path("/data") if Path("/data").exists() else Path(tempfile.gettempdir()) / "imagesl_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

class _DiskCache:
    """Analysis entries on disk, keyed by analysis id.

    Age is carried by the file's own mtime, and *nothing here ever opens a
    pickle it is not about to return*. That is not a micro-optimisation: an
    entry is several megabytes, and the previous version unpickled every cached
    entry on each `put` (to read a timestamp it had written inside the pickle)
    and rewrote the whole entry on each `get` (to refresh that timestamp). A
    batch of N slides therefore did O(N²) megabyte-scale reads and N megabyte
    writes per export — on a 46-slide batch, gigabytes of pointless I/O. On a
    small server that is slow enough for the browser's upload to time out and
    memory-hungry enough to be OOM-killed, which is what "some slides just say
    failed, but only on the slower machine" actually was.
    """

    def __init__(self, max_items: int = 50, ttl_seconds: int = 300,
                 max_bytes: int = 2 * 1024 ** 3):
        self._dir = CACHE_DIR
        self._lock = threading.RLock()
        self._max = max_items
        self._ttl = ttl_seconds
        self._max_bytes = max_bytes

    def _file(self, key: str) -> Path:
        return self._dir / f"{key}.pkl"

    def _meta_file(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def _drop(self, key: str) -> None:
        self._file(key).unlink(missing_ok=True)
        self._meta_file(key).unlink(missing_ok=True)

    @staticmethod
    def _age(path: Path) -> float:
        try:
            return time.time() - path.stat().st_mtime
        except OSError:
            return float("inf")

    @staticmethod
    def _touch(path: Path) -> None:
        try:
            os.utime(path, None)
        except OSError:
            pass

    def put(self, key: str, value: dict, meta: Optional[dict] = None) -> None:
        """Store the heavy analysis entry, plus a tiny JSON sidecar (`meta`) that
        CSV export can read without unpickling the whole (megabytes) entry.

        Written to a temporary file and renamed into place, so a reader can
        never observe a half-written entry — which is what turned a slow write
        into an "analysis expired" error mid-batch."""
        with self._lock:
            self._evict()
            path = self._file(key)
            tmp = path.with_suffix(".pkl.part")
            try:
                with open(tmp, "wb") as f:
                    pickle.dump(value, f, protocol=pickle.HIGHEST_PROTOCOL)
                os.replace(tmp, path)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
            if meta is not None:
                mp = self._meta_file(key)
                mtmp = mp.with_suffix(".json.part")
                try:
                    with open(mtmp, "w", encoding="utf-8") as f:
                        json.dump({"row": meta}, f)
                    os.replace(mtmp, mp)
                except Exception:
                    mtmp.unlink(missing_ok=True)

    def get(self, key: str) -> Optional[dict]:
        if not key:
            return None
        with self._lock:
            path = self._file(key)
            if not path.exists():
                return None
            if self._age(path) > self._ttl:
                self._drop(key)
                return None
            try:
                with open(path, "rb") as f:
                    value = pickle.load(f)
            except Exception:
                self._drop(key)
                return None
            self._touch(path)                      # keep it alive; do NOT rewrite it
            return value

    def touch(self, key: str) -> bool:
        """Mark an entry as still in use. Returns whether it is still there.

        This is what lets a browser sitting on the results screen keep its batch
        alive: a heartbeat costs one `utime` per slide, where reading the entry
        to refresh it would cost megabytes."""
        if not key:
            return False
        with self._lock:
            p = self._file(key)
            if not p.exists() or self._age(p) > self._ttl:
                return False
            self._touch(p)
            self._touch(self._meta_file(key))
            return True

    def get_meta(self, key: str) -> Optional[dict]:
        """Fast path for CSV export — reads the small JSON sidecar only."""
        if not key:
            return None
        with self._lock:
            p = self._meta_file(key)
            if not p.exists():
                return None
            if self._age(p) > self._ttl:
                self._drop(key)
                return None
            try:
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                p.unlink(missing_ok=True)
                return None
            self._touch(p)
            return d.get("row")

    def _evict(self) -> None:
        """Drop expired entries, then the oldest ones if over capacity — by
        mtime and size alone, so this stays cheap however large the entries are.

        A byte budget as well as a count, because the count alone does not bound
        anything useful: entries are several megabytes each, and the server runs
        in a small container whose ephemeral disk a long session of large
        batches would otherwise fill. Running out of disk mid-batch surfaces to
        the user as slides that "failed" for no stated reason."""
        entries = []
        total = 0
        for p in list(self._dir.glob("*.pkl")):
            age = self._age(p)
            if age > self._ttl:
                self._drop(p.stem)
                continue
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            entries.append((age, p.stem, size))
            total += size

        entries.sort(reverse=True)                 # oldest (largest age) first
        i = 0
        while i < len(entries) and (len(entries) - i >= self._max or total > self._max_bytes):
            _, key, size = entries[i]
            self._drop(key)
            total -= size
            i += 1


# TTL/size are generous enough that a large batch (dozens of slides) survives
# analysis + review + export. Still auto-wipes for privacy; tune via env.
#
# Eight hours, not one. Reviewing forty slides, drawing regions on them and
# deciding on a sensitivity is a working session, not a five-minute task, and an
# hour of inactivity is entirely normal in the middle of one. The open page also
# sends a heartbeat (see /api/keepalive) that refreshes whatever it is still
# showing, so the TTL only ever expires batches nobody has open.
_CACHE = _DiskCache(
    max_items=int(os.environ.get("IMAGESL_CACHE_MAX", "400")),
    ttl_seconds=int(os.environ.get("IMAGESL_CACHE_TTL", "28800")),
    max_bytes=int(os.environ.get("IMAGESL_CACHE_MB", "3072")) * 1024 * 1024,
)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

def _require_key(x_imagesl_key: Optional[str]) -> None:
    if not _TOKENS:
        return
    if not x_imagesl_key or x_imagesl_key not in _TOKENS:
        raise HTTPException(status_code=401, detail="Invalid or missing ImageSL access key.")


# --------------------------------------------------------------------------- #
# Analysis state helpers
# --------------------------------------------------------------------------- #

def _default_params() -> dict:
    return {
        "background_threshold": engine.BACKGROUND_OD_THRESHOLD,
        "stain_method": "auto",
        "stain_key": None,          # selection mode: chosen antibody key (or None=auto)
        "level": None,              # sensitivity level index (None = the slide's own)
        "regions": [],              # manual focus / ignore / local-sensitivity shapes
        # The detection overlay is a fixed neon green — there is nothing to configure.
        "stainA_hex": None,         # None → stain's natural colour
        "stainB_hex": None,
    }


def _analyze_kwargs(p: dict) -> dict:
    """Common engine.analyze kwargs derived from stored params (used everywhere we
    (re)run analysis, so the chosen marker, sensitivity and manual regions are
    always honoured — screen, downloads, CSV and ZIP alike)."""
    lv = p.get("level")
    return {
        "background_threshold": p.get("background_threshold", engine.BACKGROUND_OD_THRESHOLD),
        "stain_method": p.get("stain_method", "auto"),
        "stain_choice": stain_registry.lookup(p.get("stain_key")),
        "level": (int(lv) if lv is not None else None),
        "regions": p.get("regions") or None,
    }


def _clean_regions(raw) -> list:
    """Validate region shapes coming off the wire. Coordinates are normalised
    0..1, so a region drawn once stays put at any resolution or export size.

    Modes are canonicalised, which also accepts the old `focus`/`ignore` spelling
    — a browser that restored a batch saved before the rename is still holding
    regions under those names, and silently dropping them would quietly discard
    a user's corrections."""
    if not isinstance(raw, list):
        return []
    out = []
    for r in raw[:64]:
        if not isinstance(r, dict):
            continue
        mode = region_tools.canonical_mode(r.get("mode"))
        if mode is None:
            continue
        pts = r.get("points")
        if not isinstance(pts, list) or not pts:
            continue
        clean_pts = []
        for p in pts[:4096]:
            try:
                clean_pts.append([min(1.0, max(0.0, float(p[0]))),
                                  min(1.0, max(0.0, float(p[1])))])
            except (TypeError, ValueError, IndexError):
                continue
        if not clean_pts:
            continue
        out.append({
            "mode": mode,
            "kind": str(r.get("kind", "rect")).lower()[:12],
            "points": clean_pts,
            "radius": float(min(0.5, max(0.001, float(r.get("radius", 0.02) or 0.02)))),
        })
    return out


def _hex_to_rgb(h: Optional[str]) -> Optional[tuple[int, int, int]]:
    if not h:
        return None
    h = h.strip().lstrip("#")
    if len(h) != 6:
        return None
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return None


def _overlay_color(p: dict) -> tuple[int, int, int]:
    """Overlay colour — always neon green. There is no picker and no per-slide auto
    pick, so every overlay (screen, TIFF, comparison, ZIP) is the one same green."""
    return engine.OVERLAY_GREEN


def _stain_indices(maps: dict) -> tuple[int, int]:
    """(Stain-A index, Stain-B index) — A = counterstain, B = target chromogen."""
    return int(maps.get("counter_index", 0)), int(maps.get("target_index", 1))


def _render_images(entry: dict) -> None:
    """The UI shows exactly three panels — Original, Overlay and Stain only — and
    the last two are composited IN THE BROWSER from `original` + `level` (the
    detector's level map) so the sensitivity control and the manual region tools
    update both live with no server round-trip. Those two images are the only
    ones sent."""
    rgb = entry["rgb"]
    # After caching we drop the heavy maps; recompute on demand.
    if "level_map" not in entry["maps"] or "tissue_base" not in entry["maps"]:
        _reanalyze(entry)
    maps = entry["maps"]
    prev = entry.get("images") or {}
    entry["images"] = {
        # The original never changes, so re-encode it only if it is missing.
        "original": prev.get("original") or engine.original_data_uri(rgb),
        "level": engine.level_data_uri(maps["level_map"], maps.get("tissue_base")),
    }


# How many slides may be measured at the same time.
#
# Measurement is seconds of numpy that already spreads itself over every core
# (scikit-image links OpenMP), so running several at once buys no throughput on
# a small container — it just multiplies peak memory and lengthens every
# request. Left unbounded, a review session over a large batch could put dozens
# of measurements in flight at once: an export re-measuring each slide it
# packages, `/api/appearance` firing behind every slider move, and uploads still
# arriving. That is how the container reached the memory ceiling and was
# restarted, which is what the user sees as the batch expiring underneath them.
#
# A threading semaphore rather than an asyncio one, because the ZIP export runs
# as a synchronous generator in a worker thread and has to queue on the same
# gate as everything else.
_MEASURE_SLOTS = max(1, int(os.environ.get("IMAGESL_MAX_CONCURRENCY", "2")))
_MEASURE_GATE = threading.BoundedSemaphore(_MEASURE_SLOTS)


def _measure(rgb: np.ndarray, source_size, **kwargs):
    """Every call into the engine goes through here, so the gate above cannot be
    bypassed by adding another code path later."""
    with _MEASURE_GATE:
        return engine.analyze(rgb, source_size, **kwargs)


def _reanalyze(entry: dict) -> None:
    """Re-run the measurement for this entry's CURRENT params, replacing both the
    result and the maps. Every path that changes a setting goes through here, so
    the reported numbers and the pixels they describe can never come from
    different settings."""
    result, maps = _measure(
        entry["rgb"],
        entry.get("source_size", (entry["rgb"].shape[1], entry["rgb"].shape[0])),
        **_analyze_kwargs(entry["params"]),
    )
    entry["result"], entry["maps"] = result, maps


def _rerender(entry: dict, **updates) -> dict:
    """Apply a sensitivity / region update.

    The browser has already redrawn itself from the level map, so the server's
    job here is to recompute the exact metrics under the same rule — keeping the
    numbers, the CSV and every export in step with what is on screen."""
    p = entry["params"]
    touched = False
    if "level" in updates and updates["level"] is not None:
        lv = updates["level"]
        p["level"] = None if lv in ("", "auto") else int(lv)
        touched = True
    if "regions" in updates and updates["regions"] is not None:
        p["regions"] = _clean_regions(updates["regions"])
        touched = True
    if touched:
        _reanalyze(entry)
        _render_images(entry)
    return entry


def _public(entry: dict) -> dict:
    return {
        "result": entry["result"].to_dict(),
        "images": entry["images"],
        "params": entry["params"],
        "filename": entry.get("filename", ""),
    }


def _view_key(params: dict, auto_level: Optional[int] = None) -> dict:
    """The part of `params` that changes the measurement — used to tell whether a
    caller's on-screen view already matches what was last measured and cached.

    The level is written as the concrete ladder index, never `None`, so "Auto"
    and the index Auto resolves to compare equal. Left as `None` they did not,
    and the CSV fast path missed on every untouched slide."""
    lv = params.get("level")
    if lv is None:
        lv = auto_level
    return {
        "level": (None if lv is None else int(lv)),
        "regions": params.get("regions") or [],
        "stain_key": params.get("stain_key"),
    }


def _requested_view(override: Optional[dict], entry_params: dict,
                    auto_level: Optional[int] = None) -> dict:
    """A caller's requested view, canonicalised the same way `_view_key` is, so
    the two can be compared exactly."""
    ov = override or {}
    level = entry_params.get("level")
    raw = ov.get("level")
    if raw is not None and raw != "":
        if raw == "auto":
            level = None
        else:
            try:
                level = int(float(raw))
            except (TypeError, ValueError):
                pass
    regions = (_clean_regions(ov["regions"]) if ov.get("regions") is not None
               else (entry_params.get("regions") or []))
    if level is None:
        level = auto_level          # "Auto" and its index are the same setting
    return {"level": (None if level is None else int(level)),
            "regions": regions,
            "stain_key": entry_params.get("stain_key")}


def _persist(analysis_id: str, entry: dict) -> None:
    """Save an entry back to cache (keeping it lean) with its CSV row sidecar, so
    later recalcs/appearance edits are reflected in downloads and exports.

    Drops the biggest arrays from the cached copy — the optical-density,
    concentration and evidence maps — so hundreds of slides can be held at once.
    They are only needed to render Stain A/B and are recomputed on demand at
    export time."""
    for k in ("od", "conc", "excess", "brownness"):
        entry["maps"].pop(k, None)
    _CACHE.put(analysis_id, entry,
               meta={"row": _csv_row(entry),
                     "view": _view_key(entry["params"], _auto_level(entry)),
                     "auto_level": _auto_level(entry)})


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

def _asset_version() -> str:
    """Cache-busting token for the CSS and JS, derived from their CONTENT.

    These two files are served with a one-year `max-age`, which is right — they
    are versioned by the `?v=` in the HTML, so a new build is a new URL. It is
    only right while that token actually changes with the build, and it was a
    number typed into `index.html` by hand. Miss the bump once and every
    returning browser keeps running the previous JS for a year against the new
    server: the fixes are deployed and the user does not have them, which is
    indistinguishable from their not working. (That is not hypothetical — it is
    what happened while testing this change.)

    Hashing the bytes removes the step entirely: the token cannot disagree with
    what is being served, because it is computed from it.
    """
    h = hashlib.sha256()
    for f in ("app.js", "styles.css"):
        p = WEB_DIR / f
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(f.encode())
    return h.hexdigest()[:12]


def _serve_html(name: str) -> HTMLResponse:
    path = WEB_DIR / name
    if not path.is_file():
        return HTMLResponse(f"<h1>ImageSL</h1><p>Missing {name}.</p>", status_code=500)
    html = path.read_text(encoding="utf-8").replace("__ASSET_V__", _asset_version())
    # NEVER let the browser (or the desktop client's WebView) hold on to the HTML.
    # It is the only unversioned file we serve, so a stale copy pins the whole app
    # to an old build: it keeps requesting the old `?v=` CSS/JS and a deploy looks
    # like it "didn't update" until the user hard-refreshes.
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    # Desktop build: straight into the analyzer.
    if IMAGESL_DESKTOP:
        return _serve_html("index.html")
    # Public site: a simple landing page (what ImageSL is + a Download button).
    # Fall back to the analyzer if landing.html is absent, so the root is never
    # left without a working page.
    if (WEB_DIR / "landing.html").is_file():
        return _serve_html("landing.html")
    return _serve_html("index.html")


@app.get("/app", response_class=HTMLResponse)
def analyzer() -> HTMLResponse:
    """The analyzer itself. On the public site it lives under /app so the landing
    page can own the root; the desktop launcher opens this directly."""
    return _serve_html("index.html")


@app.get("/privacy", response_class=HTMLResponse)
def privacy() -> HTMLResponse:
    return _serve_html("privacy.html")


@app.get("/terms", response_class=HTMLResponse)
def terms() -> HTMLResponse:
    return _serve_html("terms.html")


# CSS/JS carry a `?v=` in the HTML, so a new build always requests a new URL and
# these can be cached hard.
_STATIC_CACHE = {"Cache-Control": "public, max-age=31536000"}


def _serve_css(name: str):
    path = WEB_DIR / name
    return (FileResponse(str(path), media_type="text/css", headers=_STATIC_CACHE)
            if path.is_file() else HTMLResponse("", status_code=404))


@app.get("/styles.css")
def styles_css():
    return _serve_css("styles.css")


@app.get("/tokens.css")
def tokens_css():
    """Design tokens shared by the landing page and the analyzer."""
    return _serve_css("tokens.css")


@app.get("/site.css")
def site_css():
    """Chrome shared by the public pages (landing, privacy, terms)."""
    return _serve_css("site.css")


@app.get("/app.js")
def app_js():
    path = WEB_DIR / "app.js"
    return (FileResponse(str(path), media_type="application/javascript", headers=_STATIC_CACHE)
            if path.is_file() else HTMLResponse("", status_code=404))


@app.get("/api/health")
async def health() -> JSONResponse:
    """Deliberately `async`, and deliberately doing no work.

    A plain `def` endpoint is run by Starlette in the shared worker threadpool —
    the same pool every slide measurement occupies. On a small container a
    measurement is seconds of numpy, so under a batch the pool is saturated and
    a health check simply queues behind it. The load balancer's check has a
    5 s timeout and restarts the container after five failures, and a restart
    takes the whole in-memory batch with it: uploads in flight come back as
    "Analysis failed", and every slide already analysed is suddenly "expired"
    mid-session. Answering on the event loop instead makes that impossible —
    this handler cannot be blocked by measurement work however busy the box is.
    """
    return JSONResponse({"status": "ok", "version": APP_VERSION})


@app.post("/api/keepalive")
async def api_keepalive(request: Request, x_imagesl_key: Optional[str] = Header(default=None)):
    """Refresh the analyses a page still has open, and report any already gone.

    A batch under review is in use even when nothing is being clicked, and the
    server has no other way to know that. Without this, a session left open over
    a lunch break came back to exports that failed or — worse — succeeded and
    produced an empty archive."""
    _require_key(x_imagesl_key)
    body = await request.json()
    ids = body.get("analysis_ids")
    if not isinstance(ids, list):
        raise HTTPException(status_code=400, detail="No analysis_ids provided.")
    alive = [a for a in ids[:2000] if _CACHE.touch(str(a))]
    return JSONResponse({"alive": alive,
                         "expired": [a for a in ids[:2000] if a not in set(alive)]})


# --------------------------------------------------------------------------- #
# Analysis API
# --------------------------------------------------------------------------- #

@app.get("/api/stains")
def api_stains() -> JSONResponse:
    """The full antibody registry for the pre-upload "select your stain" mode."""
    return JSONResponse({"stains": stain_registry.as_list()})


def _undecodable_reason(data: bytes, name: str, exc: Exception) -> str:
    """Say what is actually wrong with a file that would not decode.

    Pillow's own message for anything it does not recognise is "cannot identify
    image file <_io.BytesIO object at 0x000001C4...>", which names a memory
    address and tells the user nothing. The common cases are worth naming
    outright — above all the macOS AppleDouble sidecar, because a folder copied
    from a Mac contains one `._name.tif` for every real slide, they are picked up
    by any select-all, and a batch then reports half its files as failures with
    no hint that they were never images.
    """
    base = os.path.basename(name or "")
    if base.startswith("._") or data[:4] == b"\x00\x05\x16\x07":
        return ("This is a macOS resource-fork stub, not an image — Finder writes "
                "one of these beside every real file when copying to a non-Mac "
                "disk. The slide itself is the file of the same name without the "
                "leading \"._\". You can safely leave these out of the selection.")
    if not data[:2] in (b"II", b"MM", b"\x89P", b"\xff\xd8", b"RI", b"GI", b"BM"):
        return ("This file is not an image ImageSL can read — its contents do not "
                "match any supported format (TIFF, PNG, JPEG, WebP or BMP).")
    if "truncated" in str(exc).lower():
        return ("This image file is incomplete — it looks like the copy or "
                "download was cut short. Try copying it again.")
    return ("This file could not be read as an image. It may be corrupt, or in a "
            "TIFF variant ImageSL does not support.")


def _analyze_upload(data: bytes, name: str, stain_key: Optional[str]) -> dict:
    """Decode + measure + cache one slide. Runs off the event loop (see caller).

    Decoding and measuring are reported separately: "this file is not an image
    ImageSL can read" and "this image could not be measured" are different
    problems for the user, and collapsing them into one generic failure is what
    made a batch report "2 failed" with nothing to act on."""
    try:
        rgb, source_size = engine.load_rgb(data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=_undecodable_reason(data, name, exc))

    params = _default_params()
    chosen = stain_registry.lookup(stain_key) if stain_key else None
    if chosen:
        params["stain_key"] = chosen["key"]

    try:
        result, maps = _measure(rgb, source_size, **_analyze_kwargs(params))
    except MemoryError:
        raise HTTPException(
            status_code=507,
            detail="Ran out of memory measuring this slide. Try a smaller image.")
    except Exception as exc:
        raise HTTPException(status_code=422,
                            detail=f"Could not measure this slide ({exc}).")

    entry = {
        "rgb": rgb, "source_size": source_size, "maps": maps, "result": result,
        "params": params, "images": {"original": engine.original_data_uri(rgb)},
        "filename": name,
    }
    _render_images(entry)

    analysis_id = uuid.uuid4().hex
    _persist(analysis_id, entry)
    return {"analysis_id": analysis_id, **_public(entry)}


@app.post("/api/analyze")
async def api_analyze(
    file: UploadFile = File(...),
    filename: Optional[str] = Form(default=None),
    stain_key: Optional[str] = Form(default=None),
    x_imagesl_key: Optional[str] = Header(default=None),
):
    _require_key(x_imagesl_key)
    data = await _read_upload(file)
    name = filename or file.filename or "image"
    # Measurement is seconds of numpy per slide. Running it inline blocks the
    # event loop for the whole batch, so health checks and every other request
    # queue behind it — long enough on a modest server for a proxy to give up
    # and for the browser to record the slide as "failed".
    payload = await run_in_threadpool(_analyze_upload, data, name, stain_key)
    return JSONResponse(payload)


def _render_type(entry: dict, image_type: str) -> np.ndarray:
    """Produce the RGB array for a requested image variant (shared by download + zip)."""
    rgb, p = entry["rgb"], entry["params"]

    # Stain A/B and the overlay need the concentration/positive maps, which we drop
    # from the cache to stay lean — recompute on demand (honours marker, sensitivity
    # and manual regions). Which cached array each variant actually needs:
    # `_persist` drops the heavy maps, so only ask for a re-analysis when the
    # specific array this variant depends on is genuinely missing. The recomputed
    # maps are stored back on the entry, so a ZIP asking for several variants of
    # the same slide re-measures it once rather than once per variant.
    _NEEDS = {
        "stainA": "conc", "stainB": "conc",
        "overlay": "positive", "comparison": "positive", "stainOnly": "positive",
        "backgroundRemoved": "tissue_mask",
    }
    need = _NEEDS.get(image_type)
    if need and need not in entry["maps"]:
        _reanalyze(entry)
    maps = entry["maps"]
    a_idx, b_idx = _stain_indices(maps)

    if image_type == "original":
        return rgb
    if image_type == "overlay":
        return engine.render_overlay(rgb, maps, color=_overlay_color(p))
    if image_type == "stainOnly":
        return engine.render_stain_only(rgb, maps)
    if image_type == "backgroundRemoved":
        return engine.render_background_removed(rgb, maps)
    if image_type == "stainA":
        return engine.render_stain(maps, a_idx, _hex_to_rgb(p.get("stainA_hex")))
    if image_type == "stainB":
        return engine.render_stain(maps, b_idx, _hex_to_rgb(p.get("stainB_hex")))
    if image_type == "comparison":
        overlay = engine.render_overlay(rgb, maps, color=_overlay_color(p))
        r = entry["result"]
        return engine.compose_comparison(
            rgb, overlay, "Original", "Detection Overlay",
            metric_text=f"{r.positive_percent:.2f}% positive area",
            stain_text=getattr(r, "stain_label", ""),
        )
    raise HTTPException(status_code=400, detail="Invalid image type")


def _safe_name(name: str) -> str:
    stem = re.sub(r"\.[^.]*$", "", name or "image")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "image"
    return stem


def _tiff_bytes(arr: np.ndarray) -> bytes:
    """Lossless but DEFLATE-compressed TIFF — histology compresses well, so this
    is far smaller and faster to transfer than raw uncompressed TIFF."""
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="TIFF", compression="tiff_deflate")
    return buf.getvalue()


def _auto_level(entry: dict) -> int:
    """This slide's own automatic operating point on the sensitivity ladder."""
    r = entry.get("result")
    return int(getattr(r, "auto_level", detect_mod.AUTO_LEVEL))


def _same_level(a, b, entry: dict) -> bool:
    """Do these two level values select the same operating point?

    `None` means "the slide's own automatic point", and the browser reports that
    same point as its concrete index. They are the same setting written two ways.
    """
    auto = _auto_level(entry)
    return (auto if a is None else a) == (auto if b is None else b)


def _apply_view(entry: dict, level: Optional[str], regions=None) -> bool:
    """Adopt the caller's CURRENT on-screen sensitivity and manual regions, and
    RE-MEASURE if either of them changed.

    Re-measuring here is the whole point. Previously this only invalidated the
    cached `positive` mask, and the re-analysis that followed was thrown into a
    local variable — so `entry["result"]` still held the numbers from whatever
    settings were last persisted. Every consumer of that result then reported
    stale values against fresh pixels: the CSV row, and the percentage printed
    into the comparison TIFF's footer. That is exactly the "I change the
    sensitivity or a region, export, and the old number comes out" failure.
    Returns whether anything changed."""
    p = entry["params"]
    changed = False

    if level is not None and level != "":
        new_level = p.get("level")
        if level == "auto":
            new_level = None
        else:
            try:
                new_level = int(float(level))
            except (TypeError, ValueError):
                pass
        # `None` and the slide's own automatic index mean the same operating
        # point, so treat them as equal. They are not equal as values, and that
        # cost real time: a card sitting at Auto reports its level as the
        # concrete integer (100), while the cached params still hold `None`, so
        # every export saw a difference and re-measured every untouched slide.
        # Measured, that was 1.02 s a slide against 0.16 s — six times the work
        # to arrive at exactly the same numbers.
        if _same_level(new_level, p.get("level"), entry):
            new_level = p.get("level")
        if new_level != p.get("level"):
            p["level"] = new_level
            changed = True

    if regions is not None:
        clean = _clean_regions(regions)
        if clean != (p.get("regions") or []):
            p["regions"] = clean
            changed = True

    if changed:
        _reanalyze(entry)
    return changed


def _download_tif(analysis_id: str, image_type: str, level: Optional[str],
                  regions=None) -> StreamingResponse:
    entry = _CACHE.get(analysis_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Analysis expired")
    if _apply_view(entry, level, regions):
        _persist(analysis_id, entry)

    data = _tiff_bytes(_render_type(entry, image_type))
    stem = _safe_name(entry.get("filename", "image"))
    return StreamingResponse(
        io.BytesIO(data),
        media_type="image/tiff",
        headers={"Content-Disposition": f'attachment; filename="{stem}_{image_type}.tif"'},
    )


@app.post("/api/download_tif")
async def api_download_tif_post(request: Request,
                                x_imagesl_key: Optional[str] = Header(default=None)):
    """Single-image download. POST rather than GET so the caller can send the
    manual regions along with the sensitivity — a hand-drawn lasso does not fit
    in a query string, and without it a single-slide TIFF was rendered ignoring
    every region the user had drawn while the batch ZIP honoured them."""
    _require_key(x_imagesl_key)
    body = await request.json()
    return _download_tif(str(body.get("analysis_id") or ""),
                         str(body.get("image_type") or ""),
                         body.get("level"), body.get("regions"))


@app.get("/api/download_tif")
def api_download_tif(analysis_id: str, image_type: str, level: Optional[str] = None):
    """Kept so an older cached page keeps working; it cannot carry regions."""
    return _download_tif(analysis_id, image_type, level)


@app.post("/api/rehydrate")
async def api_rehydrate(request: Request, x_imagesl_key: Optional[str] = Header(default=None)):
    """Rebuild an analysis the server is no longer holding, from the copy the
    browser kept, and return a fresh analysis id.

    This is the answer to the failure the user actually hits: a long working
    session — forty slides, regions drawn on a dozen of them, sensitivities
    tuned — and then the export comes back empty or refuses, because the server
    let go of the batch. Every other defence here (the heartbeat, the eight-hour
    TTL, the byte budget, the health endpoint that cannot be starved) makes that
    less likely; none of them makes it recoverable, and "re-upload forty slides
    and redo your regions" is not a recovery.

    It is recoverable because the browser is not just holding ids. It holds the
    exact image the engine measured — `images.original`, the downsampled RGB,
    kept in IndexedDB across reloads — together with the slide's sensitivity and
    its regions. That is everything the measurement needs, so the analysis can
    simply be made again, and the settings the user spent the session choosing
    are carried through rather than lost.

    That copy is stored losslessly (`engine.original_data_uri`) precisely so this
    path exists: the rebuilt measurement is bit-for-bit the one the session was
    working with — verified equal on all 46 validation slides — rather than an
    approximation of it that would need a caveat attached to every number.
    """
    _require_key(x_imagesl_key)
    body = await request.json()
    src = str(body.get("image") or "")
    if not src.startswith("data:"):
        raise HTTPException(status_code=400, detail="No stored image to rebuild from.")
    try:
        import base64 as _b64
        data = _b64.b64decode(src.split(",", 1)[1])
    except Exception:
        raise HTTPException(status_code=400, detail="The stored image could not be decoded.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Stored image too large.")

    name = str(body.get("filename") or "image")
    view = body.get("params") if isinstance(body.get("params"), dict) else {}

    def work() -> dict:
        payload = _analyze_upload(data, name, view.get("stain_key"))
        aid = payload["analysis_id"]
        entry = _CACHE.get(aid)
        if entry is None:                 # written and immediately evicted: nothing to fix up
            return payload
        # Re-apply the view the user had on screen, so a rebuilt slide comes back
        # exactly as they left it rather than reset to Auto with no regions.
        if _apply_view(entry, view.get("level"), view.get("regions")):
            _render_images(entry)
            _persist(aid, entry)
        pub = _public(entry)
        # The caller sent us this image; sending it straight back would double the
        # cost of recovering a batch for nothing. The level map it does need.
        pub["images"] = {k: v for k, v in (pub.get("images") or {}).items() if k != "original"}
        return {"analysis_id": aid, "rebuilt": True, **pub}

    return JSONResponse(await run_in_threadpool(work))


@app.post("/api/appearance")
async def api_appearance(request: Request, x_imagesl_key: Optional[str] = Header(default=None)):
    _require_key(x_imagesl_key)
    body = await request.json()

    def work() -> dict:
        aid = body.get("analysis_id")
        entry = _CACHE.get(aid)
        if not entry:
            raise HTTPException(status_code=404, detail="Analysis expired; re-run analysis.")
        _rerender(entry, level=body.get("level"), regions=body.get("regions"))
        _persist(aid, entry)
        return _public(entry)

    return JSONResponse(await run_in_threadpool(work))


# --------------------------------------------------------------------------- #
# Data export (CSV spreadsheet + ZIP of images) — single or batch
# --------------------------------------------------------------------------- #

_CSV_COLUMNS = [
    "filename",
    "detected_stain",
    "stain_category",
    "positive_area_percent_of_tissue",
    "positive_pixels",
    "tissue_pixels",
    "total_image_pixels",
    "positive_percent_of_image",
    # --- what was detected, as structures ---------------------------------- #
    "stained_objects",
    "median_object_area_px",
    "mean_object_area_px",
    # --- how the operating point was reached ------------------------------- #
    "detection_bar_excess_od",
    "detection_floor_excess_od",
    "tissue_texture_sigma_od",
    "object_separability",
    "object_split_discard_fraction",
    "staining_pattern",
    "sensitivity_level",
    "sensitivity_auto_level",
    "sensitivity_levels",
    "sensitivity_setting",
    # --- corrections the user made by hand ----------------------------------- #
    "manual_regions",
    "include_regions",
    "exclude_regions",
    "pixels_added_by_include",
    "pixels_removed_by_exclude",
    "tissue_pixels_before_regions",
    "mean_positive_intensity",
    "detection_confidence",
    "stain_family",
    "target_stain_index",
    # --- background segmentation (what was removed, and how it was decided) --- #
    "dab_chromogen_detected",
    "background_pixels",
    "background_percent_of_image",
    "tissue_percent_of_image",
    "tissue_od_threshold",
    "background_method",
    "white_point_rgb",
    "analysis_width_px",
    "analysis_height_px",
    "source_width_px",
    "source_height_px",
    "notes",
]


def _csv_row(entry: dict) -> dict:
    r = entry["result"]
    total_px = int(r.width) * int(r.height)
    pct_of_image = round((r.positive_pixels / total_px * 100.0), 3) if total_px else 0.0
    sep = float(getattr(r, "separability", 0.0))
    # What the sensitivity control was actually set to, in the terms the user
    # sees on screen, so a row can be reproduced from the export alone. The
    # ladder scales the bar a structure's peak must clear, so the setting IS
    # that multiplier — a quantity that means the same thing on every slide,
    # unlike a step index (which now runs to 200).
    lvl = int(getattr(r, "level", 0))
    auto = int(getattr(r, "auto_level", 0))
    n_lv = int(getattr(r, "level_count", detect_mod.N_LEVELS))
    # The ladder's span is per-slide (its ends sit just outside this slide's own
    # strongest and weakest structure — see detect._ladder), so the multiplier is
    # read from what the slide reported rather than from the module constants,
    # which would name a span this slide never used.
    l_hi = float(getattr(r, "ladder_hi", detect_mod.LADDER_STRICT))
    l_lo = float(getattr(r, "ladder_lo", detect_mod.LADDER_LOOSE))
    if entry.get("params", {}).get("level") is None or lvl == auto:
        setting = "Auto"
    elif lvl < auto:
        setting = f"x{l_hi ** ((auto - lvl) / max(auto, 1)):.2f}"
    else:
        setting = f"x{l_lo ** ((lvl - auto) / max(n_lv - 1 - auto, 1)):.2f}"
    return {
        "filename": entry.get("filename", "image"),
        "detected_stain": getattr(r, "stain_label", ""),
        "stain_category": getattr(r, "compartment", ""),
        "positive_area_percent_of_tissue": r.positive_percent,
        "positive_pixels": r.positive_pixels,
        "tissue_pixels": r.tissue_pixels,
        "total_image_pixels": total_px,
        "positive_percent_of_image": pct_of_image,
        "stained_objects": getattr(r, "objects", 0),
        "median_object_area_px": getattr(r, "median_object_px", 0),
        "mean_object_area_px": getattr(r, "mean_object_px", 0.0),
        "detection_bar_excess_od": getattr(r, "detection_bar", 0.0),
        "detection_floor_excess_od": getattr(r, "detection_floor", 0.0),
        "tissue_texture_sigma_od": getattr(r, "texture_sigma", 0.0),
        "object_separability": round(sep, 3),
        "object_split_discard_fraction": getattr(r, "bar_discard", 0.0),
        # Focal = the stained structures formed a population clearly apart from
        # the background, and the detector used that split to set its operating
        # point. Even = they did not, so everything chromogen-coloured above the
        # slide's own noise was counted and the percentage is a relative reading
        # comparable only within a staining batch.
        #
        # Taken from the detector's own verdict rather than re-derived from a
        # separability number here, so the label cannot disagree with the rule
        # that actually produced the measurement.
        "staining_pattern": ("even" if getattr(r, "single_population", False) else "focal"),
        "sensitivity_level": lvl,
        "sensitivity_auto_level": auto,
        "sensitivity_levels": getattr(r, "level_count", 0),
        "sensitivity_setting": setting,
        "manual_regions": getattr(r, "region_count", 0),
        "include_regions": getattr(r, "include_regions", 0),
        "exclude_regions": getattr(r, "exclude_regions", 0),
        # Exactly what the hand tools changed. The denominator is untouched by
        # them, so these two numbers plus `positive_pixels` fully describe the
        # correction — a reader can recover the automatic figure from the row.
        "pixels_added_by_include": getattr(r, "region_added_pixels", 0),
        "pixels_removed_by_exclude": getattr(r, "region_removed_pixels", 0),
        # Equal to `tissue_pixels` now: manual regions no longer move the
        # denominator. Kept so a column that existed in earlier exports still
        # exists, and so the invariant is visible rather than assumed.
        "tissue_pixels_before_regions": getattr(r, "tissue_pixels_total", 0) or r.tissue_pixels,
        "mean_positive_intensity": r.mean_positive_intensity,
        "detection_confidence": getattr(r, "confidence", 1.0),
        "stain_family": r.method,
        "target_stain_index": r.target_index,
        "dab_chromogen_detected": "yes" if getattr(r, "chromogen_present", True) else "no",
        "background_pixels": getattr(r, "background_pixels", 0),
        "background_percent_of_image": getattr(r, "background_percent", 0.0),
        "tissue_percent_of_image": getattr(r, "tissue_percent", 0.0),
        "tissue_od_threshold": getattr(r, "tissue_threshold", 0.0),
        "background_method": getattr(r, "tissue_method", ""),
        "white_point_rgb": " ".join(str(x) for x in (getattr(r, "white_point", []) or [])),
        "analysis_width_px": r.width,
        "analysis_height_px": r.height,
        "source_width_px": r.source_width,
        "source_height_px": r.source_height,
        "notes": " ".join(getattr(r, "notes", []) or []),
    }


def _build_csv_from_rows(rows: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=_CSV_COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in _CSV_COLUMNS})
    return out.getvalue()


def _collect_rows(analysis_ids, overrides: Optional[dict] = None) -> list[dict]:
    """One CSV row per analysis, guaranteed to describe the caller's CURRENT view.

    For each slide the requested view (sensitivity + manual regions) is compared
    with the view the cached row was measured under. They match for a slide
    nobody has touched, and that row is served straight from the small JSON
    sidecar without going near the megabyte-scale pickle. Where they differ —
    the user moved the slider or drew a region and exported before the debounced
    background sync landed — the slide is re-measured under the requested view
    and the fresh row is used.

    The previous version read the sidecars unconditionally, so an export taken
    within a third of a second of a change (or after any change that failed to
    sync) silently carried the *old* percentage. For a measurement instrument
    that is the worst kind of bug: the number is wrong but looks authoritative.
    """
    if not isinstance(analysis_ids, list) or not analysis_ids:
        raise HTTPException(status_code=400, detail="No analysis_ids provided.")
    overrides = overrides if isinstance(overrides, dict) else {}
    rows: list[dict] = []
    for aid in analysis_ids:
        ov = overrides.get(aid)
        meta = _CACHE.get_meta(aid)
        if meta and ov is None:
            rows.append(meta.get("row", {}))
            continue
        if meta and isinstance(meta.get("view"), dict):
            entry_params = {"level": meta["view"].get("level"),
                            "regions": meta["view"].get("regions"),
                            "stain_key": meta["view"].get("stain_key")}
            if _requested_view(ov, entry_params, meta.get("auto_level")) == meta["view"]:
                rows.append(meta.get("row", {}))
                continue
        entry = _CACHE.get(aid)
        if not entry:
            continue
        # One slide that cannot be re-measured must not take the spreadsheet with
        # it. Before, any failure here surfaced as a 500 and the user got no CSV
        # at all for a batch of forty-six — the same shape of problem as the
        # truncated ZIP: a whole export lost to one slide.
        try:
            _apply_view(entry, (ov or {}).get("level"), (ov or {}).get("regions"))
            rows.append(_csv_row(entry))
            _persist(aid, entry)
        except Exception:                # noqa: BLE001 — see above
            pass
        entry = None
    if not rows:
        raise HTTPException(
            status_code=410,
            detail=(f"All {len(analysis_ids)} analyses have expired, so there is nothing "
                    f"to export. Re-upload the slides and try again."))
    return rows


@app.post("/api/export_csv")
async def api_export_csv(request: Request, x_imagesl_key: Optional[str] = Header(default=None)):
    _require_key(x_imagesl_key)
    body = await request.json()
    overrides = body.get("overrides") if isinstance(body.get("overrides"), dict) else {}
    rows = await run_in_threadpool(_collect_rows, body.get("analysis_ids"), overrides)
    csv_text = _build_csv_from_rows(rows)
    fname = "ImageSL_results.csv" if len(rows) > 1 else f"{_safe_name(rows[0].get('filename'))}_data.csv"
    return StreamingResponse(
        io.BytesIO(csv_text.encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


class _ZipSink:
    """A writable buffer the ZipFile streams into; drained between files so bytes
    flow to the client as they are produced (no gateway timeout, tiny memory)."""

    def __init__(self) -> None:
        self._b = bytearray()

    def write(self, data) -> int:
        self._b += data
        return len(data)

    def flush(self) -> None:
        pass

    def drain(self) -> bytes:
        b = bytes(self._b)
        self._b.clear()
        return b


def _zip_stream(analysis_ids, images, include_csv, overrides=None, missing=()):
    """Generator that builds the ZIP incrementally, one image at a time. Only a
    single analysis entry is ever held in memory, so 40+ slides won't OOM.
    `overrides` maps analysis_id → {level, regions} so each slide's export
    matches its exact on-screen appearance."""
    overrides = overrides or {}
    sink = _ZipSink()
    zf = zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True)  # TIFFs already compressed
    used: set[str] = set()
    rows: list[dict] = []
    dropped: list[str] = list(missing)
    problems: list[str] = []

    # Once the first byte is out, the status line is already sent: a failure from
    # here on cannot be reported as an HTTP error, and an exception escaping this
    # generator ends the response mid-archive. What the browser then saves is a
    # file with no central directory — an archive Windows and every other tool
    # calls corrupt. That is the "the ZIP downloads but will not open" report,
    # and it is why every step below is contained: one slide that fails to render
    # (a transient memory ceiling, an entry evicted while the export ran) costs
    # that slide, is named in the manifest, and the other forty-five still arrive
    # in a well-formed archive.
    try:
        for aid in analysis_ids:
            try:
                entry = _CACHE.get(aid)
            except Exception:
                entry = None
            if not entry:
                # Record it. Skipping silently is how a batch whose entries had
                # expired produced a perfectly valid, completely empty ZIP — the
                # download "worked", and the folder had nothing in it.
                dropped.append(aid)
                continue
            ov = overrides.get(aid) or {}
            stem = _safe_name(entry.get("filename", "image"))
            try:
                changed = _apply_view(entry, ov.get("level"), ov.get("regions"))
                for image_type in images:
                    name = f"{stem}_{image_type}.tif"
                    k = 2
                    while name in used:
                        name = f"{stem}_{image_type}_{k}.tif"
                        k += 1
                    used.add(name)
                    zf.writestr(name, _tiff_bytes(_render_type(entry, image_type)))
                    chunk = sink.drain()
                    if chunk:
                        yield chunk
                if include_csv:
                    # Built from the entry AFTER `_apply_view` re-measured it, so
                    # the row and the images beside it describe the same settings.
                    rows.append(_csv_row(entry))
                if changed:
                    _persist(aid, entry)   # keep later exports in step with this one
            except Exception as exc:       # noqa: BLE001 — deliberately broad, see above
                problems.append(f"{stem} ({aid}): {exc}")
            finally:
                entry = None               # release before loading the next
        if include_csv and rows:
            zf.writestr("ImageSL_results.csv", _build_csv_from_rows(rows).encode("utf-8-sig"))
        if dropped or problems:
            lines = ["Some slides are not in this archive.", ""]
            if dropped:
                lines += ["The server was no longer holding these when the export was built:", ""]
                lines += [f"  - {a}" for a in dropped]
                lines += ["", "Analyses are kept for a limited time. Reload the page with the",
                          "batch still open and export again — ImageSL will rebuild them from",
                          "the copy your browser kept — or re-upload those slides.", ""]
            if problems:
                lines += ["These could not be rendered:", ""]
                lines += [f"  - {p}" for p in problems]
                lines += [""]
            zf.writestr("MISSING_SLIDES.txt", "\n".join(lines).encode("utf-8"))
    finally:
        # The central directory is what makes the bytes a ZIP at all, so it is
        # written whatever happened above — including a client that disconnected.
        try:
            zf.close()
        except Exception:
            pass
    chunk = sink.drain()
    if chunk:
        yield chunk


@app.post("/api/export_zip")
async def api_export_zip(request: Request, x_imagesl_key: Optional[str] = Header(default=None)):
    _require_key(x_imagesl_key)
    body = await request.json()
    analysis_ids = body.get("analysis_ids")
    if not isinstance(analysis_ids, list) or not analysis_ids:
        raise HTTPException(status_code=400, detail="No analysis_ids provided.")

    valid = {"original", "overlay", "stainOnly", "backgroundRemoved",
             "stainA", "stainB", "comparison"}
    images = [t for t in (body.get("images") or ["comparison"]) if t in valid] or ["comparison"]
    include_csv = body.get("include_csv", True)
    overrides = body.get("overrides") if isinstance(body.get("overrides"), dict) else {}

    # Decide up front, while a real error can still be returned. Once the
    # response starts streaming the status line is already sent and a failure
    # can only appear as a truncated or empty archive.
    alive = [a for a in analysis_ids if _CACHE.touch(a)]
    missing = [a for a in analysis_ids if a not in set(alive)]
    if not alive:
        raise HTTPException(
            status_code=410,
            detail=(f"All {len(analysis_ids)} analyses have expired, so there is nothing "
                    f"to export. Re-upload the slides and try again."))

    return StreamingResponse(
        _zip_stream(alive, images, include_csv, overrides, missing),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="ImageSL_export.zip"',
                 "X-ImageSL-Missing": str(len(missing))},
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024*1024)} MB limit.")
    return data


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
