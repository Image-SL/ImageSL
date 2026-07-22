"""
ImageSL backend — FastAPI application (fully online, single-page web tool).

All analysis lives here; the browser is the only client. Upload a slide to get
IHC stain quantification (color deconvolution + Macenko + Otsu), then adjust the
analysis parameters to re-run the measurement and re-render the preview.
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

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._evict()
            with open(self._file(key), "wb") as f:
                pickle.dump((time.time(), value), f)

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            path = self._file(key)
            if not path.exists():
                return None
            try:
                with open(path, "rb") as f:
                    ts, value = pickle.load(f)
                if time.time() - ts > self._ttl:
                    path.unlink(missing_ok=True)
                    return None
                # Update timestamp on read
                with open(path, "wb") as f:
                    pickle.dump((time.time(), value), f)
                return value
            except Exception:
                path.unlink(missing_ok=True)
                return None

    def _evict(self) -> None:
        now = time.time()
        files = list(self._dir.glob("*.pkl"))
        
        valid_files = []
        for p in files:
            try:
                with open(p, "rb") as f:
                    ts, _ = pickle.load(f)
                if now - ts > self._ttl:
                    p.unlink(missing_ok=True)
                else:
                    valid_files.append((ts, p))
            except Exception:
                p.unlink(missing_ok=True)
                
        if len(valid_files) >= self._max:
            valid_files.sort(key=lambda x: x[0])
            to_remove = len(valid_files) - self._max + 1
            for _, p in valid_files[:to_remove]:
                p.unlink(missing_ok=True)


_CACHE = _DiskCache()


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
        "target_index": 1,
        "threshold_scale": 1.0,
        "stain_strictness": "strong",
        "stain_method": "hdab",
        "target_gain": 1.0,
        "counterstain_gain": 1.0,
        "background_hex": None,
        "overlay_hex": "#ff2d55",
    }


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
    return _hex_to_rgb(p.get("overlay_hex")) or (255, 45, 85)


def _render_images(entry: dict) -> None:
    rgb, maps, p = entry["rgb"], entry["maps"], entry["params"]
    overlay = engine.render_overlay(rgb, maps, color=_overlay_color(p))
    stainA = engine.render_variant(
        rgb, maps,
        target_gain=1.0,
        counterstain_gain=0.0,
        background_rgb=_hex_to_rgb(p["background_hex"]),
        target_index=0,  # Stain A (usually nuclei)
    )
    stainB = engine.render_variant(
        rgb, maps,
        target_gain=1.0,
        counterstain_gain=0.0,
        background_rgb=_hex_to_rgb(p["background_hex"]),
        target_index=1,  # Stain B (usually target)
    )
    entry["images"] = {
        "original": entry["images"].get("original") if entry.get("images") else engine.to_data_uri(rgb),
        "overlay": engine.to_data_uri(overlay),
        "stainA": engine.to_data_uri(stainA),
        "stainB": engine.to_data_uri(stainB),
    }


def _recompute(entry: dict, **updates) -> dict:
    """Apply analysis-parameter updates, re-run analyze, re-render images."""
    p = entry["params"]
    for k in ("background_threshold", "target_index", "threshold_scale", "stain_strictness", "stain_method"):
        if k in updates and updates[k] is not None:
            p[k] = updates[k]
    p["target_index"] = int(max(0, min(int(p["target_index"]), 1)))
    result, maps = engine.analyze(
        entry["rgb"],
        entry["source_size"],
        target_index=p["target_index"],
        background_threshold=float(p["background_threshold"]),
        threshold_scale=float(p["threshold_scale"]),
        stain_strictness=p.get("stain_strictness", "strong"),
        stain_method=p.get("stain_method", "hdab"),
    )
    entry["result"], entry["maps"] = result, maps
    _render_images(entry)
    return entry


def _rerender(entry: dict, **updates) -> dict:
    """Apply appearance-only updates and re-render the preview image."""
    p = entry["params"]
    for k in ("target_gain", "counterstain_gain", "background_hex", "overlay_hex"):
        if k in updates and updates[k] is not None:
            p[k] = updates[k]
    _render_images(entry)
    return entry


def _state_summary(entry: dict) -> str:
    r = entry["result"]
    return (
        f"positive area {r.positive_percent:.2f}% of tissue "
        f"({r.positive_pixels:,} positive / {r.tissue_pixels:,} tissue px); "
        f"threshold {r.threshold:.3f}; target stain index {r.target_index} "
        f"background_threshold {entry['params']['background_threshold']:.3f}; "
        f"threshold_scale {entry['params']['threshold_scale']:.2f}; "
        f"stain_strictness '{entry['params'].get('stain_strictness', 'strong')}'; "
        f"stain_method '{entry['params'].get('stain_method', 'hdab')}'; "
        f"stain estimation '{r.method}'."
    )


def _public(entry: dict) -> dict:
    return {
        "result": entry["result"].to_dict(),
        "images": entry["images"],
        "params": entry["params"],
        "filename": entry.get("filename", ""),
    }


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

def _serve_html(name: str) -> HTMLResponse:
    path = WEB_DIR / name
    if not path.is_file():
        return HTMLResponse(f"<h1>ImageSL</h1><p>Missing {name}.</p>", status_code=500)
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    return _serve_html("index.html")


@app.get("/styles.css")
def styles_css():
    path = WEB_DIR / "styles.css"
    return FileResponse(str(path), media_type="text/css") if path.is_file() else HTMLResponse("", status_code=404)


@app.get("/app.js")
def app_js():
    path = WEB_DIR / "app.js"
    return FileResponse(str(path), media_type="application/javascript") if path.is_file() else HTMLResponse("", status_code=404)


@app.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": APP_VERSION})


# --------------------------------------------------------------------------- #
# Analysis API
# --------------------------------------------------------------------------- #

@app.post("/api/analyze")
async def api_analyze(
    file: UploadFile = File(...),
    filename: Optional[str] = Form(default=None),
    x_imagesl_key: Optional[str] = Header(default=None),
):
    _require_key(x_imagesl_key)
    data = await _read_upload(file)

    try:
        rgb, source_size = engine.load_rgb(data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not decode image: {exc}")

    params = _default_params()
    result, maps = engine.analyze(
        rgb, source_size,
        target_index=params["target_index"],
        background_threshold=params["background_threshold"],
        threshold_scale=params["threshold_scale"],
        stain_strictness=params["stain_strictness"],
        stain_method=params["stain_method"],
    )
    entry = {
        "rgb": rgb, "source_size": source_size, "maps": maps, "result": result,
        "params": params, "images": {"original": engine.to_data_uri(rgb)},
        "filename": (filename or file.filename or "image"),
    }
    _render_images(entry)

    analysis_id = uuid.uuid4().hex
    _CACHE.put(analysis_id, entry)

    payload = {"analysis_id": analysis_id, **_public(entry)}
    return JSONResponse(payload)


def _render_type(entry: dict, image_type: str) -> np.ndarray:
    """Produce the RGB array for a requested image variant (shared by download + zip)."""
    rgb, maps, p = entry["rgb"], entry["maps"], entry["params"]
    bg = _hex_to_rgb(p.get("background_hex"))

    if image_type == "original":
        return rgb
    if image_type == "overlay":
        return engine.render_overlay(rgb, maps, color=_overlay_color(p))
    if image_type == "stainA":
        return engine.render_variant(
            rgb, maps, target_gain=1.0, counterstain_gain=0.0,
            background_rgb=bg, target_index=0,
        )
    if image_type == "stainB":
        return engine.render_variant(
            rgb, maps, target_gain=1.0, counterstain_gain=0.0,
            background_rgb=bg, target_index=1,
        )
    if image_type == "comparison":
        overlay = engine.render_overlay(rgb, maps, color=_overlay_color(p))
        return engine.compose_comparison(rgb, overlay, "Original", "Detection Overlay")
    raise HTTPException(status_code=400, detail="Invalid image type")


def _safe_name(name: str) -> str:
    stem = re.sub(r"\.[^.]*$", "", name or "image")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "image"
    return stem


@app.get("/api/download_tif")
def api_download_tif(analysis_id: str, image_type: str):
    entry = _CACHE.get(analysis_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Analysis expired")

    out_rgb = _render_type(entry, image_type)
    buf = io.BytesIO()
    Image.fromarray(out_rgb).save(buf, format="TIFF")
    buf.seek(0)

    stem = _safe_name(entry.get("filename", "image"))
    return StreamingResponse(
        buf,
        media_type="image/tiff",
        headers={"Content-Disposition": f'attachment; filename="{stem}_{image_type}.tif"'},
    )


@app.post("/api/recalculate")
async def api_recalculate(request: Request, x_imagesl_key: Optional[str] = Header(default=None)):
    _require_key(x_imagesl_key)
    body = await request.json()
    entry = _CACHE.get(body.get("analysis_id"))
    if not entry:
        raise HTTPException(status_code=404, detail="Analysis expired; re-run analysis.")
    _recompute(
        entry,
        background_threshold=body.get("background_threshold"),
        target_index=body.get("target_index"),
        threshold_scale=body.get("threshold_scale"),
        stain_strictness=body.get("stain_strictness"),
        stain_method=body.get("stain_method"),
    )
    return JSONResponse(_public(entry))


@app.post("/api/appearance")
async def api_appearance(request: Request, x_imagesl_key: Optional[str] = Header(default=None)):
    _require_key(x_imagesl_key)
    body = await request.json()
    entry = _CACHE.get(body.get("analysis_id"))
    if not entry:
        raise HTTPException(status_code=404, detail="Analysis expired; re-run analysis.")
    _rerender(
        entry,
        target_gain=body.get("target_gain"),
        counterstain_gain=body.get("counterstain_gain"),
        background_hex=body.get("background_hex"),
        overlay_hex=body.get("overlay_hex"),
    )
    return JSONResponse(_public(entry))


# --------------------------------------------------------------------------- #
# Data export (CSV spreadsheet + ZIP of images) — single or batch
# --------------------------------------------------------------------------- #

_CSV_COLUMNS = [
    "filename",
    "positive_area_percent_of_tissue",
    "positive_pixels",
    "tissue_pixels",
    "total_image_pixels",
    "positive_percent_of_image",
    "otsu_threshold",
    "mean_positive_intensity",
    "stain_method",
    "target_stain_index",
    "analysis_width_px",
    "analysis_height_px",
    "source_width_px",
    "source_height_px",
]


def _csv_row(entry: dict) -> dict:
    r = entry["result"]
    total_px = int(r.width) * int(r.height)
    pct_of_image = round((r.positive_pixels / total_px * 100.0), 3) if total_px else 0.0
    return {
        "filename": entry.get("filename", "image"),
        "positive_area_percent_of_tissue": r.positive_percent,
        "positive_pixels": r.positive_pixels,
        "tissue_pixels": r.tissue_pixels,
        "total_image_pixels": total_px,
        "positive_percent_of_image": pct_of_image,
        "otsu_threshold": r.threshold,
        "mean_positive_intensity": r.mean_positive_intensity,
        "stain_method": r.method,
        "target_stain_index": r.target_index,
        "analysis_width_px": r.width,
        "analysis_height_px": r.height,
        "source_width_px": r.source_width,
        "source_height_px": r.source_height,
    }


def _build_csv(entries: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=_CSV_COLUMNS)
    writer.writeheader()
    for entry in entries:
        writer.writerow(_csv_row(entry))
    return out.getvalue()


def _collect_entries(analysis_ids) -> list[dict]:
    if not isinstance(analysis_ids, list) or not analysis_ids:
        raise HTTPException(status_code=400, detail="No analysis_ids provided.")
    entries = []
    for aid in analysis_ids:
        entry = _CACHE.get(aid)
        if entry:
            entries.append(entry)
    if not entries:
        raise HTTPException(status_code=404, detail="All analyses expired; re-run analysis.")
    return entries


@app.post("/api/export_csv")
async def api_export_csv(request: Request, x_imagesl_key: Optional[str] = Header(default=None)):
    _require_key(x_imagesl_key)
    body = await request.json()
    entries = _collect_entries(body.get("analysis_ids"))
    csv_text = _build_csv(entries)
    fname = "ImageSL_results.csv" if len(entries) > 1 else f"{_safe_name(entries[0].get('filename'))}_data.csv"
    return StreamingResponse(
        io.BytesIO(csv_text.encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/api/export_zip")
async def api_export_zip(request: Request, x_imagesl_key: Optional[str] = Header(default=None)):
    _require_key(x_imagesl_key)
    body = await request.json()
    entries = _collect_entries(body.get("analysis_ids"))

    valid = {"original", "overlay", "stainA", "stainB", "comparison"}
    images = [t for t in (body.get("images") or ["comparison"]) if t in valid] or ["comparison"]
    include_csv = body.get("include_csv", True)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        used: dict[str, int] = {}
        for entry in entries:
            stem = _safe_name(entry.get("filename", "image"))
            # de-duplicate identical stems across the batch
            n = used.get(stem, 0)
            used[stem] = n + 1
            folder = stem if n == 0 else f"{stem}_{n+1}"
            for image_type in images:
                arr = _render_type(entry, image_type)
                img_buf = io.BytesIO()
                Image.fromarray(arr).save(img_buf, format="TIFF")
                zf.writestr(f"{folder}/{stem}_{image_type}.tif", img_buf.getvalue())
        if include_csv:
            zf.writestr("ImageSL_results.csv", _build_csv(entries))
    buf.seek(0)
    return StreamingResponse(
        buf,
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
