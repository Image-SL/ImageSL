# ImageSL — Architecture

## Overview

One engine, `server/`, runs in two places:

```
  DESKTOP (offline)                     HOSTED (browser)
  ┌──────────────────────────────┐      ┌──────────────────────────────┐
  │ ImageSL.exe / ImageSL.app    │      │  FastAPI (server/app.py)     │
  │  ├─ launcher.py              │      │   ├─ ihc/engine.py           │
  │  │   starts the server on    │      │   ├─ ihc/detect.py           │
  │  │   127.0.0.1:<random>      │      │   └─ web/                    │
  │  └─ the whole server/ tree   │      └──────────────────────────────┘
  │      bundled inside          │                    ▲
  │         ▲                    │           slide uploaded, measured,
  │         └─ native window     │           deleted after 8h idle
  │            onto 127.0.0.1    │
  └──────────────────────────────┘
      nothing leaves the machine
      (except a version check to GitHub)
```

The desktop build is the same code path as the hosted one — the analyzer HTML,
the routes and the engine are identical. The only differences are that
`IMAGESL_DESKTOP=1` makes `/` serve the analyzer rather than the landing page,
and that the server is bound to loopback on an ephemeral port with no access
tokens, because the only possible caller is the window in front of the user.

The trade this makes is deliberate and worth stating plainly: bundling the
engine means the algorithms ship with the download and can be extracted. That
was accepted in exchange for slides never leaving the machine, which is what
the people using this actually need. The source is public regardless.

## Backend components

### `server/ihc/engine.py` — the analysis core

The pixel-level work, in order:

1. **Load** (`load_rgb`) — decodes `.tif/.tiff/.png/.jpg`. For pyramidal or
   multi-page histology TIFFs it uses `tifffile`, picks a pyramid level, and
   downsamples to a bounded long edge (default 2048 px) so multi-gigabyte
   slides stay within the container's RAM and analyze in well under a second.
2. **Optical density** (`rgb_to_od`) — Beer–Lambert transform, the physically
   correct space for stain math.
3. **White point** (`estimate_white_point`, `white_is_glass`) — the brightest
   few percent of a scan is bare glass, so absorbance is measured against *that*
   rather than a theoretical 255. Applied only when the scan is genuinely dim or
   tinted **and** the white forms a population separated from the tissue bulk, so
   a well-exposed slide is untouched and a wall-to-wall-tissue slide is never
   renormalised against its own palest tissue.
4. **Background & tissue** (`segment_tissue`) — the mask every statistic is
   anchored to. Density is measured against the slide's own white point
   **always**, so the mask is a statement about how much light the material
   absorbs rather than about how bright the scan is: brighten the whole image and
   nothing here moves. Glass is then defined physically — it absorbs essentially
   nothing and carries no colour — with a **chroma rescue** so pale-but-coloured
   pixels count as dilute stain, a **texture** test that drops large smooth
   regions (vignetting, haze, defocus, scanner artefacts), and morphological
   cleanup that fills interior lumina and drops isolated specks. A wall-to-wall
   section is recognised explicitly: when the palest class still absorbs, it is
   pale tissue and the whole frame counts, so the mask can never be cut against
   itself.
5. **Stain basis** (`_estimate_basis`) — a curated reference matrix is chosen by
   the chromogen family detected on the slide. Only families in
   `ENABLED_FAMILIES` (currently `H-DAB`) can be selected; when the slide's
   chromogen falls outside them the DAB basis is kept and `chromogen_present`
   comes back `False`, so the app says "no DAB here" instead of quietly
   measuring some other colour.
6. **Deconvolution** (`_deconvolve`) — projects OD onto the stain basis to get
   per-stain concentration maps.
7. **Detection** (`ihc/detect.py`) — area based, with **no global intensity
   threshold**. A foreground-excluded background field is fitted to the optical
   density, and everything after it is measured on the *excess* over that field.
   The window of that fit is the scale that divides staining from tone: slower
   variation is absorbed, more compact variation survives. Three things keep the
   fit honest — lumina and holes are not material and are excluded from it;
   anything far darker than the slide's own tissue is excluded wherever it is,
   so a plaque wider than the window cannot hide inside its own background; and
   the fit is iterated with the foreground removed.
   The excess's colour is read as an absorbance signature (blue-over-red for DAB)
   rather than a hue, because hue collapses as a pixel approaches black — which
   is exactly why the previous build dropped the densest, least ambiguous stain
   and kept its halo. Colour-specific **seeds** are grown through contiguous
   excess into **objects**, and stained objects are separated from background
   bumps by clustering the population of object peaks: a decision about
   structures, not pixels. Each accepted object is then measured at **its own
   isophote**, so area does not inherit intensity.
8. **Quantification** — positive-area %, pixel count, and the number and size of
   the stained structures found. Running the whole decision across a ladder of
   201 sensitivities produces the **level map**: for each pixel, the first level at
   which it turns positive. That image is shipped to the browser as the red
   channel of a small PNG whose green channel carries the tissue mask, so the
   browser has both halves of the percentage — the positive pixels and the tissue
   they are measured against — and reproduces the server's object-based decision,
   at any sensitivity and under any region, by comparison alone. Shipping the
   tissue mask is what lets a region move the denominator on screen; without it
   the preview divided by the whole slide's tissue and disagreed with the CSV.
8b. **Manual regions** (`ihc/regions.py`) — hand-drawn `focus` and `ignore`
   shapes, in normalised coordinates so they survive any resolution or export
   size. Both move the positive area and the tissue denominator together. The
   rasterisation rule ("a pixel counts when its centre is inside the shape") is
   written out explicitly in both `regions.py` and `app.js` rather than inherited
   from PIL and canvas, whose conventions differ by a row and a column — enough
   to make the on-screen percentage disagree with the exported one.
9. **Rendering** — `render_overlay` highlights counted pixels (always in the one
   fixed neon green, `engine.OVERLAY_GREEN`); `render_stain_only` erases everything
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

### `server/app.py` — the API

| Route | Purpose |
| --- | --- |
| `GET /` | Landing page (the analyzer itself when `IMAGESL_DESKTOP=1`) |
| `GET /app` | In-browser analyzer console |
| `GET /privacy`, `GET /terms` | Policy pages |
| `GET /api/downloads` | What installers exist right now, so the landing page can grey out what it cannot offer |
| `GET,HEAD /download/{platform}` | Sends the installer, or 302s to object storage — see `IMAGESL_DOWNLOAD_*` |
| `POST /api/analyze` | Upload → full analysis + overlay; returns an `analysis_id` |
| `POST /api/appearance` | Re-measure at a new sensitivity / region set from the cached maps |
| `POST /api/rehydrate` | Restore an analysis the cache has dropped |
| `GET,POST /api/download_tif` | One panel at full analysis resolution |
| `POST /api/export_csv`, `POST /api/export_zip` | Batch measurements and images |
| `POST /api/keepalive` | Page heartbeat, so an open batch is not aged out mid-review |
| `GET /api/stains` | The enabled stain list |
| `GET /api/health` | Status and version |

An in-process TTL/LRU cache holds each upload's concentration maps keyed by
`analysis_id`, so the sliders don't re-upload or recompute deconvolution. It is
per-instance memory, which is why the Lightsail service must stay at `SCALE=1`:
with two nodes a slide analysed on one is invisible to the other and every
second export comes back half empty.

**Access control:** if `IMAGESL_ACCESS_TOKENS` is set, every `/api/*` call must
carry a matching `X-ImageSL-Key` header. The desktop client passes the user's
license key; the web app reads it from `?key=` and stores it locally.

## Desktop client

`client/imagesl_client.py` is a `pywebview` shell (~1 small file). It stores the
license key under `%APPDATA%/ImageSL/config.json`, then loads
`<backend>/app?key=<license>`. No analysis, no secrets, nothing proprietary.
Built to a single `.exe` with `scripts/build_client.ps1`.

## Data flow for one analysis

1. Browser `POST /api/analyze` with the slide.
2. Backend decodes + downsamples to the working resolution.
3. `engine.analyze()` runs deconvolution + Otsu, caches the maps, returns
   metrics + overlay + original + `analysis_id`.
4. User moves the sensitivity slider or draws a region → the browser re-measures
   from the level map immediately, and `POST /api/appearance` recomputes the
   authoritative numbers behind it from the same cached maps.

## Scaling notes / future work

- Replace the in-process cache with Redis for multi-instance deploys.
- Add true whole-slide tiling (OpenSlide) for `.svs` beyond the downsample cap.
- Optional: a trained nucleus/segmentation model (e.g. StarDist) as an
  alternative to Otsu for cell-level counts — a larger, separate effort with its
  own training data and validation.
