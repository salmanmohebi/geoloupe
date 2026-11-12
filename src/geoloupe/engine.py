"""The tile engine: native grids → Web Mercator data tiles, one source or many.

For every tile and every source of a layer the engine

1. picks the resolution **level**: the coarsest level that is still at least
   as fine as a screen pixel (so nothing is lost), among the levels that have
   the requested time;
2. checks the **read cost**: the number of grid cells the tile needs. If it is
   above ``settings.max_read_cells`` the next coarser level is tried, and if
   none fits the source is skipped at this zoom (with a reason the UI shows).
   This is what keeps a 20 880 × 43 200 grid usable without loading it whole;
3. **samples** the level onto the tile pixels. Lat/lon grids use the
   separable fast path: per output pixel the mean of the cells whose centres
   fall inside it (an exact block mean through an integral image), or the
   nearest cell when a pixel is smaller than a cell. Projected grids are
   sampled with nearest neighbour after a pyproj transform of the pixel centres;
4. **composites** the sources per pixel: the first source (by default the
   finest) wins wherever it has data, coarser sources fill the gaps.

The result records which source and level filled how many pixels, so the UI
can say exactly what the user is looking at.
"""

from __future__ import annotations

import gzip
import logging
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from geoloupe.catalog import Catalog, Layer, Level, Source
from geoloupe.grid import Axis, Grid
from geoloupe.tiling import Tile, zoom_for_resolution
from geoloupe.timeaxis import TimeError, TimeFormatError

log = logging.getLogger(__name__)


class SelectionError(ValueError):
    """The request selects something that does not exist (time, dimension value)."""


@dataclass(frozen=True)
class Selection:
    """What to show besides space: a time label and values of extra dimensions."""

    time: str | None = None
    dims: tuple[tuple[str, str], ...] = ()

    @classmethod
    def make(cls, time: str | None, dims: dict[str, Any] | None = None) -> Selection:
        return cls(time or None, tuple(sorted((k, str(v)) for k, v in (dims or {}).items())))


@dataclass
class Contribution:
    source_index: int
    level_index: int
    pixels: int


@dataclass
class TileResult:
    data: np.ndarray  # float32 (size, size); NaN = no data
    source_map: np.ndarray  # uint8 (size, size): index of the source of each pixel; 255 = none
    contributions: list[Contribution] = field(default_factory=list)
    bands: list[np.ndarray] | None = None  # RGB layers: red, green, blue
    skipped: list[tuple[int, str]] = field(default_factory=list)  # (source index, reason)

    @property
    def empty(self) -> bool:
        return not self.contributions


@dataclass
class _Plan:
    """How to read and resample one level for one tile."""

    level_index: int
    rows: tuple[np.ndarray, np.ndarray]  # per output row: [lo, hi) source rows (or row index, -1)
    cols: tuple[np.ndarray, np.ndarray]
    r0: int
    r1: int
    c0: int
    c1: int  # may exceed n (wrapped longitudes); storage column = c % n
    nearest: bool
    pts: tuple[np.ndarray, np.ndarray] | None = None  # 2-D (rows, cols) for projected grids

    @property
    def cells(self) -> int:
        return max(0, self.r1 - self.r0) * max(0, self.c1 - self.c0)


# ---------------------------------------------------------------- selection
def resolve_extra_dims(source: Source, dims: tuple[tuple[str, str], ...]) -> dict[str, int]:
    """Index of the requested value for every extra dimension (default: the first).

    A source's fixed selections (e.g. one band of an RGB layer) win over the request.
    """
    req = {**dict(dims), **dict(source.fixed_dims)}
    out: dict[str, int] = {}
    for d, values in source.extra_dims.items():
        if d not in req:
            out[d] = 0
            continue
        want = req[d]
        labels = [str(v) for v in values.tolist()]
        if want in labels:
            out[d] = labels.index(want)
            continue
        try:
            num = np.asarray(values, dtype=np.float64)
            out[d] = int(np.argmin(np.abs(num - float(want))))
        except (TypeError, ValueError):
            raise SelectionError(f"{d}={want!r} not found; values: {labels[:20]}") from None
    return out


def time_index(level: Level, time: str | None) -> int | None:
    """Index of `time` on the level's axis; -1 for static levels; None if missing."""
    if level.time is None:
        return -1
    if time is None:
        raise SelectionError("this layer has a time axis; give a time")
    try:
        return level.time.index(time)
    except TimeFormatError:
        raise
    except TimeError:
        # e.g. 2020-02-29 does not exist in a noleap source of a mixed composite
        return None


# ----------------------------------------------------------------- planning
def _intervals(
    pos_edges: np.ndarray, n: int, periodic: bool, max_span: float = np.inf
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Cells per output pixel from fractional positions of the pixel edges.

    A pixel takes the cells whose centres lie inside it; a pixel smaller than
    a cell takes the cell under its centre. Returns (lo, hi, valid).
    A pixel whose edges are more than `max_span` cells apart straddles the
    longitude seam of a regional grid (its edges wrapped to opposite ends):
    it is outside the data, not a pixel covering all of it.
    """
    a = np.minimum(pos_edges[:-1], pos_edges[1:])
    b = np.maximum(pos_edges[:-1], pos_edges[1:])
    seam = (b - a) > max_span
    lo = np.ceil(a - 0.5)
    hi = np.ceil(b - 0.5)
    small = hi <= lo
    mid = np.floor((a + b) / 2)
    lo = np.where(small, mid, lo)
    hi = np.where(small, mid + 1, hi)
    if periodic:
        valid = np.isfinite(lo) & np.isfinite(hi)
    else:
        lo = np.clip(lo, 0, n)
        hi = np.clip(hi, 0, n)
        valid = np.isfinite(a) & (hi > lo) & ~seam
    lo = np.where(valid, lo, 0).astype(np.int64)
    hi = np.where(valid, hi, 0).astype(np.int64)
    return lo, hi, valid


def _unwrap_positions(pos: np.ndarray, n: int) -> np.ndarray:
    """Make wrapped longitude positions monotonic across the dataset's seam."""
    if pos.size < 2:
        return pos
    return np.unwrap(pos, period=n)


def plan_separable(level: Level, tile: Tile) -> _Plan | None:
    """Read plan for a lat/lon grid (separable lookup)."""
    g = level.grid
    nx, ny = g.x.n, g.y.n
    full = g.x.covers_full_period()
    px = g.x.position(tile.lon_edges())
    if full:
        px = _unwrap_positions(px, nx)
        # Bring the start into [0, nx) so window columns are small positive numbers.
        shift = math.floor(float(np.min(px)) / nx) * nx
        px = px - shift
    py = g.y.position(tile.lat_edges())
    # half a turn in cells: no real pixel spans that much longitude
    half_turn = 180.0 / g.x.resolution
    clo, chi, cok = _intervals(px, nx, periodic=full, max_span=half_turn)
    rlo, rhi, rok = _intervals(py, ny, periodic=False)
    if not cok.any() or not rok.any():
        return None
    r0, r1 = int(rlo[rok].min()), int(rhi[rok].max())
    c0, c1 = int(clo[cok].min()), int(chi[cok].max())
    if full and c1 - c0 > nx:  # never read a column twice
        c1 = c0 + nx
        chi = np.minimum(chi, c1)
        clo = np.minimum(clo, chi - 1)
    rlo = np.where(rok, rlo, -1)
    clo = np.where(cok, clo, -1)
    return _Plan(-1, (rlo, rhi), (clo, chi), r0, r1, c0, c1, nearest=False)


def plan_projected(level: Level, tile: Tile) -> _Plan | None:
    """Read plan for a projected or curvilinear grid: nearest cell of each pixel centre."""
    lon, lat = tile.lonlat_grid()
    ri, ci = level.grid.locate(lon, lat)
    ok = ri >= 0
    if not ok.any():
        return None
    r0, r1 = int(ri[ok].min()), int(ri[ok].max()) + 1
    c0, c1 = int(ci[ok].min()), int(ci[ok].max()) + 1
    empty = (np.empty(0, np.int64), np.empty(0, np.int64))
    return _Plan(-1, empty, empty, r0, r1, c0, c1, nearest=True, pts=(ri, ci))


def pixel_size(level: Level, tile: Tile) -> tuple[float, float]:
    """Size of a tile pixel in the level's CRS units (x, y), at the tile centre."""
    g = level.grid
    if g.geographic or g.curvi is not None:
        return tile.pixel_size_deg()
    w, s, e, n = tile.bounds()
    clon, clat = (w + e) / 2, (s + n) / 2
    dlon, dlat = tile.pixel_size_deg()
    xs, ys = g.from_lonlat(np.array([clon, clon + dlon, clon]), np.array([clat, clat, clat + dlat]))
    return float(math.hypot(xs[1] - xs[0], ys[1] - ys[0])), float(
        math.hypot(xs[2] - xs[0], ys[2] - ys[0])
    )


def preferred_level(levels: list[Level], usable: list[int], px: tuple[float, float]) -> int:
    """Coarsest usable level whose cells are no larger than a screen pixel."""
    best = usable[0]
    for i in usable:
        rx, ry = levels[i].grid.resolution
        if rx <= px[0] * 1.001 and ry <= px[1] * 1.001:
            best = i  # levels are sorted fine → coarse
    return best


def clip_to_grid(vals: np.ndarray, grid: Grid, tile: Tile) -> np.ndarray:
    """No data on pixels whose centre lies outside `grid` (the native grid of an overview).

    When a grid size is not a multiple of the overview factor, the last overview row/column is
    the mean of the remaining cells but spans a full block, up to f/2 native cells beyond the
    edge of the data; those pixels are cut here."""
    if grid.curvi is not None:
        return vals
    if grid.geographic:
        px = grid.x.position(tile.lon_centers())
        py = grid.y.position(tile.lat_centers())
        if grid.x.covers_full_period():
            px = np.zeros_like(px)
        inside = np.outer((py >= 0) & (py < grid.y.n), (px >= 0) & (px < grid.x.n))
    else:
        rows, _ = grid.locate(*tile.lonlat_grid())
        inside = rows >= 0
    if inside.all():
        return vals
    return np.where(inside, vals, np.nan).astype(vals.dtype, copy=False)


# ------------------------------------------------------------------ reading
def _col_runs(c0: int, c1: int, n: int) -> list[tuple[int, int]]:
    """Storage column ranges for window columns [c0, c1) that may wrap around."""
    runs = []
    c = c0
    while c < c1:
        s = c % n
        e = min(n, s + (c1 - c))
        runs.append((s, e))
        c += e - s
    return runs


def read_window(
    level: Level,
    source: Source,
    t_idx: int,
    extra: dict[str, int],
    r0: int,
    r1: int,
    c0: int,
    c1: int,
) -> np.ndarray:
    """Read rows [r0, r1) × window columns [c0, c1) as float32 (NaN = missing)."""
    g = level.grid
    base: dict[str, Any] = {d: i for d, i in extra.items() if d in level.da.dims}
    if t_idx >= 0 and source.time_dim:
        base[source.time_dim] = t_idx
    parts = []
    for s, e in _col_runs(c0, c1, g.x.n):
        sel = {**base, g.y_dim: slice(r0, r1), g.x_dim: slice(s, e)}
        da = level.da.isel(sel)
        da = da.transpose(g.y_dim, g.x_dim)
        parts.append(np.asarray(da.values))
    arr = parts[0] if len(parts) == 1 else np.concatenate(parts, axis=1)
    return as_float(arr, level.da)


def as_float(raw: Any, da: Any) -> np.ndarray:
    """Values as float32 with NaN for missing data.

    xarray normally decodes CF packing; if a user turned that off
    (`open_kwargs: {mask_and_scale: false}`), integers still carry _FillValue,
    scale_factor and add_offset as attributes: apply them here so that tiles,
    point values and time series agree.
    """
    arr = np.asarray(raw)
    if arr.dtype.kind in "fc" and "scale_factor" not in da.attrs and "add_offset" not in da.attrs:
        return arr.astype(np.float32, copy=False)
    fill = da.attrs.get("_FillValue", da.encoding.get("_FillValue"))
    out = arr.astype(np.float64)
    missing = (arr == fill) if fill is not None else np.zeros(arr.shape, bool)
    if arr.dtype.kind in "iu" or "scale_factor" in da.attrs or "add_offset" in da.attrs:
        scale = float(da.attrs.get("scale_factor", 1.0))
        offset = float(da.attrs.get("add_offset", 0.0))
        out = np.array(out * scale + offset, dtype=np.float64)  # stays an array for 0-d input
    out[missing] = np.nan
    return out.astype(np.float32)


def resample(window: np.ndarray, plan: _Plan, method: str, size: int) -> np.ndarray:
    """Turn a read window into a (size, size) tile."""
    out = np.full((size, size), np.nan, dtype=np.float32)
    if plan.pts is not None:  # projected: nearest per pixel
        ri, ci = plan.pts
        ok = ri >= 0
        out[ok] = window[ri[ok] - plan.r0, ci[ok] - plan.c0]
        return out
    rlo, rhi = plan.rows
    clo, chi = plan.cols
    rv, cv = rlo >= 0, clo >= 0
    a_r, b_r = rlo[rv] - plan.r0, rhi[rv] - plan.r0
    a_c, b_c = clo[cv] - plan.c0, chi[cv] - plan.c0
    if method == "nearest":
        mid_r = (a_r + b_r - 1) // 2
        mid_c = (a_c + b_c - 1) // 2
        out[np.ix_(rv, cv)] = window[np.ix_(mid_r, mid_c)]
        return out
    if method == "mean":
        ok = np.isfinite(window)
        s = np.zeros((window.shape[0] + 1, window.shape[1] + 1), dtype=np.float64)
        c = np.zeros_like(s)
        s[1:, 1:] = np.where(ok, window, 0.0).cumsum(0, dtype=np.float64).cumsum(1)
        c[1:, 1:] = ok.cumsum(0, dtype=np.float64).cumsum(1)

        def box(t: np.ndarray) -> np.ndarray:
            return (
                t[np.ix_(b_r, b_c)]
                - t[np.ix_(a_r, b_c)]
                - t[np.ix_(b_r, a_c)]
                + t[np.ix_(a_r, a_c)]
            )

        tot, cnt = box(s), box(c)
        with np.errstate(invalid="ignore", divide="ignore"):
            block = np.where(cnt > 0, tot / np.maximum(cnt, 1), np.nan)
        out[np.ix_(rv, cv)] = block.astype(np.float32)
        return out
    # min / max: loop over offsets inside the largest block
    red = np.fmin if method == "min" else np.fmax
    acc = np.full((a_r.size, a_c.size), np.nan, dtype=np.float32)
    for di in range(int((b_r - a_r).max())):
        rr = a_r + di
        rok = rr < b_r
        for dj in range(int((b_c - a_c).max())):
            cc = a_c + dj
            cok = cc < b_c
            vals = window[
                np.ix_(np.minimum(rr, window.shape[0] - 1), np.minimum(cc, window.shape[1] - 1))
            ]
            vals = np.where(np.outer(rok, cok), vals, np.nan)
            acc = red(acc, vals)
    out[np.ix_(rv, cv)] = acc
    return out


# ------------------------------------------------------------------- engine
class LRUCache:
    """A thread-safe LRU cache bounded by the total size of its values in bytes."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._data: OrderedDict[Any, tuple[Any, int]] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            self._data.move_to_end(key)
            return hit[0]

    def put(self, key: Any, value: Any, nbytes: int) -> None:
        if nbytes > self.max_bytes:
            return
        with self._lock:
            old = self._data.pop(key, None)
            if old is not None:
                self._size -= old[1]
            self._data[key] = (value, nbytes)
            self._size += nbytes
            while self._size > self.max_bytes and self._data:
                _, (_, n) = self._data.popitem(last=False)
                self._size -= n


class TileEngine:
    """Computes data tiles for the layers of a catalog."""

    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.settings = catalog.settings
        self.cache = LRUCache(self.settings.tile_cache_mb * 2**20)
        # Whole 2-D fields of small grids, shared by all tiles of one time step (see _window).
        self.fields = LRUCache(self.settings.field_cache_mb * 2**20)
        self._field_locks: dict[Any, threading.Lock] = {}
        self._field_locks_guard = threading.Lock()

    def _window(
        self, lv: Level, source: Source, t_idx: int, extra: dict[str, int], p: _Plan
    ) -> np.ndarray:
        """The read window of a plan. Grids up to `field_cache_cells` are read whole, once per
        time step, and every tile cuts its window from memory: stores whose chunks hold the whole
        field (or many time steps, as in CMIP6) would otherwise be decompressed again for every
        tile."""
        ny, nx = lv.grid.shape
        if ny * nx > self.settings.field_cache_cells:
            return read_window(lv, source, t_idx, extra, p.r0, p.r1, p.c0, p.c1)
        key = (id(lv.da), t_idx, tuple(sorted(extra.items())))
        whole = self.fields.get(key)
        if whole is None:
            with self._field_locks_guard:
                lock = self._field_locks.setdefault(key, threading.Lock())
            with lock:  # one read per field, even when many tiles ask at once
                whole = self.fields.get(key)
                if whole is None:
                    whole = read_window(lv, source, t_idx, extra, 0, ny, 0, nx)
                    self.fields.put(key, whole, whole.nbytes)
            with self._field_locks_guard:
                self._field_locks.pop(key, None)
        cols = np.arange(p.c0, p.c1) % nx  # windows may wrap around in longitude
        if p.c1 > nx or p.c0 < 0:
            return whole[p.r0 : p.r1][:, cols]
        return whole[p.r0 : p.r1, p.c0 : p.c1]

    # -- one source -------------------------------------------------------
    def plan(
        self, source: Source, tile: Tile, time: str | None, max_cells: int | None = None
    ) -> tuple[_Plan | None, str | None]:
        """Pick the level and read window for one source; (None, reason) if skipped.

        ``max_cells``: the read budget (default: settings.max_read_cells)."""
        budget = max_cells or self.settings.max_read_cells
        zoom = tile.z
        if source.min_zoom is not None and zoom < source.min_zoom:
            return None, "zoom"
        if source.max_zoom is not None and zoom > source.max_zoom:
            return None, "zoom"
        time = source.time_for(time)
        usable = [i for i, lv in enumerate(source.levels) if time_index(lv, time) is not None]
        if not usable:
            return None, "time"
        px = pixel_size(source.levels[usable[0]], tile)
        first = preferred_level(source.levels, usable, px)
        tried_cost = False
        for i in [u for u in usable if u >= first]:
            lv = source.levels[i]
            p = plan_separable(lv, tile) if lv.grid.geographic else plan_projected(lv, tile)
            if p is None:
                return None, "outside"
            if p.cells > budget:
                tried_cost = True
                continue
            p.level_index = i
            if lv.grid.geographic and source.resampling in ("nearest", "mode"):
                p.nearest = True
            return p, None
        return None, "cost" if tried_cost else "outside"

    def sample(
        self, source: Source, tile: Tile, sel: Selection, max_cells: int | None = None
    ) -> tuple[np.ndarray | None, int, str | None]:
        """(values, level index, skip reason) of one source on one tile."""
        p, reason = self.plan(source, tile, sel.time, max_cells)
        if p is None:
            return None, -1, reason
        lv = source.levels[p.level_index]
        t_idx = time_index(lv, source.time_for(sel.time))
        assert t_idx is not None
        extra = resolve_extra_dims(source, sel.dims)
        win = self._window(lv, source, t_idx, extra, p)
        # categorical data: nearest on the fly (overviews hold the majority class)
        method = "nearest" if p.nearest or source.resampling == "mode" else source.resampling
        vals = resample(win, p, method, tile.size)
        if lv is not source.native and lv.origin == "overview":
            vals = clip_to_grid(vals, source.native.grid, tile)
        if source.scale != 1.0 or source.offset != 0.0:
            vals = vals * np.float32(source.scale) + np.float32(source.offset)
        return vals, p.level_index, None

    # -- a layer ----------------------------------------------------------
    def render(
        self, layer: Layer, tile: Tile, sel: Selection, max_cells: int | None = None
    ) -> TileResult:
        """Composite all sources of a layer on one tile (finest first, coarser fill gaps).

        RGB layers are not composited: each source is one channel. ``max_cells``: the read
        budget per source (default: settings.max_read_cells).
        """
        if layer.kind == "rgb":
            return self._render_rgb(layer, tile, sel)
        out = np.full((tile.size, tile.size), np.nan, dtype=np.float32)
        smap = np.full((tile.size, tile.size), 255, dtype=np.uint8)
        result = TileResult(out, smap)
        missing = np.ones(out.shape, dtype=bool)
        for k, src in enumerate(layer.sources):
            vals, lvl, reason = self.sample(src, tile, sel, max_cells)
            if vals is None:
                if reason != "outside":
                    result.skipped.append((k, reason or "unknown"))
                continue
            take = missing & np.isfinite(vals)
            n = int(take.sum())
            if n:
                out[take] = vals[take]
                smap[take] = k
                missing &= ~take
                result.contributions.append(Contribution(k, lvl, n))
            if not missing.any():
                break
        return result

    def _render_rgb(self, layer: Layer, tile: Tile, sel: Selection) -> TileResult:
        """Three value planes. A channel may have several sources (a mosaic): per pixel, the
        first source with a value wins, as in composite layers."""
        shape = (tile.size, tile.size)
        bands = [np.full(shape, np.nan, np.float32) for _ in range(3)]
        result = TileResult(np.full(shape, np.nan, np.float32), np.full(shape, 255, np.uint8))
        chans = layer.channels or list(range(len(layer.sources)))
        for k, src in enumerate(layer.sources):
            band = bands[chans[k]]
            missing = ~np.isfinite(band)
            if not missing.any():
                continue  # this channel is full: later sources are hidden anyway
            vals, lvl, reason = self.sample(src, tile, sel)
            if vals is None:
                if reason != "outside":
                    result.skipped.append((k, reason or "unknown"))
                continue
            fill = missing & np.isfinite(vals)
            band[fill] = vals[fill]
            if chans[k] == 0:
                result.source_map[fill] = min(k, 254)
            if fill.any():
                result.contributions.append(Contribution(k, lvl, int(fill.sum())))
        result.bands = bands
        result.data = bands[0]
        return result

    def tile_bytes(
        self, layer: Layer, tile: Tile, sel: Selection
    ) -> tuple[bytes | None, dict[str, str]]:
        """Encoded data tile (cached) and its metadata headers; None body = empty tile."""
        key = (layer.id, tile, sel)
        hit = self.cache.get(key)
        if hit is not None:
            body, headers = hit
            return body, headers
        res = self.render(layer, tile, sel)
        headers = {
            "X-GeoLoupe-Sources": ",".join(
                f"{c.source_index}:{c.level_index}:{c.pixels}" for c in res.contributions
            ),
            "X-GeoLoupe-Skipped": ",".join(f"{k}:{r}" for k, r in res.skipped),
        }
        if res.bands is not None:  # RGB: three value planes, no source map
            headers["X-GeoLoupe-Bands"] = str(len(res.bands))
            raw = b"".join(encode_data_tile(b) for b in res.bands)
        else:
            raw = encode_data_tile(res.data, res.source_map)
        body = None if res.empty else gzip.compress(raw, compresslevel=4, mtime=0)
        self.cache.put(key, (body, headers), len(body or b"") + 256)
        return body, headers

    # -- metadata ---------------------------------------------------------
    def min_zoom(self, source: Source) -> float:
        """Lowest tile zoom at which the source fits the read budget (0 if it always does)."""
        lv = source.levels[-1]
        ny, nx = lv.grid.shape
        if nx * ny <= self.settings.max_read_cells:
            return 0.0
        cell = lv.grid.cell_size_m()
        side = math.sqrt(self.settings.max_read_cells)
        # cells per tile side at the equator: 2^-z * C / cell  <=  side
        return max(
            0.0, zoom_for_resolution(cell * side / self.settings.tile_size, self.settings.tile_size)
        )

    @staticmethod
    def native_zoom(source: Source, tile_size: int = 256) -> float:
        """Tile zoom at which one pixel equals one native cell (at the equator)."""
        return zoom_for_resolution(source.native.grid.cell_size_m(), tile_size)


def encode_data_tile(data: np.ndarray, source_map: np.ndarray | None = None) -> bytes:
    """Data tile body (before gzip); the format is described in docs/data-tiles.md.

    1. ``size²`` float32 little-endian values, **byte-shuffled** (byte 0 of all
       values, then byte 1, 2, 3). Shuffling groups the slowly varying
       sign/exponent bytes, which roughly doubles the gzip ratio. NaN = no data.
    2. ``size²`` uint8: index of the layer source that filled each pixel
       (255 = none). Optional; omitted for plain data.
    """
    a = np.ascontiguousarray(data, dtype="<f4")
    body = a.view(np.uint8).reshape(-1, 4).T.tobytes()
    if source_map is not None:
        body += np.ascontiguousarray(source_map, dtype=np.uint8).tobytes()
    return body


def decode_data_tile(raw: bytes, size: int) -> np.ndarray:
    """Values of a data tile body (after gzip decompression)."""
    n = size * size
    b = np.frombuffer(raw[: 4 * n], dtype=np.uint8).reshape(4, -1).T
    return np.ascontiguousarray(b).view("<f4").reshape(size, size)


def decode_source_map(raw: bytes, size: int) -> np.ndarray | None:
    n = size * size
    return (
        np.frombuffer(raw[4 * n : 5 * n], dtype=np.uint8).reshape(size, size)
        if len(raw) >= 5 * n
        else None
    )


def axis_positions(axis: Axis, q: np.ndarray) -> np.ndarray:
    """Exposed for tests: fractional positions on an axis."""
    return axis.position(q)
