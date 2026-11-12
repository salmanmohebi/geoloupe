"""Exact error maps: the error of P against T from native cells, averaged to screen pixels.

A screen pixel of a zoomed-out map covers many native cells. The error of the averages
(|mean P − mean T|) is not the average error (mean |P − T|): it hides errors that cancel inside
the pixel. So an error tile is computed on a finer grid of the same tile: every screen pixel is
split into f × f sub-pixels, with f the smallest power of two that makes a sub-pixel no larger
than the finest native cell of P and T (up to ``settings.error_max_subpixels`` per tile side).
P and T are rendered on that grid by the normal engine, which then reads their native levels;
the error is computed per sub-pixel, i.e. per native cell, and averaged to the screen pixels
(class maps: the most frequent class). Each screen pixel also keeps the sums the view
statistics need (count, sum, sum of |e|, sum of e², min, max, class counts), so the statistics
of a view are exact over the native cells, not over screen pixels.

When f reaches its limit, or a source is too large for the read budget at its native level, the
tile is still computed on the finest grid allowed and marked as not exact (header
``X-GeoLoupe-Exact: 0``), with the size of the sub-pixels it used.
"""

from __future__ import annotations

import gzip
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .catalog import Layer
from .engine import (
    Selection,
    TileEngine,
    encode_data_tile,
    pixel_size,
    plan_projected,
    plan_separable,
)
from .errors import Conversion, ErrorSpec
from .tiling import EARTH_CIRCUMFERENCE_M, Tile, lonlat_to_tile


class ErrorQueryError(ValueError):
    """An error map that cannot be computed (e.g. an RGB or point layer)."""


@dataclass(frozen=True)
class ErrorQuery:
    """The error of layer P (with its selection) against layer T."""

    p: Layer
    t: Layer
    p_sel: Selection
    t_sel: Selection
    spec: ErrorSpec
    conv: Conversion

    def __post_init__(self) -> None:
        for role, layer in (("P", self.p), ("T", self.t)):
            if layer.kind == "rgb":
                raise ErrorQueryError(f"{role} is an RGB layer: error maps need one value per cell")

    @property
    def key(self) -> tuple[Any, ...]:
        return (
            self.p.id,
            self.t.id,
            self.p_sel,
            self.t_sel,
            self.spec.metric,
            self.spec.formula,
            tuple(sorted(self.spec.params.items())),
            self.conv.scale,
            self.conv.offset,
        )


@dataclass
class ErrorParts:
    """Per screen pixel sums over its valid sub-pixels (float64), and how they were made."""

    n: np.ndarray
    s: np.ndarray
    sa: np.ndarray
    ss: np.ndarray
    mn: np.ndarray
    mx: np.ndarray
    classes: dict[int, np.ndarray] | None
    exact: bool
    factor: int  # sub-pixels per screen pixel side
    sub_m: float  # size of a sub-pixel at the tile centre, in metres

    @property
    def nbytes(self) -> int:
        k = sum(c.nbytes for c in (self.classes or {}).values())
        return k + sum(a.nbytes for a in (self.n, self.s, self.sa, self.ss, self.mn, self.mx))

    def values(self) -> np.ndarray:
        """What the map shows: the mean error per pixel, or the most frequent class."""
        if self.classes:
            vals = np.array(sorted(self.classes), dtype=np.float32)
            counts = np.stack([self.classes[int(v)] for v in vals])
            out = vals[np.argmax(counts, axis=0)]
            return np.where(self.n > 0, out, np.nan).astype(np.float32)
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(self.n > 0, self.s / np.maximum(self.n, 1), np.nan).astype(np.float32)


def _finest_ratio(layer: Layer, tile: Tile) -> float:
    """How many native cells (of the finest source covering the tile) fit in a screen pixel,
    along its longer side; 0 when no source covers the tile."""
    ratio = 0.0
    for src in layer.sources:
        lv = src.levels[0]
        plan = plan_separable(lv, tile) if lv.grid.geographic else plan_projected(lv, tile)
        if plan is None:
            continue
        px = pixel_size(lv, tile)
        rx, ry = lv.grid.resolution
        ratio = max(ratio, px[0] / rx if rx > 0 else 1.0, px[1] / ry if ry > 0 else 1.0)
    return ratio


def subpixel_factor(engine: TileEngine, q: ErrorQuery, tile: Tile) -> tuple[int, bool]:
    """(f, enough): sub-pixels per pixel side, and whether they reach the native cells."""
    need = max(_finest_ratio(q.p, tile), _finest_ratio(q.t, tile))
    limit = max(1, engine.settings.error_max_subpixels // tile.size)
    f = 1
    while f < need * (1 - 1e-6) and f * 2 <= limit:
        f *= 2
    return f, f >= need * (1 - 1e-6)


def error_parts(engine: TileEngine, q: ErrorQuery, tile: Tile) -> ErrorParts | None:
    """The error sums of one tile (cached); None when P or T has no data on it."""
    key = ("error", q.key, tile)
    hit = engine.cache.get(key)
    if hit is not None:
        parts: ErrorParts | None = hit[0]
        return parts
    f, enough = subpixel_factor(engine, q, tile)
    fine = Tile(tile.z, tile.x, tile.y, tile.size * f)
    budget = engine.settings.error_read_cells
    rp = engine.render(q.p, fine, q.p_sel, budget)
    rt = engine.render(q.t, fine, q.t_sel, budget)
    if rp.empty or rt.empty:
        engine.cache.put(key, (None,), 64)
        return None
    native = all(c.level_index == 0 for c in rp.contributions + rt.contributions)
    budget_hit = any(r == "cost" for _, r in rp.skipped + rt.skipped)
    p = rp.data
    if not q.conv.identity:
        p = p * np.float32(q.conv.scale) + np.float32(q.conv.offset)
    e = q.spec.compute(p, rt.data)
    n = tile.size
    blocks = e.reshape(n, f, n, f)
    valid = np.isfinite(blocks)
    z = np.where(valid, blocks, 0.0).astype(np.float64)
    cnt = valid.sum(axis=(1, 3)).astype(np.float64)
    with np.errstate(invalid="ignore"):
        mn = np.where(valid, blocks, np.inf).min(axis=(1, 3))
        mx = np.where(valid, blocks, -np.inf).max(axis=(1, 3))
    mn = np.where(cnt > 0, mn, np.nan).astype(np.float32)
    mx = np.where(cnt > 0, mx, np.nan).astype(np.float32)
    classes = None
    if q.spec.classes:
        classes = {
            int(c["value"]): (blocks == c["value"]).sum(axis=(1, 3)).astype(np.float32)
            for c in q.spec.classes
        }
    _, s_lat, _, n_lat = tile.bounds()
    sub_m = (
        EARTH_CIRCUMFERENCE_M
        * math.cos(math.radians((s_lat + n_lat) / 2))
        / tile.n
        / (tile.size * f)
    )
    parts = ErrorParts(
        n=cnt,
        s=z.sum(axis=(1, 3)),
        sa=np.abs(z).sum(axis=(1, 3)),
        ss=(z * z).sum(axis=(1, 3)),
        mn=mn,
        mx=mx,
        classes=classes,
        exact=enough and native and not budget_hit,
        factor=f,
        sub_m=sub_m,
    )
    engine.cache.put(key, (parts,), parts.nbytes + 256)
    return parts


def error_tile_bytes(
    engine: TileEngine, q: ErrorQuery, tile: Tile
) -> tuple[bytes | None, dict[str, str]]:
    """The encoded error tile (gzip) and its headers; None body = no data here."""
    parts = error_parts(engine, q, tile)
    if parts is None:
        return None, {"X-GeoLoupe-Exact": "1"}
    headers = {
        "X-GeoLoupe-Exact": "1" if parts.exact else "0",
        "X-GeoLoupe-Subpixels": str(parts.factor),
        "X-GeoLoupe-Cell-M": f"{parts.sub_m:.1f}",
    }
    raw = encode_data_tile(parts.values())
    return gzip.compress(raw, compresslevel=4, mtime=0), headers


def tiles_for_bbox(bbox: tuple[float, float, float, float], z: int) -> list[Tile]:
    w, s, e, n = bbox
    x0, y0 = lonlat_to_tile(w, n, z)
    x1, y1 = lonlat_to_tile(min(e, 180 - 1e-9), s, z)
    return [Tile(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def error_stats(
    engine: TileEngine,
    q: ErrorQuery,
    bbox: tuple[float, float, float, float],
    z: int,
    max_tiles: int = 64,
) -> dict[str, Any]:
    """Statistics of the error over the native cells in a bounding box, from the error tiles
    of zoom z (lowered until at most max_tiles tiles cover the box). Pixels count when their
    centre is inside the box."""
    z = max(0, min(int(z), 22))
    tiles = tiles_for_bbox(bbox, z)
    while len(tiles) > max_tiles and z > 0:
        z -= 1
        tiles = tiles_for_bbox(bbox, z)
    w, s, e, n = bbox
    tot = {"n": 0.0, "s": 0.0, "sa": 0.0, "ss": 0.0}
    lo, hi = math.inf, -math.inf
    classes: dict[int, float] = {}
    exact = True
    sub_m = math.inf
    for t in tiles:
        parts = error_parts(engine, q, t)
        if parts is None:
            continue
        lon = t.lon_centers()
        lat = t.lat_centers()
        inside = np.outer((lat >= s) & (lat <= n), (lon >= w) & (lon <= e))
        m = inside & (parts.n > 0)
        if not m.any():
            continue
        exact &= parts.exact
        sub_m = min(sub_m, parts.sub_m)
        tot["n"] += float(parts.n[m].sum())
        tot["s"] += float(parts.s[m].sum())
        tot["sa"] += float(parts.sa[m].sum())
        tot["ss"] += float(parts.ss[m].sum())
        lo = min(lo, float(np.nanmin(parts.mn[m])))
        hi = max(hi, float(np.nanmax(parts.mx[m])))
        for v, c in (parts.classes or {}).items():
            classes[v] = classes.get(v, 0.0) + float(c[m].sum())
    count = int(tot["n"])
    out: dict[str, Any] = {"count": count, "zoom": z, "tiles": len(tiles), "exact": exact}
    if count:
        out.update(
            mean=tot["s"] / count,
            mean_abs=tot["sa"] / count,
            rms=math.sqrt(tot["ss"] / count),
            min=lo,
            max=hi,
            cell_m=sub_m,
        )
    if q.spec.classes:
        out["classes"] = {str(v): int(c) for v, c in sorted(classes.items())}
        out.update(contingency(classes) if q.spec.metric == "events" else {})
    return out


def contingency(counts: dict[int, float]) -> dict[str, float | None]:
    """Scores of event agreement (classes 1 hit, 2 miss, 3 false alarm, 4 correct no event)."""
    a, b, c = counts.get(1, 0.0), counts.get(2, 0.0), counts.get(3, 0.0)

    def ratio(x: float, y: float) -> float | None:
        return x / y if y > 0 else None

    return {
        "pod": ratio(a, a + b),  # probability of detection (hit rate)
        "far": ratio(c, a + c),  # false alarm ratio
        "csi": ratio(a, a + b + c),  # critical success index
        "freq_bias": ratio(a + c, a + b),  # forecast events / observed events
    }


def error_range(scale: str, values: np.ndarray) -> tuple[float, float, bool, str]:
    """(vmin, vmax, log, colormap): the default colours of an error map, fitted to its values as
    in the web client (symmetric around 0 for signed errors, from 0 for sizes of errors,
    symmetric around 1 on a log scale for ratios)."""
    v = values[np.isfinite(values)]
    if v.size == 0:
        return -1.0, 1.0, False, "RdBu_r"
    if scale == "ratio":
        pos = np.abs(np.log(v[v > 0]))
        r = float(np.quantile(pos, 0.98)) if pos.size else math.log(2)
        r = r or math.log(2)
        return math.exp(-r), math.exp(r), True, "RdBu_r"
    lo, hi = (float(x) for x in np.quantile(v, [0.02, 0.98]))
    if scale == "sequential":
        return 0.0, hi if hi > 0 else 1.0, False, "YlOrRd"
    m = max(abs(lo), abs(hi)) or 1.0
    return -m, m, False, "RdBu_r"
