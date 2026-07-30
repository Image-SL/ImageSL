"""
Manual regions — the hand tools that correct the automatic detection.

A region is a shape the user drew on the slide plus what it means. Coordinates
are normalised (0..1 of width / height) so a region survives any working
resolution, export size or re-analysis, and so the browser and the server
rasterise exactly the same area.

Modes
-----
``include``  "There is staining here that you missed." Inside the shape every
             candidate structure is counted — including the ones the detector
             formed and then refused on colour or size (``LEVEL_CANDIDATE``),
             which is where a missed duct actually goes. It cannot invent
             staining: a pixel the detector never considered chromogen at all
             (``LEVEL_NEVER``) stays negative however the shape is drawn.

``exclude``  "What you found here is wrong." Inside the shape nothing is
             counted — a fold, a pen mark, a bubble, a torn edge, or simply a
             false positive the user can see is not a structure.

These are CORRECTION tools, and that is a deliberate change from what they were.

They used to be region-of-interest tools: ``focus`` restricted the measurement
to the drawn area and ``ignore`` cut an area out, both moving the numerator and
the denominator together. That is a defensible definition and it is not the one
users read off the buttons. Two things went wrong with it in practice:

  * **The percentage barely moved, so the tools looked broken.** Shrinking both
    counts together leaves the ratio roughly where it was. Drawing an Exclude
    box over an entire slide — which a user does precisely to check the control
    does something — left a sliver of tissue around the edge of the drag, and
    the answer came back 3.868% instead of 0%: 562 positive pixels over the
    14 529 that survived. Arithmetically correct, and useless.

  * **It made slides incomparable.** A percentage measured over a hand-drawn
    subregion of one slide and the whole of another cannot be put in the same
    column of a results table, and nothing on screen forced the user to notice.

So the denominator is now ALWAYS the slide's whole tissue area, whatever is
drawn, and the tools move only the numerator — the thing the user is actually
correcting. Both statements a user makes with them are then true:

    Exclude everything  ->  no positives  ->  0.000%
    Include an area     ->  the structures in it are counted

and every slide in a batch is measured over the same denominator, so the
numbers stay comparable. What was drawn is recorded in the export
(``included_pixels`` / ``excluded_pixels``) so a reader can see the measurement
was corrected by hand and by how much.

Where the two overlap, **exclude wins**: it is the more specific statement.
"""

from __future__ import annotations

from typing import Iterable, Optional

import numpy as np

from .detect import LEVEL_CANDIDATE, LEVEL_NEVER

MODES = ("include", "exclude")

# Older saved batches (IndexedDB survives a deploy) carry the previous names.
_ALIASES = {"focus": "include", "ignore": "exclude", "boost": "include"}


def canonical_mode(mode) -> Optional[str]:
    """Map any accepted spelling of a mode to its canonical name, or None."""
    m = str(mode or "").strip().lower()
    m = _ALIASES.get(m, m)
    return m if m in MODES else None


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
    include = None
    exclude = None
    used = n_include = n_exclude = 0

    for region in regions or []:
        if not isinstance(region, dict):
            continue
        mode = canonical_mode(region.get("mode"))
        if mode is None:
            continue
        mask = rasterize(region, h, w)
        if mask is None or not mask.any():
            continue
        used += 1
        if mode == "include":
            n_include += 1
            include = mask if include is None else (include | mask)
        else:
            n_exclude += 1
            exclude = mask if exclude is None else (exclude | mask)

    return {"include": include, "exclude": exclude, "count": used,
            "include_count": n_include, "exclude_count": n_exclude,
            # Kept under the old keys as well so anything still reading them
            # (a cached page, an old export path) does not silently see zero.
            "focus_count": n_include, "ignore_count": n_exclude}


def apply(level_map: np.ndarray, tissue: np.ndarray, level: int,
          built: dict, n_levels: int) -> tuple[np.ndarray, np.ndarray]:
    """Regions + the level map → (positive mask, effective tissue mask).

    The browser runs this exact rule on the level-map image it already holds —
    including the tissue channel that travels with it — so the percentage on
    screen and the percentage the server puts in the CSV are the same
    calculation over the same two counts.

    The tissue mask comes back UNCHANGED. See the module docstring: the hand
    tools correct the numerator and never move the denominator, so a batch stays
    comparable and "exclude everything" really does read 0.000%.
    """
    lv = int(np.clip(int(level), 0, n_levels - 1))
    positive = (level_map <= lv) & tissue

    inc = built.get("include")
    if inc is not None and inc.any():
        # Everything the detector formed as a candidate here counts, including
        # what it refused (LEVEL_CANDIDATE). LEVEL_NEVER is still never positive,
        # so an Include region cannot manufacture staining out of blank tissue.
        positive |= tissue & inc & (level_map <= LEVEL_CANDIDATE)

    exc = built.get("exclude")
    if exc is not None and exc.any():
        positive &= ~exc

    return positive, tissue
