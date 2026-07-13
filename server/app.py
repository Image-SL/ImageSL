"""
ImageSL backend — FastAPI application.

Serves the premium marketing site and the in-app analyzer, exposes the IHC
analysis / variant / chat APIs, and offers the desktop client for download.

All proprietary analysis and the Claude API key live here on Railway. The
desktop client is a thin shell that only calls these endpoints.
"""

from __future__ import annotations

import json
import os
import time
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from ihc import engine
from ai import claude_client

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
ASSETS_DIR = WEB_DIR / "assets"
# Where the built Windows client is placed for download.
CLIENT_EXE = Path(os.environ.get("IMAGESL_CLIENT_EXE", str(BASE_DIR / "dist" / "ImageSL.exe")))

MAX_UPLOAD_BYTES = int(os.environ.get("IMAGESL_MAX_UPLOAD_MB", "256")) * 1024 * 1024
APP_VERSION = os.environ.get("IMAGESL_VERSION", "1.0.0")

# Optional access control. If IMAGESL_ACCESS_TOKENS is set (comma-separated),
# every /api/* call must carry a matching X-ImageSL-Key header. Unset => open
# (useful for local dev; set it in production).
_TOKENS = {t.strip() for t in os.environ.get("IMAGESL_ACCESS_TOKENS", "").split(",") if t.strip()}

app = FastAPI(title="ImageSL", version=APP_VERSION, docs_url=None, redoc_url=None)

if ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")


# --------------------------------------------------------------------------- #
# In-memory analysis cache (per instance) so slider tweaks don't re-upload.
# --------------------------------------------------------------------------- #

class _AnalysisCache:
    def __init__(self, max_items: int = 32, ttl_seconds: int = 900):
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
            self._data[key] = (time.time(), value)  # refresh LRU
            return value

    def _evict(self) -> None:
        now = time.time()
        stale = [k for k, (ts, _) in self._data.items() if now - ts > self._ttl]
        for k in stale:
            self._data.pop(k, None)
        while len(self._data) >= self._max:
            oldest = min(self._data.items(), key=lambda kv: kv[1][0])[0]
            self._data.pop(oldest, None)


_CACHE = _AnalysisCache()


# --------------------------------------------------------------------------- #
# Auth helper
# --------------------------------------------------------------------------- #

def _require_key(x_imagesl_key: Optional[str]) -> None:
    if not _TOKENS:
        return
    if not x_imagesl_key or x_imagesl_key not in _TOKENS:
        raise HTTPException(status_code=401, detail="Invalid or missing ImageSL access key.")


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

def _serve_html(name: str) -> HTMLResponse:
    path = WEB_DIR / name
    if not path.is_file():
        return HTMLResponse(f"<h1>ImageSL</h1><p>Missing {name}.</p>", status_code=500)
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/", response_class=HTMLResponse)
def landing() -> HTMLResponse:
    return _serve_html("index.html")


@app.get("/app", response_class=HTMLResponse)
def analyzer_app() -> HTMLResponse:
    return _serve_html("app.html")


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
    return JSONResponse(
        {
            "status": "ok",
            "version": APP_VERSION,
            "ai_configured": claude_client.is_configured(),
            "client_available": CLIENT_EXE.is_file(),
        }
    )


@app.get("/download/windows")
def download_windows():
    if not CLIENT_EXE.is_file():
        return HTMLResponse(
            "<h1>Client not built yet</h1>"
            "<p>The Windows client has not been published to this deployment. "
            "See <code>docs/DEPLOY.md</code>.</p>",
            status_code=404,
        )
    return FileResponse(
        str(CLIENT_EXE),
        media_type="application/vnd.microsoft.portable-executable",
        filename=CLIENT_EXE.name,
    )


# --------------------------------------------------------------------------- #
# Analysis API
# --------------------------------------------------------------------------- #

@app.post("/api/analyze")
async def api_analyze(
    file: UploadFile = File(...),
    max_edge: int = Form(engine.DEFAULT_MAX_EDGE),
    use_ai: bool = Form(True),
    background_threshold: float = Form(engine.BACKGROUND_OD_THRESHOLD),
    x_imagesl_key: Optional[str] = Header(default=None),
):
    _require_key(x_imagesl_key)
    data = await _read_upload(file)

    try:
        rgb, source_size = engine.load_rgb(data, max_edge=int(max_edge))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not decode image: {exc}")

    vision: dict = {"available": False}
    if use_ai and claude_client.is_configured():
        vision = claude_client.vision_stain_report(engine.thumbnail_jpeg_b64(rgb))

    result, maps = engine.analyze(rgb, source_size, background_threshold=float(background_threshold))

    analysis_id = uuid.uuid4().hex
    _CACHE.put(analysis_id, {"rgb": rgb, "maps": maps, "result": result})

    overlay = engine.render_overlay(rgb, maps)

    payload = {
        "analysis_id": analysis_id,
        "result": result.to_dict(),
        "vision": vision,
        "images": {
            "original": engine.to_data_uri(rgb),
            "overlay": engine.to_data_uri(overlay),
        },
    }
    return JSONResponse(payload)


@app.post("/api/variant")
async def api_variant(request: Request, x_imagesl_key: Optional[str] = Header(default=None)):
    _require_key(x_imagesl_key)
    body = await request.json()
    analysis_id = body.get("analysis_id")
    cached = _CACHE.get(analysis_id) if analysis_id else None
    if not cached:
        raise HTTPException(status_code=404, detail="Analysis expired or not found; re-run analysis.")

    target_gain = float(body.get("target_gain", 1.0))
    counterstain_gain = float(body.get("counterstain_gain", 1.0))
    bg = body.get("background_rgb")
    bg_rgb = tuple(int(c) for c in bg) if bg else None
    target_index = int(cached["result"].target_index)

    variant = engine.render_variant(
        cached["rgb"],
        cached["maps"],
        target_gain=target_gain,
        counterstain_gain=counterstain_gain,
        background_rgb=bg_rgb,
        target_index=target_index,
    )
    return JSONResponse({"image": engine.to_data_uri(variant)})


@app.post("/api/set-target")
async def api_set_target(request: Request, x_imagesl_key: Optional[str] = Header(default=None)):
    """Re-quantify treating the other separated stain as the target."""
    _require_key(x_imagesl_key)
    body = await request.json()
    analysis_id = body.get("analysis_id")
    cached = _CACHE.get(analysis_id) if analysis_id else None
    if not cached:
        raise HTTPException(status_code=404, detail="Analysis expired or not found; re-run analysis.")

    target_index = int(body.get("target_index", 1))
    result, maps = engine.analyze(
        cached["rgb"],
        (cached["result"].source_width, cached["result"].source_height),
        target_index=target_index,
    )
    _CACHE.put(analysis_id, {"rgb": cached["rgb"], "maps": maps, "result": result})
    overlay = engine.render_overlay(cached["rgb"], maps)
    return JSONResponse(
        {
            "result": result.to_dict(),
            "images": {"overlay": engine.to_data_uri(overlay)},
        }
    )


# --------------------------------------------------------------------------- #
# Chat API (SSE stream)
# --------------------------------------------------------------------------- #

@app.post("/api/chat")
async def api_chat(request: Request, x_imagesl_key: Optional[str] = Header(default=None)):
    _require_key(x_imagesl_key)
    body = await request.json()
    messages = body.get("messages", [])
    analysis_id = body.get("analysis_id")

    context = None
    cached = _CACHE.get(analysis_id) if analysis_id else None
    if cached:
        r = cached["result"]
        context = (
            "The user is viewing a slide with these current results: "
            f"positive area {r.positive_percent:.2f}% of tissue "
            f"({r.positive_pixels:,} of {r.tissue_pixels:,} tissue pixels), "
            f"mean target optical density {r.mean_positive_intensity:.3f}, "
            f"Otsu threshold {r.threshold:.3f}, stain estimation method '{r.method}'."
        )

    def event_stream():
        try:
            for chunk in claude_client.chat_stream(messages, context=context):
                yield f"data: {json.dumps({'delta': chunk})}\n\n"
        except Exception as exc:  # pragma: no cover
            yield f"data: {json.dumps({'delta': f'[error: {exc}]'})}\n\n"
        yield "data: {\"done\": true}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024*1024)} MB limit.",
        )
    return data


if __name__ == "__main__":  # local dev convenience
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), reload=True)
