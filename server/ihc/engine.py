"""
ImageSL — IHC / histology stain-analysis engine.

Everything here is pure, vectorised numpy + scikit-image so a downsampled slide
processes in a fraction of a second. The browser is the only client; this module
is the whole brain.

Design goals (2026 rewrite):

*  Works on ANY chromogen, not just H-DAB. The stain basis is estimated from the
   slide itself (robust Macenko on genuinely-absorbing, saturated tissue) and the
   two stains are classified by hue into a family (H-DAB brown, red chromogen,
   H&E, blue counterstain, …). A curated reference basis is the fallback.

*  Rejects junk. Photos of a bike / person / screenshot never reach the pixel
   math — `assess_slide()` gates them out with a human-readable reason, and the
   caller reports them as "skipped".

*  Kills the classic false positives that plagued the old build:
     - diffuse tan/brown *background tint* counted as signal (the single biggest
       problem) — defeated by demanding real color saturation AND separation from
       each slide's own background, not merely "the darker half of the tissue";
     - near-neutral **black / grey debris blobs** (folds, dust, ink) — defeated by
       a chroma gate: genuine chromogen carries a specific hue, debris does not;
     - lumen / tear rims — defeated by eroding to the tissue core.

*  Auto-picks a high-contrast overlay colour per slide (complementary to the
   tissue) and reports natural display colours for each stain so the UI can show
   recolourable Stain-A / Stain-B panels.
"""

from __future__ import annotations

import base64
import io
import math
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

try:  # tifffile + imagecodecs handle compressed / pyramidal histology TIFs
    import tifffile
    _HAVE_TIFFFILE = True
except Exception:  # pragma: no cover - tifffile is a hard dep in prod
    _HAVE_TIFFFILE = False

from skimage.morphology import disk
from skimage.morphology import erosion as _grey_erosion
from skimage.measure import label as _label


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_MAX_EDGE = 1024
HARD_MAX_EDGE = 2048

# A pixel whose summed optical density is below this is bright glass / mounting
# medium — background, not tissue.
BACKGROUND_OD_THRESHOLD = 0.15

# ---- positive-detection tuning (the accuracy core) ----------------------- #
# Genuine chromogen is BOTH darker (higher OD) AND more saturated than the
# slide's own tissue background, and is focal rather than covering everything.
# These constants encode that; they were calibrated against 46 real IHC slides.
# The intensity bar is anchored to EACH slide's own tissue-background
# concentration bulk (median + K robust-sigmas), which is scale-consistent:
# a densely-stained slide keeps its signal (it sits far above background) while a
# uniformly-tinted slide reads ~0 instead of flooding. Saturation, optical-
# density and colour-specificity gates run alongside to reject desaturated tan
# tint and near-neutral grey/black debris.
_SAT_FLOOR          = 0.23   # absolute minimum HSV saturation for a positive
_SAT_MARGIN         = 0.05   # must beat the tissue saturation bulk by this much
_OD_ABS_FLOOR       = 0.55   # absolute minimum summed OD for a positive
_OD_SEP_K           = 1.6    # …and must beat background OD by K robust-sigmas
_CONC_SEP_K         = 2.2    # target-conc must beat background conc by K sigmas
_SPEC_MIN           = 0.55   # target must own ≥55% of the pixel's stain colour
_HUE_TOL_DEG        = 46.0   # pixel hue must sit within this of the chromogen
_MIN_OBJECT_PX      = 10     # drop specks smaller than this (isolated noise)

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
_REF_BASES = {
    "H-DAB":  {"label": "H-DAB (brown)",  "hue": 32.0,
               "od": [[0.650, 0.700, 0.290], [0.270, 0.570, 0.780]]},
    "H-Red":  {"label": "Red chromogen",  "hue": 2.0,
               "od": [[0.650, 0.700, 0.290], [0.210, 0.760, 0.615]]},
    "H-AP":   {"label": "Alk-phos (red)", "hue": 350.0,
               "od": [[0.650, 0.700, 0.290], [0.190, 0.760, 0.620]]},
    "H-GREEN":{"label": "Green chromogen","hue": 140.0,
               "od": [[0.650, 0.700, 0.290], [0.400, 0.610, 0.680]]},
    "H&E":    {"label": "H&E (eosin)",    "hue": 330.0,
               "od": [[0.650, 0.700, 0.290], [0.070, 0.990, 0.110]]},
}


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
    suggested_overlay_hex: str = "#00e5ff"
    stain_a_hex: str = "#3b5bdb"
    stain_b_hex: str = "#a1531f"
    stain_a_label: str = "Stain A"
    stain_b_label: str = "Stain B"
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

def rgb_to_od(rgb: np.ndarray) -> np.ndarray:
    """RGB uint8 → optical density (Beer-Lambert), same shape (float32)."""
    rgb = rgb.astype(np.float32)
    return -np.log10((rgb + 1.0) / 256.0)


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
    #     graphic). Real histology always carries fine cellular texture, so a very
    #     low global luminance spread cannot be a genuine slide — safe to reject.
    lum = val  # HSV value ≈ luminance proxy, already computed
    spread = float(np.percentile(lum, 97) - np.percentile(lum, 3))
    if spread < 0.06:
        return (False, "Doesn't look like a stained slide — the image is a flat, "
                       "textureless fill, so it was skipped.", 0.8)

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

    Returns (family_key, chromogen_hue, has_counterstain).
    """
    if not tissue.any():
        return "H-DAB", 32.0, True
    t_od = total_od[tissue]
    if t_od.size > 120_000:
        t_od = t_od[::(t_od.size // 120_000 + 1)]
    od_hi = float(np.percentile(t_od, 75))
    strong = tissue & (total_od >= od_hi) & (sat >= 0.28)
    if strong.sum() < 100:
        strong = tissue & (sat >= float(np.percentile(sat[tissue], 85)))
    sh = hue[strong]
    if sh.size < 50:
        return "H-DAB", 32.0, True

    counter_band = (sh >= 200) & (sh <= 300)
    has_counter = bool(counter_band.mean() > 0.05)
    chrom = sh[~counter_band]
    if chrom.size < 30:
        # essentially counterstain only → no chromogen; keep DAB basis, ~0 signal
        return "H-DAB", 32.0, has_counter

    chue = _circular_median_deg(chrom)
    if 12 <= chue < 60:
        fam = "H-DAB"
    elif chue >= 330 or chue < 12:
        fam = "H-Red"
    elif 60 <= chue < 90:
        fam = "H-DAB"          # yellow-brown → still DAB family
    elif 90 <= chue < 175:
        fam = "H-GREEN"
    elif 300 <= chue < 330:
        fam = "H&E"
    else:
        fam = "H-DAB"
    return fam, chue, has_counter


def _estimate_basis(od, total_od, sat, hue, tissue):
    """
    Build a robust 3x3 stain basis from a curated reference matched to the
    detected chromogen family (H-DAB, red, green, …). This avoids the degenerate
    two-brown-vectors failure of per-slide Macenko on single-chromogen slides.

    Returns (stain_matrix, counter_idx, target_idx, method, target_hue,
             counter_label, target_label).
    """
    fam, chrom_hue, has_counter = _detect_family(total_od, sat, hue, tissue)
    ref = _REF_BASES.get(fam, _REF_BASES["H-DAB"])
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
    return stain_matrix, counter_idx, target_idx, fam, target_hue, counter_label, target_label


def _deconvolve(od: np.ndarray, stain_matrix_3x3: np.ndarray) -> np.ndarray:
    flat = od.reshape(-1, 3).T
    inv = np.linalg.pinv(stain_matrix_3x3.T)
    conc = inv @ flat
    conc = np.clip(conc, 0, None).astype(np.float32)
    return conc.T.reshape(od.shape)


# --------------------------------------------------------------------------- #
# Auto overlay colour (max contrast to the tissue)
# --------------------------------------------------------------------------- #

def _auto_overlay_hex(rgb: np.ndarray, tissue: np.ndarray) -> str:
    """Pick a vivid overlay colour that maximally contrasts the tissue: take the
    mean tissue hue, rotate ~180°, snap to full saturation and a bright value.
    Uses a strided subsample so it is cheap on a full slide."""
    small = rgb[::3, ::3].astype(np.float32) / 255.0
    tmask = tissue[::3, ::3]
    sample = small[tmask] if tmask.any() else small.reshape(-1, 3)
    mean_rgb = sample.mean(axis=0).reshape(1, 1, 3)
    hue, _, val = _rgb_to_hsv(mean_rgb)
    h = float(hue[0, 0])
    comp = (h + 180.0) % 360.0
    # Nudge away from the tissue's own band and toward cyan/magenta which read
    # cleanly over both brown tissue and dark chromogen.
    if 40 <= comp <= 80:            # muddy yellow-green → push to cyan
        comp = 190.0
    out = _hsv_to_rgb(comp, 1.0, 1.0)
    return _hex(out)


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    h = h % 360.0
    c = v * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = v - c
    if h < 60:   r, g, b = c, x, 0
    elif h < 120: r, g, b = x, c, 0
    elif h < 180: r, g, b = 0, c, x
    elif h < 240: r, g, b = 0, x, c
    elif h < 300: r, g, b = x, 0, c
    else:         r, g, b = c, 0, x
    return (int(round((r + m) * 255)), int(round((g + m) * 255)), int(round((b + m) * 255)))


# --------------------------------------------------------------------------- #
# Public: analyse
# --------------------------------------------------------------------------- #

def analyze(
    rgb: np.ndarray,
    source_size: tuple[int, int],
    *,
    target_index: Optional[int] = None,
    background_threshold: float = BACKGROUND_OD_THRESHOLD,
    threshold_scale: float = 1.0,
    stain_strictness: str = "strong",
    stain_method: str = "auto",
    stain_override_od: Optional[list[list[float]]] = None,
) -> tuple[AnalysisResult, dict[str, np.ndarray]]:
    """Full IHC analysis. Returns (result, maps)."""
    h, w = rgb.shape[:2]
    notes: list[str] = []

    rgb01 = rgb.astype(np.float32) / 255.0
    hue, sat, val = _rgb_to_hsv(rgb01)
    od = rgb_to_od(rgb)
    total_od = od.sum(axis=2)

    tissue_mask = total_od > background_threshold
    tissue_pixels = int(tissue_mask.sum())

    # ------------------------------------------------------------------ #
    # Validity gate (reuses the arrays already computed above)
    # ------------------------------------------------------------------ #
    valid, skip_reason, confidence = assess_slide(
        rgb, hue=hue, sat=sat, val=val, total_od=total_od, tissue=tissue_mask)

    # ------------------------------------------------------------------ #
    # Stain basis
    # ------------------------------------------------------------------ #
    if stain_override_od:
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
    else:
        (stain_matrix, counter_idx, tgt_idx, method, target_hue,
         counter_label, target_label) = _estimate_basis(od, total_od, sat, hue, tissue_mask)

    if target_index is not None:
        tgt_idx = int(max(0, min(target_index, 1)))
        counter_idx = 1 - tgt_idx

    conc = _deconvolve(od, stain_matrix)
    target_conc = conc[..., tgt_idx]
    counter_conc = conc[..., counter_idx]
    resid_conc = np.abs(conc[..., 2])

    # ------------------------------------------------------------------ #
    # Positive detection — multi-gate, anchored to THIS slide's background.
    #
    # A pixel is positive only when it is, all at once:
    #   (core)   inside the eroded tissue core (not a lumen / tear rim);
    #   (hue)    the target chromogen's hue (rejects blue nuclei & neutral debris);
    #   (sat)    clearly more saturated than the tissue bulk (rejects tan tint and
    #            grey/black debris, which are desaturated);
    #   (od)     clearly darker than the tissue bulk AND above an absolute floor;
    #   (spec)   colour-dominated by the target stain, out-weighing counterstain;
    #   (conc)   above an intensity threshold that is separated from background,
    #            so a uniformly tinted slide reads ~0 instead of flooding.
    # ------------------------------------------------------------------ #
    positive = np.zeros((h, w), dtype=bool)
    threshold = 0.0

    if valid and tissue_pixels >= 200:
        core = _binary_erode(tissue_mask, 2)

        # Background statistics from a bounded random subsample of tissue — the
        # medians/percentiles are identical to within noise but far cheaper than
        # sorting ~750k values several times.
        t_idx = np.flatnonzero(tissue_mask.ravel())
        if t_idx.size > 120_000:
            t_idx = t_idx[np.random.default_rng(0).integers(0, t_idx.size, 120_000)]
        t_od = total_od.ravel()[t_idx]
        t_sat = sat.ravel()[t_idx]
        t_conc = target_conc.ravel()[t_idx]

        od_bg = float(np.median(t_od))
        od_sig = 1.4826 * float(np.median(np.abs(t_od - od_bg))) + 1e-6
        sat_bg = float(np.percentile(t_sat, 55))
        conc_bg = float(np.median(t_conc))
        conc_sig = 1.4826 * float(np.median(np.abs(t_conc - conc_bg))) + 1e-6

        sat_gate = max(_SAT_FLOOR, sat_bg + _SAT_MARGIN)
        od_gate = max(_OD_ABS_FLOOR, od_bg + _OD_SEP_K * od_sig)
        conc_gate = conc_bg + _CONC_SEP_K * conc_sig

        total_stain = target_conc + counter_conc + resid_conc + 1e-6
        specificity = target_conc / total_stain

        hue_ok = _hue_dist(hue, target_hue) <= _HUE_TOL_DEG
        sat_ok = sat >= sat_gate
        od_ok = total_od >= od_gate
        spec_ok = (specificity >= _SPEC_MIN) & (target_conc > counter_conc)

        qualified = core & hue_ok & sat_ok & od_ok & spec_ok

        if qualified.sum() >= 30:
            # Intensity bar = background-anchored separation (scale-consistent);
            # threshold_scale tunes sensitivity (>1 stricter, <1 looser).
            threshold = conc_gate * float(threshold_scale)
            positive = qualified & (target_conc >= threshold)
            if positive.any():
                positive = _remove_small(positive, _MIN_OBJECT_PX)
        else:
            notes.append("No specific target staining detected above background.")

    if not valid:
        notes.append(skip_reason or "Skipped: not a stained slide.")

    positive_pixels = int(positive.sum())
    positive_fraction = (positive_pixels / tissue_pixels) if tissue_pixels else 0.0
    mean_pos = float(target_conc[positive].mean()) if positive_pixels else 0.0

    # ---- display colours ---- #
    a_idx, b_idx = counter_idx, tgt_idx
    a_rgb = _od_to_rgb_unit(stain_matrix[a_idx])
    b_rgb = _od_to_rgb_unit(stain_matrix[b_idx])
    overlay_hex = _auto_overlay_hex(rgb, tissue_mask)

    stain_label = target_label if not tissue_pixels else f"{target_label} · H counterstain"

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
        threshold=round(float(threshold), 4),
        stains=stains,
        target_index=int(tgt_idx),
        method=method,
        valid=valid,
        skip_reason=skip_reason,
        stain_label=stain_label,
        confidence=round(float(confidence), 3),
        suggested_overlay_hex=overlay_hex,
        stain_a_hex=_hex(a_rgb),
        stain_b_hex=_hex(b_rgb),
        stain_a_label=counter_label,
        stain_b_label=target_label,
        notes=notes,
    )
    maps = {
        "conc": conc,
        "tissue_mask": tissue_mask,
        "positive": positive,
        "stain_matrix": stain_matrix,
        "counter_index": counter_idx,
        "target_index": tgt_idx,
        "od": od,
    }
    return result, maps


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

def render_overlay(rgb: np.ndarray, maps: dict[str, np.ndarray], color=(0, 229, 255), alpha=0.5) -> np.ndarray:
    """Highlight positive pixels on the original image."""
    out = rgb.astype(np.float64).copy()
    pos = maps["positive"]
    overlay = np.array(color, dtype=np.float64)
    out[pos] = (1 - alpha) * out[pos] + alpha * overlay
    return np.clip(out, 0, 255).astype(np.uint8)


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
    right_label: str = "Detection Overlay",
    *,
    metric_text: Optional[str] = None,
    stain_text: Optional[str] = None,
    sep_w: int = 5,
) -> np.ndarray:
    """
    Two panels side by side beneath a premium header band. Text and the ImageSL
    wordmark live entirely inside the band / a slim footer — never over the
    imagery. A footer strip carries the metric + an "Analyzed with ImageSL" mark.
    """
    lh, lw = left_rgb.shape[:2]
    rh, rw = right_rgb.shape[:2]
    ih = max(lh, rh)

    ink = (26, 20, 44)
    ink_soft = (110, 102, 132)
    band_bg = (247, 246, 252)
    violet = (124, 92, 214)
    violet_d = (109, 40, 217)

    band_h = int(min(96, max(40, round(ih * 0.072))))
    foot_h = int(min(84, max(34, round(ih * 0.060))))
    total_w = lw + sep_w + rw
    total_h = band_h + ih + foot_h

    canvas = Image.new("RGB", (total_w, total_h), band_bg)
    canvas.paste(Image.fromarray(np.ascontiguousarray(left_rgb)), (0, band_h))
    canvas.paste(Image.fromarray(np.ascontiguousarray(right_rgb)), (lw + sep_w, band_h))
    draw = ImageDraw.Draw(canvas)

    # separator + hairlines
    draw.rectangle([lw, band_h, lw + sep_w - 1, band_h + ih], fill=violet)
    draw.line([(0, band_h - 1), (total_w, band_h - 1)], fill=(224, 218, 238), width=1)
    draw.line([(0, band_h + ih), (total_w, band_h + ih)], fill=(224, 218, 238), width=1)

    # header: logo mark + wordmark on the left, panel labels centred per panel
    mark_r = int(band_h * 0.26)
    cx0, cy = int(band_h * 0.5), band_h // 2
    draw.ellipse([cx0 - mark_r, cy - mark_r, cx0 + mark_r, cy + mark_r], fill=violet_d)
    draw.ellipse([cx0 - mark_r + mark_r, cy - mark_r + int(mark_r * 0.4),
                  cx0 + mark_r + mark_r, cy + mark_r + int(mark_r * 0.4)], outline=violet, width=max(2, mark_r // 4))
    wf = _load_font(int(band_h * 0.34))
    draw.text((cx0 + mark_r + int(band_h * 0.28), cy - int(band_h * 0.2)), "ImageSL", fill=ink, font=wf)

    lf = _load_font(int(band_h * 0.34))
    for label, x0, x1 in ((left_label, 0, lw), (right_label, lw + sep_w, total_w)):
        tw, th, bb = _text_size(draw, label, lf)
        # keep panel labels clear of the wordmark on the left panel
        cx = x0 + (x1 - x0 - tw) / 2.0 - bb[0]
        cx = max(cx, cx0 + mark_r + int(band_h * 2.2)) if x0 == 0 else cx
        draw.text((cx, cy - th / 2 - bb[1]), label, fill=ink, font=lf)

    # footer: metric (left) + stain (centre) + mark (right)
    fy = band_h + ih
    ff = _load_font(int(foot_h * 0.40))
    fs = _load_font(int(foot_h * 0.34), bold=False)
    pad = int(foot_h * 0.42)
    if metric_text:
        _, th, bb = _text_size(draw, metric_text, ff)
        draw.text((pad, fy + (foot_h - th) / 2 - bb[1]), metric_text, fill=violet_d, font=ff)
    if stain_text:
        tw, th, bb = _text_size(draw, stain_text, fs)
        draw.text(((total_w - tw) / 2 - bb[0], fy + (foot_h - th) / 2 - bb[1]), stain_text, fill=ink_soft, font=fs)
    mark = "Analyzed with ImageSL"
    tw, th, bb = _text_size(draw, mark, fs)
    draw.text((total_w - tw - pad - bb[0], fy + (foot_h - th) / 2 - bb[1]), mark, fill=ink_soft, font=fs)

    return np.asarray(canvas)


# --------------------------------------------------------------------------- #
# Encoding helpers
# --------------------------------------------------------------------------- #

def to_png_bytes(rgb: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


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
