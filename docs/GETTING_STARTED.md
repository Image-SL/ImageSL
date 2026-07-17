# Getting ImageSL up and running

Two paths: **A) deploy on Railway** (recommended — makes it fully online) or
**B) run locally**. Either way, the AI chatbot needs one thing: an
`ANTHROPIC_API_KEY`.

---

## 0. Get your Anthropic API key (powers the chatbot + AI vision)

1. Go to <https://console.anthropic.com> → sign in.
2. **Settings → API Keys → Create Key**. Copy it (starts with `sk-ant-`).
3. Add credit/billing on that account (the chatbot calls the Claude API, which is
   metered).

Without this key the app still runs and analyzes slides — but the chatbot and AI
vision will reply "not configured." **This key is the whole chatbot.**

---

## A. Deploy on Railway (fully online)

### A1. Put the code on GitHub

From `C:\Users\sli92\Downloads\ImageSL\ImageSL`:

```bash
git add -A
git commit -m "ImageSL online web app with recalculating assistant"
git branch -M main
git push -u origin main        # remote is already github.com/solperp/ImageSL
```

### A2. Create the Railway service

1. <https://railway.app> → **New Project → Deploy from GitHub repo → solperp/ImageSL**.
2. Railway detects `railway.json` and builds the `Dockerfile` automatically.
   (The old deploy failed on `$PORT`; that's fixed — `railway.json` now starts
   uvicorn through a shell that expands the port.)

### A3. Set environment variables

Railway service → **Variables** tab → add these (only the first matters for the
chatbot):

```
ANTHROPIC_API_KEY = sk-ant-...your key...
IMAGESL_CLAUDE_MODEL = claude-opus-4-8
IMAGESL_VISION_MODEL = claude-opus-4-8
IMAGESL_MAX_UPLOAD_MB = 256
IMAGESL_VERSION = 2.0.0
```

Optional — lock the app behind license keys you invent (comma-separated):

```
IMAGESL_ACCESS_TOKENS = KEY-ALICE-01,KEY-BOB-02
```

Do **not** set `PORT` — Railway injects it.

### A4. Deploy and open

1. Railway redeploys on save. Watch **Deploy Logs** for `Uvicorn running`.
2. **Settings → Networking → Generate Domain** to get a public URL.
3. Open the URL. Health check: `https://<your-domain>/api/health` should return
   `{"status":"ok","ai_configured":true,...}`. If `ai_configured` is `false`,
   the key isn't set — recheck step A3 and redeploy.

---

## B. Run locally (for testing)

Your machine currently has **no Python installed** (only the Windows Store stub),
so step B1 is required.

### B1. Install Python 3.12

Download from <https://www.python.org/downloads/> → run the installer → tick
**"Add python.exe to PATH"**. Reopen your terminal and confirm: `python --version`.

### B2. Install and run

```powershell
cd C:\Users\sli92\Downloads\ImageSL\ImageSL\server
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:ANTHROPIC_API_KEY = "sk-ant-...your key..."   # enables the chatbot
python app.py
```

Open <http://localhost:8000>.

---

## Using it

1. **Upload** a `.tif` / `.png` / `.jpg` slide and click **Analyze**.
   You get: the original, the green *positive overlay*, a recolored preview, and
   the numbers (positive-area %, positive/tissue pixels, threshold).
2. **Sliders** (manual control):
   - *Background threshold* — how much faint tissue counts as tissue.
   - *Sensitivity* — strict (fewer positives) ↔ loose (more positives).
   - *Target stain* — chromogen vs counterstain.
   - *Preview target darkness* / *background color* — appearance only.
3. **Assistant** (the AI way — it recalculates for you). Try:
   - *"You're counting too much of the pale background as positive — be stricter."*
   - *"The stain I care about is the blue one, not the brown."*
   - *"Ignore the faint tissue at the edges."*
   - *"Make the target staining darker and set the background to white."*

   It calls a tool, the server re-runs the analysis, and the numbers/images on the
   page update, followed by a short explanation of what it changed.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Chatbot: "assistant is not configured" | `ANTHROPIC_API_KEY` isn't set on the server. Add it in Railway Variables (or your shell) and redeploy/restart. |
| Deploy failed: `'$PORT' is not a valid integer` | Old `railway.json`. The current one uses `sh -c "... ${PORT:-8000}"`; re-push and redeploy. |
| Analyze: "Analysis expired" when using sliders/chat | The per-slide state is held in memory for 30 min; just click **Analyze** again. |
| Upload rejected as too large | Raise `IMAGESL_MAX_UPLOAD_MB` (default 256). |
| 401 on every API call | You set `IMAGESL_ACCESS_TOKENS`; enter one of those keys in the app's *Access key* box. |
| Chatbot replies but numbers don't change | It answered a general question without needing a tool. Ask it explicitly to adjust (e.g. "be stricter and recalculate"). |
