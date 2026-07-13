# ImageSL — Deploying to Railway

The backend deploys from this repo's `Dockerfile`. These are the steps only you
can do (they need your accounts, your money, and your keys) — everything else is
already wired.

## Prerequisites

- A [Railway](https://railway.app) account.
- The GitHub repo `github.com/solperp/ImageSL` (already the `origin` remote).
- An Anthropic API key from <https://console.anthropic.com> (for AI features).

## 1. Push the code

```bash
cd C:/Users/sli92/Downloads/ImageSL/ImageSL
git add -A
git commit -m "ImageSL v1"
git push -u origin main
```

## 2. Create the Railway service

1. Railway dashboard → **New Project** → **Deploy from GitHub repo** →
   select `solperp/ImageSL`.
2. Railway detects `railway.json` + `Dockerfile` and builds automatically.
3. Under the service → **Settings** → **Networking** → **Generate Domain**
   (e.g. `imagesl.up.railway.app`).

Railway sets `$PORT` itself; the app binds to it.

## 3. Set environment variables

Service → **Variables** → add:

| Variable | Value | Notes |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | Enables AI vision + chatbot. Without it, analysis still works. |
| `IMAGESL_ACCESS_TOKENS` | `IMAGESL-ALICE-01,IMAGESL-BOB-02` | Comma-separated license keys. Omit to run open (dev only). |
| `IMAGESL_CLAUDE_MODEL` | `claude-opus-4-8` | Optional model override. |
| `IMAGESL_MAX_UPLOAD_MB` | `256` | Optional upload cap. |

(Full list in `.env.example`.) Redeploy after changing variables.

## 4. Verify

- `https://<your-domain>/` → landing page.
- `https://<your-domain>/api/health` → `{"status":"ok","ai_configured":true,...}`.
- `https://<your-domain>/app?key=IMAGESL-ALICE-01` → analyzer; upload a `.tif`.

## 5. Publish the Windows client for download

The `/download/windows` button needs the built exe present in the image.

1. Point the client at your domain: edit `client/config.json` →
   `"backend_url": "https://<your-domain>"`.
2. Build it (on a Windows machine with Python):
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/build_client.ps1
   ```
3. **Code-sign** `client/dist/ImageSL.exe` (see [SECURITY.md](SECURITY.md) — this
   is what actually stops the SmartScreen warning).
4. Copy the signed exe to `server/dist/ImageSL.exe`, commit, and push. (It's
   git-ignored by default — either force-add it, `git add -f server/dist/ImageSL.exe`,
   or, better for a large binary, host it on Railway volume / object storage and
   set `IMAGESL_CLIENT_EXE` to that path.)
5. Redeploy. `/api/health` will now report `"client_available": true` and the
   download button activates.

## Local development without Docker

```bash
cd server
pip install -r requirements.txt
uvicorn app:app --reload      # http://localhost:8000
```

## Notes on the AI model

The app defaults to `claude-opus-4-8` for both vision reasoning and chat. To use
Anthropic's most capable model instead, set `IMAGESL_CLAUDE_MODEL=claude-fable-5`
(higher cost; see the Anthropic pricing page). AI features fail safe — if the key
is missing or a call errors, slide analysis continues to work.
