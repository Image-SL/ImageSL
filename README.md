# ImageSL

**A fully online tool for DAB IHC stain quantification, with automatic background
detection and removal.**

Upload an immunohistochemistry (IHC) slide. ImageSL finds the slide background
and throws it away, separates the true chromogenic stain from the counterstain
(color deconvolution + per-slide background segmentation + Otsu thresholding —
the methods used by QuPath/Fiji), quantifies the positive area, and lets you
re-render the image.

> **This build is DAB-only.** Both analysis modes still work — *Auto-detect*
> finds the background and the brown chromogen for you, *Select stain* lets you
> name it — they just resolve to DAB. Further stains are added by enabling them
> in two lists; see [Adding a stain](#adding-a-stain).

There is no desktop app and no download — it's a single web page. All analysis
runs on the server.

> ⚕️ ImageSL assists interpretation. It is **not** a clinical diagnosis; results
> must be confirmed by a qualified pathologist.

> **Note:** an earlier design included an in-app Claude assistant with
> recalculation tools (`server/ai/claude_client.py`, `POST /api/chat`). That code
> is **not present in this repository** — `docs/` still describes it in places.
> Nothing in the current app calls the Anthropic API.

## Adjusting a result

Every control re-measures live in the browser, with no server round-trip:

| Control | What it does |
| --- | --- |
| **Detection threshold** slider — sits directly under the slide, so the image stays in view while you drag | re-thresholds the per-pixel stainness score — the %, pixel counts, overlay and *Stain only* panel all update as you drag |
| **TIF** / **Download** | exports any panel at full analysis resolution, at exactly the threshold on screen |

Each result shows three panels — **Original**, **Overlay** and **Stain only**.
The detection highlight is always neon green (`#39ff14`); there is no colour picker.

Batch work is first-class: drop a folder of slides, then export one ZIP of
images plus a CSV of every measurement.

## Repository layout

```
ImageSL/
├── server/                  # FastAPI backend (deployed to Railway) — the whole app
│   ├── app.py               # routes: page, /api/analyze, /api/appearance, /api/stains,
│   │                        #         /api/download_tif, /api/export_csv, /api/export_zip
│   ├── ihc/engine.py        # THE analysis: white point → background segmentation →
│   │                        #   deconvolution → colour gates → score → renderers
│   ├── ihc/stains.py        # stain registry + ENABLED_KEYS (what is shipped)
│   ├── web/                 # plain single-page UI (index.html, styles.css, app.js)
│   └── requirements.txt
├── Dockerfile, railway.json, .env.example
└── docs/                    # ARCHITECTURE.md, SECURITY.md, DEPLOY.md
```

## How the background is removed

The tissue mask every number is anchored to comes from `segment_tissue()`, which
combines four independent pieces of evidence rather than one fixed cutoff:

| Evidence | What it defeats |
| --- | --- |
| **White point** — the brightest few % of the scan *is* bare glass, so density is measured against that | dim, warm-lamp or grey-background scans reading as "tissue everywhere" |
| **Otsu on the slide's own OD histogram** | one global threshold being wrong for every slide |
| **Chroma rescue** — a pale but distinctly *coloured* pixel is dilute stain | faint staining being thrown away as background |
| **Texture** — real tissue has fine cellular structure | vignetting, haze, defocus blur, flat scanner artefacts |

The mask is then cleaned: interior gaps (lumina, fat vacuoles) are filled so they
count as tissue, isolated specks (dust, ink, edge debris) are dropped so they do
not. A slide with no glass in frame is detected and handled separately, so Otsu
can never split tissue against itself.

Two outputs expose the result: **Stain only** (everything except the detected
chromogen erased) and **Tissue** (glass removed, all tissue kept).

## Adding a stain

DAB is the only stain currently enabled. Every other chromogen and special stain
is still defined and vetted in `server/ihc/stains.py` — switched off, not
deleted. To bring one online:

1. add its key to `ENABLED_KEYS` in `server/ihc/stains.py` → it appears in the
   "Select stain" picker and is accepted by the API;
2. if auto-detect should also be able to find it unaided, add its family to
   `ENABLED_FAMILIES` in `server/ihc/engine.py`.

Nothing else needs to change. A request naming a stain that is not enabled falls
back to auto-detect rather than failing.

## Run it — see GETTING_STARTED

Step-by-step (local + Railway) is in
**[docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)**. Ignore its
`ANTHROPIC_API_KEY` steps — see the note above; no key is needed.

Shortest path:

1. Push this repo to GitHub and point a Railway service at it (Dockerfile build).
2. Open the Railway URL and drop a slide on it.

Locally:

```bash
pip install -r server/requirements.txt && uvicorn --app-dir server app:app --port 8000
```

## One honest note

The stain/background separation is rigorous per-pixel digital-pathology math —
per-slide white-point normalisation, Otsu segmentation, texture analysis, colour
deconvolution and hue/saturation/specificity gating — not a model guessing at a
thumbnail. Every number in the CSV traces back to a pixel rule you can read in
`server/ihc/engine.py`. Full design in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
