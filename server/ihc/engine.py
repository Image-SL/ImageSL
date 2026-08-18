from __future__ import annotations

import base64
import io
import math
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    import tifffile
    _HAVE_TIFFFILE = True
except Exception:
    _HAVE_TIFFFILE = False

from skimage.morphology import disk
from skimage.morphology import erosion as _grey_erosion
from skimage.measure import label as _label
from skimage.filters import threshold_otsu as _otsu

try:
    from scipy.ndimage import uniform_filter as _uniform_filter
    _HAVE_SCIPY = True
except Exception:
    _HAVE_SCIPY = False

from . import detect
from . import regions as regions_mod

DEFAULT_MAX_EDGE = 1024
HARD_MAX_EDGE = 2048

BACKGROUND_OD_THRESHOLD = 0.15

_WHITE_PCT          = 99.0
_WHITE_MIN          = 48.0
_WHITE_APPLY_BELOW  = 245.0
_WHITE_GLASS_SEP    = 20.0
_TISSUE_THR_MAX     = 0.55
_TISSUE_BG_MIN_FRAC = 0.02
_BG_GLASS_OD_MAX    = 0.075
_GLASS_MAX_FRAC     = 0.92
_GLASS_CONTRAST_K   = 4.0
_BG_GLASS_SAT       = 0.20
_CHROMA_RESCUE_SAT  = 0.22
_TEXTURE_WIN        = 7
_TEXTURE_REL        = 0.35
_TEXTURE_ABS        = 0.006
_TEXTURE_OD_GUARD   = 1.6
_TEXTURE_FLAT_P95   = 0.015
_HOLE_FRAC          = 0.02
_SPECK_FRAC         = 2e-5

_HDAB_REFERENCE = np.array(
    [
        [0.65, 0.70, 0.29],
        [0.27, 0.57, 0.78],
        [0.00, 0.00, 0.00],
    ],
    dtype=np.float64,
)

_REF_BASES = {
    "H-DAB":  {"label": "H-DAB (brown)",  "hue": 32.0, "band": (10.0, 78.0),
               "od": [[0.650, 0.700, 0.290], [0.270, 0.570, 0.780]]},
    "H-Red":  {"label": "Red chromogen",  "hue": 2.0,  "band": (330.0, 18.0),
               "od": [[0.650, 0.700, 0.290], [0.210, 0.760, 0.615]]},
    "H-AP":   {"label": "Alk-phos (red)", "hue": 350.0, "band": (325.0, 12.0),
               "od": [[0.650, 0.700, 0.290], [0.190, 0.760, 0.620]]},
    "H-GREEN":{"label": "Green chromogen","hue": 140.0, "band": (95.0, 180.0),
               "od": [[0.650, 0.700, 0.290], [0.800, 0.327, 0.503]]},
    "H&E":    {"label": "H&E (eosin)",    "hue": 330.0, "band": (300.0, 355.0),
               "od": [[0.650, 0.700, 0.290], [0.070, 0.990, 0.110]]},
}

ENABLED_FAMILIES: tuple[str, ...] = ("H-DAB",)

_DAB_HUE_BAND = (10.0, 78.0)

OVERLAY_GREEN: tuple[int, int, int] = (57, 255, 20)
OVERLAY_CHOICES: dict[str, tuple[int, int, int]] = {"green": OVERLAY_GREEN}
OVERLAY_DEFAULT_HEX = "#39ff14"

@dataclass
class StainVector:
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
    valid: bool = True
    skip_reason: Optional[str] = None
    stain_label: str = ""
    confidence: float = 1.0
    suggested_overlay_hex: str = OVERLAY_DEFAULT_HEX
    background_pixels: int = 0
    background_percent: float = 0.0
    tissue_percent: float = 0.0
    tissue_threshold: float = 0.0
    tissue_method: str = ""
    white_point: list[float] = field(default_factory=list)
    chromogen_present: bool = True
    stain_a_hex: str = "#3b5bdb"
    stain_b_hex: str = "#a1531f"
    stain_a_label: str = "Stain A"
    stain_b_label: str = "Stain B"
    level: int = 0
    auto_level: int = 0
    level_count: int = 1
    ladder_hi: float = detect.LADDER_STRICT
    ladder_lo: float = detect.LADDER_LOOSE
    detection_bar: float = 0.0
    detection_floor: float = 0.0
    texture_sigma: float = 0.0
    separability: float = 0.0
    bar_discard: float = 0.0
    single_population: bool = False
    objects: int = 0
    median_object_px: int = 0
    mean_object_px: float = 0.0
    region_count: int = 0
    include_regions: int = 0
    exclude_regions: int = 0
    region_added_pixels: int = 0
    region_removed_pixels: int = 0
    tissue_pixels_total: int = 0
    chromogen: str = ""
    compartment: str = ""
    marker: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["stains"] = [s.to_dict() if isinstance(s, StainVector) else s for s in self.stains]
        return d

def load_rgb(data: bytes, *, max_edge: int = DEFAULT_MAX_EDGE) -> tuple[np.ndarray, tuple[int, int]]:
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

def rgb_to_od(rgb: np.ndarray, white: Optional[np.ndarray] = None) -> np.ndarray:
    rgb = rgb.astype(np.float32)
    if white is None:
        return -np.log10((rgb + 1.0) / 256.0)
    w = np.asarray(white, dtype=np.float32).reshape(1, 1, -1)
    return np.clip(-np.log10((rgb + 1.0) / (w + 1.0)), 0.0, None)

def estimate_white_point(rgb: np.ndarray) -> np.ndarray:
    flat = rgb.reshape(-1, 3)
    step = max(1, flat.shape[0] // 200_000)
    sample = flat[::step].astype(np.float32)
    fallback = np.percentile(sample, _WHITE_PCT, axis=0)

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
    sub = below[::3, ::3]
    if not sub.any():
        return True
    od_med = float(np.median(total_od[::3, ::3][sub]))
    chroma = float(np.median(sat[::3, ::3][sub]))
    return od_med <= _BG_GLASS_OD_MAX and chroma <= _BG_GLASS_SAT

def white_is_glass(rgb: np.ndarray, white: np.ndarray) -> bool:
    med = float(np.median(rgb[::4, ::4]))
    return float(np.min(white)) - med > _WHITE_GLASS_SEP

def _rgb_to_hsv(rgb01: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rgb01 = rgb01.astype(np.float32, copy=False)
    r, g, b = rgb01[..., 0], rgb01[..., 1], rgb01[..., 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    diff = mx - mn
    dsafe = diff + (diff == 0)
    rc = (mx - r) / dsafe
    gc = (mx - g) / dsafe
    bc = (mx - b) / dsafe
    h = np.where(mx == r, bc - gc, np.where(mx == g, 2.0 + rc - bc, 4.0 + gc - rc))
    hue = (h / 6.0) % 1.0 * 360.0
    hue[diff == 0] = 0.0
    sat = np.where(mx > 1e-6, diff / (mx + 1e-6), 0.0)
    return hue.astype(np.float32), sat.astype(np.float32), mx

def _hue_dist(h: np.ndarray, target: float) -> np.ndarray:
    d = np.abs(h - target) % 360.0
    return np.minimum(d, 360.0 - d)

def _in_hue_band(h: np.ndarray, band) -> np.ndarray:
    lo, hi = float(band[0]), float(band[1])
    return (h >= lo) & (h <= hi) if lo <= hi else (h >= lo) | (h <= hi)

def _hue_band_for(target_hue: float, method: str):
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

def _classify_stain(rgb: list[int]) -> tuple[str, float]:
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

def _local_std(x: np.ndarray, size: int = _TEXTURE_WIN) -> Optional[np.ndarray]:
    if not _HAVE_SCIPY:
        return None
    x = x.astype(np.float32, copy=False)
    m = _uniform_filter(x, size)
    m2 = _uniform_filter(x * x, size)
    return np.sqrt(np.clip(m2 - m * m, 0.0, None))

def _fill_small_holes(mask: np.ndarray, max_px: int) -> np.ndarray:
    inv = ~mask
    if not inv.any():
        return mask
    lbl = _label(inv, connectivity=1)
    if lbl.max() == 0:
        return mask
    counts = np.bincount(lbl.ravel())
    fill = counts <= max_px
    edge = np.unique(np.concatenate([lbl[0], lbl[-1], lbl[:, 0], lbl[:, -1]]))
    fill[edge] = False
    fill[0] = False
    return mask | fill[lbl]

def segment_tissue(
    rgb: np.ndarray,
    *,
    hue=None, sat=None, val=None,
    od_floor: float = BACKGROUND_OD_THRESHOLD,
    white: Optional[np.ndarray] = None,
    od: Optional[np.ndarray] = None,
) -> tuple[np.ndarray, np.ndarray, dict]:
    h, w = rgb.shape[:2]
    if hue is None or sat is None or val is None:
        hue, sat, val = _rgb_to_hsv(rgb.astype(np.float32) / 255.0)
    if white is None:
        white = estimate_white_point(rgb)

    total_od = np.clip(rgb_to_od(rgb, white), 0.0, None).sum(axis=2)

    thr = float(od_floor)
    below = total_od < thr
    method = "glass (transmittance)"
    if float(below.mean()) < _TISSUE_BG_MIN_FRAC:
        thr = 0.0
        below = np.zeros_like(below)
        method = "full-field (no glass in frame)"
    elif not _bg_class_is_glass(total_od, sat, below):
        thr = 0.0
        below = np.zeros_like(below)
        method = "full-field (palest class is tissue, not glass)"
    elif float(below.mean()) > _GLASS_MAX_FRAC and total_od.size:
        strong = float(np.percentile(total_od, 99.5))
        base = max(float(np.median(total_od[below])), 0.02)
        if strong > _GLASS_CONTRAST_K * base:
            thr = 0.0
            below = np.zeros_like(below)
            method = "full-field (faint section, not an empty slide)"

    mask = (total_od >= thr) | ((sat >= _CHROMA_RESCUE_SAT) & (total_od >= od_floor * 0.55))

    std = _local_std(val)
    if std is not None and mask.any() and thr > 0.0:
        med = float(np.median(std[mask]))
        flat_cut = min(_TEXTURE_ABS, med * _TEXTURE_REL) if med > 0 else _TEXTURE_ABS
        smooth = std < flat_cut
        mask &= ~(smooth & (total_od < thr * _TEXTURE_OD_GUARD))
        method += " + texture"

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

def assess_slide(rgb: np.ndarray, *, hue=None, sat=None, val=None,
                 total_od=None, tissue=None) -> tuple[bool, Optional[str], float]:
    if hue is None or sat is None or val is None:
        hue, sat, val = _rgb_to_hsv(rgb.astype(np.float32) / 255.0)
    if total_od is None:
        total_od = rgb_to_od(rgb).sum(axis=2)
    if tissue is None:
        tissue = total_od > BACKGROUND_OD_THRESHOLD

    tissue_frac = float(tissue.mean())

    if tissue_frac < 0.02:
        return (False, "No tissue detected — the image is essentially blank.", 0.9)

    std = _local_std(val)
    if std is not None:
        fine = float(np.percentile(std, 95))
        if fine < _TEXTURE_FLAT_P95:
            return (False, "Doesn't look like a stained slide — the image carries no "
                           "cellular texture at any scale, so it was skipped.", 0.8)

    strong = sat > 0.55
    strong_frac = float(strong.mean())
    if strong_frac > 0.02:
        sh = hue[strong]
        stainy = ((sh <= 55) | (sh >= 340) | ((sh >= 200) & (sh <= 320)))
        offband_frac = float((~stainy).mean()) * strong_frac
        if offband_frac > 0.12:
            return (False,
                    "Doesn't look like a stained slide — it contains large vivid "
                    "non-tissue colours (greens/blues), so it was skipped.",
                    0.75)

    tsat = sat[tissue]
    thue = hue[tissue]
    vivid = thue[tsat > 0.30]
    if vivid.size > 500:
        hist, _ = np.histogram(vivid, bins=36, range=(0, 360))
        occupied = int((hist > vivid.size * 0.01).sum())
        mean_sat = float(tsat.mean())
        if occupied >= 14 and mean_sat > 0.35:
            return (False,
                    "Doesn't look like a stained slide — colours are spread across "
                    "the spectrum like a photograph, so it was skipped.",
                    0.6)

    return (True, None, 1.0)

def _hdab_matrix() -> np.ndarray:
    m = _HDAB_REFERENCE.copy()
    m[2] = np.cross(m[0], m[1])
    return _normalize_rows(m)

def estimate_stains_macenko(od_tissue: np.ndarray, *, alpha: float = 1.0) -> Optional[np.ndarray]:
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
    if stains[0, 2] < stains[1, 2]:
        stains = stains[::-1]
    return stains

def _circular_median_deg(angles: np.ndarray) -> float:
    a = np.deg2rad(angles)
    return float(np.rad2deg(np.arctan2(np.sin(a).mean(), np.cos(a).mean())) % 360.0)

def _detect_family(total_od, sat, hue, tissue):
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
        return default, 32.0, has_counter, False

    chue = _circular_median_deg(chrom)
    lo, hi = _DAB_HUE_BAND
    if lo <= chue < hi:
        fam = "H-DAB"
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
    target_hue = ref["hue"]
    return (stain_matrix, counter_idx, target_idx, fam, target_hue,
            counter_label, target_label, in_band)

def _deconvolve(od: np.ndarray, stain_matrix_3x3: np.ndarray) -> np.ndarray:
    flat = od.reshape(-1, 3).T
    inv = np.linalg.pinv(stain_matrix_3x3.T)
    conc = inv @ flat
    conc = np.clip(conc, 0, None).astype(np.float32)
    return conc.T.reshape(od.shape)

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
    h, w = rgb.shape[:2]
    notes: list[str] = []

    rgb01 = rgb.astype(np.float32) / 255.0
    hue, sat, val = _rgb_to_hsv(rgb01)

    white = estimate_white_point(rgb)
    od = rgb_to_od(rgb, white)

    tissue_mask, total_od, bg_info = segment_tissue(
        rgb, hue=hue, sat=sat, val=val, od_floor=background_threshold,
        white=white, od=od)
    tissue_pixels = int(tissue_mask.sum())

    valid, skip_reason, confidence = assess_slide(
        rgb, hue=hue, sat=sat, val=val, total_od=total_od, tissue=tissue_mask)

    black_mode = bool(stain_choice and stain_choice.get("black"))
    chromogen_in_band = True
    if stain_choice and stain_choice.get("target_od"):
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
        chromogen_in_band = True

    if target_index is not None:
        tgt_idx = int(max(0, min(target_index, 1)))
        counter_idx = 1 - tgt_idx

    conc = _deconvolve(od, stain_matrix)
    target_conc = conc[..., tgt_idx]

    min_px = int(stain_choice.get("min_px", 0)) if stain_choice else 0

    _basis_override = bool(stain_choice or stain_override_od) or \
        method not in ("H-DAB", "hdab-reference")
    band_mask = None

    det = detect.detect(
        od, tissue_mask,
        level=level,
        min_area_px=(min_px or None),
        hue_band_mask=band_mask,
        target_od=(stain_matrix[tgt_idx] if _basis_override else None),
        counter_od=(stain_matrix[counter_idx] if _basis_override else None),
        saturation=sat,
        debug=detect_debug,
    )
    notes.extend(det.notes)

    if det.chromogen_share < detect.CHROMOGEN_SHARE_MIN and not stain_choice:
        chromogen_in_band = False
        notes.append(
            "No brown (DAB) chromogen found — the strongly absorbing material on "
            "this slide does not have DAB's absorbance signature. Any positive "
            "area reported here is therefore NOT a DAB measurement: if the slide "
            "is neutral material (ink, dust, a fold) it will read at or near "
            "zero, but if it carries a different chromogen the structures are "
            "still measured and the percentage will look ordinary. This build "
            "measures DAB only — do not read this figure as a DAB result.")

    built = regions_mod.build(regions, h, w, detect.N_LEVELS)
    tissue_base = tissue_mask
    tissue_pixels_total = tissue_pixels
    auto_positive = (det.level_map <= det.level) & tissue_mask
    positive, tissue_mask = regions_mod.apply(
        det.level_map, tissue_mask, det.level, built, detect.N_LEVELS)
    region_added = int((positive & ~auto_positive).sum())
    region_removed = int((auto_positive & ~positive).sum())
    if built["count"]:
        bits = []
        if built.get("include_count"):
            bits.append(f"{built['include_count']} include (+{region_added:,} px)")
        if built.get("exclude_count"):
            bits.append(f"{built['exclude_count']} exclude (−{region_removed:,} px)")
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

    a_idx, b_idx = counter_idx, tgt_idx
    a_rgb = _od_to_rgb_unit(stain_matrix[a_idx])
    b_rgb = _od_to_rgb_unit(stain_matrix[b_idx])
    overlay_hex = OVERLAY_DEFAULT_HEX

    chromogen = target_label
    if stain_choice:
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
    lbl = _label(mask, connectivity=1)
    if lbl.max() == 0:
        return mask
    counts = np.bincount(lbl.ravel())
    keep = counts >= min_px
    keep[0] = False
    return keep[lbl]

def render_overlay(rgb: np.ndarray, maps: dict[str, np.ndarray],
                   color=OVERLAY_GREEN, alpha=0.5) -> np.ndarray:
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
    conc = maps["conc"][..., index].astype(np.float64)
    stain_matrix = maps["stain_matrix"]
    if color is None:
        color = _od_to_rgb_unit(stain_matrix[index])
    color = np.array(color, dtype=np.float64)
    bg = np.array(background_rgb, dtype=np.float64)

    amt = 1.0 - np.power(10.0, -np.clip(conc * gain, 0, None))
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
    left_rgb = np.ascontiguousarray(left_rgb)
    right_rgb = np.ascontiguousarray(right_rgb)
    ph = max(left_rgb.shape[0], right_rgb.shape[0])
    pw = max(left_rgb.shape[1], right_rgb.shape[1])

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

    mask = Image.new("L", (pw, ph), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    for x, arr in zip(xs, (left_rgb, right_rgb)):
        panel = Image.new("RGB", (pw, ph), (255, 255, 255))
        panel.paste(Image.fromarray(arr), (0, 0))
        canvas.paste(panel, (x, margin), mask)

    draw = ImageDraw.Draw(canvas)
    for x in xs:
        draw.rounded_rectangle([x, margin, x + pw - 1, margin + ph - 1],
                               radius=radius, outline=(223, 220, 233), width=1)

    cf = _load_font(cap_size)
    cy = margin + ph + cap_gap
    for x, label in zip(xs, (left_label, right_label)):
        tw, th, bb = _text_size(draw, label, cf)
        draw.text((x + (pw - tw) / 2.0 - bb[0], cy - bb[1]), label, fill=ink, font=cf)

    mf = _load_font(mark_size, bold=False)
    mark = "Analyzed with ImageSL"
    tw, th, bb = _text_size(draw, mark, mf)
    my = cy + cap_h + foot_gap
    dot_r = max(2, int(round(mark_size * 0.26)))
    gap = dot_r * 3
    group_w = dot_r * 2 + gap + tw
    gx = (total_w - group_w) / 2.0
    draw.ellipse([gx, my + th / 2 - dot_r, gx + dot_r * 2, my + th / 2 + dot_r], fill=violet)
    draw.text((gx + dot_r * 2 + gap - bb[0], my - bb[1]), mark, fill=ink_soft, font=mf)

    return np.asarray(canvas)

def to_png_bytes(rgb: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="PNG", optimize=True)
    return buf.getvalue()

def original_data_uri(rgb: np.ndarray) -> str:
    buf = io.BytesIO()
    im = Image.fromarray(rgb).convert("RGB")
    try:
        im.save(buf, format="WEBP", lossless=True, quality=80, method=0)
        mime = "image/webp"
    except Exception:
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
    arr = np.asarray(level_map, dtype=np.uint8)
    h, w = arr.shape[:2]
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    rgb[..., 0] = arr
    if tissue_mask is None:
        rgb[..., 1] = 255
    else:
        rgb[..., 1] = np.asarray(tissue_mask, dtype=bool).astype(np.uint8) * 255
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG", compress_level=6)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
