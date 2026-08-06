"""
ImageSL — IHC / histology stain-analysis engine.

Everything here is pure, vectorised numpy + scikit-image so a downsampled slide
processes in a fraction of a second. The browser is the only client; this module
is the whole brain.

Design goals (2026 rewrite):

*  Ships DAB-only for now (see ENABLED_FAMILIES) while keeping the full
   any-chromogen machinery intact: the stain basis is chosen from a curated
   reference matched to the chromogen family detected on the slide itself, and
   turning another family back on is a one-line change. Auto-detect still runs —
   it now reports whether a brown DAB chromogen is actually present rather than
   silently switching to a different chromogen's basis.

*  Detection is AREA BASED and lives in `ihc/detect.py`. There is no global
   intensity threshold anywhere in this build. The detector fits each slide's own
   local background absorbance, asks what colour the *excess* over that
   background is, groups the answer into connected structures, and separates
   stained objects from background by clustering the population of object peaks.
   Each accepted structure is then measured at its own isophote. See that
   module's header for why every step is the way it is.

*  Finds the real background. `segment_tissue()` estimates each slide's own
   white point (bare glass) and cuts glass from tissue by a physical test —
   glass absorbs essentially nothing — measured against that white point, so the
   mask does not move when the illumination does. A pale, wall-to-wall section is
   explicitly recognised instead of being cut against itself.

*  Rejects junk. Photos of a bike / person / screenshot never reach the pixel
   math — `assess_slide()` gates them out with a human-readable reason, and the
   caller reports them as "skipped".

*  Hands control back where the data is genuinely ambiguous. The sensitivity
   ladder, the level map and `ihc/regions.py` let the user move the operating
   point globally or in a hand-drawn region, and the slide says so in `notes`
   when its staining is diffuse enough that the boundary is a judgement.

*  Highlights detections in ONE fixed colour (neon green) on every slide, and
   reports natural display colours for each stain so the UI can show
   recolourable Stain-A / Stain-B panels.

*  Can erase the background entirely — `render_stain_only()` returns the slide
   with everything except the detected chromogen replaced by a flat field.
"""

from __future__ import annotations

import base64
import io
import math
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:  # tifffile + imagecodecs handle compressed / pyramidal histology TIFs
    import tifffile
    _HAVE_TIFFFILE = True
except Exception:  # pragma: no cover - tifffile is a hard dep in prod
    _HAVE_TIFFFILE = False

from skimage.morphology import disk
from skimage.morphology import erosion as _grey_erosion
from skimage.measure import label as _label
from skimage.filters import threshold_otsu as _otsu

try:  # scipy ships with scikit-image; the texture gate degrades gracefully.
    from scipy.ndimage import uniform_filter as _uniform_filter
    _HAVE_SCIPY = True
except Exception:  # pragma: no cover
    _HAVE_SCIPY = False

from . import detect
from . import regions as regions_mod


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_MAX_EDGE = 1024
HARD_MAX_EDGE = 2048

# Absolute floor for "this pixel absorbs light at all". `segment_tissue()` picks
# the REAL glass-vs-tissue cut per slide and never goes below this floor, so a
# blank scan can't be talked into having tissue.
BACKGROUND_OD_THRESHOLD = 0.15

# ---- background / tissue segmentation tuning ----------------------------- #
_WHITE_PCT          = 99.0   # brightest percentile of a scan ≈ bare glass
_WHITE_MIN          = 48.0   # never trust an absurdly dark "white point"
_WHITE_APPLY_BELOW  = 245.0  # only renormalise OD when the scan really is dim/tinted
_WHITE_GLASS_SEP    = 20.0   # ... and only if that white is separated from the tissue bulk
_TISSUE_THR_MAX     = 0.55   # clamp the per-slide Otsu cut (faint tissue must survive)
_TISSUE_BG_MIN_FRAC = 0.02   # < 2% glass ⇒ full-field slide, Otsu would split tissue
_BG_GLASS_OD_MAX    = 0.075  # bare glass absorbs essentially NOTHING (summed OD)
_GLASS_MAX_FRAC     = 0.92   # a slide this empty is either blank or a faint section
_GLASS_CONTRAST_K   = 4.0    # ... told apart by whether anything on it absorbs strongly
_BG_GLASS_SAT       = 0.20   # ... and carries essentially no colour
_CHROMA_RESCUE_SAT  = 0.22   # a pale but distinctly coloured pixel is dilute stain
_TEXTURE_WIN        = 7      # local-std window (px) for the flat-region test
_TEXTURE_REL        = 0.35   # "smooth" = local std below this × the tissue median
_TEXTURE_ABS        = 0.006  # ... but never call anything above this smooth
_TEXTURE_OD_GUARD   = 1.6    # only smooth pixels weaker than thr×this are dropped
_TEXTURE_FLAT_P95   = 0.015  # below this 95th-pct local std, nothing cellular is present
_HOLE_FRAC          = 0.02   # interior gaps up to 2% of frame are lumina → tissue
_SPECK_FRAC         = 2e-5   # isolated blobs below this share of frame are debris

# Positive detection itself lives in ihc/detect.py — it is area based and has no
# global intensity threshold to tune. What remains here is the stain basis, the
# tissue mask and the reporting.

_HDAB_REFERENCE = np.array(
    [
        [0.65, 0.70, 0.29],   # Haematoxylin (nuclei, blue/purple)
        [0.27, 0.57, 0.78],   # DAB (target, brown)
        [0.00, 0.00, 0.00],   # residual, filled by cross product
    ],
    dtype=np.float64,
)

# Curated reference stain pairs [counterstain, chromogen] in OD space. Using a
# proven fixed basis per detected family is FAR more robust than per-slide
# Macenko (which goes degenerate on single-chromogen slides), while still
# covering every common chromogen. `hue` is the chromogen's display hue (deg).
# `band` is the hue window (deg) a pixel must fall inside to be that chromogen at
# all. It is deliberately tighter than the generic ±_HUE_TOL_DEG tolerance: being
# "within 46° of brown" is not the same as being brown, and on a DAB-only build
# that difference is exactly what keeps a red or magenta chromogen from being
# measured as if it were DAB.
_REF_BASES = {
    "H-DAB":  {"label": "H-DAB (brown)",  "hue": 32.0, "band": (10.0, 78.0),
               "od": [[0.650, 0.700, 0.290], [0.270, 0.570, 0.780]]},
    "H-Red":  {"label": "Red chromogen",  "hue": 2.0,  "band": (330.0, 18.0),
               "od": [[0.650, 0.700, 0.290], [0.210, 0.760, 0.615]]},
    "H-AP":   {"label": "Alk-phos (red)", "hue": 350.0, "band": (325.0, 12.0),
               "od": [[0.650, 0.700, 0.290], [0.190, 0.760, 0.620]]},
    # The target vector here was [0.400, 0.610, 0.680], which is not a green
    # stain: rendered, it is an orange-brown at 11.6 deg, inside H-DAB's own
    # (10, 78) band and 128 deg from the 140 this entry declares. A green
    # chromogen absorbs LEAST in green, so its OD must be high in R and B and low
    # in G -- the opposite arrangement. Taken from the registry's own Vina Green
    # entry (stains.py, _od((40, 120, 80))), it renders at 150 deg, inside the
    # (95, 180) band this entry always claimed.
    #
    # Nothing shipped read this: H-GREEN is not in ENABLED_FAMILIES. It was a
    # trap for whoever enabled it first, who would have found auto-detect routing
    # green slides to a brown basis and never understood why.
    "H-GREEN":{"label": "Green chromogen","hue": 140.0, "band": (95.0, 180.0),
               "od": [[0.650, 0.700, 0.290], [0.800, 0.327, 0.503]]},
    "H&E":    {"label": "H&E (eosin)",    "hue": 330.0, "band": (300.0, 355.0),
               "od": [[0.650, 0.700, 0.290], [0.070, 0.990, 0.110]]},
}

# --------------------------------------------------------------------------- #
# WHICH CHROMOGENS ARE LIVE
# --------------------------------------------------------------------------- #
# ImageSL currently ships DAB-only detection. Every other reference basis above
# stays defined and vetted; auto-detect simply refuses to resolve to one. To add
# a chromogen as it is signed off:
#
#   1. add its key here                    → auto-detect can select it
#   2. add the matching key to
#      ihc/stains.py ENABLED_KEYS          → it appears in the "Select stain" list
#
# Nothing else in the engine or the UI needs to change — but "signed off" is
# doing the load-bearing work in that sentence, and it is not a formality.
#
# STEP 1 DOES NOT CURRENTLY WORK, and adding a key here is not safe until it
# does. `_detect_family` is uncalibrated (see the long note on it): it misreads
# the chromogen hue by tens of degrees, and with H-Red enabled a *DAB* slide
# classifies as red. Today every branch falls back to H-DAB because H-DAB is the
# only entry in this tuple, which is the only reason none of it is reachable.
#
# Step 2 (stains.ENABLED_KEYS, the "Select stain" picker) is a different path and
# does work: it passes the chosen basis to the detector explicitly, bypassing
# detection of the family entirely. Measured on the synthetic grid, a red scene
# analysed with an explicit AEC choice reaches 98.3% recall against 98.6% for DAB
# on DAB — parity — while the same scene on auto-detect gets 88.7%.
#
# So the picker can be opened for a chromogen before auto-detect can find it.
# Measure the family first either way:
#
#   python scripts/synthetic_matrix.py --chromogen H-Red
ENABLED_FAMILIES: tuple[str, ...] = ("H-DAB",)

# Hue window (deg) that counts as "brown DAB" — see _REF_BASES["H-DAB"]["band"].
# Outside it, auto-detect keeps the DAB basis but tells the user no DAB-range
# chromogen was found, instead of silently measuring some other colour.
_DAB_HUE_BAND = (10.0, 78.0)

# The detection-overlay colour. There is exactly ONE — neon green — everywhere:
# no picker, no per-slide auto pick, no second colour. Keep in step with app.js
# OVERLAY_RGB.
OVERLAY_GREEN: tuple[int, int, int] = (57, 255, 20)
OVERLAY_CHOICES: dict[str, tuple[int, int, int]] = {"green": OVERLAY_GREEN}
OVERLAY_DEFAULT_HEX = "#39ff14"


# --------------------------------------------------------------------------- #
# Result containers
# --------------------------------------------------------------------------- #

@dataclass
class StainVector:
    """One estimated stain colour, in RGB 0-255 for display."""
    name: str
    rgb: list[int]
    od: list[float]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AnalysisResult:
    width: int
    height: int
    source_width: int
    source_height: int
    tissue_pixels: int
    positive_pixels: int
    positive_fraction: float
    positive_percent: float
    mean_positive_intensity: float
    threshold: float
    stains: list[StainVector]
    target_index: int
    method: str
    # --- new fields (all have defaults → old callers keep working) ---------- #
    valid: bool = True
    skip_reason: Optional[str] = None
    stain_label: str = ""
    confidence: float = 1.0
    suggested_overlay_hex: str = OVERLAY_DEFAULT_HEX
    # --- background segmentation report ------------------------------------ #
    background_pixels: int = 0
    background_percent: float = 0.0
    tissue_percent: float = 0.0
    tissue_threshold: float = 0.0     # per-slide glass-vs-tissue OD cut actually used
    tissue_method: str = ""           # how that cut was reached (Otsu / floor / …)
    white_point: list[float] = field(default_factory=list)   # estimated bare-glass RGB
    chromogen_present: bool = True    # a DAB-range chromogen was actually found
    stain_a_hex: str = "#3b5bdb"
    stain_b_hex: str = "#a1531f"
    stain_a_label: str = "Stain A"
    stain_b_label: str = "Stain B"
    # --- area-based detection ------------------------------------------------ #
    level: int = 0                    # sensitivity level actually applied
    auto_level: int = 0               # the slide's own automatic operating point
    level_count: int = 1              # size of the sensitivity ladder
    # The ladder's two ends as multiples of the automatic bar. Per-slide (the
    # ladder spans this slide's own object peaks — see detect._ladder), so the
    # slider label and the CSV read them from here rather than from a constant.
    ladder_hi: float = detect.LADDER_STRICT
    ladder_lo: float = detect.LADDER_LOOSE
    detection_bar: float = 0.0        # excess OD an object's peak must reach (auto)
    detection_floor: float = 0.0      # weakest excess that can belong to an object
    texture_sigma: float = 0.0        # this slide's unstained-tissue variation
    separability: float = 0.0         # how cleanly stained objects split from background
    bar_discard: float = 0.0          # share of stained area the object split would drop
    single_population: bool = False   # the objects were one cloud, so no split was used
    objects: int = 0                  # number of detected stained structures
    median_object_px: int = 0
    mean_object_px: float = 0.0
    region_count: int = 0             # manual regions applied
    include_regions: int = 0          # ... of which "there is staining here you missed"
    exclude_regions: int = 0          # ... of which "what you found here is wrong"
    region_added_pixels: int = 0      # positives the Include regions recovered
    region_removed_pixels: int = 0    # positives the Exclude regions removed
    tissue_pixels_total: int = 0      # the whole tissue area — always the denominator
    chromogen: str = ""                 # detected chromogen family label
    compartment: str = ""               # expected localisation (if a marker chosen)
    marker: str = ""                    # chosen antibody name (selection mode)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["stains"] = [s.to_dict() if isinstance(s, StainVector) else s for s in self.stains]
        return d


# --------------------------------------------------------------------------- #
# Image loading
# --------------------------------------------------------------------------- #

def load_rgb(data: bytes, *, max_edge: int = DEFAULT_MAX_EDGE) -> tuple[np.ndarray, tuple[int, int]]:
    """Decode arbitrary histology bytes → RGB uint8, downsampled so the longest
    edge is ≤ max_edge. Returns (array, (source_w, source_h))."""
    max_edge = int(max(64, min(max_edge, HARD_MAX_EDGE)))
    arr: Optional[np.ndarray] = None
    source_size: tuple[int, int] = (0, 0)

    if _HAVE_TIFFFILE and _looks_like_tiff(data):
        arr, source_size = _load_tiff(data, max_edge)

    if arr is None:
        with Image.open(io.BytesIO(data)) as im:
            source_size = (im.width, im.height)
            im = im.convert("RGB")
            im = _pil_downsample(im, max_edge)
            arr = np.asarray(im, dtype=np.uint8)

    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    if arr.shape[-1] == 4:
        arr = arr[..., :3]
    return np.ascontiguousarray(arr[..., :3]), source_size


def _looks_like_tiff(data: bytes) -> bool:
    return len(data) >= 4 and data[:2] in (b"II", b"MM")


def _load_tiff(data: bytes, max_edge: int) -> tuple[Optional[np.ndarray], tuple[int, int]]:
    try:
        with tifffile.TiffFile(io.BytesIO(data)) as tf:
            series = tf.series[0]
            source_shape = series.shape
            page = _choose_tiff_level(series, max_edge)
            arr = page.asarray()
            source_h, source_w = _hw_from_shape(source_shape)
    except Exception:
        return None, (0, 0)

    if arr.ndim == 3 and arr.shape[0] in (1, 3, 4) and arr.shape[0] < arr.shape[-1]:
        arr = np.moveaxis(arr, 0, -1)

    arr = _to_uint8(arr)
    im = Image.fromarray(arr).convert("RGB")
    im = _pil_downsample(im, max_edge)
    return np.asarray(im, dtype=np.uint8), (source_w, source_h)


def _choose_tiff_level(series, max_edge: int):
    levels = getattr(series, "levels", None) or [series]
    best = None
    best_edge = None
    for lvl in levels:
        h, w = _hw_from_shape(lvl.shape)
        edge = max(h, w)
        if edge >= max_edge and (best_edge is None or edge < best_edge):
            best, best_edge = lvl, edge
    chosen = best if best is not None else levels[0]
    return getattr(chosen, "pages", [chosen])[0] if hasattr(chosen, "pages") else chosen


def _hw_from_shape(shape) -> tuple[int, int]:
    dims = [d for d in shape if d not in (1, 3, 4)]
    if len(dims) >= 2:
        return int(dims[-2]), int(dims[-1])
    if len(shape) >= 2:
        return int(shape[-2]), int(shape[-1])
    return int(shape[0]), 1


def _to_uint8(arr: np.ndarray) -> np.ndarray:
    if arr.dtype == np.uint8:
        return arr
    a = arr.astype(np.float64)
    hi = float(a.max()) if a.size else 1.0
    if hi <= 0:
        return np.zeros(arr.shape, dtype=np.uint8)
    if hi <= 1.0:
        a = a * 255.0
    elif hi > 255:
        a = a / hi * 255.0
    return np.clip(a, 0, 255).astype(np.uint8)


def _pil_downsample(im: Image.Image, max_edge: int) -> Image.Image:
    edge = max(im.width, im.height)
    if edge <= max_edge:
        return im
    scale = max_edge / float(edge)
    new = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
    return im.resize(new, Image.LANCZOS)


# --------------------------------------------------------------------------- #
# Colour helpers
# --------------------------------------------------------------------------- #

def rgb_to_od(rgb: np.ndarray, white: Optional[np.ndarray] = None) -> np.ndarray:
    """RGB uint8 → optical density (Beer-Lambert), same shape (float32).

    `white` is the slide's own bare-glass level per channel. Passing it measures
    absorbance against the light that actually reached the sensor rather than
    against a theoretical 255, which is what stops a dim or warm-lamp scan from
    reading as "tissue everywhere" (and a blazing one from losing pale tissue).
    """
    rgb = rgb.astype(np.float32)
    if white is None:
        return -np.log10((rgb + 1.0) / 256.0)
    w = np.asarray(white, dtype=np.float32).reshape(1, 1, -1)
    return np.clip(-np.log10((rgb + 1.0) / (w + 1.0)), 0.0, None)


def estimate_white_point(rgb: np.ndarray) -> np.ndarray:
    """Per-channel bare-glass level for this slide.

    Glass is not simply "the brightest few percent": on a very pale section the
    brightest few percent is the palest TISSUE, and taking it as white makes the
    whole section read as transparent — the mask then collapses to nothing and
    every percentage is measured against a denominator of almost zero.

    So look for pixels that actually behave like glass — bright *and*
    colourless — and use their level. Only when no such population exists does
    the brightest-percentile estimate stand in, which is the right answer for a
    frame that genuinely has no glass in it.
    """
    flat = rgb.reshape(-1, 3)
    step = max(1, flat.shape[0] // 200_000)
    sample = flat[::step].astype(np.float32)
    fallback = np.percentile(sample, _WHITE_PCT, axis=0)

    # Glass is bright, colourless AND featureless. The third condition is what
    # keeps pale tissue out: a faintly counterstained section is bright and
    # nearly colourless too, but it carries cellular texture, and mistaking it
    # for glass sets the white point at the tissue's own level — after which the
    # section reads as transparent and the mask collapses to nothing.
    val = rgb.max(axis=2).astype(np.float32) / 255.0
    smooth = None
    if _HAVE_SCIPY:
        m = _uniform_filter(val, 9)
        m2 = _uniform_filter(val * val, 9)
        std = np.sqrt(np.clip(m2 - m * m, 0.0, None))
        smooth = (std < 0.012).reshape(-1)[::step]

    mx = sample.max(axis=1)
    mn = sample.min(axis=1)
    sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    bright = mx >= float(np.percentile(mx, 90.0))
    glassy = bright & (sat <= _BG_GLASS_SAT)
    if smooth is not None and smooth.shape == glassy.shape:
        glassy &= smooth
    if int(glassy.sum()) >= max(50, int(0.002 * sample.shape[0])):
        white = np.percentile(sample[glassy], 60.0, axis=0)
        white = np.maximum(white, fallback)
    else:
        white = fallback
    return np.clip(white, _WHITE_MIN, 255.0)


def _bg_class_is_glass(total_od: np.ndarray, sat: np.ndarray,
                       below: np.ndarray) -> bool:
    """Does the class Otsu put BELOW its cut actually look like bare glass?

    Otsu only splits a histogram in two — it cannot tell which half is glass. On
    a wall-to-wall section (no empty slide in frame) it therefore splits *tissue
    against tissue*, and the sub-threshold class is pale tissue, not background.
    That failure is silent and expensive: the pale tissue is discarded, so the
    denominator every percentage is reported against collapses, the mask left
    behind is a filigree of the stained structures themselves, and the
    background statistics that calibrate detection are measured on stain. It is
    exactly what reduced two of the validation slides to a ~10% tissue mask and
    a near-zero positive area.

    The test is physical rather than cosmetic: **bare glass absorbs essentially
    nothing**. Whatever its brightness relative to the white point, a class
    whose own median optical density is above a hair's breadth of zero is
    transmitting through *material*, so it is tissue and the cut is rejected.
    Pale beige tissue reads 0.2-0.4 OD and can no longer masquerade as glass by
    merely sitting close to the white point.
    """
    sub = below[::3, ::3]
    if not sub.any():
        return True
    od_med = float(np.median(total_od[::3, ::3][sub]))
    chroma = float(np.median(sat[::3, ::3][sub]))
    return od_med <= _BG_GLASS_OD_MAX and chroma <= _BG_GLASS_SAT


def white_is_glass(rgb: np.ndarray, white: np.ndarray) -> bool:
    """Is that white point genuinely bare glass, or merely the palest tissue?

    Glass forms a bright population clearly separated from the tissue bulk. On a
    wall-to-wall tissue slide there is no such separation, and renormalising
    against the palest tissue would wrongly flatten every density on the slide —
    so this test gates whether the white point is trusted at all.
    """
    med = float(np.median(rgb[::4, ::4]))
    return float(np.min(white)) - med > _WHITE_GLASS_SEP


def _rgb_to_hsv(rgb01: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Fast vectorised RGB(0..1) → (hue deg 0..360, saturation 0..1, value 0..1).

    Branch-free (np.select-free) formulation — one pass of cheap arithmetic, no
    per-channel boolean fancy-indexing, so it is several times faster on a full
    slide than the naive masked version.
    """
    rgb01 = rgb01.astype(np.float32, copy=False)
    r, g, b = rgb01[..., 0], rgb01[..., 1], rgb01[..., 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    diff = mx - mn
    dsafe = diff + (diff == 0)
    rc = (mx - r) / dsafe
    gc = (mx - g) / dsafe
    bc = (mx - b) / dsafe
    # hue sextant selection via arithmetic on the max channel
    h = np.where(mx == r, bc - gc, np.where(mx == g, 2.0 + rc - bc, 4.0 + gc - rc))
    hue = (h / 6.0) % 1.0 * 360.0
    hue[diff == 0] = 0.0
    sat = np.where(mx > 1e-6, diff / (mx + 1e-6), 0.0)
    return hue.astype(np.float32), sat.astype(np.float32), mx


def _hue_dist(h: np.ndarray, target: float) -> np.ndarray:
    """Circular distance (deg) between hue array and a target hue."""
    d = np.abs(h - target) % 360.0
    return np.minimum(d, 360.0 - d)


def _in_hue_band(h: np.ndarray, band) -> np.ndarray:
    """Is each hue inside `band` = (lo, hi) degrees? Bands may wrap through 0°."""
    lo, hi = float(band[0]), float(band[1])
    return (h >= lo) & (h <= hi) if lo <= hi else (h >= lo) | (h <= hi)


def _hue_band_for(target_hue: float, method: str):
    """The plausible hue window for the chromogen being measured.

    Prefer the band of the reference family actually in use; otherwise find the
    family whose band contains this target hue (covers selection mode, where
    `method` is a stain key rather than a family key). None ⇒ no band is known
    and only the generic ±_HUE_TOL_DEG tolerance applies.
    """
    ref = _REF_BASES.get(method)
    if ref and ref.get("band"):
        return ref["band"]
    probe = np.array([float(target_hue) % 360.0])
    for r in _REF_BASES.values():
        band = r.get("band")
        if band and bool(_in_hue_band(probe, band)[0]):
            return band
    return None


def _normalize_rows(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


def _od_to_rgb_unit(od_vec: np.ndarray) -> list[int]:
    v = od_vec / (np.linalg.norm(od_vec) or 1.0)
    rgb = 255.0 * np.power(10.0, -v * 1.0)
    return [int(round(x)) for x in np.clip(rgb, 0, 255)]


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % (int(rgb[0]), int(rgb[1]), int(rgb[2]))


# --------------------------------------------------------------------------- #
# Stain family classification
# --------------------------------------------------------------------------- #

def _classify_stain(rgb: list[int]) -> tuple[str, float]:
    """Name a stain from its display RGB. Returns (label, hue_deg)."""
    arr = np.array(rgb, dtype=np.float64).reshape(1, 1, 3) / 255.0
    hue, sat, _ = _rgb_to_hsv(arr)
    h = float(hue[0, 0]); s = float(sat[0, 0])
    if s < 0.12:
        return ("Neutral", h)
    if 200 <= h <= 300:
        return ("Haematoxylin (blue)", h)
    if 300 < h < 340:
        return ("Purple counterstain", h)
    if 5 <= h < 50:
        return ("DAB (brown)", h)
    if h >= 340 or h < 5:
        return ("Red chromogen", h)
    if 50 <= h < 80:
        return ("Yellow chromogen", h)
    if 80 <= h < 170:
        return ("Green chromogen", h)
    if 170 <= h < 200:
        return ("Teal chromogen", h)
    return ("Chromogen", h)


# --------------------------------------------------------------------------- #
# Background / tissue segmentation
# --------------------------------------------------------------------------- #

def _local_std(x: np.ndarray, size: int = _TEXTURE_WIN) -> Optional[np.ndarray]:
    """Local standard deviation of `x` — a cheap texture map. None without scipy."""
    if not _HAVE_SCIPY:
        return None
    x = x.astype(np.float32, copy=False)
    m = _uniform_filter(x, size)
    m2 = _uniform_filter(x * x, size)
    return np.sqrt(np.clip(m2 - m * m, 0.0, None))


def _fill_small_holes(mask: np.ndarray, max_px: int) -> np.ndarray:
    """Fill interior gaps up to `max_px` (lumina, fat vacuoles, unstained cores).
    Anything touching the frame edge is genuine outside-background and is kept."""
    inv = ~mask
    if not inv.any():
        return mask
    lbl = _label(inv, connectivity=1)
    if lbl.max() == 0:
        return mask
    counts = np.bincount(lbl.ravel())
    fill = counts <= max_px
    edge = np.unique(np.concatenate([lbl[0], lbl[-1], lbl[:, 0], lbl[:, -1]]))
    fill[edge] = False          # open to the outside ⇒ real background
    fill[0] = False             # label 0 is the mask itself
    return mask | fill[lbl]


def segment_tissue(
    rgb: np.ndarray,
    *,
    hue=None, sat=None, val=None,
    od_floor: float = BACKGROUND_OD_THRESHOLD,
    white: Optional[np.ndarray] = None,
    od: Optional[np.ndarray] = None,
) -> tuple[np.ndarray, np.ndarray, dict]:
    """
    Separate SLIDE BACKGROUND (bare glass, mounting medium, scanner white) from
    real tissue — per slide, and *without any absolute brightness constant*.

      1. **Everything is measured against this slide's own bare glass.** Optical
         density is taken relative to the white point, always, so the cut is a
         statement about how much light the material absorbs — not about how
         bright the scan happens to be. This is what makes the mask survive a
         lamp change, a different scanner, or an exposure correction: brighten
         the whole image and every density here is unchanged. An absolute floor
         on un-normalised density does the opposite, and it is what used to make
         a 12% brighter copy of the same slide lose half its tissue.

      2. **Glass is defined physically:** it absorbs essentially nothing and
         carries no colour. Anything that absorbs is material, however pale.

      3. **Wall-to-wall sections are recognised, not split.** When the class
         below the cut is not colourless-and-transparent, there is no glass in
         frame — the frame is full of tissue and all of it counts. Without that
         check a pale beige section is cut against itself, the denominator
         collapses and every percentage silently changes.

      4. **Chroma rescue.** A pale but distinctly *coloured* pixel is dilute
         stain, not glass, however bright it is.

      5. **Texture.** Real tissue carries fine cellular structure; vignetting,
         haze, defocus and flat scanner artefacts do not.

    The mask is then cleaned morphologically: interior holes are filled so lumina
    count as tissue, isolated specks are dropped so dust and edge debris do not.

    Returns (tissue_mask, total_od, info).
    """
    h, w = rgb.shape[:2]
    if hue is None or sat is None or val is None:
        hue, sat, val = _rgb_to_hsv(rgb.astype(np.float32) / 255.0)
    if white is None:
        white = estimate_white_point(rgb)

    # 1) OD against this slide's own white point — unconditionally.
    total_od = np.clip(rgb_to_od(rgb, white), 0.0, None).sum(axis=2)

    # 2) the glass cut, and the check that what it cut really is glass
    thr = float(od_floor)
    below = total_od < thr
    method = "glass (transmittance)"
    if float(below.mean()) < _TISSUE_BG_MIN_FRAC:
        # Almost nothing is transparent → wall-to-wall section, no glass in frame.
        thr = 0.0
        below = np.zeros_like(below)
        method = "full-field (no glass in frame)"
    elif not _bg_class_is_glass(total_od, sat, below):
        # What sits below the cut still absorbs, or carries colour: it is pale
        # tissue, not background. Keep the whole frame.
        thr = 0.0
        below = np.zeros_like(below)
        method = "full-field (palest class is tissue, not glass)"
    elif float(below.mean()) > _GLASS_MAX_FRAC and total_od.size:
        # The cut says the slide is almost entirely glass. That is true of a
        # genuinely blank scan — and false of a very faintly stained section,
        # which absorbs little but is not empty. The two are told apart by
        # whether anything on the slide absorbs strongly: a blank scan has no
        # such material anywhere, while a pale section with structures in it
        # does. Without this a whole faint section is discarded as background
        # and every percentage is then measured against almost nothing.
        strong = float(np.percentile(total_od, 99.5))
        base = max(float(np.median(total_od[below])), 0.02)
        if strong > _GLASS_CONTRAST_K * base:
            thr = 0.0
            below = np.zeros_like(below)
            method = "full-field (faint section, not an empty slide)"

    # 3) intensity + chroma rescue
    mask = (total_od >= thr) | ((sat >= _CHROMA_RESCUE_SAT) & (total_od >= od_floor * 0.55))

    # 4) texture: remove large SMOOTH regions, but only weakly-absorbing ones, so
    #    densely stained tissue can never be thrown away by this test.
    std = _local_std(val)
    if std is not None and mask.any() and thr > 0.0:
        med = float(np.median(std[mask]))
        flat_cut = min(_TEXTURE_ABS, med * _TEXTURE_REL) if med > 0 else _TEXTURE_ABS
        smooth = std < flat_cut
        mask &= ~(smooth & (total_od < thr * _TEXTURE_OD_GUARD))
        method += " + texture"

    # 5) morphological cleanup
    frame = h * w
    mask = _fill_small_holes(mask, int(max(64, frame * _HOLE_FRAC)))
    speck = int(max(16, frame * _SPECK_FRAC))
    if mask.any():
        mask = _remove_small(mask, speck)

    tissue_px = int(mask.sum())
    info = {
        "tissue_threshold": round(float(thr), 4),
        "tissue_method": method,
        "white_point": [round(float(x), 1) for x in np.atleast_1d(white)],
        "white_applied": True,
        "tissue_pixels": tissue_px,
        "background_pixels": int(frame - tissue_px),
        "tissue_percent": round(tissue_px / frame * 100.0, 3) if frame else 0.0,
        "background_percent": round((frame - tissue_px) / frame * 100.0, 3) if frame else 0.0,
    }
    return mask, total_od, info


# --------------------------------------------------------------------------- #
# Slide validity gate — reject non-histology images
# --------------------------------------------------------------------------- #

def assess_slide(rgb: np.ndarray, *, hue=None, sat=None, val=None,
                 total_od=None, tissue=None) -> tuple[bool, Optional[str], float]:
    """
    Decide whether `rgb` plausibly is a stained histology slide.

    Biased HARD toward acceptance — a real slide must never be skipped — so we
    only reject on strong, specific evidence of a non-slide (vivid broad-spectrum
    photo colours, or an image with no absorbing tissue at all). Returns
    (is_slide, reason_if_not, confidence 0..1). Callers that already have the
    HSV / OD arrays pass them in to avoid recomputation.
    """
    if hue is None or sat is None or val is None:
        hue, sat, val = _rgb_to_hsv(rgb.astype(np.float32) / 255.0)
    if total_od is None:
        total_od = rgb_to_od(rgb).sum(axis=2)
    if tissue is None:
        tissue = total_od > BACKGROUND_OD_THRESHOLD

    tissue_frac = float(tissue.mean())

    # 1) Essentially blank (all glass / all white) — nothing to measure.
    if tissue_frac < 0.02:
        return (False, "No tissue detected — the image is essentially blank.", 0.9)

    # 1b) Near-uniform / textureless fill (solid colour, smooth gradient, flat
    #     graphic). Real histology always carries fine CELLULAR texture, and that
    #     is what has to be measured — at the scale cells actually live at.
    #
    #     This used to test the global luminance spread (p97 − p3 < 0.06), which
    #     is a different property and one a genuine slide can fail: a thin,
    #     weakly counterstained or brightly scanned section occupies a narrow
    #     band of luminance while being covered in cellular detail. Measured,
    #     such a section lands at 0.059-0.067 against a 0.06 bar — so whether a
    #     real slide was accepted or thrown out as "a flat, textureless fill"
    #     came down to which side of the bar its noise happened to fall on.
    #
    #     Local standard deviation separates the two cleanly, because it asks the
    #     question that was always meant. Measured: solid fill 0.000, smooth
    #     gradient 0.002, flat field with sensor noise 0.004; real sections
    #     0.050-0.091, and the faint sections that used to be rejected 0.093.
    #     The bar below has more than 3x margin to the nearest genuine slide and
    #     12x to the nearest non-slide.
    std = _local_std(val)
    if std is not None:
        fine = float(np.percentile(std, 95))
        if fine < _TEXTURE_FLAT_P95:
            return (False, "Doesn't look like a stained slide — the image carries no "
                           "cellular texture at any scale, so it was skipped.", 0.8)

    # 2) Vivid, broad-spectrum colour = a natural photo / graphic, not a stain.
    #    Stains live in a narrow warm/purple band at modest saturation. Count
    #    strongly-saturated pixels whose hue is OUTSIDE the plausible stain band
    #    (i.e. vivid greens / cyans / pure blues / vivid reds).
    strong = sat > 0.55
    strong_frac = float(strong.mean())
    if strong_frac > 0.02:
        sh = hue[strong]
        # plausible stain hues: brown/red 0-55, purple/blue counterstain 200-320
        stainy = ((sh <= 55) | (sh >= 340) | ((sh >= 200) & (sh <= 320)))
        offband_frac = float((~stainy).mean()) * strong_frac
        # e.g. grass, sky, skin, product photos → lots of vivid off-band colour
        if offband_frac > 0.12:
            return (False,
                    "Doesn't look like a stained slide — it contains large vivid "
                    "non-tissue colours (greens/blues), so it was skipped.",
                    0.75)

    # 3) Colour variety: histology occupies few hues; photos sprawl across many.
    tsat = sat[tissue]
    thue = hue[tissue]
    vivid = thue[tsat > 0.30]
    if vivid.size > 500:
        hist, _ = np.histogram(vivid, bins=36, range=(0, 360))
        occupied = int((hist > vivid.size * 0.01).sum())  # hue bins with ≥1% mass
        mean_sat = float(tsat.mean())
        if occupied >= 14 and mean_sat > 0.35:
            return (False,
                    "Doesn't look like a stained slide — colours are spread across "
                    "the spectrum like a photograph, so it was skipped.",
                    0.6)

    return (True, None, 1.0)


# --------------------------------------------------------------------------- #
# Stain-vector estimation (robust, auto, any chromogen)
# --------------------------------------------------------------------------- #

def _hdab_matrix() -> np.ndarray:
    m = _HDAB_REFERENCE.copy()
    m[2] = np.cross(m[0], m[1])
    return _normalize_rows(m)


def estimate_stains_macenko(od_tissue: np.ndarray, *, alpha: float = 1.0) -> Optional[np.ndarray]:
    """Macenko estimation from a set of tissue OD pixels (N,3). Returns a 2x3 of
    OD unit vectors ordered [counterstain(bluer), chromogen], or None."""
    if od_tissue.shape[0] < 200:
        return None
    try:
        cov = np.cov(od_tissue.T)
        _, eigvecs = np.linalg.eigh(cov)
    except np.linalg.LinAlgError:
        return None
    plane = eigvecs[:, [2, 1]] if eigvecs.shape[1] >= 3 else eigvecs[:, -2:]
    if plane.shape[1] < 2:
        return None
    proj = od_tissue @ plane
    phi = np.arctan2(proj[:, 1], proj[:, 0])
    lo = np.percentile(phi, alpha)
    hi = np.percentile(phi, 100 - alpha)
    v_min = np.abs(plane @ np.array([math.cos(lo), math.sin(lo)]))
    v_max = np.abs(plane @ np.array([math.cos(hi), math.sin(hi)]))
    stains = _normalize_rows(np.vstack([v_min, v_max]))
    if not np.all(np.isfinite(stains)) or np.linalg.norm(stains[0] - stains[1]) < 1e-3:
        return None
    # order so the bluer (haematoxylin-like) vector is row 0
    if stains[0, 2] < stains[1, 2]:
        stains = stains[::-1]
    return stains


def _circular_median_deg(angles: np.ndarray) -> float:
    a = np.deg2rad(angles)
    return float(np.rad2deg(np.arctan2(np.sin(a).mean(), np.cos(a).mean())) % 360.0)


def _detect_family(total_od, sat, hue, tissue):
    """
    Identify the chromogen family from the genuinely-stained tissue: look at the
    hue of the darkest, most-saturated pixels (the real stain), ignoring the
    blue/purple counterstain band. Robust and stain-agnostic.

    Only families listed in ENABLED_FAMILIES can be selected. When the chromogen
    that IS on the slide falls outside every enabled family, the first enabled
    basis is kept and `in_band` comes back False, so the caller can say "no DAB
    found here" rather than quietly measuring a colour we don't ship yet.

    Returns (family_key, chromogen_hue, has_counterstain, in_band).

    ---------------------------------------------------------------------------
    THIS FUNCTION IS NOT CALIBRATED, AND IT IS THE BLOCKER ON ENABLING A SECOND
    FAMILY. Read this before adding anything to ENABLED_FAMILIES.

    The hue it measures is tens of degrees away from the chromogen actually on
    the slide, because it reads *blended* pixels — chromogen over haematoxylin —
    and excludes only a fixed 200-300 deg counterstain band, which does not
    remove haematoxylin's pull on everything else. Measured on the synthetic
    grid (scripts/synthetic_matrix.py, one chromogen per scene, hue of the pure
    vector against what arrives here):

        scene      pure chromogen      measured      lands in
        H-DAB          16.7 deg        356.7 deg     H-Red's band
        H-Red         350.7 deg        326.1 deg     H&E's band
        H-GREEN       150.0 deg        187.1 deg     no band -> else: H-DAB

    Every one of those is wrong, and the first is the dangerous one: with H-Red
    enabled, a *DAB* slide classifies as H-Red and would be measured on a red
    basis. None of it is reachable today only because H-DAB is the sole enabled
    family, so every branch falls back to it — the fallback is load-bearing, and
    adding a second family removes it.

    Fixing this needs real sections, not these scenes. The blend that skews the
    reading depends on how heavily a section is counterstained, which is exactly
    what a synthetic scene fixes by construction; tuning the bands against these
    numbers would be fitting the generator. scripts/backtest.py and its slide
    corpus are the right instrument.
    ---------------------------------------------------------------------------
    """
    default = ENABLED_FAMILIES[0]
    if not tissue.any():
        return default, 32.0, True, False
    t_od = total_od[tissue]
    if t_od.size > 120_000:
        t_od = t_od[::(t_od.size // 120_000 + 1)]
    od_hi = float(np.percentile(t_od, 75))
    strong = tissue & (total_od >= od_hi) & (sat >= 0.28)
    if strong.sum() < 100:
        strong = tissue & (sat >= float(np.percentile(sat[tissue], 85)))
    sh = hue[strong]
    if sh.size < 50:
        return default, 32.0, True, False

    counter_band = (sh >= 200) & (sh <= 300)
    has_counter = bool(counter_band.mean() > 0.05)
    chrom = sh[~counter_band]
    if chrom.size < 30:
        # essentially counterstain only → no chromogen at all; ~0 signal expected
        return default, 32.0, has_counter, False

    chue = _circular_median_deg(chrom)
    lo, hi = _DAB_HUE_BAND
    if lo <= chue < hi:
        fam = "H-DAB"          # brown through yellow-brown
    elif chue >= 330 or chue < lo:
        fam = "H-Red"
    elif hi <= chue < 175:
        fam = "H-GREEN"
    elif 300 <= chue < 330:
        fam = "H&E"
    else:
        fam = "H-DAB"

    in_band = fam in ENABLED_FAMILIES
    return (fam if in_band else default), chue, has_counter, in_band


def _estimate_basis(od, total_od, sat, hue, tissue):
    """
    Build a robust 3x3 stain basis from a curated reference matched to the
    detected chromogen family (H-DAB, red, green, …). This avoids the degenerate
    two-brown-vectors failure of per-slide Macenko on single-chromogen slides.

    Returns (stain_matrix, counter_idx, target_idx, method, target_hue,
             counter_label, target_label, in_band).
    """
    fam, chrom_hue, has_counter, in_band = _detect_family(total_od, sat, hue, tissue)
    ref = _REF_BASES.get(fam, _REF_BASES[ENABLED_FAMILIES[0]])
    stains2 = np.array(ref["od"], dtype=np.float64)
    residual = np.cross(stains2[0], stains2[1])
    if np.linalg.norm(residual) < 1e-6:
        residual = np.array([0.0, 0.0, 1.0])
    stain_matrix = _normalize_rows(np.vstack([stains2, residual]))

    counter_idx, target_idx = 0, 1
    counter_label = "Haematoxylin (blue)"
    target_label = ref["label"]
    # Use the reference chromogen hue (stable); the detected hue only chose family.
    target_hue = ref["hue"]
    return (stain_matrix, counter_idx, target_idx, fam, target_hue,
            counter_label, target_label, in_band)


def _deconvolve(od: np.ndarray, stain_matrix_3x3: np.ndarray) -> np.ndarray:
    flat = od.reshape(-1, 3).T
    inv = np.linalg.pinv(stain_matrix_3x3.T)
    conc = inv @ flat
    conc = np.clip(conc, 0, None).astype(np.float32)
    return conc.T.reshape(od.shape)


# --------------------------------------------------------------------------- #
# Public: analyse
# --------------------------------------------------------------------------- #

def analyze(
    rgb: np.ndarray,
    source_size: tuple[int, int],
    *,
    target_index: Optional[int] = None,
    background_threshold: float = BACKGROUND_OD_THRESHOLD,
    stain_method: str = "auto",
    stain_override_od: Optional[list[list[float]]] = None,
    stain_choice: Optional[dict] = None,
    level: Optional[int] = None,
    regions: Optional[list] = None,
    detect_debug: Optional[dict] = None,
) -> tuple[AnalysisResult, dict[str, np.ndarray]]:
    """
    Full IHC analysis. Returns (result, maps).

    `stain_choice` is an optional antibody registry entry (selection mode) used
    for labelling and for the exact deconvolution vectors. `level` is the
    sensitivity index on the detector's ladder (None = the automatic operating
    point the slide's own object population chose). `regions` are the manual
    include / exclude correction shapes — see ihc/regions.py.
    """
    h, w = rgb.shape[:2]
    notes: list[str] = []

    rgb01 = rgb.astype(np.float32) / 255.0
    hue, sat, val = _rgb_to_hsv(rgb01)

    # Optical density against THIS slide's bare-glass white point — always, so
    # every density below is a property of the material rather than of the lamp.
    white = estimate_white_point(rgb)
    od = rgb_to_od(rgb, white)

    # Background segmentation — the tissue mask every statistic below is
    # anchored to. `background_threshold` is the glass cut, in absorbance.
    tissue_mask, total_od, bg_info = segment_tissue(
        rgb, hue=hue, sat=sat, val=val, od_floor=background_threshold,
        white=white, od=od)
    tissue_pixels = int(tissue_mask.sum())

    # ------------------------------------------------------------------ #
    # Validity gate (reuses the arrays already computed above)
    # ------------------------------------------------------------------ #
    valid, skip_reason, confidence = assess_slide(
        rgb, hue=hue, sat=sat, val=val, total_od=total_od, tissue=tissue_mask)

    # ------------------------------------------------------------------ #
    # Stain basis
    # ------------------------------------------------------------------ #
    black_mode = bool(stain_choice and stain_choice.get("black"))
    chromogen_in_band = True
    if stain_choice and stain_choice.get("target_od"):
        # Selection mode: use this stain's exact deconvolution vectors
        # [counterstain, chromogen] (Ruifrok / derived).
        counter_v = np.array(stain_choice["counter_od"], dtype=np.float64)
        target_v = np.array(stain_choice["target_od"], dtype=np.float64)
        stains2 = _normalize_rows(np.vstack([counter_v, target_v]))
        residual = np.cross(stains2[0], stains2[1])
        if np.linalg.norm(residual) < 1e-6:
            residual = np.array([0.0, 0.0, 1.0])
        stain_matrix = _normalize_rows(np.vstack([stains2, residual]))
        counter_idx, tgt_idx = 0, 1
        method = stain_choice.get("key", "selected")
        target_hue = float(stain_choice.get("target_hue", 30.0))
        counter_label = stain_choice.get("counter_name", "Counterstain")
        target_label = stain_choice.get("name", "Selected stain")
    elif stain_override_od:
        stains2 = _normalize_rows(np.array(stain_override_od, dtype=np.float64)[:2])
        residual = np.cross(stains2[0], stains2[1])
        if np.linalg.norm(residual) < 1e-6:
            residual = np.array([0.0, 0.0, 1.0])
        stain_matrix = _normalize_rows(np.vstack([stains2, residual]))
        counter_idx, tgt_idx, method = 0, 1, "override"
        target_hue = _classify_stain(_od_to_rgb_unit(stain_matrix[1]))[1]
        counter_label, target_label = "Stain A", "Stain B"
    elif not tissue_pixels:
        stain_matrix = _hdab_matrix()
        counter_idx, tgt_idx, method = 0, 1, "hdab-reference"
        target_hue, counter_label, target_label = 30.0, "Haematoxylin (blue)", "DAB (brown)"
        chromogen_in_band = False
    else:
        (stain_matrix, counter_idx, tgt_idx, method, target_hue,
         counter_label, target_label, _hue_in_band) = _estimate_basis(
            od, total_od, sat, hue, tissue_mask)
        # Whether the chromogen on this slide really is the one being measured
        # is decided AFTER detection, from the absorbance direction of the
        # strongly stained material (`chromogen_share`) rather than from a hue
        # window here. Assume yes until the detector says otherwise.
        chromogen_in_band = True

    if target_index is not None:
        tgt_idx = int(max(0, min(target_index, 1)))
        counter_idx = 1 - tgt_idx

    conc = _deconvolve(od, stain_matrix)
    target_conc = conc[..., tgt_idx]

    # ------------------------------------------------------------------ #
    # Detection — AREA based, not a global intensity cut.
    #
    # `ihc/detect.py` owns this decision end to end: it fits the slide's own
    # local background absorbance, measures what each pixel carries OVER that
    # background, asks what colour the excess is, groups the answer into
    # connected structures, and separates "background bump" from "stained
    # object" by clustering the population of object peaks. The area of each
    # accepted object is then its own isophote.
    #
    # The by-product is a LEVEL MAP: for each pixel, the first sensitivity level
    # at which it becomes positive. One 8-bit image therefore carries the whole
    # family of results, so the sensitivity slider and the manual region tools
    # are applied by simple comparison — in the browser for the live preview and
    # here for the numbers — and the two can never disagree.
    # ------------------------------------------------------------------ #
    min_px = int(stain_choice.get("min_px", 0)) if stain_choice else 0

    # Does the detector need to be told which chromogen to ask about?
    #
    # Only when it is not the one it already assumes. `method` carries the family
    # key chosen above ("H-DAB", "H-Red", ...) or "hdab-reference" on the forced
    # path; both of those mean the detector's own DAB_OD / HEMA_OD defaults are
    # already the right vectors, and its DAB-specific code paths are the ones
    # validated on real sections. See the note on target_od below.
    _basis_override = bool(stain_choice or stain_override_od) or \
        method not in ("H-DAB", "hdab-reference")
    # No HSV hue window is applied to pixels. Hue is unusable on dense chromogen
    # — it shifts red as a pixel darkens, so real DAB lands just outside a window
    # drawn around DAB — and the detector answers "is this that chromogen?" by
    # absorbance direction instead, which holds at any density. See
    # detect.BLUE_OVER_GREEN_MIN.
    band_mask = None

    det = detect.detect(
        od, tissue_mask,
        level=level,
        min_area_px=(min_px or None),
        hue_band_mask=band_mask,
        # The basis this slide is being measured on — on auto-detect too, unless
        # that basis IS the DAB reference.
        #
        # These used to be passed only when the user had named a stain, so an
        # auto-detected slide fell back to detect.DAB_OD no matter which family
        # _estimate_basis had just chosen. That made ENABLED_FAMILIES a label:
        # auto-detect could *select* a family and then measure it with DAB's
        # colour question anyway. Measured on the synthetic grid, adding H-Red to
        # ENABLED_FAMILIES changed not one condition — every recall and every
        # area identical to leaving it out — while the same red scene analysed
        # with an explicit AEC choice went from 88.7% recall to 98.3%, level with
        # DAB on DAB. The whole gap was this argument.
        #
        # Why the DAB reference is still passed as None rather than as its own
        # vectors, which are numerically the same ones: `target_od is not None`
        # is not only a value, it selects a code path. detect.excess_colour reads
        # brownness as the exact (B − R) split when it is None and as a generic
        # projection off the neutral axis when it is not, and BLUE_OVER_GREEN_MIN
        # switches from a measured constant to one derived from the vector. Both
        # are deliberately close for DAB and neither is identical: routing DAB
        # through the generic path moved the tonal-gradient case from 1.076% to
        # 1.026%. Small, and comfortably inside its bound — but it is a change to
        # the one configuration that ships, bought for a family that does not,
        # and the DAB path is the one with 46 real sections behind it.
        target_od=(stain_matrix[tgt_idx] if _basis_override else None),
        # The counterstain's own absorbance direction, so the object colour test
        # can factor it out rather than being dragged toward it on a heavily
        # counterstained section — see detect.CHROMO_FRAC_MIN.
        counter_od=(stain_matrix[counter_idx] if _basis_override else None),
        # Raw saturation: whether the material carries any colour AT ALL. An
        # absolute property, deliberately — relative to a blue counterstain a
        # grey blob looks warm. (The colour readings that decide what the
        # material IS are derived inside the detector from the white-point
        # normalised image; see detect.chroma_readings.)
        saturation=sat,
        debug=detect_debug,
    )
    notes.extend(det.notes)

    # Now that the strongly absorbing material has been looked at, decide whether
    # the chromogen on this slide really is the one being measured — by the
    # direction of its absorbance, which is readable at any density.
    if det.chromogen_share < detect.CHROMOGEN_SHARE_MIN and not stain_choice:
        chromogen_in_band = False
        # Say what the figure IS, not what it was assumed to be.
        #
        # This note used to end "so the positive area reads at or near zero",
        # which is only true when the absorbing material is neutral — ink, dust,
        # a fold — and the colour tests refuse all of it. It is not true of a
        # slide carrying a *different chromogen*, which is the case the rest of
        # the sentence is about: measured on the synthetic grid, a red-chromogen
        # section reads 0.407% and a green one 0.416% against the same scene in
        # DAB at 0.420%. The structures are found, and the number that comes back
        # looks entirely ordinary.
        #
        # So the reading a pathologist needs is not "this came back empty" but
        # "this number is not a DAB measurement" — a plausible figure carrying a
        # note that says it read as zero is worse than no note at all, because
        # the contradiction invites the reader to discount the warning rather
        # than the number.
        notes.append(
            "No brown (DAB) chromogen found — the strongly absorbing material on "
            "this slide does not have DAB's absorbance signature. Any positive "
            "area reported here is therefore NOT a DAB measurement: if the slide "
            "is neutral material (ink, dust, a fold) it will read at or near "
            "zero, but if it carries a different chromogen the structures are "
            "still measured and the percentage will look ordinary. This build "
            "measures DAB only — do not read this figure as a DAB result.")

    built = regions_mod.build(regions, h, w, detect.N_LEVELS)
    # The tissue mask segmentation found is the denominator, always — manual
    # regions correct the numerator only (see ihc/regions.py). It is also what
    # the browser needs in order to apply the regions itself and arrive at the
    # same two counts the server used.
    tissue_base = tissue_mask
    tissue_pixels_total = tissue_pixels
    auto_positive = (det.level_map <= det.level) & tissue_mask
    positive, tissue_mask = regions_mod.apply(
        det.level_map, tissue_mask, det.level, built, detect.N_LEVELS)
    # What the hand tools actually changed, in pixels, so the export can say so.
    region_added = int((positive & ~auto_positive).sum())
    region_removed = int((auto_positive & ~positive).sum())
    if built["count"]:
        bits = []
        if built.get("include_count"):
            bits.append(f"{built['include_count']} include (+{region_added:,} px)")
        if built.get("exclude_count"):
            bits.append(f"{built['exclude_count']} exclude (−{region_removed:,} px)")
        # State plainly that the number was corrected by hand and by how much.
        # The denominator is untouched, so the percentage stays comparable with
        # every other slide in the batch — which is the point of the change.
        notes.append(
            f"Corrected by hand ({', '.join(bits)}). The percentage is still "
            f"measured over this slide's whole tissue area ({tissue_pixels:,} px).")

    if not valid:
        positive = np.zeros((h, w), dtype=bool)
        notes.append(skip_reason or "Skipped: not a stained slide.")

    object_count, object_areas = _object_summary(positive)

    positive_pixels = int(positive.sum())
    positive_fraction = (positive_pixels / tissue_pixels) if tissue_pixels else 0.0
    mean_pos = float(target_conc[positive].mean()) if positive_pixels else 0.0

    # ---- display colours ---- #
    a_idx, b_idx = counter_idx, tgt_idx
    a_rgb = _od_to_rgb_unit(stain_matrix[a_idx])
    b_rgb = _od_to_rgb_unit(stain_matrix[b_idx])
    overlay_hex = OVERLAY_DEFAULT_HEX      # detection overlay is always neon green

    chromogen = target_label
    if stain_choice:
        # Selection mode: the label IS the chosen stain (already the target label).
        marker = str(stain_choice.get("name", ""))
        compartment = str(stain_choice.get("category", ""))
        stain_label = f"{marker} (selected)"
    else:
        marker = ""
        compartment = ""
        stain_label = chromogen if not tissue_pixels else f"{chromogen} · H counterstain"

    stains = [
        StainVector(counter_label, a_rgb, stain_matrix[a_idx].tolist()),
        StainVector(target_label, b_rgb, stain_matrix[b_idx].tolist()),
    ]

    result = AnalysisResult(
        width=w, height=h,
        source_width=int(source_size[0] or w),
        source_height=int(source_size[1] or h),
        tissue_pixels=tissue_pixels,
        positive_pixels=positive_pixels,
        positive_fraction=round(positive_fraction, 6),
        positive_percent=round(positive_fraction * 100.0, 3),
        mean_positive_intensity=round(mean_pos, 4),
        threshold=round(float(det.levels[det.level]), 4),
        stains=stains,
        target_index=int(tgt_idx),
        method=method,
        valid=valid,
        skip_reason=skip_reason,
        stain_label=stain_label,
        confidence=round(float(confidence), 3),
        suggested_overlay_hex=overlay_hex,
        background_pixels=int(bg_info["background_pixels"]),
        background_percent=float(bg_info["background_percent"]),
        tissue_percent=float(bg_info["tissue_percent"]),
        tissue_threshold=float(bg_info["tissue_threshold"]),
        tissue_method=str(bg_info["tissue_method"]),
        white_point=list(bg_info["white_point"]),
        chromogen_present=bool(chromogen_in_band),
        stain_a_hex=_hex(a_rgb),
        stain_b_hex=_hex(b_rgb),
        stain_a_label=counter_label,
        stain_b_label=target_label,
        level=int(det.level),
        auto_level=int(det.auto_level),
        level_count=int(detect.N_LEVELS),
        ladder_hi=round(float(det.ladder_hi), 4),
        ladder_lo=round(float(det.ladder_lo), 4),
        detection_bar=round(float(det.bar), 4),
        detection_floor=round(float(det.floor), 4),
        texture_sigma=round(float(det.sigma), 5),
        separability=round(float(det.separability), 3),
        bar_discard=round(float(det.bar_discard), 3),
        single_population=bool(det.single_population),
        objects=int(object_count),
        median_object_px=int(np.median(object_areas)) if len(object_areas) else 0,
        mean_object_px=round(float(np.mean(object_areas)), 1) if len(object_areas) else 0.0,
        region_count=int(built["count"]),
        include_regions=int(built.get("include_count", 0)),
        exclude_regions=int(built.get("exclude_count", 0)),
        region_added_pixels=region_added,
        region_removed_pixels=region_removed,
        tissue_pixels_total=int(tissue_pixels_total),
        chromogen=chromogen,
        compartment=compartment,
        marker=marker,
        notes=notes,
    )
    maps = {
        "conc": conc,
        "tissue_mask": tissue_mask,
        "tissue_base": tissue_base,
        "positive": positive,
        "level_map": det.level_map,
        "excess": det.excess,
        "brownness": det.brownness,
        "sigma": det.sigma,
        "stain_matrix": stain_matrix,
        "counter_index": counter_idx,
        "target_index": tgt_idx,
        "od": od,
    }
    return result, maps


def _object_summary(positive: np.ndarray) -> tuple[int, np.ndarray]:
    """How many separate stained structures were found, and how big each is.

    Reported alongside the percentage because two slides can share a positive
    area and mean completely different things: one large plaque is not the same
    finding as two hundred puncta."""
    if not positive.any():
        return 0, np.array([], dtype=np.int64)
    lbl = _label(positive, connectivity=2)
    counts = np.bincount(lbl.ravel())
    areas = counts[1:] if counts.size > 1 else np.array([], dtype=np.int64)
    return int(areas.size), areas


def _binary_erode(mask: np.ndarray, radius: int) -> np.ndarray:
    if not mask.any():
        return mask
    return _grey_erosion(mask.astype(np.uint8), disk(radius)).astype(bool)


def _remove_small(mask: np.ndarray, min_px: int) -> np.ndarray:
    """Drop connected components smaller than `min_px` pixels. Version-safe
    replacement for remove_small_objects (whose min_size kwarg is deprecated)."""
    lbl = _label(mask, connectivity=1)
    if lbl.max() == 0:
        return mask
    counts = np.bincount(lbl.ravel())
    keep = counts >= min_px
    keep[0] = False
    return keep[lbl]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def render_overlay(rgb: np.ndarray, maps: dict[str, np.ndarray],
                   color=OVERLAY_GREEN, alpha=0.5) -> np.ndarray:
    """Highlight positive pixels on the original image."""
    out = rgb.astype(np.float64).copy()
    pos = maps["positive"]
    overlay = np.array(color, dtype=np.float64)
    out[pos] = (1 - alpha) * out[pos] + alpha * overlay
    return np.clip(out, 0, 255).astype(np.uint8)


def render_stain_only(
    rgb: np.ndarray,
    maps: dict[str, np.ndarray],
    *,
    background_rgb: tuple[int, int, int] = (255, 255, 255),
    positive: Optional[np.ndarray] = None,
) -> np.ndarray:
    """
    BACKGROUND REMOVED — only the detected stain survives.

    Every pixel the engine did not classify as chromogen is replaced by a flat
    field: bare glass, counterstain, unstained tissue and grey/black debris all
    go. Positive pixels keep their ORIGINAL colour (not a synthetic tint), so
    what is left is exactly the measured stain and nothing else — which is what
    makes it usable as evidence for the reported positive-area number.
    """
    pos = maps["positive"] if positive is None else np.asarray(positive, dtype=bool)
    out = np.empty_like(rgb)
    out[...] = np.array(background_rgb, dtype=np.uint8)
    out[pos] = rgb[pos]
    return out


def render_background_removed(
    rgb: np.ndarray,
    maps: dict[str, np.ndarray],
    *,
    background_rgb: tuple[int, int, int] = (255, 255, 255),
) -> np.ndarray:
    """Slide with the BACKGROUND (glass / mounting medium) erased but all tissue
    kept — the intermediate view that shows what `segment_tissue()` decided."""
    out = rgb.copy()
    out[~maps["tissue_mask"]] = np.array(background_rgb, dtype=np.uint8)
    return out


def _recompose(conc: np.ndarray, stain_matrix: np.ndarray) -> np.ndarray:
    flat = conc.reshape(-1, 3) @ stain_matrix
    rgb = 255.0 * np.power(10.0, -flat)
    rgb = np.clip(rgb, 0, 255).reshape(conc.shape)
    return rgb.astype(np.uint8)


def render_stain(
    maps: dict[str, np.ndarray],
    index: int,
    color: Optional[tuple[int, int, int]] = None,
    *,
    gain: float = 1.0,
    background_rgb: tuple[int, int, int] = (255, 255, 255),
) -> np.ndarray:
    """
    Single-stain visualisation: paint stain `index`'s concentration as a tint of
    `color` over a clean background. `color=None` uses the stain's natural colour.
    This is what powers the recolourable Stain-A / Stain-B panels.
    """
    conc = maps["conc"][..., index].astype(np.float64)
    stain_matrix = maps["stain_matrix"]
    if color is None:
        color = _od_to_rgb_unit(stain_matrix[index])
    color = np.array(color, dtype=np.float64)
    bg = np.array(background_rgb, dtype=np.float64)

    # concentration → opacity via Beer-Lambert on a single channel (natural falloff)
    amt = 1.0 - np.power(10.0, -np.clip(conc * gain, 0, None))   # 0..1
    amt = amt[..., None]
    out = bg[None, None, :] * (1 - amt) + color[None, None, :] * amt
    return np.clip(out, 0, 255).astype(np.uint8)


def render_variant(
    rgb: np.ndarray,
    maps: dict[str, np.ndarray],
    *,
    target_gain: float = 1.0,
    counterstain_gain: float = 1.0,
    background_rgb: Optional[tuple[int, int, int]] = None,
    target_index: int = 1,
) -> np.ndarray:
    """Legacy variant renderer (kept for compatibility)."""
    conc = maps["conc"].copy()
    stain_matrix = maps["stain_matrix"]
    counter_index = 1 - target_index if target_index in (0, 1) else 0
    conc[..., target_index] *= float(target_gain)
    conc[..., counter_index] *= float(counterstain_gain)
    conc = np.clip(conc, 0, None)
    out = _recompose(conc, stain_matrix)
    if background_rgb is not None:
        bg = ~maps["tissue_mask"]
        out[bg] = np.array(background_rgb, dtype=np.uint8)
    return out


# --------------------------------------------------------------------------- #
# Premium labelled comparison (for export) — scientific header + ImageSL mark
# --------------------------------------------------------------------------- #

def _load_font(size: int, bold: bool = True):
    size = max(10, int(size))
    names = (["DejaVuSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"] if bold
             else ["DejaVuSans.ttf", "Arial.ttf", "arial.ttf"])
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _text_size(draw, text, font):
    b = draw.textbbox((0, 0), text, font=font)
    return b[2] - b[0], b[3] - b[1], b


def compose_comparison(
    left_rgb: np.ndarray,
    right_rgb: np.ndarray,
    left_label: str = "Original",
    right_label: str = "DAB detection",
    *,
    metric_text: Optional[str] = None,
    stain_text: Optional[str] = None,
    sep_w: int = 0,
) -> np.ndarray:
    """Two panels, labelled, on a clean card. Nothing else.

    This is the figure people actually put in a slide deck or a paper, so it is
    built like one: generous margins, the two images the same size on a neutral
    ground, a soft shadow to lift them off it, and a caption on each so nobody
    has to guess which is which. One quiet ImageSL mark underneath.

    Everything else that used to be here is gone on purpose. The old layout put
    a header band, a wordmark, the percentage, the stain name and the mark into
    two 22-pixel strips above and below a 2000-pixel-wide image — six competing
    elements at three different alignments, with the logo clipped by the frame.
    The numbers belong in the CSV, which is in the same download and is where a
    reader can actually use them.

    `metric_text` and `stain_text` are still accepted so existing callers do not
    break, and are deliberately ignored.
    """
    left_rgb = np.ascontiguousarray(left_rgb)
    right_rgb = np.ascontiguousarray(right_rgb)
    ph = max(left_rgb.shape[0], right_rgb.shape[0])
    pw = max(left_rgb.shape[1], right_rgb.shape[1])

    # Everything scales off the panel width, so a 512 px thumbnail and a 2048 px
    # export are the same design rather than the same pixel sizes.
    u = pw / 100.0
    margin = int(round(u * 3.2))
    gutter = int(round(u * 2.4))
    radius = max(4, int(round(u * 0.9)))
    cap_gap = int(round(u * 1.9))
    cap_size = max(11, int(round(u * 2.5)))
    mark_size = max(10, int(round(u * 2.0)))

    cap_h = int(round(cap_size * 1.35))
    mark_h = int(round(mark_size * 1.35))
    foot_gap = int(round(u * 2.2))

    total_w = margin * 2 + pw * 2 + gutter
    total_h = margin + ph + cap_gap + cap_h + foot_gap + mark_h + margin

    bg = (244, 244, 248)
    ink = (28, 22, 48)
    ink_soft = (128, 122, 148)
    violet = (109, 40, 217)

    canvas = Image.new("RGB", (total_w, total_h), bg)

    xs = (margin, margin + pw + gutter)
    box = [0, 0, pw - 1, ph - 1]

    # Soft drop shadow: one blurred rounded rectangle under both panels.
    shadow = Image.new("L", (total_w, total_h), 0)
    sd = ImageDraw.Draw(shadow)
    off = max(1, int(round(u * 0.35)))
    for x in xs:
        sd.rounded_rectangle([x, margin + off, x + pw - 1, margin + ph - 1 + off],
                             radius=radius, fill=70)
    try:
        shadow = shadow.filter(ImageFilter.GaussianBlur(max(1.0, u * 0.55)))
    except Exception:
        pass
    canvas.paste(Image.new("RGB", (total_w, total_h), (196, 192, 210)), (0, 0), shadow)

    # Rounded panels.
    mask = Image.new("L", (pw, ph), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    for x, arr in zip(xs, (left_rgb, right_rgb)):
        panel = Image.new("RGB", (pw, ph), (255, 255, 255))
        panel.paste(Image.fromarray(arr), (0, 0))
        canvas.paste(panel, (x, margin), mask)

    draw = ImageDraw.Draw(canvas)
    for x in xs:                                  # hairline to define the edge
        draw.rounded_rectangle([x, margin, x + pw - 1, margin + ph - 1],
                               radius=radius, outline=(223, 220, 233), width=1)

    # Captions under each panel, centred on it.
    cf = _load_font(cap_size)
    cy = margin + ph + cap_gap
    for x, label in zip(xs, (left_label, right_label)):
        tw, th, bb = _text_size(draw, label, cf)
        draw.text((x + (pw - tw) / 2.0 - bb[0], cy - bb[1]), label, fill=ink, font=cf)

    # One quiet mark, centred under the whole card.
    mf = _load_font(mark_size, bold=False)
    mark = "Analyzed with ImageSL"
    tw, th, bb = _text_size(draw, mark, mf)
    my = cy + cap_h + foot_gap
    # Centre the dot AND the wordmark as one group, not the text alone — a dot
    # hung off the left of centred text reads as a centring mistake.
    dot_r = max(2, int(round(mark_size * 0.26)))
    gap = dot_r * 3
    group_w = dot_r * 2 + gap + tw
    gx = (total_w - group_w) / 2.0
    draw.ellipse([gx, my + th / 2 - dot_r, gx + dot_r * 2, my + th / 2 + dot_r], fill=violet)
    draw.text((gx + dot_r * 2 + gap - bb[0], my - bb[1]), mark, fill=ink_soft, font=mf)

    return np.asarray(canvas)


# --------------------------------------------------------------------------- #
# Encoding helpers
# --------------------------------------------------------------------------- #

def to_png_bytes(rgb: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def original_data_uri(rgb: np.ndarray) -> str:
    """The "Original" panel — and the browser's own copy of the measured pixels.

    LOSSLESS, and that is the entire point. This image is not only displayed: it
    is kept in IndexedDB across reloads, and it is what a slide is rebuilt from
    when the server has let go of a long working session (see
    `/api/rehydrate`). A rebuild re-runs the engine on this file, so whatever
    this encoding loses, the recovered measurement loses too.

    Measured across the 46-slide validation set, rebuilding from the JPEG this
    used to be moved the reported positive area by 0.05 pp at the median and
    0.52 pp at worst — 12% of the reading on one section. For an instrument
    somebody publishes from, a recovered number that quietly disagrees with the
    one they wrote down is worse than no recovery at all. Chroma subsampling did
    most of that damage (the detector reads colour, and 4:2:0 stores colour at
    half resolution); turning it off cut the worst case to 0.11 pp, which is
    better but still not nothing.

    Lossless WebP makes it exactly zero on all 46, for about 1.2 MB a slide
    against 0.4 MB — a trade worth making once, here, so that nothing anywhere
    downstream has to carry a caveat about rebuilt slides. PNG would be exact
    too and is the fallback, but runs half again as large.
    """
    buf = io.BytesIO()
    im = Image.fromarray(rgb).convert("RGB")
    try:
        # method=0 is the fastest of WebP's lossless search strategies. Measured
        # on a working-resolution slide it takes 0.068 s against 0.216 s at the
        # default 4 — a third of the time — for 6% more bytes (1245 KB vs 1170).
        # The output is lossless at every setting, so the choice is purely
        # time-against-size and cannot touch a measurement. At three encodes a
        # second this was the second largest cost in the whole upload path.
        im.save(buf, format="WEBP", lossless=True, quality=80, method=0)
        mime = "image/webp"
    except Exception:
        # No libwebp in this build: PNG is equally exact, merely larger.
        buf = io.BytesIO()
        im.save(buf, format="PNG", compress_level=1)
        mime = "image/png"
    return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def to_data_uri(rgb: np.ndarray, fmt: str = "PNG", quality: int = 86) -> str:
    buf = io.BytesIO()
    im = Image.fromarray(rgb)
    if fmt.upper() in ("JPEG", "JPG"):
        im.convert("RGB").save(buf, format="JPEG", quality=quality)
        mime = "image/jpeg"
    else:
        im.save(buf, format=fmt)
        mime = "image/png"
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def mask_data_uri(mask: np.ndarray) -> str:
    h, w = mask.shape[:2]
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[mask.astype(bool)] = (255, 255, 255, 255)
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def level_data_uri(level_map: np.ndarray, tissue_mask: Optional[np.ndarray] = None) -> str:
    """Encode the detector's LEVEL MAP — and the tissue mask — as one RGB PNG.

    ``R`` holds the first sensitivity level at which the pixel becomes positive —
    plus the two sentinels above the ladder, ``detect.LEVEL_CANDIDATE`` (254, a
    structure the detector formed and refused, which only an Include region may
    recover) and ``detect.LEVEL_NEVER`` (255, nothing here at any setting).
    ``G`` is 255 on tissue and 0 on background. One image therefore carries the
    complete family of results *and* the denominator every percentage is
    measured against, so the browser reproduces the server's decision — at any
    sensitivity, under any manual correction — by comparison alone, with no
    round-trip.

    Carrying the tissue mask is what makes a **percentage** reproducible in the
    browser, not just a picture: it is the denominator, and it is the same one
    whatever the user draws.

    The mask travels in a colour channel rather than in alpha deliberately:
    canvas un-premultiplies on read, so a pixel with alpha 0 comes back with its
    other channels zeroed — which would turn "never positive" (255) into "positive
    at level 0" everywhere off the tissue.
    """
    arr = np.asarray(level_map, dtype=np.uint8)
    h, w = arr.shape[:2]
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    rgb[..., 0] = arr
    if tissue_mask is None:
        rgb[..., 1] = 255
    else:
        rgb[..., 1] = np.asarray(tissue_mask, dtype=bool).astype(np.uint8) * 255
    buf = io.BytesIO()
    # `optimize=True` makes PNG try every filter strategy to shave a few percent.
    # This image is a level index plus a binary mask — it is already trivially
    # compressible, so that search buys almost nothing and costs real time on
    # every single analysis. Lossless at any level; the bytes decode identically.
    Image.fromarray(rgb, "RGB").save(buf, format="PNG", compress_level=6)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
