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

## How detection works

There is **no global intensity threshold** anywhere in the engine. A pixel is
never judged by how dark it is. Instead, for each slide:

1. A **local background field** of optical density is fitted with the stain
   excluded, so a diffuse tan wash, a lamp gradient or genuinely darker tissue is
   subtracted away *where it occurs*. Everything after this is measured on the
   **excess** over that background.

   The size of that neighbourhood is what separates *staining* from *tone*:
   anything varying more slowly than the window is absorbed into the background,
   anything more compact survives. Tissue tone — the centrilobular gradient of a
   diffusely stained liver — varies over hundreds of pixels; stained structures
   are a handful across. Lumina and holes are excluded from the fit (they are
   inside the section but transmit nearly all the light, and averaging them in
   rings every hole with false positives), and material far darker than the
   slide's own tissue is excluded wherever it is, so a stained region *wider*
   than the window cannot end up inside its own background.
2. The **colour of the excess** is read as an absorbance signature rather than a
   hue, because hue stops meaning anything as a pixel approaches black — which is
   why dense, unmistakable DAB used to be dropped while its pale halo was kept.
   It is read at two spatial scales and the stronger reading wins, so a
   two-pixel-wide streak is not diluted by the unstained tissue beside it. A
   second, independent reading of the *raw* warmth backs it up on the densest
   cores, where every channel is crushed: those stay clearly warm while ink,
   dust and folds sit at zero, which is what keeps dark grey deposits out.
3. The excess is grouped into **connected structures**, and stained objects are
   separated from background bumps by clustering the population of object peaks.
   The decision is made about structures, not pixels.
4. Each accepted structure is measured at **its own isophote**, so area does not
   inherit intensity: two structures of the same size measure the same area even
   if one is twice as dark.

Where a slide's staining is diffuse rather than focal, the engine says so in the
result notes instead of presenting an arbitrary boundary as a measurement.

## Adjusting a result

Every control re-measures live in the browser, with no server round-trip. The
server recomputes the authoritative numbers behind you and they agree exactly,
because both sides apply the same rule to the same level map.

| Control | What it does |
| --- | --- |
| **Sensitivity** slider — sits directly under the slide, so the image stays in view while you drag | moves the bar a structure's peak has to clear, around the operating point this slide chose for itself. `Auto` returns to it |
| **Focus** region | measure only inside the shapes you draw — one cortical layer, one TMA core, one half of a section. Tissue outside stops counting, denominator included |
| **Ignore** region | cut a fold, pen mark, bubble or torn edge out of the measurement entirely, denominator included |
| **More here** / **Less here** region | shift sensitivity *locally*, for an area that is genuinely weaker or stronger, without moving the whole slide's operating point |
| **TIF** / **Download** | exports any panel at full analysis resolution, at exactly the sensitivity and regions on screen |

A boosted region cannot invent staining: it still only admits chromogen-coloured
structures. The strongest boost means "count everything this slide could
plausibly call stain, here", not "paint this area positive".

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
│   ├── ihc/engine.py        # white point → tissue segmentation → stain basis →
│   │                        #   detect() → metrics → renderers
│   ├── ihc/detect.py        # THE detection: local background → excess colour →
│   │                        #   objects → per-object area → level map
│   ├── ihc/regions.py       # manual focus / ignore / local-sensitivity shapes
│   ├── ihc/stains.py        # stain registry + ENABLED_KEYS (what is shipped)
│   ├── web/                 # plain single-page UI (index.html, styles.css, app.js)
│   └── requirements.txt
├── scripts/backtest.py      # regression suite on real slides: misses, flooding,
│                            #   grey debris, tissue-mask collapse, and stability
│                            #   under illumination / resolution / compression
├── scripts/synthetic_cases.py  # constructed scenes with a known answer
├── scripts/synthetic_matrix.py # parameter grid: recall/precision vs ground truth
├── Dockerfile, railway.json, .env.example
└── docs/                    # ARCHITECTURE.md, SECURITY.md, DEPLOY.md
```

## Regression testing

Three suites, all run after any engine change. The point of the last two is to
stop the engine being right about a handful of slides and wrong in general.

```bash
python scripts/backtest.py /path/to/slides --montage out/ --json results.json
python scripts/synthetic_cases.py
python scripts/synthetic_matrix.py            # --full for the dense grid
```

`backtest.py` runs real sections and applies checks that need no hand-drawn
truth: unmistakable stain that was missed (in the engine's own units, and again
in raw-image units), background counted as signal, positive structures that are
not chromogen-coloured, tissue-mask collapse, and stability under illumination,
resolution and compression change.

`synthetic_cases.py` runs constructed scenes where the answer is known exactly —
a plaque wider than the background window, spots stained to one even density,
neutral grey debris, blank tissue, and a slow tonal gradient. Each case exists
because that failure actually happened.

`synthetic_matrix.py` sweeps a grid of conditions and measures **recall and
precision against exact ground truth** in each: structure size from a 2-pixel
punctum to a 120-pixel plaque, staining from barely visible to saturated, tissue
from nearly transparent to heavily counterstained, slow tonal gradients with and
without structures in them, lumina density, neutral debris, noise, and blur.
Every axis is one on which a detector can be accidentally right — one tuned on
medium puncta in pale tan tissue passes the real-slide suite and fails most of
these. It found, among others, that an HSV hue window was discarding genuine DAB
(dense chromogen shifts red, landing just outside a window drawn around DAB),
that a raw-warmth test only worked because the validation slides happened to
have pale warm tissue, and that a stained region wider than the background
window was erasing itself.

Every check is a statement that must hold for any correct quantifier, and each
failure names the case and the number. Exit status is non-zero if anything
fails.

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
