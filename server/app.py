"""
ImageSL backend — FastAPI application (fully online, single-page web tool).

All analysis and the Claude API key live here. The browser is the only client.
The built-in assistant can call tools that RE-RUN the analysis, so the user can
improve results conversationally and see the numbers/images update.
"""

from __future__ import annotations

import os
import time
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import io
from PIL import Image
import numpy as np

from ihc import engine
from ai import claude_client

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
# In-memory analysis cache (per instance)
# --------------------------------------------------------------------------- #

class _AnalysisCache:
    def __init__(self, max_items: int = 15, ttl_seconds: int = 3600):
        self._data: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self._max = max_items
        self._ttl = ttl_seconds

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._evict()
            self._data[key] = (time.time(), value)

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            ts, value = item
            if time.time() - ts > self._ttl:
                self._data.pop(key, None)
                return None
            self._data[key] = (time.time(), value)
            return value

    def _evict(self) -> None:
        now = time.time()
        for k in [k for k, (ts, _) in self._data.items() if now - ts > self._ttl]:
            self._data.pop(k, None)
        while len(self._data) >= self._max:
            oldest = min(self._data.items(), key=lambda kv: kv[1][0])[0]
            self._data.pop(oldest, None)


_CACHE = _AnalysisCache()


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


def _render_images(entry: dict) -> None:
    rgb, maps, p = entry["rgb"], entry["maps"], entry["params"]
    overlay = engine.render_overlay(rgb, maps)
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
    for k in ("target_gain", "counterstain_gain", "background_hex"):
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
    return JSONResponse({"status": "ok", "version": APP_VERSION, "ai_configured": claude_client.is_configured()})


# --------------------------------------------------------------------------- #
# Analysis API
# --------------------------------------------------------------------------- #

@app.post("/api/analyze")
async def api_analyze(
    file: UploadFile = File(...),
    use_ai: bool = Form(True),
    x_imagesl_key: Optional[str] = Header(default=None),
):
    _require_key(x_imagesl_key)
    data = await _read_upload(file)

    try:
        rgb, source_size = engine.load_rgb(data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not decode image: {exc}")

    vision: dict = {"available": False}
    if use_ai and claude_client.is_configured():
        vision = claude_client.vision_stain_report(engine.thumbnail_jpeg_b64(rgb))

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
    }
    _render_images(entry)

    analysis_id = uuid.uuid4().hex
    _CACHE.put(analysis_id, entry)

    payload = {"analysis_id": analysis_id, "vision": vision, **_public(entry)}
    return JSONResponse(payload)


@app.get("/api/download_tif")
def api_download_tif(analysis_id: str, image_type: str):
    entry = _CACHE.get(analysis_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Analysis expired")
    
    rgb, maps, p = entry["rgb"], entry["maps"], entry["params"]
    
    if image_type == "original":
        out_rgb = rgb
    elif image_type == "overlay":
        out_rgb = engine.render_overlay(rgb, maps)
    elif image_type == "stainA":
        out_rgb = engine.render_variant(
            rgb, maps, target_gain=1.0, counterstain_gain=0.0,
            background_rgb=_hex_to_rgb(p.get("background_hex")), target_index=0
        )
    elif image_type == "stainB":
        out_rgb = engine.render_variant(
            rgb, maps, target_gain=1.0, counterstain_gain=0.0,
            background_rgb=_hex_to_rgb(p.get("background_hex")), target_index=1
        )
    elif image_type == "comparison":
        overlay = engine.render_overlay(rgb, maps)
        out_rgb = np.hstack([rgb, overlay])
    else:
        raise HTTPException(status_code=400, detail="Invalid image type")
        
    buf = io.BytesIO()
    Image.fromarray(out_rgb).save(buf, format="TIFF")
    buf.seek(0)
    
    return StreamingResponse(
        buf, 
        media_type="image/tiff", 
        headers={"Content-Disposition": f'attachment; filename="ImageSL_{image_type}.tif"'}
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
    )
    return JSONResponse(_public(entry))


# --------------------------------------------------------------------------- #
# Chat API — agentic, can recalculate
# --------------------------------------------------------------------------- #

@app.post("/api/chat")
async def api_chat(request: Request, x_imagesl_key: Optional[str] = Header(default=None)):
    _require_key(x_imagesl_key)
    body = await request.json()
    messages = body.get("messages", [])
    entry = _CACHE.get(body.get("analysis_id"))

    if not entry:
        # Chat still works without a loaded slide (general questions).
        out = claude_client.chat_agentic(messages, context=None, executor=lambda n, i: "No slide is loaded.")
        return JSONResponse({"reply": out["reply"], "updated": False})

    def executor(name: str, args: dict) -> str:
        if name == "recalculate_analysis":
            _recompute(
                entry,
                background_threshold=args.get("background_threshold"),
                target_index=args.get("target_index"),
                threshold_scale=args.get("threshold_scale"),
                stain_strictness=args.get("stain_strictness"),
                stain_method=args.get("stain_method"),
            )
            return "Recalculated. New state: " + _state_summary(entry)
        if name == "set_appearance":
            _rerender(
                entry,
                target_gain=args.get("target_gain"),
                counterstain_gain=args.get("counterstain_gain"),
                background_hex=args.get("background_hex"),
            )
            return "Preview appearance updated."
        return f"Unknown tool: {name}"

    out = claude_client.chat_agentic(messages, context=_state_summary(entry), executor=executor)
    return JSONResponse({"reply": out["reply"], "updated": out["used_tools"], **_public(entry)})


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
