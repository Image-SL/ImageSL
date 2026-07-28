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
``boost``    Shift the sensitivity **locally** by `delta` ladder steps. Positive
             is more sensitive (catches fainter structures in this area only),
             negative is stricter. This is the tool for "that region is
             genuinely weaker, count it properly" without moving the whole
             slide's operating point.

Nothing here can invent staining out of nothing: a boosted region still only
admits pixels that belong to a chromogen-coloured object somewhere on the
sensitivity ladder. The strongest boost is therefore "count everything this
slide could plausibly call stain, here", not "paint this area positive".
"""

from __future__ import annotations

from typing import Iterable, Optional

import numpy as np
from PIL import Image, ImageDraw

MODES = ("focus", "ignore", "boost")


def _pts(points, w: int, h: int) -> list[tuple[float, float]]:
    out = []
    for p in points or []:
        try:
            x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        out.append((x * (w - 1), y * (h - 1)))
    return out


def rasterize(region: dict, h: int, w: int) -> Optional[np.ndarray]:
    """One region → a boolean mask of the analysis grid."""
    kind = str(region.get("kind") or region.get("type") or "rect").lower()
    pts = _pts(region.get("points"), w, h)
    if not pts:
        return None

    img = Image.new("1", (w, h), 0)
    draw = ImageDraw.Draw(img)

    if kind == "rect" and len(pts) >= 2:
        (x0, y0), (x1, y1) = pts[0], pts[1]
        draw.rectangle([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)], fill=1)
    elif kind == "ellipse" and len(pts) >= 2:
        (x0, y0), (x1, y1) = pts[0], pts[1]
        draw.ellipse([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)], fill=1)
    elif kind == "brush":
        r = max(1.0, float(region.get("radius", 0.02)) * w)
        if len(pts) == 1:
            x, y = pts[0]
            draw.ellipse([x - r, y - r, x + r, y + r], fill=1)
        else:
            draw.line(pts, fill=1, width=int(round(r * 2)), joint="curve")
            for x, y in pts:
                draw.ellipse([x - r, y - r, x + r, y + r], fill=1)
    elif len(pts) >= 3:                      # polygon / lasso
        draw.polygon(pts, fill=1)
    else:
        return None

    return np.array(img, dtype=bool)


def build(regions: Optional[Iterable[dict]], h: int, w: int, n_levels: int) -> dict:
    """Rasterise every region into the three things detection actually needs."""
    focus = None
    ignore = np.zeros((h, w), dtype=bool)
    offset = np.zeros((h, w), dtype=np.int16)
    used = 0

    for region in regions or []:
        if not isinstance(region, dict):
            continue
        mode = str(region.get("mode", "boost")).lower()
        if mode not in MODES:
            continue
        mask = rasterize(region, h, w)
        if mask is None or not mask.any():
            continue
        used += 1
        if mode == "focus":
            focus = mask if focus is None else (focus | mask)
        elif mode == "ignore":
            ignore |= mask
        else:
            delta = int(np.clip(int(region.get("delta", 0)), -n_levels, n_levels))
            offset[mask] += delta

    return {"focus": focus, "ignore": ignore, "offset": offset, "count": used}


def apply(level_map: np.ndarray, tissue: np.ndarray, level: int,
          built: dict, n_levels: int) -> tuple[np.ndarray, np.ndarray]:
    """Regions + the level map → (positive mask, effective tissue mask).

    The browser runs this exact rule on the level-map image it already holds, so
    what is on screen and what the server measures cannot drift apart.
    """
    tis = tissue
    if built.get("focus") is not None:
        tis = tis & built["focus"]
    ignore = built.get("ignore")
    if ignore is not None and ignore.any():
        tis = tis & ~ignore

    eff = np.clip(int(level) + built["offset"].astype(np.int32), 0, n_levels - 1)
    positive = (level_map.astype(np.int32) <= eff) & (level_map < 255) & tis
    return positive, tis
