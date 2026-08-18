from __future__ import annotations

from typing import Iterable, Optional

import numpy as np

from .detect import LEVEL_CANDIDATE, LEVEL_NEVER

MODES = ("include", "exclude")

_ALIASES = {"focus": "include", "ignore": "exclude", "boost": "include"}

def canonical_mode(mode) -> Optional[str]:
    m = str(mode or "").strip().lower()
    m = _ALIASES.get(m, m)
    return m if m in MODES else None

def _pts(points, w: int, h: int) -> list[tuple[float, float]]:
    out = []
    for p in points or []:
        try:
            x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        out.append((x * w, y * h))
    return out

def _rect_mask(x0, y0, x1, y1, h: int, w: int) -> np.ndarray:
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
            if i + 1 < len(pts):
                nx, ny = pts[i + 1]
                dx, dy = nx - x, ny - y
                ll = dx * dx + dy * dy
                if ll <= 0:
                    continue
                t = np.clip(((ys[:, None] - y) * dy + (xs[None, :] - x) * dx) / ll, 0.0, 1.0)
                mask |= ((ys[:, None] - (y + t * dy)) ** 2
                         + (xs[None, :] - (x + t * dx)) ** 2) <= r * r
        return mask
    if len(pts) >= 3:
        return _polygon_mask(pts, h, w)
    return None

def build(regions: Optional[Iterable[dict]], h: int, w: int, n_levels: int) -> dict:
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
            "focus_count": n_include, "ignore_count": n_exclude}

def apply(level_map: np.ndarray, tissue: np.ndarray, level: int,
          built: dict, n_levels: int) -> tuple[np.ndarray, np.ndarray]:
    lv = int(np.clip(int(level), 0, n_levels - 1))
    positive = (level_map <= lv) & tissue

    inc = built.get("include")
    if inc is not None and inc.any():
        positive |= tissue & inc & (level_map <= LEVEL_CANDIDATE)

    exc = built.get("exclude")
    if exc is not None and exc.any():
        positive &= ~exc

    return positive, tissue
