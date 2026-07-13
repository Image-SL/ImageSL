"""
ImageSL desktop client — a thin native shell.

This program deliberately contains NO analysis logic and NO API keys. It:
  1. asks the user for their ImageSL license key (once, stored locally),
  2. opens a native window (Edge WebView2 on Windows) pointed at the hosted
     ImageSL web app, passing the license key,
  3. lets the backend on Railway do all the work.

Because nothing proprietary ships in the executable, there is nothing to
decompile out of it. The build is small and unobfuscated, which also keeps
antivirus heuristics calm (the durable fix for SmartScreen is code signing —
see docs/SECURITY.md).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import webview  # pywebview

APP_NAME = "ImageSL"
# Set this to your deployed Railway URL at build time (env wins, then bundled
# config.json, then this default). Example: https://imagesl.up.railway.app
DEFAULT_BACKEND = "https://imagesl.up.railway.app"


def _config_dir() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _bundled_config() -> dict:
    # config.json placed next to the exe or bundled by PyInstaller.
    for candidate in (
        Path(getattr(sys, "_MEIPASS", ".")) / "config.json",
        Path(sys.argv[0]).resolve().parent / "config.json",
    ):
        try:
            if candidate.is_file():
                return json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def backend_url() -> str:
    return (
        os.environ.get("IMAGESL_BACKEND_URL")
        or _bundled_config().get("backend_url")
        or DEFAULT_BACKEND
    ).rstrip("/")


class Bridge:
    """JS <-> Python bridge exposed to the local bootstrap page."""

    def __init__(self):
        self._cfg_path = _config_dir() / "config.json"

    def get_state(self) -> dict:
        key = ""
        try:
            if self._cfg_path.is_file():
                key = json.loads(self._cfg_path.read_text(encoding="utf-8")).get("license_key", "")
        except Exception:
            key = ""
        return {"backend": backend_url(), "has_key": bool(key), "key": key}

    def save_key(self, key: str) -> dict:
        key = (key or "").strip()
        try:
            self._cfg_path.write_text(json.dumps({"license_key": key}), encoding="utf-8")
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "url": self._app_url(key)}

    def _app_url(self, key: str) -> str:
        base = backend_url()
        return f"{base}/app?key={key}" if key else f"{base}/app"

    def open_app(self, key: str) -> None:
        webview.windows[0].load_url(self._app_url((key or "").strip()))


BOOTSTRAP_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>ImageSL</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; height:100vh; display:grid; place-items:center; font-family:'Segoe UI',sans-serif;
         color:#f4f1fb; background:radial-gradient(900px 600px at 70% -10%, #3b1e6e, #08060d 60%); }
  .card { width:360px; text-align:center; padding:34px 30px; background:rgba(24,17,40,.7);
          border:1px solid rgba(168,130,255,.34); border-radius:20px; box-shadow:0 30px 80px -30px rgba(124,58,237,.6); }
  h1 { font-size:22px; margin:14px 0 4px; }
  p { color:#b9aed6; font-size:14px; margin:0 0 20px; }
  input { width:100%; padding:12px; border-radius:12px; border:1px solid rgba(160,120,255,.3);
          background:rgba(0,0,0,.35); color:#fff; font-size:14px; box-sizing:border-box; margin-bottom:12px; }
  button { width:100%; padding:12px; border:none; border-radius:12px; cursor:pointer; font-size:15px; font-weight:600;
           color:#fff; background:linear-gradient(135deg,#a855f7,#6d28d9); }
  a { color:#b9aed6; font-size:12px; }
  .skip { display:block; margin-top:14px; }
</style></head><body>
  <div class="card">
    <div style="font-size:40px;">◆</div>
    <h1>Welcome to ImageSL</h1>
    <p>Enter your license key to connect to the analysis engine.</p>
    <input id="key" type="text" placeholder="IMAGESL-XXXX-XXXX" autofocus />
    <button onclick="go()">Launch</button>
    <a class="skip" href="#" onclick="skip();return false;">Continue without a key</a>
  </div>
<script>
  async function go(){
    const k = document.getElementById('key').value.trim();
    const r = await window.pywebview.api.save_key(k);
    if (r && r.ok) window.location = r.url;
  }
  async function skip(){ const s = await window.pywebview.api.get_state(); window.pywebview.api.open_app(''); }
  window.addEventListener('pywebviewready', async () => {
    const s = await window.pywebview.api.get_state();
    if (s.has_key) window.pywebview.api.open_app(s.key);   // returning user -> straight in
    else document.getElementById('key').focus();
  });
</script></body></html>
"""


def main() -> None:
    bridge = Bridge()
    webview.create_window(
        APP_NAME,
        html=BOOTSTRAP_HTML,
        js_api=bridge,
        width=1360,
        height=880,
        min_size=(1024, 700),
        background_color="#08060d",
    )
    webview.start()


if __name__ == "__main__":
    main()
