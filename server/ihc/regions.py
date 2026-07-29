"""
Manual regions — the hand tools that sit on top of the automatic detection.

A region is a shape the user drew on the slide plus what it means. Coordinates
are normalised (0..1 of width / height) so a region survives any working
resolution, export size or re-analysis, and so the browser and the server
rasterise exactly the same area.

Modes
-----
``focus``    Measure only inside these regions. Tissue outside them stops
             counting — for both the positive area and the denominator — which
             is how you quantify one cortical layer, one core of a TMA, or one
             half of a section.
``ignore``   Cut these regions out entirely: a fold, a pen mark, a bubble, a
             torn edge. Removed from the tissue denominator as well, so it does
             not silently dilute the percentage.

A region says **where** to measure and nothing else. Both modes move the
numerator and the denominator together, so the reported percentage is always
"positive area / tissue actually measured" over exactly the area the user chose.

There used to be a third mode ("More here" / "Less here") that shifted the
sensitivity inside a drawn shape. It is gone on purpose. A per-region operating
point makes the headline percentage a mixture of several different decision
rules, which is not a quantity that can be compared between slides or reported
in a method — and because the shift applied only inside the shape, the same
structure was counted or not depending on which side of a hand-drawn line it
fell. Sensitivity is now one setting for the whole slide, recorded in the export
next to the number it produced.
"""

from __future__ import annotations

from typing import Iterable, Optional

import numpy as np

MODES = ("focus", "ignore")

# --------------------------------------------------------------------------- #
# The rasterisation rule
# --------------------------------------------------------------------------- #
# A pixel is inside a region when its CENTRE is inside the shape. That rule is
# stated here, in one place, and implemented identically in the browser
# (app.js `rasterRegion`) — it is not inherited from a drawing library.
#
# It has to be, because the two sides do not share one. The server used PIL,
# whose `rectangle` includes both endpoints, and the browser used a canvas fill,
# which excludes the far edge and antialiases the boundary. Drawing the same box
# therefore produced masks differing by a row and a column: on a 512x384 frame,
# 368 pixels of denominator, which is a percentage that disagrees with the CSV
# in the third significant figure. For a measurement that gets published, "the
# number on screen is not the number in the export" is not a rounding detail.
#
# Everything below is an exact, integer-boundary construction — no antialiasing,
# no sub-pixel coverage, nothing either side has to approximate.


def _pts(points, w: int, h: int) -> list[tuple[float, float]]:
    """Normalised 0..1 coordinates → pixel coordinates on the analysis grid.

    1.0 maps to the OUTER EDGE of the last pixel (w), not to the last pixel's
    centre (w-1). With the centre convention a region dragged to the very edge of
    the slide stopped half a pixel short, so the last row and column were never
    included — and two regions splitting the frame in half neither tiled it nor
    covered it: 480 pixels of a 640x480 frame belonged to neither half. Against
    the outer edge, [0,0.5] and [0.5,1] partition the grid exactly.
    """
    out = []
    for p in points or []:
        try:
            x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        out.append((x * w, y * h))
    return out


def _rect_mask(x0, y0, x1, y1, h: int, w: int) -> np.ndarray:
    """Pixels whose centre lies within [x0,x1]x[y0,y1]."""
    xs = np.arange(w) + 0.5
    ys = np.arange(h) + 0.5
    col = (xs >= min(x0, x1)) & (xs <= max(x0, x1))
    row = (ys >= min(y0, y1)) & (ys <= max(y0, y1))
    return row[:, None] & col[None, :]


def _ellipse_mask(x0, y0, x1, y1, h: int, w: int) -> np.ndarray:
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    rx, ry = abs(x1 - x0) / 2.0, abs(y1 - y0) / 2.0
    if rx <= 0 or ry <= 0:
        return np.zeros((h, w), dtype=bool)
    xs = (np.arange(w) + 0.5 - cx) / rx
    ys = (np.arange(h) + 0.5 - cy) / ry
    return (ys[:, None] ** 2 + xs[None, :] ** 2) <= 1.0


def _polygon_mask(pts, h: int, w: int) -> np.ndarray:
    """Even-odd scanline fill evaluated at pixel centres.

    Written out rather than delegated so the browser can run the identical rule:
    for each row, find where the polygon's edges cross that row's centre line,
    and a pixel is inside when an odd number of crossings lie to its left.
    """
    p = np.asarray(pts, dtype=np.float64)
    if p.shape[0] < 3:
        return np.zeros((h, w), dtype=bool)
    x0, y0 = p[:, 0], p[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
    dy = y1 - y0
    xs = np.arange(w) + 0.5
    mask = np.zeros((h, w), dtype=bool)
    for r in range(h):
        yy = r + 0.5
        crossing = ((y0 > yy) != (y1 > yy)) & (dy != 0.0)
        if not crossing.any():
            continue
        xi = x0[crossing] + (yy - y0[crossing]) * (x1[crossing] - x0[crossing]) / dy[crossing]
        xi.sort()
        mask[r] = (np.searchsorted(xi, xs, side="right") % 2) == 1
    return mask


def rasterize(region: dict, h: int, w: int) -> Optional[np.ndarray]:
    """One region → a boolean mask of the analysis grid."""
    kind = str(region.get("kind") or region.get("type") or "rect").lower()
    pts = _pts(region.get("points"), w, h)
    if not pts:
        return None

    if kind == "rect" and len(pts) >= 2:
        (ax, ay), (bx, by) = pts[0], pts[1]
        return _rect_mask(ax, ay, bx, by, h, w)
    if kind == "ellipse" and len(pts) >= 2:
        (ax, ay), (bx, by) = pts[0], pts[1]
        return _ellipse_mask(ax, ay, bx, by, h, w)
    if kind == "brush":
        r = max(1.0, float(region.get("radius", 0.02)) * w)
        mask = np.zeros((h, w), dtype=bool)
        xs = np.arange(w) + 0.5
        ys = np.arange(h) + 0.5
        for i, (x, y) in enumerate(pts):
            mask |= ((ys[:, None] - y) ** 2 + (xs[None, :] - x) ** 2) <= r * r
            if i + 1 < len(pts):             # capsule between consecutive points
                nx, ny = pts[i + 1]
                dx, dy = nx - x, ny - y
                ll = dx * dx + dy * dy
                if ll <= 0:
                    continue
                t = np.clip(((ys[:, None] - y) * dy + (xs[None, :] - x) * dx) / ll, 0.0, 1.0)
                mask |= ((ys[:, None] - (y + t * dy)) ** 2
                         + (xs[None, :] - (x + t * dx)) ** 2) <= r * r
        return mask
    if len(pts) >= 3:                        # polygon / lasso
        return _polygon_mask(pts, h, w)
    return None


def build(regions: Optional[Iterable[dict]], h: int, w: int, n_levels: int) -> dict:
    """Rasterise every region into the two masks detection actually needs."""
    focus = None
    ignore = np.zeros((h, w), dtype=bool)
    used = n_focus = n_ignore = 0

    for region in regions or []:
        if not isinstance(region, dict):
            continue
        mode = str(region.get("mode", "")).lower()
        if mode not in MODES:
            continue
        mask = rasterize(region, h, w)
        if mask is None or not mask.any():
            continue
        used += 1
        if mode == "focus":
            n_focus += 1
            focus = mask if focus is None else (focus | mask)
        else:
            n_ignore += 1
            ignore |= mask

    return {"focus": focus, "ignore": ignore, "count": used,
            "focus_count": n_focus, "ignore_count": n_ignore}


def apply(level_map: np.ndarray, tissue: np.ndarray, level: int,
          built: dict, n_levels: int) -> tuple[np.ndarray, np.ndarray]:
    """Regions + the level map → (positive mask, effective tissue mask).

    The browser runs this exact rule on the level-map image it already holds —
    including the tissue channel that travels with it — so the percentage on
    screen and the percentage the server puts in the CSV are the same
    calculation over the same two counts.
    """
    tis = tissue
    if built.get("focus") is not None:
        tis = tis & built["focus"]
    ignore = built.get("ignore")
    if ignore is not None and ignore.any():
        tis = tis & ~ignore

    lv = int(np.clip(int(level), 0, n_levels - 1))
    positive = (level_map <= lv) & (level_map < 255) & tis
    return positive, tis
