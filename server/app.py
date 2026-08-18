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
from fastapi import Response
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
import io
from PIL import Image
import numpy as np

from ihc import detect as detect_mod
from ihc import engine
from ihc import regions as region_tools
from ihc import stains as stain_registry

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
ASSETS_DIR = WEB_DIR / "assets"

MAX_UPLOAD_BYTES = int(os.environ.get("IMAGESL_MAX_UPLOAD_MB", "256")) * 1024 * 1024

def _repo_version() -> str:
    for p in (BASE_DIR.parent / "version.txt", BASE_DIR / "version.txt"):
        try:
            if p.is_file():
                v = p.read_text(encoding="utf-8").strip()
                if v:
                    return v
        except Exception:
            pass
    return "0.0.0-dev"

APP_VERSION = os.environ.get("IMAGESL_VERSION") or _repo_version()

IMAGESL_DESKTOP = os.environ.get("IMAGESL_DESKTOP") == "1"

WEB_ANALYZER = os.environ.get("IMAGESL_WEB_ANALYZER") == "1"
ANALYZER_ENABLED = IMAGESL_DESKTOP or WEB_ANALYZER

SITE_URL = (os.environ.get("IMAGESL_SITE_URL") or "https://imagesl.com").rstrip("/")

LEGACY_HOSTS = {
    h.strip().lower()
    for h in (os.environ.get("IMAGESL_LEGACY_HOSTS") or "").split(",")
    if h.strip()
}

_PUBLIC_API = {"/api/health", "/api/downloads"}

_PUBLIC_API_PREFIXES = ("/api/update/",)

_TOKENS = {t.strip() for t in os.environ.get("IMAGESL_ACCESS_TOKENS", "").split(",") if t.strip()}

app = FastAPI(title="ImageSL", version=APP_VERSION, docs_url=None, redoc_url=None)

if ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")

_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])

_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Permitted-Cross-Domain-Policies": "none",
    "Permissions-Policy": ("accelerometer=(), camera=(), geolocation=(), "
                           "gyroscope=(), magnetometer=(), microphone=(), "
                           "payment=(), usb=()"),
}

@app.middleware("http")
async def _legacy_domain_redirect(request: Request, call_next):
    if LEGACY_HOSTS:
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if host in LEGACY_HOSTS:
            target = f"{SITE_URL}{request.url.path}"
            if request.url.query:
                target = f"{target}?{request.url.query}"
            return RedirectResponse(target, status_code=301)
    return await call_next(request)

@app.middleware("http")
async def _analyzer_gate(request: Request, call_next):
    if not ANALYZER_ENABLED:
        path = request.url.path
        if (path.startswith("/api/") and path not in _PUBLIC_API
                and not path.startswith(_PUBLIC_API_PREFIXES)):
            return JSONResponse(
                {"detail": "ImageSL runs as a downloadable application. "
                           "This site does not analyse slides."},
                status_code=404)
    return await call_next(request)

@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    for key, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)

    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    if proto == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response

_cache_env = os.environ.get("IMAGESL_CACHE_DIR")
if _cache_env:
    CACHE_DIR = Path(_cache_env)
elif Path("/data").exists():
    CACHE_DIR = Path("/data")
else:
    CACHE_DIR = Path(tempfile.gettempdir()) / "imagesl_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

class _DiskCache:

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
            self._touch(path)
            return value

    def touch(self, key: str) -> bool:
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

        entries.sort(reverse=True)
        i = 0
        while i < len(entries) and (len(entries) - i >= self._max or total > self._max_bytes):
            _, key, size = entries[i]
            self._drop(key)
            total -= size
            i += 1

_CACHE = _DiskCache(
    max_items=int(os.environ.get("IMAGESL_CACHE_MAX", "400")),
    ttl_seconds=int(os.environ.get("IMAGESL_CACHE_TTL", "28800")),
    max_bytes=int(os.environ.get("IMAGESL_CACHE_MB", "3072")) * 1024 * 1024,
)

def _require_key(x_imagesl_key: Optional[str]) -> None:
    if not _TOKENS:
        return
    if not x_imagesl_key or x_imagesl_key not in _TOKENS:
        raise HTTPException(status_code=401, detail="Invalid or missing ImageSL access key.")

def _default_params() -> dict:
    return {
        "background_threshold": engine.BACKGROUND_OD_THRESHOLD,
        "stain_method": "auto",
        "stain_key": None,
        "level": None,
        "regions": [],
        "stainA_hex": None,
        "stainB_hex": None,
    }

def _analyze_kwargs(p: dict) -> dict:
    lv = p.get("level")
    return {
        "background_threshold": p.get("background_threshold", engine.BACKGROUND_OD_THRESHOLD),
        "stain_method": p.get("stain_method", "auto"),
        "stain_choice": stain_registry.lookup(p.get("stain_key")),
        "level": (int(lv) if lv is not None else None),
        "regions": p.get("regions") or None,
    }

def _clean_regions(raw) -> list:
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
    return engine.OVERLAY_GREEN

def _stain_indices(maps: dict) -> tuple[int, int]:
    return int(maps.get("counter_index", 0)), int(maps.get("target_index", 1))

def _render_images(entry: dict) -> None:
    rgb = entry["rgb"]
    if "level_map" not in entry["maps"] or "tissue_base" not in entry["maps"]:
        _reanalyze(entry)
    maps = entry["maps"]
    prev = entry.get("images") or {}
    entry["images"] = {
        "original": prev.get("original") or engine.original_data_uri(rgb),
        "level": engine.level_data_uri(maps["level_map"], maps.get("tissue_base")),
    }

_MEASURE_SLOTS = max(1, int(os.environ.get("IMAGESL_MAX_CONCURRENCY", "2")))
_MEASURE_GATE = threading.BoundedSemaphore(_MEASURE_SLOTS)

def _measure(rgb: np.ndarray, source_size, **kwargs):
    with _MEASURE_GATE:
        return engine.analyze(rgb, source_size, **kwargs)

def _reanalyze(entry: dict) -> None:
    result, maps = _measure(
        entry["rgb"],
        entry.get("source_size", (entry["rgb"].shape[1], entry["rgb"].shape[0])),
        **_analyze_kwargs(entry["params"]),
    )
    entry["result"], entry["maps"] = result, maps

def _rerender(entry: dict, **updates) -> dict:
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
        level = auto_level
    return {"level": (None if level is None else int(level)),
            "regions": regions,
            "stain_key": entry_params.get("stain_key")}

def _persist(analysis_id: str, entry: dict) -> None:
    for k in ("od", "conc", "excess", "brownness"):
        entry["maps"].pop(k, None)
    _CACHE.put(analysis_id, entry,
               meta={"row": _csv_row(entry),
                     "view": _view_key(entry["params"], _auto_level(entry)),
                     "auto_level": _auto_level(entry)})

def _asset_version() -> str:
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
    html = (path.read_text(encoding="utf-8")
            .replace("__ASSET_V__", _asset_version())
            .replace("__SITE_URL__", SITE_URL)
            .replace("__APP_VERSION__", APP_VERSION))
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, must-revalidate"})

@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    if IMAGESL_DESKTOP:
        return _serve_html("index.html")
    if (WEB_DIR / "landing.html").is_file():
        return _serve_html("landing.html")
    return _serve_html("index.html")

@app.get("/app")
def analyzer():
    if not ANALYZER_ENABLED:
        return RedirectResponse("/", status_code=307)
    return _serve_html("index.html")

DOWNLOAD_DIR = Path(os.environ.get("IMAGESL_DOWNLOAD_DIR",
                                   str(BASE_DIR.parent / "downloads")))

_DOWNLOADS = {
    "windows": ("ImageSL-Setup-Windows.exe", "application/octet-stream",
                "IMAGESL_DOWNLOAD_URL_WINDOWS"),
    "macos":   ("ImageSL-macOS.dmg",         "application/x-apple-diskimage",
                "IMAGESL_DOWNLOAD_URL_MACOS"),
}

def _external_url(env_key: str) -> str:
    return (os.environ.get(env_key) or "").strip()

_PROBE_TTL_OK = 300.0
_PROBE_TTL_BAD = 60.0
_probe_cache: dict[str, tuple[float, bool, int]] = {}
_probe_lock = threading.Lock()

def _probe_external(url: str) -> tuple[bool, int]:
    now = time.time()
    with _probe_lock:
        cached = _probe_cache.get(url)
        if cached is not None:
            checked_at, ok, size = cached
            if now - checked_at < (_PROBE_TTL_OK if ok else _PROBE_TTL_BAD):
                return ok, size

    import urllib.request
    import urllib.error
    ok, size = False, 0
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=4) as r:
            ok = 200 <= getattr(r, "status", 200) < 300
            size = int(r.headers.get("Content-Length") or 0)
    except urllib.error.HTTPError:
        ok, size = False, 0
    except Exception:
        with _probe_lock:
            cached = _probe_cache.get(url)
        if cached is not None:
            return cached[1], cached[2]
        return False, 0

    with _probe_lock:
        _probe_cache[url] = (now, ok, size)
    return ok, size

_VERSION_URL_KEY = "IMAGESL_DOWNLOAD_VERSION_URL"
_VERSION_TTL_OK = 300.0
_VERSION_TTL_BAD = 60.0
_version_cache: tuple[float, str] | None = None
_version_lock = threading.Lock()

def _published_version() -> str:
    url = (os.environ.get(_VERSION_URL_KEY) or "").strip()
    if not url:
        return ""

    global _version_cache
    now = time.time()
    with _version_lock:
        if _version_cache is not None:
            checked_at, val = _version_cache
            if now - checked_at < (_VERSION_TTL_OK if val else _VERSION_TTL_BAD):
                return val

    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ImageSL"})
        with urllib.request.urlopen(req, timeout=4) as r:
            if not (200 <= getattr(r, "status", 200) < 300):
                raise ValueError("bad status")
            val = r.read(64).decode("utf-8", "replace").strip()
    except Exception:
        with _version_lock:
            if _version_cache is not None:
                return _version_cache[1]
        return ""

    if not re.fullmatch(r"[0-9A-Za-z._+-]{1,32}", val or ""):
        val = ""

    with _version_lock:
        _version_cache = (now, val)
    return val

_sha_cache: dict[str, tuple[tuple, str]] = {}
_sha_lock = threading.Lock()

def _sha256_local(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return ""
    key, stamp = str(path), (st.st_size, int(st.st_mtime))
    with _sha_lock:
        hit = _sha_cache.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(block)
    except OSError:
        return ""
    digest = h.hexdigest()
    with _sha_lock:
        _sha_cache[key] = (stamp, digest)
    return digest

def _sha256_external(url: str) -> str:
    key = url + ".sha256"
    with _sha_lock:
        hit = _sha_cache.get(key)
        if hit and (time.time() - hit[0][0]) < _PROBE_TTL_OK:
            return hit[1]
    import urllib.request
    digest = ""
    try:
        req = urllib.request.Request(key, headers={"User-Agent": "ImageSL"})
        with urllib.request.urlopen(req, timeout=8) as r:
            if 200 <= getattr(r, "status", 200) < 300:
                token = r.read(200).decode("utf-8", "ignore").split()
                if token and len(token[0]) == 64 and all(
                        c in "0123456789abcdefABCDEF" for c in token[0]):
                    digest = token[0].lower()
    except Exception:
        digest = ""
    with _sha_lock:
        _sha_cache[key] = ((time.time(),), digest)
    return digest

@app.get("/api/downloads")
async def api_downloads() -> JSONResponse:
    platforms = {}
    for key, (name, _ct, env_key) in _DOWNLOADS.items():
        external = _external_url(env_key)
        path = DOWNLOAD_DIR / name
        on_disk = path.is_file()

        if external:
            ok, size = await run_in_threadpool(_probe_external, external)
            digest = await run_in_threadpool(_sha256_external, external) if ok else ""
        else:
            ok, size = on_disk, (path.stat().st_size if on_disk else 0)
            digest = await run_in_threadpool(_sha256_local, path) if ok else ""

        manifest_digest = await run_in_threadpool(_manifest_digest, key)

        platforms[key] = {
            "available": ok,
            "filename": name,
            "bytes": size,
            "sha256": digest or None,
            "url": f"/download/{key}" if ok else None,
            "manifest_url": f"/api/update/{key}/manifest" if manifest_digest else None,
            "manifest_sha256": manifest_digest or None,
            "file_url": f"/update/{key}/file" if manifest_digest else None,
        }
    published = await run_in_threadpool(_published_version)

    return JSONResponse({"version": published or APP_VERSION,
                         "server_version": APP_VERSION,
                         "platforms": platforms},
                        headers={"Cache-Control": "no-store"})

UPDATE_DIR = Path(os.environ.get("IMAGESL_UPDATE_DIR",
                                 str(DOWNLOAD_DIR / "update")))
_UPDATE_URL_KEYS = {"windows": "IMAGESL_UPDATE_URL_WINDOWS",
                    "macos": "IMAGESL_UPDATE_URL_MACOS"}

def _update_base(platform: str) -> str:
    return (os.environ.get(_UPDATE_URL_KEYS.get(platform, "")) or "").strip()

def _manifest_digest(platform: str) -> str:
    external = _update_base(platform)
    if external:
        return _sha256_external(external.rstrip("/") + "/manifest.json")
    return _sha256_local(UPDATE_DIR / platform / "manifest.json")

@app.get("/api/update/{platform}/manifest")
def api_update_manifest(platform: str):
    if platform not in _DOWNLOADS:
        raise HTTPException(status_code=404, detail="Unknown platform.")
    external = _update_base(platform)
    if external:
        return RedirectResponse(external.rstrip("/") + "/manifest.json",
                                status_code=302)
    path = UPDATE_DIR / platform / "manifest.json"
    if not path.is_file():
        raise HTTPException(status_code=404,
                            detail="No update manifest is published.")
    return FileResponse(path, media_type="application/json",
                        headers={"Cache-Control": "no-store"})

@app.get("/update/{platform}/file/{digest}")
def update_file(platform: str, digest: str):
    if platform not in _DOWNLOADS:
        raise HTTPException(status_code=404, detail="Unknown platform.")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
        raise HTTPException(status_code=400, detail="Not a digest.")
    digest = digest.lower()

    external = _update_base(platform)
    if external:
        return RedirectResponse(f"{external.rstrip('/')}/files/{digest}",
                                status_code=302)
    path = UPDATE_DIR / platform / "files" / digest
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No such object.")
    return FileResponse(path, media_type="application/octet-stream",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})

def _open_external(url: str):
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ImageSL"})
        r = urllib.request.urlopen(req, timeout=20)
    except Exception:
        return None
    if not (200 <= getattr(r, "status", 200) < 300):
        try:
            r.close()
        except Exception:
            pass
        return None
    try:
        size = int(r.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        size = 0
    return r, size

def _iter_external(remote, chunk: int = 256 * 1024):
    try:
        while True:
            block = remote.read(chunk)
            if not block:
                break
            yield block
    finally:
        try:
            remote.close()
        except Exception:
            pass

@app.api_route("/download/{platform}", methods=["GET", "HEAD"])
def download(platform: str, request: Request):
    entry = _DOWNLOADS.get(platform.lower())
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown platform.")
    name, content_type, env_key = entry
    request_is_head = request.method == "HEAD"

    external = _external_url(env_key)
    if external:
        upstream = _open_external(external)
        if upstream is None:
            raise HTTPException(status_code=502,
                                detail="The installer host did not answer. Try again shortly.")
        remote, size = upstream
        headers = {
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "public, max-age=300",
        }
        if size:
            headers["Content-Length"] = str(size)
        if request_is_head:
            remote.close()
            return Response(status_code=200, media_type=content_type, headers=headers)
        return StreamingResponse(_iter_external(remote), media_type=content_type,
                                 headers=headers)

    path = DOWNLOAD_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404,
                            detail="That build has not been published yet.")
    return FileResponse(str(path), media_type=content_type, filename=name,
                        headers={"Cache-Control": "public, max-age=300"})

if IMAGESL_DESKTOP:
    try:
        import desktop_settings
        desktop_settings.register(app, APP_VERSION, CACHE_DIR)
    except Exception as _exc:
        print(f"ImageSL: desktop settings unavailable ({_exc})")

_PUBLIC_PAGES = [("/", "1.0"), ("/privacy", "0.4"), ("/terms", "0.4")]

@app.get("/robots.txt")
def robots_txt():
    lines = ["User-agent: *", "Allow: /"]
    lines += ["Disallow: /download/", "Disallow: /api/"]
    if not ANALYZER_ENABLED:
        lines.append("Disallow: /app")
    lines += ["", f"Sitemap: {SITE_URL}/sitemap.xml", ""]
    return PlainTextResponse("\n".join(lines),
                             headers={"Cache-Control": "public, max-age=3600"})

@app.get("/sitemap.xml")
def sitemap_xml():
    today = time.strftime("%Y-%m-%d", time.gmtime())
    urls = "".join(
        f"<url><loc>{SITE_URL}{path}</loc><lastmod>{today}</lastmod>"
        f"<changefreq>weekly</changefreq><priority>{pri}</priority></url>"
        for path, pri in _PUBLIC_PAGES
    )
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f'{urls}</urlset>')
    return Response(content=xml, media_type="application/xml",
                    headers={"Cache-Control": "public, max-age=3600"})

@app.get("/privacy", response_class=HTMLResponse)
def privacy() -> HTMLResponse:
    return _serve_html("privacy.html")

@app.get("/terms", response_class=HTMLResponse)
def terms() -> HTMLResponse:
    return _serve_html("terms.html")

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
    return _serve_css("tokens.css")

@app.get("/site.css")
def site_css():
    return _serve_css("site.css")

@app.get("/app.js")
def app_js():
    path = WEB_DIR / "app.js"
    return (FileResponse(str(path), media_type="application/javascript", headers=_STATIC_CACHE)
            if path.is_file() else HTMLResponse("", status_code=404))

@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": APP_VERSION})

@app.post("/api/keepalive")
async def api_keepalive(request: Request, x_imagesl_key: Optional[str] = Header(default=None)):
    _require_key(x_imagesl_key)
    body = await request.json()
    ids = body.get("analysis_ids")
    if not isinstance(ids, list):
        raise HTTPException(status_code=400, detail="No analysis_ids provided.")
    alive = [a for a in ids[:2000] if _CACHE.touch(str(a))]
    return JSONResponse({"alive": alive,
                         "expired": [a for a in ids[:2000] if a not in set(alive)]})

@app.get("/api/stains")
def api_stains() -> JSONResponse:
    return JSONResponse({"stains": stain_registry.as_list()})

def _undecodable_reason(data: bytes, name: str, exc: Exception) -> str:
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
    payload = await run_in_threadpool(_analyze_upload, data, name, stain_key)
    return JSONResponse(payload)

def _render_type(entry: dict, image_type: str) -> np.ndarray:
    rgb, p = entry["rgb"], entry["params"]

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
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="TIFF", compression="tiff_deflate")
    return buf.getvalue()

def _auto_level(entry: dict) -> int:
    r = entry.get("result")
    return int(getattr(r, "auto_level", detect_mod.AUTO_LEVEL))

def _same_level(a, b, entry: dict) -> bool:
    auto = _auto_level(entry)
    return (auto if a is None else a) == (auto if b is None else b)

def _apply_view(entry: dict, level: Optional[str], regions=None) -> bool:
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
    _require_key(x_imagesl_key)
    body = await request.json()
    return _download_tif(str(body.get("analysis_id") or ""),
                         str(body.get("image_type") or ""),
                         body.get("level"), body.get("regions"))

@app.get("/api/download_tif")
def api_download_tif(analysis_id: str, image_type: str, level: Optional[str] = None):
    return _download_tif(analysis_id, image_type, level)

@app.post("/api/rehydrate")
async def api_rehydrate(request: Request, x_imagesl_key: Optional[str] = Header(default=None)):
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
        if entry is None:
            return payload
        if _apply_view(entry, view.get("level"), view.get("regions")):
            _render_images(entry)
            _persist(aid, entry)
        pub = _public(entry)
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

_CSV_COLUMNS = [
    "filename",
    "detected_stain",
    "stain_category",
    "positive_percent_of_image",
    "positive_pixels",
    "tissue_pixels",
    "total_image_pixels",
    "stained_objects",
    "median_object_area_px",
    "mean_object_area_px",
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
    lvl = int(getattr(r, "level", 0))
    auto = int(getattr(r, "auto_level", 0))
    n_lv = int(getattr(r, "level_count", detect_mod.N_LEVELS))
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
        "positive_percent_of_image": pct_of_image,
        "positive_pixels": r.positive_pixels,
        "tissue_pixels": r.tissue_pixels,
        "total_image_pixels": total_px,
        "stained_objects": getattr(r, "objects", 0),
        "median_object_area_px": getattr(r, "median_object_px", 0),
        "mean_object_area_px": getattr(r, "mean_object_px", 0.0),
        "detection_bar_excess_od": getattr(r, "detection_bar", 0.0),
        "detection_floor_excess_od": getattr(r, "detection_floor", 0.0),
        "tissue_texture_sigma_od": getattr(r, "texture_sigma", 0.0),
        "object_separability": round(sep, 3),
        "object_split_discard_fraction": getattr(r, "bar_discard", 0.0),
        "staining_pattern": ("even" if getattr(r, "single_population", False) else "focal"),
        "sensitivity_level": lvl,
        "sensitivity_auto_level": auto,
        "sensitivity_levels": getattr(r, "level_count", 0),
        "sensitivity_setting": setting,
        "manual_regions": getattr(r, "region_count", 0),
        "include_regions": getattr(r, "include_regions", 0),
        "exclude_regions": getattr(r, "exclude_regions", 0),
        "pixels_added_by_include": getattr(r, "region_added_pixels", 0),
        "pixels_removed_by_exclude": getattr(r, "region_removed_pixels", 0),
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
        try:
            _apply_view(entry, (ov or {}).get("level"), (ov or {}).get("regions"))
            rows.append(_csv_row(entry))
            _persist(aid, entry)
        except Exception:
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
    overrides = overrides or {}
    sink = _ZipSink()
    zf = zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True)
    used: set[str] = set()
    rows: list[dict] = []
    dropped: list[str] = list(missing)
    problems: list[str] = []

    try:
        for aid in analysis_ids:
            try:
                entry = _CACHE.get(aid)
            except Exception:
                entry = None
            if not entry:
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
                    rows.append(_csv_row(entry))
                if changed:
                    _persist(aid, entry)
            except Exception as exc:
                problems.append(f"{stem} ({aid}): {exc}")
            finally:
                entry = None
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
