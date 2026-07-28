"""
ImageSL — area / object based chromogen detection.

This module replaces the old "one global intensity threshold" decision. Nothing
here asks *"is this pixel darker than 0.43?"*. It asks, in order:

  1. **What extra absorbance does this pixel carry over its own neighbourhood?**
     A large-window, foreground-excluded background field is fitted to the
     optical density itself, so a diffuse tan wash, a lamp gradient, a warm
     scanner tint or genuinely dark tissue is subtracted away *locally*. Every
     later decision is made on that excess, never on the raw density — which is
     what makes the engine scale-consistent: a heavily stained slide is not
     penalised by its own high background, and a pale one is not flooded.

  2. **What colour is the excess?** The excess OD vector is compared against
     reference directions (DAB, haematoxylin, red/RBC, neutral debris). DAB has
     a distinctive blue-over-red absorbance signature that survives at any
     density, unlike HSV hue, which becomes meaningless as a pixel approaches
     black — the exact reason the previous build dropped the darkest, most
     unambiguous stain and kept its halo.

  3. **Which connected AREAS does that evidence form?** Colour-specific *seeds*
     are grown through contiguous excess-carrying pixels (hysteresis), and the
     grown component — the object — is what gets accepted or rejected, by its
     own area, peak evidence, colour purity and how much of it is genuinely
     chromogen-directional. A stained structure is therefore measured whole,
     including the black-saturated core that carries no usable hue on its own,
     while an isolated grey fold or a dust speck with no chromogen-coloured seed
     is rejected however dark it is.

  4. **Where is that area stable?** Sensitivity is not a magic constant. The
     detector is run over a ladder of evidence levels and the operating point is
     chosen by MSER-style stability: the level at which the detected area
     changes least for a change in sensitivity. Real structures persist across
     many levels; noise and background wash explode.

The ladder also produces the `level map`: for every pixel, the *first* level at
which it becomes positive (255 = never). One 8-bit image therefore encodes the
full family of results, so the browser can move the sensitivity slider — and
apply per-region sensitivity offsets from the manual tools — live, with no
server round-trip, and still reproduce the server's object-based decision
exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from skimage.measure import label as _label

try:
    from scipy.ndimage import uniform_filter as _uniform_filter
    from scipy.ndimage import gaussian_filter as _gaussian
    _HAVE_SCIPY = True
except Exception:  # pragma: no cover - scipy ships with scikit-image
    _HAVE_SCIPY = False
    _gaussian = None


# --------------------------------------------------------------------------- #
# Reference absorbance directions (unit OD vectors)
# --------------------------------------------------------------------------- #
# Used to answer "what colour is the EXCESS absorbance here", which is a very
# different question from "what colour is this pixel" and is the one that stays
# answerable on dark pixels.

DAB_OD     = np.array([0.270, 0.570, 0.780])   # brown  — blue >> red
HEMA_OD    = np.array([0.650, 0.700, 0.290])   # blue   — red > blue
RED_OD     = np.array([0.070, 0.990, 0.110])   # eosin / RBC / red chromogen
NEUTRAL_OD = np.array([0.577, 0.577, 0.577])   # ink, dust, folds, shadow


# --------------------------------------------------------------------------- #
# Tuning — every constant is dimensionless (a ratio, or a multiple of this
# slide's own noise), so nothing here encodes an absolute brightness.
# --------------------------------------------------------------------------- #

BG_WIN_FRAC      = 0.13   # background-field window as a fraction of the long edge
BG_WIN_MIN       = 33     # ... never smaller than this (px)
BG_COARSE_MULT   = 4      # second, coarser scale used where the fine one is starved
BG_ITERS         = 2      # foreground-exclusion refinement passes
BG_FG_MAX_FRAC   = 0.55   # never exclude more than this share of tissue as foreground

# Colour specificity of the EXCESS absorbance. `brownness` = (od_B − od_R)/‖od‖:
# +0.51 for pure DAB, −0.36 for haematoxylin, +0.03 for eosin/red, 0.00 for
# neutral debris. A dark, channel-crushed DAB core drifts toward 0, which is
# exactly why it is allowed in by CONNECTIVITY (growth) instead of by colour.
BROWN_SEED       = 0.22   # a seed must be unmistakably DAB-directional
BROWN_GROW       = -0.05  # growth only stops at clearly non-brown excess (blue nuclei)
OBJ_BROWN_MEAN   = 0.14   # the object must be brown ON AVERAGE — what separates dark
                          # stain (brown rim, crushed core) from dark debris (neutral
                          # throughout). A mean is used rather than "what share of the
                          # pixels clear a colour bar", because a share is a step
                          # function of every pixel it counts: JPEG chroma subsampling
                          # nudges pixels across that bar and whole structures then
                          # appear or vanish, while the mean barely moves.
OBJ_SEED_FRAC    = 0.06   # ... and the chromogen colour must not be one stray pixel
OBJ_SEED_MIN_PX  = 2      # ... on at least two pixels

# Mild spatial smoothing of the excess before objects are formed. Genuine
# structures are several pixels across; single-pixel spikes are compression
# artefacts and sensor noise. Smoothing first is what keeps a measurement stable
# when the same slide arrives as a JPEG. The radius is expressed against a
# 1024-pixel working edge and rescaled with the image, so the same physical
# amount of blur is applied whatever resolution the slide is analysed at —
# a fixed pixel radius would smooth a downscaled copy twice as hard and shift
# its answer.
SMOOTH_SIGMA     = 1.0
SMOOTH_REF_EDGE  = 1024.0
# The COLOUR of the excess is smoothed harder than its magnitude. A chromogen's
# hue is a property of the structure and varies slowly across it, while its
# density varies pixel to pixel — and every image codec in existence exploits
# exactly that by storing chroma at reduced resolution. Reading colour at the
# scale it actually lives at costs nothing and stops a re-encoded copy of the
# same slide from losing structures to chroma subsampling.
CHROMA_SMOOTH    = 2.2

# Candidate region. The floor is the weakest excess absorbance that can belong
# to an object at all. `sigma` here is not sensor noise — it is how much this
# slide's own unstained tissue varies along the chromogen axis, i.e. its
# texture. Scaling the floor by it is what makes a coarse, mottled section and a
# smooth one behave the same.
NOISE_MULT       = 1.6
ABS_MIN_EXCESS   = 0.025

# Object detection bar. Objects are split from background by clustering the
# population of OBJECT PEAKS (Otsu in log space) — a decision about structures,
# not about pixels, and the reason the operating point transfers across slides
# with wildly different staining density. The clamps stop a slide with no
# staining at all (a unimodal noise population, where the split is arbitrary)
# from inventing a bar low enough to "detect" its own texture.
PEAK_BAR_ABS_MIN = 0.09   # excess OD: below this a blob is not visibly stained
PEAK_BAR_SIG_MIN = 2.6    # ... and never below this many texture sigmas
PEAK_BAR_MAX     = 1.20
PEAK_BIMODAL_MIN = 0.22   # separability below this ⇒ no stain population at all
# How much to trust the split. When the object peaks really are two clusters,
# the split is a strong, meaningful statement and is used as-is. When they are a
# continuum, it is not: the position that best separates the population is then
# barely better than its neighbours, and the same slide re-encoded or resized
# can land the split at either end — one reading 0.2% and the other 6%. The
# distribution's own quantiles, by contrast, are almost unchanged between those
# two copies. So the bar is blended between the two by exactly how bimodal the
# population is, and no slide sits on a cliff edge.
HIST_SMOOTH_BINS = 3.0    # blur (in histogram bins) applied before the split
# Below this separability the peaks are one continuous cloud, not two groups.
# A separation criterion applied to a population that does not separate has no
# preferred answer: two nearly-equal splits sit at opposite ends of the
# distribution and the same slide, resized or re-encoded, lands on either — one
# copy reading 0.3% and the other 6%. Where that is the case the split is
# dropped in favour of a fixed quantile of the population, which barely moves
# between those copies, and the user is told the boundary is a judgement.
#
# The handover is gradual rather than a switch at one separability value: a hard
# switch is itself a cliff, and a slide sitting on it flips between the two rules
# for no better reason than being resized.
PEAK_DIFFUSE_LO  = 0.50   # at or below: the split is ignored entirely
PEAK_DIFFUSE_HI  = 0.58   # at or above: the split is used as-is
BAR_DIFFUSE_PCT  = 78.0
# Only well-resolved objects get a VOTE on where the bar goes. A three-pixel
# blob's peak is mostly a statement about the sampling grid, and there are
# thousands of them; letting them decide makes the operating point jump when the
# same slide arrives as a JPEG or at another resolution. They are still detected
# once the bar is set — they just do not choose it.
BAR_MIN_AREA_FRAC = 3.5e-5
# Some slides carry three populations, not two — dense puncta, a moderate zone,
# and background — and a two-way split then has two nearly equally good answers
# it can flip between. Averaging the whole near-optimal band of splits lands
# between them and moves smoothly instead of jumping.
OTSU_PLATEAU     = 0.90

# Object extent. The AREA of an accepted structure is the isophote at half its
# OWN peak absorbance — the classic full-width-half-maximum edge — floored so it
# can never run out into the noise.
#
# Measuring each object against itself rather than against a shared cut is what
# keeps *area* from being contaminated by *intensity*: two structures of the
# same physical size measure the same area whether one is twice as dark as the
# other. It also decouples area from the detection bar, so moving the
# sensitivity changes WHICH structures are counted without silently resizing
# every structure already counted — which is what made the measurement lurch
# when the same slide arrived compressed or at another resolution.
EXTENT_PEAK_FRAC = 0.34
EXTENT_FLOOR_MULT = 1.0   # ... never below this × the candidate floor

# Object gates (level-independent).
MIN_AREA_FRAC    = 6e-6   # ≈5 px at 1024×768; below this it is sensor noise

# Sensitivity ladder — pure relative scaling of the detection bar around the
# automatic operating point, so the slider means the same thing on every slide.
N_LEVELS         = 25
AUTO_LEVEL       = 12     # centre of the ladder = the automatic bar
LADDER_STRICT    = 4.0    # level 0  = 4× stricter than auto
LADDER_LOOSE     = 0.25   # level 24 = 4× more permissive than auto


@dataclass
class Detection:
    """Everything the caller needs, at every sensitivity, in one object."""
    positive: np.ndarray                 # bool mask at the selected level
    level_map: np.ndarray                # uint8 — first level at which a px is positive
    levels: list                         # detection bar (excess OD) per level
    level: int                           # selected level index
    auto_level: int                      # automatic operating point (ladder centre)
    areas: list                          # positive px per level
    evidence: np.ndarray                 # float32 chromogen excess in noise sigmas
    excess: np.ndarray                   # float32 chromogen excess (OD units)
    brownness: np.ndarray                # float32 colour of that excess
    background_od: np.ndarray            # float32 HxWx3 fitted background OD
    sigma: float                         # robust noise of the excess (OD units)
    bar: float = 0.0                     # automatic detection bar (excess OD)
    floor: float = 0.0                   # weakest excess that can belong to an object
    separability: float = 0.0            # how cleanly the object peaks split in two
    objects: int = 0                     # accepted objects at the selected level
    object_areas: list = field(default_factory=list)
    notes: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Background field
# --------------------------------------------------------------------------- #

def _masked_mean(x: np.ndarray, m: np.ndarray, win: int) -> tuple[np.ndarray, np.ndarray]:
    """Normalised box mean of `x` over the pixels in `m` (0 elsewhere)."""
    mf = m.astype(np.float32)
    num = _uniform_filter(np.where(m, x, 0.0).astype(np.float32), win)
    den = _uniform_filter(mf, win)
    return num, den


def _field(x: np.ndarray, keep: np.ndarray, win: int, fallback: float) -> np.ndarray:
    """Smooth `x` over `keep` only, at two scales, so the estimate survives even
    where a whole neighbourhood is foreground."""
    num, den = _masked_mean(x, keep, win)
    cnum, cden = _masked_mean(x, keep, win * BG_COARSE_MULT)
    fine_ok = den > 0.06
    coarse_ok = cden > 0.02
    out = np.full(x.shape, float(fallback), dtype=np.float32)
    np.divide(cnum, np.maximum(cden, 1e-6), out=out, where=coarse_ok)
    fine = np.divide(num, np.maximum(den, 1e-6), where=fine_ok, out=np.zeros_like(out))
    out = np.where(fine_ok, fine, out)
    return out.astype(np.float32)


def background_od_field(od: np.ndarray, tissue: np.ndarray, win: int) -> tuple[np.ndarray, np.ndarray]:
    """Fit the slide's own *local* background absorbance, excluding stain.

    Returns (background OD field HxWx3, background pixel mask).

    Why a field and not a number: real slides carry a diffuse chromogen wash, a
    counterstain gradient, uneven illumination and tissue that is genuinely
    denser in places. A single global background makes every one of those look
    like signal in one region and hides signal in another. Fitting the
    background *where there is no stain* and interpolating across the stain is
    what makes the excess comparable everywhere on the slide.
    """
    h, w = od.shape[:2]
    dab_dir = DAB_OD / np.linalg.norm(DAB_OD)
    proj = (od @ dab_dir).astype(np.float32)          # absorbance along the DAB axis

    keep = tissue.copy()
    if not keep.any():
        keep = np.ones((h, w), dtype=bool)
    bg_od = np.empty_like(od, dtype=np.float32)

    for it in range(BG_ITERS + 1):
        fallbacks = [float(np.median(od[..., c][keep])) if keep.any() else 0.0 for c in range(3)]
        for c in range(3):
            bg_od[..., c] = _field(od[..., c], keep, win, fallbacks[c])
        if it == BG_ITERS:
            break
        exc = proj - (bg_od @ dab_dir)
        base = exc[tissue] if tissue.any() else exc.ravel()
        sig = 1.4826 * float(np.median(np.abs(base - np.median(base)))) + 1e-6
        fg = exc > 3.0 * sig
        # never let foreground exclusion run away on a densely stained slide
        if float(fg.mean()) > BG_FG_MAX_FRAC:
            cut = float(np.percentile(exc, 100.0 * (1.0 - BG_FG_MAX_FRAC)))
            fg = exc > cut
        keep = tissue & ~fg
        if float(keep.mean()) < 0.05:      # pathological: keep the whole tissue
            keep = tissue.copy()
    return bg_od, keep


# --------------------------------------------------------------------------- #
# Colour specificity of the excess
# --------------------------------------------------------------------------- #

def _unit(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) or 1.0)


def excess_colour(od_exc: np.ndarray, target_od: Optional[np.ndarray] = None
                  ) -> tuple[np.ndarray, np.ndarray]:
    """Describe the EXTRA absorbance at each pixel.

    Returns (magnitude, brownness):

      * magnitude — ‖excess OD‖, how much extra light this pixel absorbs.
      * brownness — the chromogen's own absorbance signature, projected out of
                    the excess and normalised: for DAB the blue-over-red split
                    (B − R)/‖e‖, which is +0.51 for pure DAB, −0.36 for
                    haematoxylin, +0.03 for eosin / red blood cells and 0.00 for
                    neutral debris (ink, dust, folds, shadow).

    Why not hue: HSV hue is computed from ratios that collapse as a pixel
    approaches black, so the densest — least ambiguous — chromogen reads as
    almost any colour, and a hue window then drops precisely the pixels a human
    would call obviously positive. A channel-ORDER contrast survives that
    crushing; it merely weakens toward 0, which the object logic handles by
    connectivity rather than by pretending the colour is still measurable.
    """
    mag = np.linalg.norm(od_exc, axis=2).astype(np.float32)
    safe = np.maximum(mag, 1e-6)
    if target_od is None:
        contrast = od_exc[..., 2] - od_exc[..., 0]
    else:
        # Generic chromogen: contrast along the direction that separates this
        # chromogen from neutral absorbance.
        d = _unit(np.asarray(target_od, dtype=np.float64))
        axis = d - _unit(NEUTRAL_OD) * float(d @ _unit(NEUTRAL_OD))
        axis = _unit(axis) * np.sqrt(2.0)     # scale to match the DAB B−R form
        contrast = od_exc @ axis.astype(np.float32)
    return mag, (contrast / safe).astype(np.float32)


# --------------------------------------------------------------------------- #
# The detector
# --------------------------------------------------------------------------- #

def _otsu_log(values: np.ndarray) -> tuple[float, float]:
    """Otsu split of a positive population in log space.

    Returns (threshold, separability). Separability is the between-class share
    of the total variance: high when the population really is two populations
    (background bumps vs stained structures), low when it is one smooth cloud —
    which is how a slide with no staining announces itself.
    """
    v = np.log10(np.maximum(values, 1e-4))
    if v.size < 12:
        return float("nan"), 0.0
    hist, edges = np.histogram(v, bins=96)
    centres = 0.5 * (edges[:-1] + edges[1:])
    w = hist.astype(np.float64)
    # Smooth the population before splitting it. Sampling noise puts small extra
    # peaks in the histogram, and each one is a candidate split that a two-class
    # criterion can jump to; the same slide re-encoded or resized then lands on a
    # different one. Blurring at the scale of that noise leaves the real modes
    # exactly where they are and takes the spurious ones away.
    if _gaussian is not None:
        w = _gaussian(w, HIST_SMOOTH_BINS, mode="nearest")
    total = w.sum()
    if total <= 0:
        return float("nan"), 0.0
    p = w / total
    omega = np.cumsum(p)
    mu = np.cumsum(p * centres)
    mu_t = mu[-1]
    denom = omega * (1.0 - omega)
    with np.errstate(invalid="ignore", divide="ignore"):
        sigma_b = np.where(denom > 1e-12, (mu_t * omega - mu) ** 2 / np.maximum(denom, 1e-12), 0.0)
    # The between-class variance is usually flat near its maximum, so taking the
    # single best bin lets the split hop several bins for a change as small as a
    # JPEG re-encode. Averaging over the near-optimal plateau gives the same
    # answer with none of that jitter. (A soft two-component fit was tried here
    # and is worse: on a population that is a continuum rather than two clean
    # clusters, the fitted boundary wanders far outside the plateau.)
    best = float(sigma_b.max())
    plateau = sigma_b >= best * OTSU_PLATEAU
    k = int(np.argmax(sigma_b))
    centre = float((centres[plateau] * sigma_b[plateau]).sum() / max(sigma_b[plateau].sum(), 1e-12))
    var_total = float(((centres - mu_t) ** 2 * p).sum())
    sep = float(sigma_b[k] / var_total) if var_total > 1e-12 else 0.0
    return float(10.0 ** centre), sep


def _area_weighted(peaks: np.ndarray, areas: np.ndarray, cap: int = 400) -> np.ndarray:
    """Repeat each object's peak in proportion to the area it contributes, so the
    split is decided by where the stained AREA is, not by how many specks exist."""
    if peaks.size == 0:
        return peaks
    reps = np.clip((areas // 2).astype(np.int64), 1, cap)
    return np.repeat(peaks, reps)


def _ladder(bar: float) -> np.ndarray:
    """Sensitivity ladder: the detection bar scaled around the automatic one.

    Level AUTO_LEVEL is the bar the object population itself chose; lower
    indices are stricter, higher ones more permissive. Because it is a pure
    relative scaling, one slider position means the same *degree of
    conservatism* on a faintly and a densely stained slide alike.
    """
    lo = np.geomspace(LADDER_STRICT, 1.0, AUTO_LEVEL + 1)
    hi = np.geomspace(1.0, LADDER_LOOSE, N_LEVELS - AUTO_LEVEL)
    return (np.concatenate([lo[:-1], hi]) * float(bar)).astype(np.float32)


def _object_stats(lbl: np.ndarray, n: int, signal: np.ndarray,
                  brown: np.ndarray, seed: np.ndarray) -> dict:
    """Per-object measurements, all from three bincounts over the label image."""
    flat = lbl.ravel()
    area = np.bincount(flat, minlength=n + 1).astype(np.int64)
    peak = np.zeros(n + 1, dtype=np.float32)
    np.maximum.at(peak, flat, signal.ravel())
    seeds = np.bincount(flat, weights=seed.ravel().astype(np.float64), minlength=n + 1)
    brown_sum = np.bincount(flat, weights=brown.ravel().astype(np.float64), minlength=n + 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        brown_mean = brown_sum / np.maximum(area, 1)
    return {"area": area, "peak": peak, "seeds": seeds,
            "seed_frac": seeds / np.maximum(area, 1), "brown_mean": brown_mean}


def detect(
    od: np.ndarray,
    tissue: np.ndarray,
    *,
    level: Optional[int] = None,
    min_area_px: Optional[int] = None,
    hue_band_mask: Optional[np.ndarray] = None,
    target_od: Optional[np.ndarray] = None,
) -> Detection:
    """Area-based chromogen detection.

    `od`        HxWx3 optical density (already white-point corrected).
    `tissue`    bool mask of real tissue — sets what can be measured at all.
    `level`     sensitivity level index; None → the automatic operating point.
    `target_od` overrides the chromogen direction (stain-selection mode).

    The decision has three separable parts, and none of them is a global
    brightness cut:

      1. *Where is the background?*  A foreground-excluded OD field, fitted per
         slide and per neighbourhood.
      2. *Which blobs are structures?*  Connected areas of excess absorbance,
         clustered by their peak into "background bump" and "stained object".
      3. *How big is each structure?*  Its own isophote, at a fixed fraction of
         the detection bar, inside its own footprint.
    """
    h, w = od.shape[:2]
    notes: list[str] = []
    frame = h * w
    if min_area_px is None:
        min_area_px = int(max(4, round(frame * MIN_AREA_FRAC)))
    dab_vec = np.asarray(target_od, dtype=np.float64) if target_od is not None else DAB_OD

    # ---- 1. local background and the excess absorbance it leaves ---------- #
    win = int(max(BG_WIN_MIN, round(max(h, w) * BG_WIN_FRAC)) | 1)
    bg_od, bg_mask = background_od_field(od, tissue, win)
    # NOT clipped per channel: clipping negatives first would turn a pixel that
    # is merely a shade bluer than its background into a "pure brown" excess, and
    # the whole slide's texture noise then reads as maximally chromogen-coloured.
    od_exc = (od - bg_od).astype(np.float32)
    rel_scale = max(h, w) / SMOOTH_REF_EDGE
    smooth = SMOOTH_SIGMA * rel_scale
    if smooth > 0.15 and _gaussian is not None:
        od_exc = _gaussian(od_exc, (smooth, smooth, 0), mode="nearest")
    chroma_src = od_exc
    csmooth = CHROMA_SMOOTH * rel_scale
    if csmooth > smooth and _gaussian is not None:
        extra = float(np.sqrt(max(csmooth ** 2 - smooth ** 2, 0.0)))
        chroma_src = _gaussian(od_exc, (extra, extra, 0), mode="nearest")
    _, brown = excess_colour(chroma_src, None if target_od is None else dab_vec)

    dab_dir = _unit(dab_vec).astype(np.float32)
    signal = (od_exc @ dab_dir).astype(np.float32)      # excess along the chromogen axis

    base = signal[bg_mask] if bg_mask.any() else signal.ravel()
    if base.size > 200_000:
        base = base[:: base.size // 200_000 + 1]
    med = float(np.median(base))
    sigma = max(1.4826 * float(np.median(np.abs(base - med))) + 1e-6, 1e-4)

    signal = np.clip(signal - med, 0.0, None).astype(np.float32)
    signal[~tissue] = 0.0
    evidence = (signal / sigma).astype(np.float32)

    # ---- 2. candidate objects -------------------------------------------- #
    floor = float(max(NOISE_MULT * sigma, ABS_MIN_EXCESS))
    seed_px = tissue & (brown >= BROWN_SEED) & (signal >= floor)
    region = tissue & (brown >= BROWN_GROW) & (signal >= floor)
    if hue_band_mask is not None:
        seed_px &= hue_band_mask

    bar_min = float(max(PEAK_BAR_ABS_MIN, PEAK_BAR_SIG_MIN * sigma))
    lbl = _label(region, connectivity=2)
    n = int(lbl.max())
    levels = _ladder(bar_min)
    level_map = np.full((h, w), 255, dtype=np.uint8)
    bar = bar_min
    separability = 0.0

    if n:
        st = _object_stats(lbl, n, signal, brown, seed_px)
        eligible = ((st["area"] >= min_area_px) & (st["seeds"] >= OBJ_SEED_MIN_PX)
                    & (st["seed_frac"] >= OBJ_SEED_FRAC) & (st["brown_mean"] >= OBJ_BROWN_MEAN))
        eligible[0] = False

        # The detection bar comes from the population of object peaks, not from
        # any pixel statistic: two clusters ⇒ "bumps" and "structures".
        # Weighted by area: a stained slide's AREA lives in a few strong
        # structures, while its noise lives in thousands of tiny blobs that
        # would otherwise dominate an unweighted population and drag the split
        # down into the texture.
        voters = eligible & (st["area"] >= max(min_area_px, int(frame * BAR_MIN_AREA_FRAC)))
        if int(voters.sum()) < 12:
            voters = eligible
        peaks = _area_weighted(st["peak"][voters], st["area"][voters])
        if peaks.size >= 12:
            t, separability = _otsu_log(peaks)
            if np.isfinite(t) and separability >= PEAK_BIMODAL_MIN:
                span = max(PEAK_DIFFUSE_HI - PEAK_DIFFUSE_LO, 1e-6)
                trust = float(np.clip((separability - PEAK_DIFFUSE_LO) / span, 0.0, 1.0))
                if trust < 1.0:
                    q = float(np.percentile(peaks, BAR_DIFFUSE_PCT))
                    t = float(np.exp(trust * np.log(max(t, 1e-4))
                                     + (1.0 - trust) * np.log(max(q, 1e-4))))
                bar = float(np.clip(t, bar_min, PEAK_BAR_MAX))
                if trust < 0.5:
                    notes.append(
                        "Staining on this slide is diffuse rather than focal — the "
                        "stained material does not form a population of structures "
                        "clearly separate from the background, so where the boundary "
                        "falls is a judgement rather than a measurement. Treat the "
                        "percentage as a relative reading, compare only within a "
                        "staining batch, and use the sensitivity control to set the "
                        "boundary where you want it.")
            else:
                bar = bar_min
                notes.append("Staining is diffuse rather than focal on this slide — "
                             "no separate population of stained structures was found, "
                             "so only clearly absorbing material is counted.")
        levels = _ladder(bar)

        # ---- 3. level map, analytically ---------------------------------- #
        # The ladder descends, so "this object qualifies" is monotone in the
        # level index: the first level at which an object appears is found by a
        # single searchsorted. Its extent does not depend on the level at all, so
        # a pixel is simply in or out of its object's isophote.
        desc = levels.astype(np.float64)
        obj_level = np.full(n + 1, 255, dtype=np.int32)
        idx = np.searchsorted(-desc, -st["peak"][eligible], side="left")
        obj_level[np.flatnonzero(eligible)] = np.clip(idx, 0, N_LEVELS - 1)
        obj_level[~eligible] = 255
        obj_level[0] = 255

        extent = np.maximum(st["peak"] * EXTENT_PEAK_FRAC, floor * EXTENT_FLOOR_MULT)
        flat_lbl = lbl.ravel()
        inside = signal.ravel() >= extent[flat_lbl]

        lv = obj_level[flat_lbl]
        lv[~inside] = 255
        lv[flat_lbl == 0] = 255
        level_map = lv.astype(np.uint8).reshape(h, w)

    sel = int(np.clip(AUTO_LEVEL if level is None else level, 0, N_LEVELS - 1))
    positive = level_map <= sel

    areas = [int((level_map <= i).sum()) for i in range(N_LEVELS)]
    olbl = _label(positive, connectivity=2)
    obj_areas = np.bincount(olbl.ravel())[1:] if olbl.max() else np.array([], dtype=np.int64)

    if not positive.any():
        notes.append("No chromogen-specific staining was found above this slide's own background.")

    return Detection(
        positive=positive,
        level_map=level_map,
        levels=[round(float(x), 4) for x in levels],
        level=sel,
        auto_level=AUTO_LEVEL,
        areas=areas,
        evidence=evidence,
        excess=signal,
        brownness=brown,
        background_od=bg_od,
        sigma=float(sigma),
        bar=float(bar),
        floor=float(floor),
        separability=float(separability),
        objects=int(obj_areas.size),
        object_areas=[int(a) for a in obj_areas],
        notes=notes,
    )
