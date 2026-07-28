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
import io
from PIL import Image
import numpy as np

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
    def __init__(self, max_items: int = 50, ttl_seconds: int = 300):
        self._dir = CACHE_DIR
        self._lock = threading.Lock()
        self._max = max_items
        self._ttl = ttl_seconds

    def _file(self, key: str) -> Path:
        return self._dir / f"{key}.pkl"

    def _meta_file(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def _drop(self, key: str) -> None:
        self._file(key).unlink(missing_ok=True)
        self._meta_file(key).unlink(missing_ok=True)

    def put(self, key: str, value: dict, meta: Optional[dict] = None) -> None:
        """Store the heavy analysis entry, plus a tiny JSON sidecar (`meta`) that
        CSV export can read without unpickling the whole (megabytes) entry."""
        with self._lock:
            self._evict()
            with open(self._file(key), "wb") as f:
                pickle.dump((time.time(), value), f)
            if meta is not None:
                with open(self._meta_file(key), "w", encoding="utf-8") as f:
                    json.dump({"ts": time.time(), "row": meta}, f)

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            path = self._file(key)
            if not path.exists():
                return None
            try:
                with open(path, "rb") as f:
                    ts, value = pickle.load(f)
                if time.time() - ts > self._ttl:
                    self._drop(key)
                    return None
                with open(path, "wb") as f:  # refresh timestamp on read
                    pickle.dump((time.time(), value), f)
                return value
            except Exception:
                self._drop(key)
                return None

    def get_meta(self, key: str) -> Optional[dict]:
        """Fast path for CSV export — reads the small JSON sidecar only."""
        with self._lock:
            p = self._meta_file(key)
            if not p.exists():
                return None
            try:
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
                if time.time() - d.get("ts", 0) > self._ttl:
                    self._drop(key)
                    return None
                d["ts"] = time.time()
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(d, f)
                return d.get("row")
            except Exception:
                p.unlink(missing_ok=True)
                return None

    def _evict(self) -> None:
        now = time.time()
        valid_files = []
        for p in list(self._dir.glob("*.pkl")):
            key = p.stem
            try:
                with open(p, "rb") as f:
                    ts, _ = pickle.load(f)
                if now - ts > self._ttl:
                    self._drop(key)
                else:
                    valid_files.append((ts, key))
            except Exception:
                self._drop(key)

        if len(valid_files) >= self._max:
            valid_files.sort(key=lambda x: x[0])
            for _, key in valid_files[: len(valid_files) - self._max + 1]:
                self._drop(key)


# TTL/size are generous enough that a large batch (dozens of slides) survives
# analysis + review + export. Still auto-wipes for privacy; tune via env.
_CACHE = _DiskCache(
    max_items=int(os.environ.get("IMAGESL_CACHE_MAX", "400")),
    ttl_seconds=int(os.environ.get("IMAGESL_CACHE_TTL", "3600")),
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
    0..1, so a region drawn once stays put at any resolution or export size."""
    if not isinstance(raw, list):
        return []
    out = []
    for r in raw[:64]:
        if not isinstance(r, dict):
            continue
        mode = str(r.get("mode", "")).lower()
        if mode not in region_tools.MODES:
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
            "delta": int(max(-64, min(64, int(r.get("delta", 0) or 0)))),
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
    rgb, p = entry["rgb"], entry["params"]
    maps = entry["maps"]
    # After caching we drop the heavy maps; recompute on demand.
    if "level_map" not in maps:
        _, maps = engine.analyze(rgb, entry.get("source_size", (rgb.shape[1], rgb.shape[0])),
                                 **_analyze_kwargs(p))
        entry["maps"] = maps
    entry["images"] = {
        "original": entry["images"].get("original") if entry.get("images") else engine.to_data_uri(rgb, "JPEG"),
        "level": engine.level_data_uri(maps["level_map"]),
    }


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
        result, maps = engine.analyze(entry["rgb"], entry["source_size"], **_analyze_kwargs(p))
        entry["result"], entry["maps"] = result, maps
        _render_images(entry)
    return entry


def _public(entry: dict) -> dict:
    return {
        "result": entry["result"].to_dict(),
        "images": entry["images"],
        "params": entry["params"],
        "filename": entry.get("filename", ""),
    }


def _persist(analysis_id: str, entry: dict) -> None:
    """Save an entry back to cache (keeping it lean) with its CSV row sidecar, so
    later recalcs/appearance edits are reflected in downloads and exports.

    Drops the biggest arrays from the cached copy — the optical-density,
    concentration and evidence maps — so hundreds of slides can be held at once.
    They are only needed to render Stain A/B and are recomputed on demand at
    export time."""
    for k in ("od", "conc", "excess", "brownness"):
        entry["maps"].pop(k, None)
    _CACHE.put(analysis_id, entry, meta=_csv_row(entry))


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

def _serve_html(name: str) -> HTMLResponse:
    path = WEB_DIR / name
    if not path.is_file():
        return HTMLResponse(f"<h1>ImageSL</h1><p>Missing {name}.</p>", status_code=500)
    # NEVER let the browser (or the desktop client's WebView) hold on to the HTML.
    # It is the only unversioned file we serve, so a stale copy pins the whole app
    # to an old build: it keeps requesting the old `?v=` CSS/JS and a deploy looks
    # like it "didn't update" until the user hard-refreshes.
    return HTMLResponse(path.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    return _serve_html("index.html")


# CSS/JS carry a `?v=` in the HTML, so a new build always requests a new URL and
# these can be cached hard.
_STATIC_CACHE = {"Cache-Control": "public, max-age=31536000"}


@app.get("/styles.css")
def styles_css():
    path = WEB_DIR / "styles.css"
    return (FileResponse(str(path), media_type="text/css", headers=_STATIC_CACHE)
            if path.is_file() else HTMLResponse("", status_code=404))


@app.get("/app.js")
def app_js():
    path = WEB_DIR / "app.js"
    return (FileResponse(str(path), media_type="application/javascript", headers=_STATIC_CACHE)
            if path.is_file() else HTMLResponse("", status_code=404))


@app.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": APP_VERSION})


# --------------------------------------------------------------------------- #
# Analysis API
# --------------------------------------------------------------------------- #

@app.get("/api/stains")
def api_stains() -> JSONResponse:
    """The full antibody registry for the pre-upload "select your stain" mode."""
    return JSONResponse({"stains": stain_registry.as_list()})


@app.post("/api/analyze")
async def api_analyze(
    file: UploadFile = File(...),
    filename: Optional[str] = Form(default=None),
    stain_key: Optional[str] = Form(default=None),
    x_imagesl_key: Optional[str] = Header(default=None),
):
    _require_key(x_imagesl_key)
    data = await _read_upload(file)

    try:
        rgb, source_size = engine.load_rgb(data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not decode image: {exc}")

    params = _default_params()
    if stain_key and stain_registry.lookup(stain_key):
        params["stain_key"] = stain_registry.lookup(stain_key)["key"]
    result, maps = engine.analyze(rgb, source_size, **_analyze_kwargs(params))
    entry = {
        "rgb": rgb, "source_size": source_size, "maps": maps, "result": result,
        "params": params, "images": {"original": engine.to_data_uri(rgb, "JPEG")},
        "filename": (filename or file.filename or "image"),
    }
    _render_images(entry)

    analysis_id = uuid.uuid4().hex
    _persist(analysis_id, entry)

    payload = {"analysis_id": analysis_id, **_public(entry)}
    return JSONResponse(payload)


def _render_type(entry: dict, image_type: str) -> np.ndarray:
    """Produce the RGB array for a requested image variant (shared by download + zip)."""
    rgb, maps, p = entry["rgb"], entry["maps"], entry["params"]

    # Stain A/B and the overlay need the concentration/positive maps, which we drop
    # from the cache to stay lean — recompute on demand (honours marker, sensitivity
    # and manual regions). Which cached array each variant actually needs:
    # `_persist` drops the heavy maps, so only ask for a re-analysis when the
    # specific array this variant depends on is genuinely missing.
    _NEEDS = {
        "stainA": "conc", "stainB": "conc",
        "overlay": "positive", "comparison": "positive", "stainOnly": "positive",
        "backgroundRemoved": "tissue_mask",
    }
    need = _NEEDS.get(image_type)
    if need and need not in maps:
        _, maps = engine.analyze(rgb, entry.get("source_size", (rgb.shape[1], rgb.shape[0])),
                                 **_analyze_kwargs(p))
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


def _apply_view(entry: dict, level: Optional[str], regions=None) -> None:
    """Apply the caller's CURRENT on-screen sensitivity and manual regions to
    this entry before rendering, so a download always matches exactly what is
    shown — no dependence on a debounced background persist. (The overlay colour
    is always neon green.)"""
    p = entry["params"]
    if level is not None and level != "":
        try:
            p["level"] = None if level == "auto" else int(float(level))
            entry["maps"].pop("positive", None)   # force recompute at this level
        except ValueError:
            pass
    if regions is not None:
        p["regions"] = _clean_regions(regions)
        entry["maps"].pop("positive", None)


@app.get("/api/download_tif")
def api_download_tif(analysis_id: str, image_type: str, level: Optional[str] = None):
    entry = _CACHE.get(analysis_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Analysis expired")
    _apply_view(entry, level)

    data = _tiff_bytes(_render_type(entry, image_type))
    stem = _safe_name(entry.get("filename", "image"))
    return StreamingResponse(
        io.BytesIO(data),
        media_type="image/tiff",
        headers={"Content-Disposition": f'attachment; filename="{stem}_{image_type}.tif"'},
    )


@app.post("/api/appearance")
async def api_appearance(request: Request, x_imagesl_key: Optional[str] = Header(default=None)):
    _require_key(x_imagesl_key)
    body = await request.json()
    entry = _CACHE.get(body.get("analysis_id"))
    if not entry:
        raise HTTPException(status_code=404, detail="Analysis expired; re-run analysis.")
    _rerender(entry, level=body.get("level"), regions=body.get("regions"))
    _persist(body.get("analysis_id"), entry)
    return JSONResponse(_public(entry))


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
    "staining_pattern",
    "sensitivity_level",
    "sensitivity_auto_level",
    "sensitivity_levels",
    "manual_regions",
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
        # Focal = stained structures form a population clearly apart from the
        # background. Diffuse = they do not, so where the boundary sits is a
        # judgement and the percentage is only comparable within a batch.
        "staining_pattern": "focal" if sep >= 0.60 else "diffuse",
        "sensitivity_level": getattr(r, "level", 0),
        "sensitivity_auto_level": getattr(r, "auto_level", 0),
        "sensitivity_levels": getattr(r, "level_count", 0),
        "manual_regions": getattr(r, "region_count", 0),
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


def _collect_meta(analysis_ids) -> list[dict]:
    """Fast: read only the small JSON sidecars — never touches the heavy pickles."""
    if not isinstance(analysis_ids, list) or not analysis_ids:
        raise HTTPException(status_code=400, detail="No analysis_ids provided.")
    rows = [m for m in (_CACHE.get_meta(a) for a in analysis_ids) if m]
    if not rows:
        raise HTTPException(status_code=404, detail="All analyses expired; re-run analysis.")
    return rows


@app.post("/api/export_csv")
async def api_export_csv(request: Request, x_imagesl_key: Optional[str] = Header(default=None)):
    _require_key(x_imagesl_key)
    body = await request.json()
    rows = _collect_meta(body.get("analysis_ids"))
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


def _zip_stream(analysis_ids, images, include_csv, overrides=None):
    """Generator that builds the ZIP incrementally, one image at a time. Only a
    single analysis entry is ever held in memory, so 40+ slides won't OOM.
    `overrides` maps analysis_id → {level, regions} so each slide's export
    matches its exact on-screen appearance."""
    overrides = overrides or {}
    sink = _ZipSink()
    zf = zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True)  # TIFFs already compressed
    used: set[str] = set()
    rows: list[dict] = []
    for aid in analysis_ids:
        entry = _CACHE.get(aid)
        if not entry:
            continue
        ov = overrides.get(aid) or {}
        _apply_view(entry, ov.get("level"), ov.get("regions"))
        stem = _safe_name(entry.get("filename", "image"))
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
            rows.append(_csv_row(entry))
        entry = None  # release before loading the next
    if include_csv and rows:
        zf.writestr("ImageSL_results.csv", _build_csv_from_rows(rows).encode("utf-8-sig"))
    zf.close()
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

    return StreamingResponse(
        _zip_stream(analysis_ids, images, include_csv, overrides),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="ImageSL_export.zip"'},
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
