# ImageSL

[![Build](https://github.com/ImageSL/ImageSL/actions/workflows/build-desktop.yml/badge.svg)](https://github.com/ImageSL/ImageSL/actions/workflows/build-desktop.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Cite this repository](https://img.shields.io/badge/cite-CITATION.cff-brightgreen.svg)](CITATION.cff)
[![Research use only](https://img.shields.io/badge/use-research%20only-important.svg)](DISCLAIMER.md)


**A tool for DAB IHC stain quantification, with automatic background
detection and removal.**

> [!IMPORTANT]
> **Research use only. ImageSL is not a medical device and must not be used
> for clinical diagnosis or patient management.** It has not been cleared or
> approved by any regulatory authority. Validate against your own ground truth
> before relying on any measurement, and report the sensitivity level and any
> manual corrections alongside published results — see [DISCLAIMER.md](DISCLAIMER.md).

Upload an immunohistochemistry (IHC) slide. ImageSL finds the slide background
and throws it away, separates the true chromogenic stain from the counterstain
(color deconvolution + per-slide background segmentation + Otsu thresholding —
the methods used by QuPath/Fiji), quantifies the positive area, and lets you
re-render the image.

> **This build is DAB-only.** Both analysis modes still work — *Auto-detect*
> finds the background and the brown chromogen for you, *Select stain* lets you
> name it — they just resolve to DAB. Further stains are added by enabling them
> in two lists; see [Adding a stain](#adding-a-stain).
>
> **A slide carrying a different chromogen does not read as empty.** Handed a red
> section, this build still finds the structures and reports an ordinary-looking
> percentage — measured, 0.407% where the same scene in DAB reads 0.420%. The
> number is a real measurement of *something*, but it is not a DAB result, and
> the result notes say so on any slide whose absorbing material lacks DAB's
> signature. Read that note before quoting the figure.

It runs two ways from the same engine and the same code:

- **Desktop app** (Windows and macOS) — bundles the whole analysis engine and
  runs it on a private loopback port. Fully offline; a slide never leaves the
  machine. Download it from **<https://imagesl.com>**, or see
  [desktop/BUILD.md](desktop/BUILD.md) to build and publish it. (Not from GitHub
  Releases: this repository is private, so a release link 404s for everyone who
  cannot see it — and it 404s as an HTML page, which navigates the visitor away
  instead of downloading.)
- **Web page** — the same analyzer served from a host, with analysis running on
  the server.

> ⚕️ ImageSL assists interpretation. It is **not** a clinical diagnosis; results
> must be confirmed by a qualified pathologist.

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
   two-pixel-wide streak is not diluted by the unstained tissue beside it.

   On the *densest* staining that reading fails, unavoidably: the local
   background under a dark structure is itself tinted by the same chromogen, so
   subtracting it cancels the signature being tested for, and a near-black bile
   duct measures as neutral debris. So the colour question is also put to the
   material's **own** absorbance direction, which does not degrade with density —
   DAB reads +0.51 on it, haematoxylin −0.36, a red chromogen +0.04 and ink,
   dust or a fold 0.00. Either reading may vouch for a structure; neutral
   material fails both. This is what recovered the darkest, least ambiguous
   staining on the validation set, and with it the "is there DAB on this slide?"
   verdict, which had been answering *no* for ten of forty-six plainly DAB
   sections.
3. **Intraluminal debris is removed before anything is grouped.** The granular
   casts of pigment and cells that sit inside vessel lumina are the hardest false
   positive on liver sections: they are dense, so every absorbance test passes
   them, and they sit against a pale lumen, so their excess over the local
   background is large and reads faintly brown. What they are not is *coloured
   like DAB* — measured, they run 0.16-0.26 saturation at 29-71° hue against
   0.41-0.49 at 16-29° for real ductules. Material that is washed-out **and**
   olive at once is dropped, per pixel, so a duct running along the wall of a
   vessel still forms its own structure and is counted normally.
4. The excess is grouped into **connected structures**, and stained objects are
   separated from background bumps by clustering the population of object peaks.
   The decision is made about structures, not pixels.
5. Each accepted structure is measured at **its own isophote**, so area does not
   inherit intensity: two structures of the same size measure the same area even
   if one is twice as dark.

Every colour reading above is taken against the slide's own white point and
low-passed at the scale chroma actually lives at. Neither is optional: raw HSV is
not a property of the material, and a threshold on it moves with the lamp's
colour temperature and with JPEG's chroma subsampling — measured, by enough to
swing two sections' results 65-88%. Normalised, the same slide reads the same
under a ±12% illumination change, at a different resolution, and after
compression.

Where a slide's staining is diffuse rather than focal, the engine says so in the
result notes instead of presenting an arbitrary boundary as a measurement.

## Adjusting a result

Every control re-measures live in the browser, with no server round-trip. The
server recomputes the authoritative numbers behind you and they agree exactly,
because both sides apply the same rule to the same level map.

| Control | What it does |
| --- | --- |
| **Sensitivity** slider — sits directly under the slide, so the image stays in view while you drag | scales the bar a structure's peak has to clear, from 4× stricter to 4× more permissive than the operating point this slide chose for itself. 201 steps, so dragging eases structures in and out rather than jumping; the readout is the multiplier applied (`×0.76`), which means the same thing on every slide. `Auto` returns to the centre |
| **Focus** region | measure only inside the shapes you draw — one cortical layer, one TMA core, one half of a section. Tissue outside stops counting, denominator included |
| **Ignore** region | cut a fold, pen mark, bubble or torn edge out of the measurement entirely, denominator included |
| **TIF** / **Download** | exports any panel at full analysis resolution, at exactly the sensitivity and regions on screen |

A region says **where** to measure and nothing else. Both modes move the positive
area and the tissue it is measured against together, so the reported percentage
is always "positive area ÷ tissue actually measured" over exactly the area you
drew — and the figure on screen is the figure in the CSV, to the pixel. The
browser and the server rasterise a drawn shape with the identical rule (a pixel
counts when its centre is inside, and 1.0 is the outer edge of the last pixel),
so the two cannot drift apart and two regions splitting a slide in half
partition it exactly.

**Ignoring an area can move the percentage either way, and both directions are
correct.** Because a region moves the numerator *and* the denominator, cutting
out a lightly stained area makes the remaining tissue more stained on average
and the figure rises; cutting out a heavily stained one makes it fall. The card
spells out what the number is being measured over whenever a region is active
(`measured over 50.0% of the slide's tissue`), and the export carries both
`tissue_pixels` and `tissue_pixels_before_regions` so the scope of any published
figure is unambiguous.

There is no longer a *More here* / *Less here* region. A per-region operating
point made the headline percentage a mixture of several different decision
rules, which is not a quantity that can be compared between slides or written
into a method — and because the shift applied only inside the shape, the same
structure was counted or not depending on which side of a hand-drawn line it
fell. Sensitivity is one setting for the whole slide, and the export records
which setting produced each number.

Each result shows three panels — **Original**, **Overlay** and **Stain only**.
The detection highlight is always neon green (`#39ff14`); there is no colour picker.

Batch work is first-class: drop a folder of slides, then export one ZIP of
images plus a CSV of every measurement.

An open batch is kept alive by a heartbeat from the page, so a long review
session cannot expire underneath you; analyses are held for eight hours of
genuine inactivity. If an export ever does find a slide gone it says so — by
name, and with a `MISSING_SLIDES.txt` in the archive — rather than quietly
handing over a ZIP with nothing in it.

## Repository layout

```
ImageSL/
├── server/                  # FastAPI backend (deployed to AWS Lightsail) — the whole app
│   ├── app.py               # routes: page, /api/analyze, /api/appearance, /api/stains,
│   │                        #         /api/download_tif, /api/export_csv, /api/export_zip
│   ├── ihc/engine.py        # white point → tissue segmentation → stain basis →
│   │                        #   detect() → metrics → renderers
│   ├── ihc/detect.py        # THE detection: local background → excess colour →
│   │                        #   objects → per-object area → level map
│   ├── ihc/regions.py       # manual focus / ignore shapes (exact, centre-in-pixel)
│   ├── ihc/stains.py        # stain registry + ENABLED_KEYS (what is shipped)
│   ├── web/                 # plain single-page UI — index.html at "/",
│   │                        #   index.html (the analyzer) at "/app", styles.css, app.js
│   └── requirements.txt
├── desktop/                 # the offline app: launcher.py starts the bundled
│                            #   engine and opens a native window; ImageSL.spec
│                            #   (PyInstaller) + installer.iss (Inno Setup)
│                            #   package it — see desktop/BUILD.md
├── .github/workflows/       # build-desktop.yml: builds and smoke-tests Windows
│                            #   and macOS on a tag, then publishes the release
├── scripts/backtest.py      # regression suite on real slides: misses, flooding,
│                            #   grey debris, tissue-mask collapse, and stability
│                            #   under illumination / resolution / compression
├── scripts/synthetic_cases.py  # constructed scenes with a known answer
├── scripts/synthetic_matrix.py # parameter grid: recall/precision vs ground truth
├── Dockerfile, .env.example
└── docs/                    # ARCHITECTURE.md, DESIGN.md, THREAT_MODEL.md
```

## Regression testing

Three suites, all run after any engine change. The point of the last two is to
stop the engine being right about a handful of slides and wrong in general.

```bash
python scripts/backtest.py /path/to/slides --montage out/ --json results.json
python scripts/synthetic_cases.py
python scripts/synthetic_matrix.py            # --full for the dense grid
python scripts/synthetic_matrix.py --chromogen H-Red   # a family before enabling it
```

`backtest.py` runs real sections and applies checks that need no hand-drawn
truth: unmistakable stain that was missed (in the engine's own units, and again
in raw-image units), background counted as signal, positive structures that are
not chromogen-coloured, tissue-mask collapse, and stability under illumination,
resolution and compression change. It loads each slide through
`engine.load_rgb` from the file's own bytes — the exact path an upload takes,
including the downsample to the working resolution — so the suite cannot pass on
a code path no user ever exercises.

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
have pale warm tissue, that a stained region wider than the background window
was erasing itself, and that an absolute "is this material?" cut was classifying
97% of a very pale section as empty space — after which its noise estimate came
back ten times too large and the section reported 0.000% with forty obvious
structures on it.

Each condition is run over **five fixed realisations** and judged on the worst
of them, and a condition whose answer moves more than 35% between realisations
fails on that alone. Statistically identical scenes must measure the same: one
sample per condition hid a case where recall ranged from 13% to 98% — and the
reported area from 0.058% to 0.436% — depending only on where the structures
happened to land. (The seeds are fixed constants for the same reason. They were
`hash(name)`, and Python randomises string hashing per process, so the suite
generated different scenes on every run and its verdict was not reproducible.)

Every check is a statement that must hold for any correct quantifier, and each
failure names the case and the number. Exit status is non-zero if anything
fails.

Conditions marked as sitting *below the detection floor* are run and reported
but not failed on recall. At ~0.12 OD of added stain the tissue's own texture
out-peaks the staining, so no unsupervised rule separates them; what is still
required there is that the engine does not invent staining, and that it says the
boundary is a judgement rather than presenting it as a measurement.

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

A request naming a stain that is not enabled falls back to auto-detect rather
than failing.

**The two steps are not equally ready, and they are not the same mechanism.**

**Step 1 — the picker — works.** Choosing a stain explicitly hands its basis
straight to the detector, so it never depends on the engine guessing the
chromogen. Measured on the synthetic grid, a red-chromogen scene analysed as AEC
reaches **98.3%** recall against DAB's own **98.6%** on DAB. The engine can
already measure a red chromogen at parity; only the list is closed.

**Step 2 — auto-detect — does not.** `_detect_family()` is uncalibrated: it reads
the hue of *blended* pixels (chromogen over counterstain) and lands tens of
degrees off the stain actually present.

| Scene | Pure chromogen | What the engine measures | Lands in |
| --- | --- | --- | --- |
| DAB | 16.7° | 356.7° | H-Red's band |
| Red chromogen | 350.7° | 326.1° | H&E's band |
| Green chromogen | 150.0° | 187.1° | no band → falls back to DAB |

None of that is reachable today, because with `H-DAB` the only enabled family
every branch falls back to it — **the fallback is load-bearing, and adding a
second family removes it.** With `H-Red` enabled, a DAB slide would classify as
red. Calibrating this needs real sections (`scripts/backtest.py`); the blend that
skews the reading depends on counterstain density, which a synthetic scene fixes
by construction, so tuning the bands against the numbers above would just be
fitting the generator.

Either way, measure the family first:

```bash
python scripts/synthetic_matrix.py --chromogen H-Red
```

The suite stains its scenes with the family you name and scores recall and
precision against exact ground truth, the same way it does for DAB. Note what it
does **not** cover: recall is only half the question, and the gates that reject
debris — the brownness axis (DAB +0.51, a red chromogen +0.03, neutral debris
0.00) and the washed-out-and-olive rule at 30° — are still written in DAB's
terms. A stain can score well above and still admit debris on a real section.

## Run it — see GETTING_STARTED

Step-by-step is in **[docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)**. There
are no keys or accounts to configure.

Shortest path: open **<https://imagesl.com>** and drop a slide on it.

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


## Licence, citation and contributing

ImageSL is released under the [Apache License 2.0](LICENSE), which
includes an express patent grant. See [NOTICE](NOTICE). Third-party components
and their licences are listed in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md);
all are permissive, and the PyInstaller bootloader exception means frozen
builds carry no copyleft obligation.

If you use ImageSL in research, please cite it — see [CITATION.cff](CITATION.cff).

* [DISCLAIMER.md](DISCLAIMER.md) — intended use and its limits
* [CONTRIBUTING.md](CONTRIBUTING.md) — how to propose changes, and the higher
  bar for anything that moves a measured number
* [SECURITY.md](SECURITY.md) — reporting vulnerabilities, and how update
  integrity is enforced
* [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
