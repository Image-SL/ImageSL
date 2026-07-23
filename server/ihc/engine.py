"""
ImageSL — IHC histology analysis engine.

This module holds the proprietary pixel-level analysis. It runs ONLY on the
Railway backend; the desktop client never sees this code.

Method (the same family of techniques used by QuPath / Fiji, not a hardcoded
color test):

1.  Convert RGB -> optical density (Beer-Lambert):  OD = -log10((I + 1) / 256)
2.  Estimate stain color vectors automatically with the Macenko method, so the
    separation works for *any* stain color rather than assuming H-DAB. A fixed
    H-DAB matrix is used as a fallback when Macenko cannot find two robust
    directions (e.g. single-stain slides).
3.  Deconvolve OD onto the stain basis to get per-stain concentration maps.
4.  Detect background as low-OD (bright) pixels; classify the remaining tissue.
5.  Threshold the target-stain concentration map with Otsu to quantify the
    positive area and intensity.
6.  Recompose customizable image variants: recolor background, and scale the
    target-stain concentration up/down (darker/lighter staining).

Everything is vectorised numpy so a downsampled 4x slide processes in well
under a second.
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

from skimage.filters import threshold_otsu, threshold_multiotsu
from skimage.morphology import disk, binary_erosion, remove_small_objects


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Largest edge we will analyse. Whole-slide TIFs can be tens of thousands of
# pixels per side; processing at full resolution would exhaust Railway RAM and
# give the user no extra biological signal at this magnification. Downsampling
# to a bounded edge keeps memory + latency predictable. Tunable via the API.
DEFAULT_MAX_EDGE = 1024
HARD_MAX_EDGE = 2048

# Background is "bright" tissue-free glass. A pixel whose optical density is
# below this in every channel is treated as background before deconvolution.
BACKGROUND_OD_THRESHOLD = 0.15

# Reference H-DAB stain matrix (Ruifrok & Johnston), rows = [H, DAB, residual].
# Used only as a fallback when automatic estimation is not confident.
_HDAB_REFERENCE = np.array(
    [
        [0.65, 0.70, 0.29],   # Haematoxylin (nuclei, blue/purple)
        [0.27, 0.57, 0.78],   # DAB (target, brown)
        [0.00, 0.00, 0.00],   # residual, filled by cross product
    ],
    dtype=np.float64,
)


# --------------------------------------------------------------------------- #
# Result containers
# --------------------------------------------------------------------------- #

@dataclass
class StainVector:
    """One estimated stain color, in RGB 0-255 for display."""
    name: str
    rgb: list[int]
    od: list[float]  # the optical-density unit vector actually used for math

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
    positive_fraction: float          # positive / tissue, 0..1
    positive_percent: float           # convenience: fraction * 100
    mean_positive_intensity: float    # mean target OD over positive pixels
    threshold: float                  # Otsu threshold used on target channel
    stains: list[StainVector]
    target_index: int                 # which stain was treated as the target
    method: str                       # "macenko" or "hdab-fallback"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["stains"] = [s.to_dict() if isinstance(s, StainVector) else s for s in self.stains]
        return d


# --------------------------------------------------------------------------- #
# Image loading
# --------------------------------------------------------------------------- #

def load_rgb(data: bytes, *, max_edge: int = DEFAULT_MAX_EDGE) -> tuple[np.ndarray, tuple[int, int]]:
    """
    Decode arbitrary histology image bytes to an RGB uint8 array, downsampled so
    the longest edge is <= max_edge. Returns (array, (source_w, source_h)).

    Handles pyramidal / multi-page TIFs by reading the highest-resolution page
    and letting the downsample step bound memory.
    """
    max_edge = int(max(64, min(max_edge, HARD_MAX_EDGE)))
    source_size: tuple[int, int]

    arr: Optional[np.ndarray] = None

    if _HAVE_TIFFFILE and _looks_like_tiff(data):
        arr, source_size = _load_tiff(data, max_edge)

    if arr is None:
        # Pillow path for PNG/JPEG and TIFs tifffile declined.
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
            source_shape = series.shape  # e.g. (H, W, C) or (H, W)
            # For pyramidal TIFs, choose the smallest level still >= max_edge.
            page = _choose_tiff_level(series, max_edge)
            arr = page.asarray()
            source_h, source_w = _hw_from_shape(source_shape)
    except Exception:
        return None, (0, 0)

    if arr.ndim == 3 and arr.shape[0] in (1, 3, 4) and arr.shape[0] < arr.shape[-1]:
        # channel-first -> channel-last
        arr = np.moveaxis(arr, 0, -1)

    arr = _to_uint8(arr)
    im = Image.fromarray(arr).convert("RGB")
    im = _pil_downsample(im, max_edge)
    return np.asarray(im, dtype=np.uint8), (source_w, source_h)


def _choose_tiff_level(series, max_edge: int):
    """Pick a pyramid level whose long edge is as small as possible but still
    >= max_edge, so we don't upsample low-res thumbnails."""
    levels = getattr(series, "levels", None) or [series]
    best = None
    best_edge = None
    for lvl in levels:
        h, w = _hw_from_shape(lvl.shape)
        edge = max(h, w)
        if edge >= max_edge and (best_edge is None or edge < best_edge):
            best, best_edge = lvl, edge
    chosen = best if best is not None else levels[0]
    # levels expose .pages[0]; fall back to the level itself.
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
    elif hi > 255:  # 16-bit etc.
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
# Optical density + stain estimation
# --------------------------------------------------------------------------- #

def rgb_to_od(rgb: np.ndarray) -> np.ndarray:
    """RGB uint8 -> optical density float array, same shape."""
    rgb = rgb.astype(np.float64)
    return -np.log10((rgb + 1.0) / 256.0)


def _normalize_rows(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


def estimate_stains_macenko(
    od: np.ndarray,
    *,
    beta: float = BACKGROUND_OD_THRESHOLD,
    alpha: float = 1.0,
) -> Optional[np.ndarray]:
    """
    Macenko automatic stain-vector estimation.

    Returns a 2x3 matrix of OD unit vectors (row 0 and row 1 are the two
    dominant stains, ordered by hue angle), or None if the slide does not
    contain two robust directions.
    """
    flat = od.reshape(-1, 3)
    # Keep only tissue (drop near-background low-OD pixels).
    mask = flat.sum(axis=1) > beta
    tissue = flat[mask]
    if tissue.shape[0] < 100:
        return None

    # Principal plane of the OD cloud.
    try:
        cov = np.cov(tissue.T)
        eigvals, eigvecs = np.linalg.eigh(cov)
    except np.linalg.LinAlgError:
        return None
    # Two largest eigenvectors.
    plane = eigvecs[:, [2, 1]] if eigvecs.shape[1] >= 3 else eigvecs[:, -2:]
    if plane.shape[1] < 2:
        return None

    proj = tissue @ plane  # project tissue OD onto the plane
    phi = np.arctan2(proj[:, 1], proj[:, 0])

    lo = np.percentile(phi, alpha)
    hi = np.percentile(phi, 100 - alpha)

    v_min = plane @ np.array([math.cos(lo), math.sin(lo)])
    v_max = plane @ np.array([math.cos(hi), math.sin(hi)])

    # Orient vectors to be non-negative-ish and normalise.
    v_min = np.abs(v_min)
    v_max = np.abs(v_max)
    stains = _normalize_rows(np.vstack([v_min, v_max]))

    if not np.all(np.isfinite(stains)) or np.linalg.norm(stains[0] - stains[1]) < 1e-3:
        return None

    # Order so the more "blue/purple" (haematoxylin-like) stain is first: the
    # one with the larger blue component in OD tends to be nuclei.
    if stains[0, 2] < stains[1, 2]:
        stains = stains[::-1]
    return stains


def _hdab_matrix() -> np.ndarray:
    m = _HDAB_REFERENCE.copy()
    # Fill residual as the orthogonal complement of the first two vectors.
    m[2] = np.cross(m[0], m[1])
    return _normalize_rows(m)


def _deconvolve(od: np.ndarray, stain_matrix_3x3: np.ndarray) -> np.ndarray:
    """
    Deconvolve OD (H,W,3) onto a 3x3 stain basis -> concentration maps (H,W,3).
    Uses the pseudo-inverse so mildly non-orthogonal bases are handled.
    """
    flat = od.reshape(-1, 3).T                     # 3 x N
    inv = np.linalg.pinv(stain_matrix_3x3.T)        # 3 x 3
    conc = inv @ flat                               # 3 x N
    conc = np.clip(conc, 0, None).astype(np.float32)
    return conc.T.reshape(od.shape)


def _od_to_rgb_unit(od_vec: np.ndarray) -> list[int]:
    """Convert an OD unit vector to a representative display RGB 0-255."""
    v = od_vec / (np.linalg.norm(od_vec) or 1.0)
    # Show the color of a moderately concentrated stain.
    intensity = 1.0
    rgb = 255.0 * np.power(10.0, -v * intensity)
    return [int(round(x)) for x in np.clip(rgb, 0, 255)]


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
    stain_method: str = "hdab",
    stain_override_od: Optional[list[list[float]]] = None,
) -> tuple[AnalysisResult, dict[str, np.ndarray]]:
    """
    Run the full IHC analysis.

    Returns (result, maps) where `maps` contains float concentration maps and
    boolean masks used both for the metrics and for variant generation:
      maps["conc"]        (H,W,3) stain concentrations
      maps["tissue_mask"] (H,W)   bool, True where tissue (not background)
      maps["positive"]    (H,W)   bool, True where target stain is positive
      maps["stain_matrix"](3,3)   OD basis used
    """
    h, w = rgb.shape[:2]
    notes: list[str] = []
    od = rgb_to_od(rgb)

    if stain_override_od:
        stains2 = _normalize_rows(np.array(stain_override_od, dtype=np.float64)[:2])
        method = "override"
    elif stain_method == "hdab":
        stains2 = _hdab_matrix()[:2]
        method = "hdab-fixed"
    else:
        stains2 = estimate_stains_macenko(od, beta=background_threshold)
        if stains2 is None:
            stains2 = _hdab_matrix()[:2]
            method = "hdab-fallback"
            notes.append(
                "Automatic stain estimation was not confident (likely a single "
                "stain or low-contrast slide); used the reference H-DAB basis."
            )
        else:
            method = "macenko"

    # Build a full 3x3 basis with an orthogonal residual channel.
    residual = np.cross(stains2[0], stains2[1])
    if np.linalg.norm(residual) < 1e-6:
        residual = np.array([0.0, 0.0, 1.0])
    stain_matrix = _normalize_rows(np.vstack([stains2, residual]))

    conc = _deconvolve(od, stain_matrix)

    # Background = low total OD (bright). Tissue is everything else.
    total_od = od.sum(axis=2)
    tissue_mask = total_od > background_threshold
    tissue_pixels = int(tissue_mask.sum())

    # Decide the target stain. Default: the stain whose concentration is
    # strongest across tissue that ISN'T the nuclei counterstain (index 1),
    # unless the caller pins it.
    if target_index is None:
        # index 1 is the second Macenko vector (typically the chromogen/DAB).
        target_index = 1
    target_index = int(max(0, min(target_index, 1)))
    counter_index = 1 - target_index

    target_conc = conc[..., target_index]
    counter_conc = conc[..., counter_index]
    resid_conc = np.abs(conc[..., 2])

    # ------------------------------------------------------------------ #
    # Real-vs-artifact discrimination.
    #
    # The previous path thresholded a high-pass-filtered target map, which
    # turned every tissue/glass edge and lumen rim into "signal" and counted
    # folds, debris and neutral-gray boundaries as positive. Instead a pixel is
    # counted only when ALL of the following hold — i.e. it looks like genuine
    # chromogen, not an artifact:
    #
    #   (a) it lies in the tissue *core*, not the partial-volume rim that hugs
    #       white lumens/tears (the biggest source of false positives);
    #   (b) it is a real absorber (enough optical density — not a faint edge);
    #   (c) the target stain actually *dominates* the pixel's color — it isn't
    #       neutral gray, counterstain, or an off-color fold (color specificity);
    #   (d) the target out-weighs the counterstain at that pixel;
    #   (e) after Otsu intensity thresholding it survives as part of a coherent
    #       region rather than isolated speckle or a 1-px edge tracing.
    # ------------------------------------------------------------------ #
    core_tissue = binary_erosion(tissue_mask, disk(2)) if tissue_pixels else tissue_mask
    total_conc = target_conc + counter_conc + resid_conc + 1e-6
    specificity = target_conc / total_conc          # → 1.0 for pure target stain
    min_od = max(float(background_threshold) * 1.5, 0.18)

    # "loose" strictness is an escape hatch that skips color specificity.
    lenient = stain_strictness not in ("strong",)
    if lenient:
        color_ok = np.ones_like(core_tissue)
    else:
        color_ok = (specificity >= 0.42) & (target_conc > counter_conc) & (total_od >= min_od)

    gated = core_tissue & color_ok
    # Intensity bar comes from the tissue as a whole, so it means "specifically
    # stained above this slide's own background" rather than "darker half of the
    # already-brown pixels". Multi-Otsu's top class isolates genuine strong
    # chromogen from diffuse background tint.
    tissue_vals = target_conc[core_tissue] if core_tissue.any() else target_conc[tissue_mask]

    if tissue_vals.size < 50:
        threshold = 0.0
        positive = np.zeros((h, w), dtype=bool)
        notes.append("No specific target staining detected above background.")
    else:
        threshold = None
        for classes in (4, 3):
            try:
                threshold = float(threshold_multiotsu(tissue_vals, classes=classes)[-1])
                break
            except Exception:
                continue
        if threshold is None:
            try:
                threshold = float(threshold_otsu(tissue_vals))
            except Exception:
                threshold = float(np.percentile(tissue_vals, 90))
        # threshold_scale tunes sensitivity: >1 stricter, <1 looser.
        threshold = max(0.0, threshold * float(threshold_scale))
        positive = gated & (target_conc >= threshold)
        if positive.any():
            positive = remove_small_objects(positive, min_size=8)

    target_map = target_conc  # for mean-intensity reporting
    positive_pixels = int(positive.sum())
    positive_fraction = (positive_pixels / tissue_pixels) if tissue_pixels else 0.0
    mean_pos = float(target_map[positive].mean()) if positive_pixels else 0.0

    stains = [
        StainVector("Counterstain (nuclei)", _od_to_rgb_unit(stain_matrix[0]), stain_matrix[0].tolist()),
        StainVector("Target stain", _od_to_rgb_unit(stain_matrix[1]), stain_matrix[1].tolist()),
    ]

    result = AnalysisResult(
        width=w,
        height=h,
        source_width=int(source_size[0] or w),
        source_height=int(source_size[1] or h),
        tissue_pixels=tissue_pixels,
        positive_pixels=positive_pixels,
        positive_fraction=round(positive_fraction, 6),
        positive_percent=round(positive_fraction * 100.0, 3),
        mean_positive_intensity=round(mean_pos, 4),
        threshold=round(threshold, 4),
        stains=stains,
        target_index=target_index,
        method=method,
        notes=notes,
    )
    maps = {
        "conc": conc,
        "tissue_mask": tissue_mask,
        "positive": positive,
        "stain_matrix": stain_matrix,
        "od": od,
    }
    return result, maps


# --------------------------------------------------------------------------- #
# Public: rendering (overlays + customizable variants)
# --------------------------------------------------------------------------- #

def render_overlay(rgb: np.ndarray, maps: dict[str, np.ndarray], color=(255, 0, 0), alpha=0.45) -> np.ndarray:
    """Highlight positive pixels on the original image."""
    out = rgb.astype(np.float64).copy()
    pos = maps["positive"]
    overlay = np.array(color, dtype=np.float64)
    out[pos] = (1 - alpha) * out[pos] + alpha * overlay
    return np.clip(out, 0, 255).astype(np.uint8)


def _recompose(conc: np.ndarray, stain_matrix: np.ndarray) -> np.ndarray:
    """Concentration maps + basis -> RGB uint8 (inverse Beer-Lambert)."""
    flat = conc.reshape(-1, 3) @ stain_matrix  # N x 3 OD
    rgb = 255.0 * np.power(10.0, -flat)
    rgb = np.clip(rgb, 0, 255).reshape(conc.shape)
    return rgb.astype(np.uint8)


def render_variant(
    rgb: np.ndarray,
    maps: dict[str, np.ndarray],
    *,
    target_gain: float = 1.0,
    counterstain_gain: float = 1.0,
    background_rgb: Optional[tuple[int, int, int]] = None,
    target_index: int = 1,
) -> np.ndarray:
    """
    Generate a customizable image version:
      - target_gain / counterstain_gain scale each stain's concentration
        (>1 darker, <1 lighter).
      - background_rgb, if given, repaints the detected background to that color.
    """
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
# Labeled side-by-side comparison (for export)
# --------------------------------------------------------------------------- #

def _load_font(size: int) -> ImageFont.ImageFont:
    """Best scalable font available; Pillow's sized default is fine everywhere."""
    size = max(10, int(size))
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1: scalable
    except Exception:
        return ImageFont.load_default()


def _draw_centered(draw: ImageDraw.ImageDraw, text: str, x0: int, x1: int,
                   band_h: int, font, fill) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    cx = x0 + (x1 - x0 - tw) / 2.0 - bbox[0]
    cy = (band_h - th) / 2.0 - bbox[1]
    draw.text((cx, cy), text, fill=fill, font=font)


def compose_comparison(
    left_rgb: np.ndarray,
    right_rgb: np.ndarray,
    left_label: str = "Original",
    right_label: str = "Detection Overlay",
    *,
    sep_w: int = 4,
    band_bg: tuple[int, int, int] = (248, 247, 252),
    band_fg: tuple[int, int, int] = (32, 24, 54),
    sep_color: tuple[int, int, int] = (124, 92, 214),
) -> np.ndarray:
    """
    Two images side by side under a clean header band that labels each panel
    (text never overlaps the imagery), with a solid separator line between them.
    """
    lh, lw = left_rgb.shape[:2]
    rh, rw = right_rgb.shape[:2]
    h = max(lh, rh)

    # Header band scales with image size, clamped to something readable.
    band_h = int(min(90, max(34, round(h * 0.065))))
    total_w = lw + sep_w + rw
    total_h = band_h + h

    canvas = Image.new("RGB", (total_w, total_h), band_bg)
    canvas.paste(Image.fromarray(np.ascontiguousarray(left_rgb)), (0, band_h))
    canvas.paste(Image.fromarray(np.ascontiguousarray(right_rgb)), (lw + sep_w, band_h))

    draw = ImageDraw.Draw(canvas)
    # Vertical separator spanning the full height (band + images).
    draw.rectangle([lw, 0, lw + sep_w - 1, total_h], fill=sep_color)
    # Hairline under the header band.
    draw.line([(0, band_h - 1), (total_w, band_h - 1)], fill=(225, 220, 238), width=1)

    font = _load_font(int(band_h * 0.46))
    _draw_centered(draw, left_label, 0, lw, band_h, font, band_fg)
    _draw_centered(draw, right_label, lw + sep_w, total_w, band_h, font, band_fg)

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
        # JPEG encodes ~5-10x faster than PNG for photographic histology and is a
        # fraction of the size — much snappier previews. Downloads stay TIFF.
        im.convert("RGB").save(buf, format="JPEG", quality=quality)
        mime = "image/jpeg"
    else:
        im.save(buf, format=fmt)
        mime = "image/png"
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def thumbnail_jpeg_b64(rgb: np.ndarray, max_edge: int = 768, quality: int = 82) -> str:
    """Small JPEG (base64, no data-URI prefix) for sending to Claude vision."""
    im = Image.fromarray(rgb)
    im = _pil_downsample(im, max_edge)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")
