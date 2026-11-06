"""Point values, time series, value statistics and data export.

Point values and time series always come from the **native** (finest) level
of each source, so the numbers a user clicks on are the stored values, not a
display aggregate. Statistics for colour ranges come from low-zoom tiles and
therefore follow the same level choice and cost limits as the map.
"""

from __future__ import annotations

import math
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np
import xarray as xr

from terrascope.catalog import Layer, Level, Source
from terrascope.engine import (
    Selection,
    SelectionError,
    TileEngine,
    as_float,
    resolve_extra_dims,
    time_index,
)
from terrascope.tiling import Tile, lonlat_to_tile


class QueryError(ValueError):
    """A query cannot be answered (outside the data, too large, ...)."""


def _cell(level: Level, lon: float, lat: float) -> tuple[int, int] | None:
    g = level.grid
    gx, gy = g.from_lonlat(np.array([lon]), np.array([lat]))
    px, py = g.x.position(gx), g.y.position(gy)
    if not (np.isfinite(px[0]) and np.isfinite(py[0])):
        return None
    c, r = math.floor(px[0]), math.floor(py[0])
    if not (0 <= c < g.x.n and 0 <= r < g.y.n):
        return None
    return r, c


def _cell_info(level: Level, r: int, c: int) -> dict[str, Any]:
    g = level.grid
    x, y = float(g.x.centers[c]), float(g.y.centers[r])
    if g.geographic:
        lon = ((x + 180.0) % 360.0) - 180.0
        return {"lon": lon, "lat": y, "row": r, "col": c}
    lon_a, lat_a = g.transformer_to_lonlat.transform([x], [y])
    return {"lon": float(lon_a[0]), "lat": float(lat_a[0]), "x": x, "y": y, "row": r, "col": c}


def _isel(source: Source, level: Level, extra: dict[str, int], **dims: Any) -> xr.DataArray:
    sel = {d: i for d, i in extra.items() if d in level.da.dims}
    sel.update({k: v for k, v in dims.items() if v is not None})
    return level.da.isel(sel)


def _clean(v: float) -> float | None:
    """JSON number: None for missing, and no float noise from unpacking (261.03999999999996)."""
    return float(f"{v:.10g}") if np.isfinite(v) else None


def point(layer: Layer, lon: float, lat: float, sel: Selection) -> dict[str, Any]:
    """Native values of every source at one location; `best` = the composite value."""
    out: list[dict[str, Any]] = []
    best: float | None = None
    best_source: int | None = None
    for k, src in enumerate(layer.sources):
        lv = src.native
        entry: dict[str, Any] = {
            "source": k,
            "id": src.id,
            "resolution": lv.grid.resolution_label(),
        }
        cell = _cell(lv, lon, lat)
        if cell is None:
            entry["status"] = "outside"
            out.append(entry)
            continue
        t_idx = time_index(lv, src.time_for(sel.time))
        if t_idx is None:
            entry["status"] = "no data at this time"
            out.append(entry)
            continue
        r, c = cell
        extra = resolve_extra_dims(src, sel.dims)
        tdim = src.time_dim if t_idx >= 0 else None
        da = _isel(
            src,
            lv,
            extra,
            **{lv.grid.y_dim: r, lv.grid.x_dim: c},
            **({tdim: t_idx} if tdim else {}),
        )
        v = float(as_float(da.values, lv.da)[()]) * src.scale + src.offset
        entry.update({"status": "ok", "value": _clean(v), "cell": _cell_info(lv, r, c)})
        if best is None and np.isfinite(v):
            best, best_source = v, k
        out.append(entry)
    return {
        "lon": lon,
        "lat": lat,
        "time": sel.time,
        "units": layer.units,
        "value": best,
        "source": best_source,
        "sources": out,
    }


_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="terrascope-ts")


def _read_series(
    src: Source, lv: Level, extra: dict[str, int], r: int, c: int, i0: int, i1: int
) -> np.ndarray:
    """One cell over time steps [i0, i1), read in parallel blocks.

    Each time step usually lives in its own chunk (maps are chunked for space),
    so a series touches one chunk per step; reading blocks of steps in
    parallel threads keeps the decompression of those chunks concurrent.
    """
    tdim: str = src.time_dim or ""
    assert tdim, "a time series needs a time dimension"
    n = i1 - i0
    block = max(8, -(-n // 16))

    def read(a: int) -> np.ndarray:
        b = min(i1, a + block)
        da = _isel(src, lv, extra, **{lv.grid.y_dim: r, lv.grid.x_dim: c, tdim: slice(a, b)})
        return as_float(da.values, lv.da).astype(np.float64).reshape(-1)

    parts = list(_POOL.map(read, range(i0, i1, block)))
    return np.concatenate(parts) if parts else np.empty(0)


def timeseries(
    layer: Layer,
    lon: float,
    lat: float,
    sel: Selection,
    start: str | None,
    end: str | None,
    max_steps: int,
) -> dict[str, Any]:
    """Native time series of every source at one location over [start, end]."""
    series: list[dict[str, Any]] = []
    for k, src in enumerate(layer.sources):
        lv = src.native
        if lv.time is None or src.time_dim is None or src.fixed_time is not None:
            continue
        cell = _cell(lv, lon, lat)
        if cell is None:
            continue
        idx = lv.time.range(start, end)
        if idx.size == 0:
            continue
        if idx.size > max_steps:
            raise QueryError(
                f"{idx.size} time steps requested from {src.id}; the limit is {max_steps}. Narrow the range."
            )
        r, c = cell
        extra = resolve_extra_dims(src, sel.dims)
        i0, i1 = int(idx.min()), int(idx.max()) + 1
        vals = _read_series(src, lv, extra, r, c, i0, i1)[idx - i0] * src.scale + src.offset
        series.append(
            {
                "source": k,
                "id": src.id,
                "resolution": lv.grid.resolution_label(),
                "cell": _cell_info(lv, r, c),
                "times": [lv.time.labels[i] for i in idx],
                "values": [_clean(v) for v in vals],
            }
        )
    if not series:
        raise QueryError("no time-dependent data at this location")
    return {"lon": lon, "lat": lat, "units": layer.units, "series": series}


def stats(
    engine: TileEngine,
    layer: Layer,
    sel: Selection,
    bbox: tuple[float, float, float, float] | None = None,
    bins: int = 64,
) -> dict[str, Any]:
    """Value statistics from a few low-zoom tiles over `bbox` (default: the layer extent)."""
    w, s, e, n = bbox or layer.bounds
    s, n = max(s, -85.0), min(n, 85.0)
    span = max(e - w, (n - s) * 1.5, 1e-6)
    z = int(np.clip(math.floor(math.log2(360.0 / span)) + 1, 0, 12))
    x0, y0 = lonlat_to_tile(w, n, z)
    if e > 180.0 and e - w < 360.0:  # a grid across the antimeridian, e.g. 160…200
        e -= 360.0
    x1, y1 = lonlat_to_tile(min(e, 179.999), s, z)
    if x1 < x0:  # bbox across the antimeridian: just use the whole width
        x0, x1 = 0, (1 << z) - 1
    size = engine.settings.tile_size
    values = []
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            res = engine.render(layer, Tile(z, x, y, size), sel)
            planes = res.bands if res.bands is not None else [res.data]
            v = np.concatenate([p[np.isfinite(p)] for p in planes])
            if v.size:
                values.append(v)
    if not values:
        return {"count": 0, "zoom": z}
    v = np.concatenate(values)
    p = np.percentile(v, [2, 25, 50, 75, 98])
    lo, hi = float(v.min()), float(v.max())
    hist, edges = np.histogram(v, bins=bins, range=(lo, hi if hi > lo else lo + 1))
    return {
        "count": int(v.size),
        "zoom": z,
        "min": lo,
        "max": hi,
        "mean": float(v.mean()),
        "p2": float(p[0]),
        "p25": float(p[1]),
        "median": float(p[2]),
        "p75": float(p[3]),
        "p98": float(p[4]),
        "histogram": {"counts": hist.tolist(), "edges": edges.tolist()},
    }


def export_subset(
    source: Source,
    bbox: tuple[float, float, float, float],
    sel: Selection,
    start: str | None,
    end: str | None,
    max_mb: int,
) -> xr.Dataset:
    """Native-resolution subset of one source as a Dataset (attributes kept)."""
    lv = source.native
    g = lv.grid
    w, s, e, n = bbox
    lons = np.array([w, e, e, w, (w + e) / 2])
    lats = np.array([s, s, n, n, (s + n) / 2])
    gx, gy = g.from_lonlat(lons, lats)
    px, py = g.x.position(gx), g.y.position(gy)

    def cells(lo: float, hi: float, n: int) -> tuple[int, int]:
        """Cells whose centres lie in [lo, hi] (positions); at least the cell under the middle."""
        a, b = int(np.ceil(lo - 0.5)), int(np.ceil(hi - 0.5))
        if b <= a:
            a = int(np.floor((lo + hi) / 2))
            b = a + 1
        return max(0, a), min(n, b)

    if g.geographic and e - w >= 360:
        c0, c1 = 0, g.x.n
    else:
        c0, c1 = cells(float(np.nanmin(px)), float(np.nanmax(px)), g.x.n)
    r0, r1 = cells(float(np.nanmin(py)), float(np.nanmax(py)), g.y.n)
    if c1 <= c0 or r1 <= r0:
        raise QueryError("the box does not overlap the data")
    if g.geographic and px[1] < px[0] and not (e - w >= 360):
        raise QueryError(
            "boxes across the dataset's longitude seam are not supported yet; split the box"
        )
    extra = resolve_extra_dims(source, sel.dims)
    isel: dict[str, Any] = {g.y_dim: slice(r0, r1), g.x_dim: slice(c0, c1)}
    if source.time_dim and lv.time is not None:
        if start or end:
            idx = lv.time.range(start, end)
            if idx.size == 0:
                raise QueryError("no time steps in the requested range")
            isel[source.time_dim] = slice(int(idx.min()), int(idx.max()) + 1)
        else:
            t = time_index(lv, sel.time)
            if t is None:
                raise SelectionError(f"no data at {sel.time}")
            isel[source.time_dim] = slice(t, t + 1)
    da = _isel(source, lv, extra, **isel)
    nbytes = int(np.prod(da.shape)) * 4
    if nbytes > max_mb * 2**20:
        raise QueryError(f"the export would be {nbytes / 2**20:.0f} MiB; the limit is {max_mb} MiB")
    da = da.astype(np.float32) * np.float32(source.scale) + np.float32(source.offset)
    da.attrs = {
        k: v
        for k, v in source.attrs.items()
        if k not in ("scale_factor", "add_offset", "_FillValue")
    }
    ds = da.to_dataset(name=source.variable)
    for v in ds.variables.values():
        v.encoding = {}
    ds.attrs = {
        "title": f"{source.dataset_title}: {source.variable} subset",
        "source": source.native.location,
        "history": "exported by TerraScope at native resolution",
        "terrascope_bbox": [float(b) for b in bbox],
    }
    return ds


def to_netcdf_bytes(ds: xr.Dataset) -> bytes:
    fd, path = tempfile.mkstemp(suffix=".nc")
    os.close(fd)
    try:
        ds.to_netcdf(path, engine="h5netcdf")
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        os.unlink(path)
