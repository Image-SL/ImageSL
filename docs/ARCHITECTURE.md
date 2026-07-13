# ImageSL — Architecture

## Overview

```
                    ┌──────────────────────────────────────────────┐
   ImageSL.exe      │                 Railway backend               │
  (thin shell) ───► │  FastAPI (server/app.py)                      │
   upload slide     │   ├─ ihc/engine.py   deconvolution + Otsu     │
   + license key    │   │                   + variant rendering      │
                    │   ├─ ai/claude_client.py  vision + chat        │
   ◄─────────────── │   ├─ web/  premium site + analyzer console     │
   results,         │   └─ ANTHROPIC_API_KEY  🔒 (never leaves here) │
   renders, chat    └──────────────────────────────────────────────┘
```

Everything of value — the algorithm, the AI, the keys — is server-side. The
desktop client is a native WebView2 window that loads `/app` and passes a
license key. This is what makes "source fully protected" literally true: there
is no analysis code in the download to reverse-engineer.

## Backend components

### `server/ihc/engine.py` — the analysis core

The pixel-level work, in order:

1. **Load** (`load_rgb`) — decodes `.tif/.tiff/.png/.jpg`. For pyramidal or
   multi-page histology TIFFs it uses `tifffile`, picks a pyramid level, and
   downsamples to a bounded long edge (default 2048 px) so multi-gigabyte
   slides stay within Railway RAM and analyze in well under a second.
2. **Optical density** (`rgb_to_od`) — Beer–Lambert transform, the physically
   correct space for stain math.
3. **Automatic stain estimation** (`estimate_stains_macenko`) — the **Macenko**
   method finds the two dominant stain color vectors from the image itself. This
   is why ImageSL separates *any* stain color, not a hardcoded brown. A fixed
   H-DAB matrix is the fallback for single-stain / low-contrast slides.
4. **Deconvolution** (`_deconvolve`) — projects OD onto the stain basis to get
   per-stain concentration maps.
5. **Background & tissue** — pixels with low total OD (bright glass) are
   background; the rest is tissue.
6. **Quantification** — Otsu threshold on the target concentration within tissue
   yields positive-area %, positive pixel count, and mean optical density.
7. **Rendering** — `render_overlay` highlights counted pixels; `render_variant`
   rescales each stain's concentration (darker/lighter) and repaints the
   background to any color, then inverts Beer–Lambert to RGB.

This is the same family of techniques used by QuPath and Fiji — principled, not
a per-pixel color threshold.

### `server/ai/claude_client.py` — the reasoning + conversation layer

- `vision_stain_report()` sends a small JPEG thumbnail to Claude with a
  JSON-schema structured-output request; Claude identifies the stain type,
  target chromogen color, tissue quality, and a suggested background color. The
  app uses this to label and pre-tune the deterministic pipeline.
- `chat_stream()` streams the in-app assistant. The current slide's metrics are
  injected as a trailing `role: "system"` message (an Opus-4.8 feature) so the
  assistant can reference real numbers without re-sending the whole prompt.
- Default model `claude-opus-4-8` (override via `IMAGESL_CLAUDE_MODEL`). Both
  functions **degrade gracefully** when `ANTHROPIC_API_KEY` is unset — analysis
  still works, AI features return a clear "not configured" message.

### `server/app.py` — the API

| Route | Purpose |
| --- | --- |
| `GET /` | Premium marketing landing page |
| `GET /app` | In-browser analyzer console |
| `GET /download/windows` | Serves the built desktop client |
| `POST /api/analyze` | Upload → full analysis + overlay + AI vision report; returns an `analysis_id` |
| `POST /api/variant` | Re-render with target/counterstain gain + background color (uses the cached analysis, so slider tweaks are instant) |
| `POST /api/set-target` | Re-quantify treating the other separated stain as the target |
| `POST /api/chat` | SSE token stream from the assistant |
| `GET /api/health` | Status, AI-configured flag, client-available flag |

An in-process TTL/LRU cache holds each upload's concentration maps keyed by
`analysis_id`, so the render sliders don't re-upload or recompute deconvolution.
(Per-instance memory; fine for a single Railway service. Scale-out would move
this to Redis or object storage.)

**Access control:** if `IMAGESL_ACCESS_TOKENS` is set, every `/api/*` call must
carry a matching `X-ImageSL-Key` header. The desktop client passes the user's
license key; the web app reads it from `?key=` and stores it locally.

## Desktop client

`client/imagesl_client.py` is a `pywebview` shell (~1 small file). It stores the
license key under `%APPDATA%/ImageSL/config.json`, then loads
`<backend>/app?key=<license>`. No analysis, no secrets, nothing proprietary.
Built to a single `.exe` with `scripts/build_client.ps1`.

## Data flow for one analysis

1. Client/browser `POST /api/analyze` with the slide.
2. Backend decodes + downsamples, optionally calls Claude vision on a thumbnail.
3. `engine.analyze()` runs deconvolution + Otsu, caches the maps, returns
   metrics + overlay + original + vision report + `analysis_id`.
4. User moves sliders → `POST /api/variant` with the `analysis_id` → instant
   re-render from cached maps.
5. User chats → `POST /api/chat` streams the assistant, primed with the metrics.

## Scaling notes / future work

- Replace the in-process cache with Redis for multi-instance deploys.
- Add true whole-slide tiling (OpenSlide) for `.svs` beyond the downsample cap.
- Optional: a trained nucleus/segmentation model (e.g. StarDist) as an
  alternative to Otsu for cell-level counts — a larger, separate effort with its
  own training data and validation.
