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
3. **White point** (`estimate_white_point`, `white_is_glass`) — the brightest
   few percent of a scan is bare glass, so absorbance is measured against *that*
   rather than a theoretical 255. Applied only when the scan is genuinely dim or
   tinted **and** the white forms a population separated from the tissue bulk, so
   a well-exposed slide is untouched and a wall-to-wall-tissue slide is never
   renormalised against its own palest tissue.
4. **Background & tissue** (`segment_tissue`) — the mask every statistic is
   anchored to, from four independent signals: per-slide **Otsu** on the OD
   histogram; a **chroma rescue** so pale-but-coloured pixels count as dilute
   stain; a **local-standard-deviation texture** test that drops large smooth
   regions (vignetting, haze, defocus, scanner artefacts) carrying no cellular
   structure; and morphological cleanup that fills interior lumina and drops
   isolated specks. A frame with no glass in it is detected and falls back to the
   absolute OD floor, so Otsu can never split tissue against itself.
5. **Stain basis** (`_estimate_basis`) — a curated reference matrix is chosen by
   the chromogen family detected on the slide. Only families in
   `ENABLED_FAMILIES` (currently `H-DAB`) can be selected; when the slide's
   chromogen falls outside them the DAB basis is kept and `chromogen_present`
   comes back `False`, so the app says "no DAB here" instead of quietly
   measuring some other colour.
6. **Deconvolution** (`_deconvolve`) — projects OD onto the stain basis to get
   per-stain concentration maps.
7. **Colour gating → score** — a pixel is a candidate only if it is inside the
   chromogen's hue *band* (tighter than the generic ±46° tolerance — this is what
   stops a red chromogen counting as brown), saturated above the slide's own
   tissue bulk, chromogen-dominated, and in the eroded tissue core. Candidates
   carry a normalised concentration **score**; everything else is zeroed. The
   score is shipped to the browser as a grayscale PNG so the threshold slider
   re-measures live.
8. **Quantification** — threshold on that score (auto-anchored to each slide's
   background concentration, or manual) yields positive-area %, positive pixel
   count, and mean optical density.
9. **Rendering** — `render_overlay` highlights counted pixels (always in the one
   fixed blue, `engine.OVERLAY_BLUE`); `render_stain_only` erases everything
   *except* the chromogen; `compose_comparison` builds the labelled export.
   `render_background_removed` (glass only) and `render_stain` (one separated
   stain) are still available for export but are no longer shown in the UI, which
   displays exactly three panels: Original, Overlay, Stain only.

This is the same family of techniques used by QuPath and Fiji — principled, not
a per-pixel color threshold.

### `server/ihc/stains.py` — the stain registry

Every IHC chromogen and special stain is defined here with its Ruifrok &
Johnston deconvolution vectors. `ENABLED_KEYS` decides which are actually
shipped — currently `{"dab"}`. Disabled entries stay defined and vetted;
`lookup()` returns `None` for them, which callers read as "use auto-detect", so
an old or hand-written request for a non-shipped stain degrades instead of
failing.

### ~~`server/ai/claude_client.py`~~ — not in this repository

An earlier design had a Claude vision + chat layer (`vision_stain_report()`,
`chat_stream()`, `POST /api/chat`, `ANTHROPIC_API_KEY`). **That module is not
present in this codebase** and nothing in the current app calls the Anthropic
API. References to it below and in `DEPLOY.md` / `GETTING_STARTED.md` are stale.

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
