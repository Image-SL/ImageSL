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
        "target_index": 1,
        "threshold_scale": 1.0,
        "stain_strictness": "strong",
        "stain_method": "auto",
        # colours — all auto-derived at analysis time; user may override any.
        "overlay_hex": None,     # None → per-slide auto-contrast colour
        "stainA_hex": None,      # None → stain's natural colour
        "stainB_hex": None,
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
    """Overlay colour: user override, else the per-slide auto-contrast colour."""
    return _hex_to_rgb(p.get("overlay_hex")) or _hex_to_rgb(p.get("_overlay_auto")) or (0, 229, 255)


def _stain_indices(maps: dict) -> tuple[int, int]:
    """(Stain-A index, Stain-B index) — A = counterstain, B = target chromogen."""
    return int(maps.get("counter_index", 0)), int(maps.get("target_index", 1))


def _render_images(entry: dict) -> None:
    rgb, p = entry["rgb"], entry["params"]
    maps = entry["maps"]
    # After caching we drop the heavy concentration maps; recompute on demand.
    if "conc" not in maps:
        _, maps = engine.analyze(
            rgb, entry.get("source_size", (rgb.shape[1], rgb.shape[0])),
            background_threshold=p.get("background_threshold", engine.BACKGROUND_OD_THRESHOLD),
            stain_strictness=p.get("stain_strictness", "strong"),
            stain_method=p.get("stain_method", "auto"),
        )
        entry["maps"] = maps
    a_idx, b_idx = _stain_indices(maps)
    stainA = engine.render_stain(maps, a_idx, _hex_to_rgb(p.get("stainA_hex")))
    stainB = engine.render_stain(maps, b_idx, _hex_to_rgb(p.get("stainB_hex")))
    # The overlay is composited in the browser from `original` + `mask`, so the
    # detection color changes instantly (no server round-trip) and analysis
    # renders one fewer image. The mask is a transparent PNG of positive pixels.
    entry["images"] = {
        "original": entry["images"].get("original") if entry.get("images") else engine.to_data_uri(rgb, "JPEG"),
        "stainA": engine.to_data_uri(stainA, "JPEG"),
        "stainB": engine.to_data_uri(stainB, "JPEG"),
        "mask": engine.mask_data_uri(maps["positive"]),
    }


def _rerender(entry: dict, **updates) -> dict:
    """Apply appearance-only updates. Overlay colour is composited in the browser,
    so an overlay-colour-only change needs no server re-render — it just updates
    the stored param so downloads/exports use the chosen colour. Recolouring
    Stain A / Stain B re-renders those two panels."""
    p = entry["params"]
    needs_render = False
    for k in ("stainA_hex", "stainB_hex"):
        if k in updates and updates[k] is not None:
            p[k] = updates[k] or None
            needs_render = True
    if updates.get("overlay_hex") is not None:
        p["overlay_hex"] = updates["overlay_hex"] or None
    if needs_render:
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


def _persist(analysis_id: str, entry: dict) -> None:
    """Save an entry back to cache (keeping it lean) with its CSV row sidecar, so
    later recalcs/appearance edits are reflected in downloads and exports.

    Drops the two biggest arrays from the cached copy — the optical-density and
    concentration maps — so hundreds of slides can be held at once. They are only
    needed to render Stain A/B, which is recomputed on demand at export time."""
    entry["maps"].pop("od", None)
    entry["maps"].pop("conc", None)
    _CACHE.put(analysis_id, entry, meta=_csv_row(entry))


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
        background_threshold=params["background_threshold"],
        stain_strictness=params["stain_strictness"],
        stain_method=params["stain_method"],
    )
    # Remember the per-slide auto-contrast overlay colour; used until the user
    # explicitly overrides it.
    params["_overlay_auto"] = result.suggested_overlay_hex
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

    # Stain A/B and the overlay need the concentration maps, which we drop from
    # the cache to stay lean — recompute them here on demand (export path).
    if image_type in ("stainA", "stainB", "overlay", "comparison") and "conc" not in maps:
        _, maps = engine.analyze(
            rgb, entry.get("source_size", (rgb.shape[1], rgb.shape[0])),
            background_threshold=p.get("background_threshold", engine.BACKGROUND_OD_THRESHOLD),
            stain_strictness=p.get("stain_strictness", "strong"),
            stain_method=p.get("stain_method", "auto"),
        )
    a_idx, b_idx = _stain_indices(maps)

    if image_type == "original":
        return rgb
    if image_type == "overlay":
        return engine.render_overlay(rgb, maps, color=_overlay_color(p))
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


@app.get("/api/download_tif")
def api_download_tif(analysis_id: str, image_type: str):
    entry = _CACHE.get(analysis_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Analysis expired")

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
    _rerender(
        entry,
        overlay_hex=body.get("overlay_hex"),
        stainA_hex=body.get("stainA_hex"),
        stainB_hex=body.get("stainB_hex"),
    )
    _persist(body.get("analysis_id"), entry)
    return JSONResponse(_public(entry))


# --------------------------------------------------------------------------- #
# Data export (CSV spreadsheet + ZIP of images) — single or batch
# --------------------------------------------------------------------------- #

_CSV_COLUMNS = [
    "filename",
    "detected_stain",
    "positive_area_percent_of_tissue",
    "positive_pixels",
    "tissue_pixels",
    "total_image_pixels",
    "positive_percent_of_image",
    "intensity_threshold",
    "mean_positive_intensity",
    "detection_confidence",
    "stain_family",
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
        "detected_stain": getattr(r, "stain_label", ""),
        "positive_area_percent_of_tissue": r.positive_percent,
        "positive_pixels": r.positive_pixels,
        "tissue_pixels": r.tissue_pixels,
        "total_image_pixels": total_px,
        "positive_percent_of_image": pct_of_image,
        "intensity_threshold": r.threshold,
        "mean_positive_intensity": r.mean_positive_intensity,
        "detection_confidence": getattr(r, "confidence", 1.0),
        "stain_family": r.method,
        "target_stain_index": r.target_index,
        "analysis_width_px": r.width,
        "analysis_height_px": r.height,
        "source_width_px": r.source_width,
        "source_height_px": r.source_height,
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


def _zip_stream(analysis_ids, images, include_csv):
    """Generator that builds the ZIP incrementally, one image at a time. Only a
    single analysis entry is ever held in memory, so 40+ slides won't OOM."""
    sink = _ZipSink()
    zf = zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True)  # TIFFs already compressed
    used: set[str] = set()
    rows: list[dict] = []
    for aid in analysis_ids:
        entry = _CACHE.get(aid)
        if not entry:
            continue
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

    valid = {"original", "overlay", "stainA", "stainB", "comparison"}
    images = [t for t in (body.get("images") or ["comparison"]) if t in valid] or ["comparison"]
    include_csv = body.get("include_csv", True)

    return StreamingResponse(
        _zip_stream(analysis_ids, images, include_csv),
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
