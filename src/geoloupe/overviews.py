"""Build overview pyramids in the native grid (external overviews, like GDAL .ovr).

``geoloupe overviews build data.zarr data.overviews.zarr`` writes coarser
copies of a dataset — factors 2, 4, 8, ... (or starting at ``--min-factor``)
— into a separate Zarr v3 store. The source is never modified, so read-only
archives work, and the native level is not duplicated.

Design choices:

* **Native grid, not Web Mercator.** Each level is a block aggregate (default:
  NaN-aware mean of the cells in a factor × factor block) on the dataset's own
  grid and CRS. Values stay traceable to the source cells and one pyramid
  serves any display projection; reprojection happens per tile at display
  time.
* **Every level is computed from the native data**, not from the level
  above, so means are exact block means even where blocks are partly missing
  (coastlines): a mean of means would weight cells unequally.
* **Packing is kept.** A source stored as integers with scale_factor and
  _FillValue (e.g. uint16 with a 0.1 K step) is written with the same packing, so
  overviews cost about a twelfth of the source with --min-factor 4.
* **Metadata** follows the Zarr conventions ``multiscales``, ``proj`` and
  ``spatial`` v0.1 (the GeoZarr suite), so other tools (xpublish-tiles,
  zarr-layer, OpenLayers GeoZarr, titiler-eopf) can read the pyramid too.

Time steps are independent and can be processed in parallel (``--workers``).
"""

from __future__ import annotations

import itertools
import logging
import math
import os
import shutil
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from multiprocessing import get_context
from typing import Any, cast

import numpy as np
import xarray as xr

from geoloupe.grid import Axis, Grid, find_spatial_dims
from geoloupe.readers import open_dataset
from geoloupe.timeaxis import TimeAxis

log = logging.getLogger(__name__)

CONVENTIONS = [
    {
        "schema_url": "https://raw.githubusercontent.com/zarr-conventions/multiscales/refs/tags/v0.1/schema.json",
        "spec_url": "https://github.com/zarr-conventions/multiscales/blob/v0.1/README.md",
        "uuid": "d35379db-88df-4056-af3a-620245f8e347",
        "name": "multiscales",
        "description": "Multiscale layout of zarr datasets",
    },
    {
        "schema_url": "https://raw.githubusercontent.com/zarr-conventions/proj/refs/tags/v0.1/schema.json",
        "spec_url": "https://github.com/zarr-conventions/proj/blob/v0.1/README.md",
        "uuid": "f17cb550-5864-4468-aeb7-f3180cfb622f",
        "name": "proj",
        "description": "Coordinate reference system information for geospatial data",
    },
    {
        "schema_url": "https://raw.githubusercontent.com/zarr-conventions/spatial/refs/tags/v0.1/schema.json",
        "spec_url": "https://github.com/zarr-conventions/spatial/blob/v0.1/README.md",
        "uuid": "689b58e2-cf7b-45e0-9fff-9cfc0883d6b4",
        "name": "spatial",
        "description": "Spatial coordinate information",
    },
]
_RESAMPLING_NAME = {
    "mean": "average",
    "nearest": "nearest",
    "min": "min",
    "max": "max",
    "mode": "mode",
}


@dataclass
class BuildSpec:
    """Everything a worker needs (picklable)."""

    source: str
    engine: str
    out: str
    variables: list[str]
    x_dim: str
    y_dim: str
    time_dim: str | None
    var_dims: dict[str, list[str]]
    factors: list[int]
    method: str
    band_rows: int
    packing: dict[str, dict[str, Any]]


def level_factors(nx: int, ny: int, min_factor: int = 2, target: int = 256) -> list[int]:
    """Factors 2^k from `min_factor` until the level's larger side is <= 2 * target."""
    if min_factor < 2 or min_factor & (min_factor - 1):
        raise ValueError("min_factor must be a power of two >= 2")
    out = []
    f = min_factor
    while max(nx, ny) / (f / 2) > 2 * target:
        out.append(f)
        f *= 2
    return out


def coarsen_axis(axis: Axis, f: int) -> np.ndarray:
    """Cell centres of the level with factor f (regular axes continue regularly)."""
    c = axis.centers
    n = math.ceil(c.size / f)
    if axis.regular and c.size > 1:
        step = (c[-1] - c[0]) / (c.size - 1)
        return c[0] + (np.arange(n) * f + (f - 1) / 2) * step
    pad = n * f - c.size
    padded = np.concatenate([c, np.full(pad, np.nan)])
    return np.nanmean(padded.reshape(n, f), axis=1)


def block_reduce(a: np.ndarray, f: int, method: str) -> np.ndarray:
    """Aggregate the last two axes of `a` in f × f blocks (NaN-aware; pads with NaN)."""
    *lead, h, w = a.shape
    H, W = math.ceil(h / f), math.ceil(w / f)
    if (H * f, W * f) != (h, w):
        p = np.full((*lead, H * f, W * f), np.nan, dtype=np.float32)
        p[..., :h, :w] = a
        a = p
    if method == "nearest":
        return a[..., f // 2 :: f, f // 2 :: f].copy()
    if method == "mode":  # majority class (categorical data), ties → smallest value
        b = a.reshape(*lead, H, f, W, f)
        values = np.unique(a[np.isfinite(a)])
        best = np.full((*lead, H, W), np.nan, dtype=np.float32)
        if values.size > 256:
            raise ValueError("mode aggregation is for categorical data (at most 256 classes)")
        top = np.zeros((*lead, H, W), dtype=np.int32)
        for v in values:
            n = (b == v).sum(axis=(-3, -1))
            better = n > top
            best[better] = v
            top = np.maximum(top, n)
        return best
    b = a.reshape(*lead, H, f, W, f)
    with np.errstate(invalid="ignore", divide="ignore"):
        if method == "mean":
            ok = np.isfinite(b)
            s = np.where(ok, b, 0).sum(axis=(-3, -1), dtype=np.float64)
            n = ok.sum(axis=(-3, -1))
            return np.where(n > 0, s / np.maximum(n, 1), np.nan).astype(np.float32)
        if method == "min":
            return np.nanmin(b, axis=(-3, -1)).astype(np.float32)
        if method == "max":
            return np.nanmax(b, axis=(-3, -1)).astype(np.float32)
    raise ValueError(f"unknown method {method!r}")


def _pack(values: np.ndarray, packing: dict[str, Any] | None) -> np.ndarray:
    if not packing:
        return values.astype(np.float32)
    dt = np.dtype(packing["dtype"])
    scale = packing.get("scale_factor", 1.0)
    offset = packing.get("add_offset", 0.0)
    fill = packing["_FillValue"]
    with np.errstate(invalid="ignore"):
        q = np.round((values.astype(np.float64) - offset) / scale)
    info = np.iinfo(dt)
    q = np.clip(q, info.min, info.max)
    q[~np.isfinite(values)] = fill
    return q.astype(dt)


def _packing_of(da: xr.DataArray) -> dict[str, Any] | None:
    enc = da.encoding
    dt = enc.get("dtype")
    if dt is None or np.dtype(dt).kind not in "iu" or "_FillValue" not in enc:
        return None
    return {
        "dtype": str(np.dtype(dt)),
        "scale_factor": float(enc.get("scale_factor", 1.0)),
        "add_offset": float(enc.get("add_offset", 0.0)),
        "_FillValue": int(enc["_FillValue"]),
    }


def _cf_mapping(crs: Any) -> dict[str, Any]:
    """CF grid-mapping attributes of a CRS (JSON-safe), or nothing if pyproj cannot express it."""
    try:
        cf = crs.to_cf()
    except Exception:  # some CRSs have no CF form; crs_wkt is enough
        return {}
    return {k: v for k, v in cf.items() if isinstance(v, str | int | float) and k != "crs_wkt"}


def _affine(xc: np.ndarray, yc: np.ndarray) -> list[float] | None:
    """spatial:transform [a, b, c, d, e, f] (rasterio order) for regular centres."""
    if xc.size < 2 or yc.size < 2:
        return None
    dx = (xc[-1] - xc[0]) / (xc.size - 1)
    dy = (yc[-1] - yc[0]) / (yc.size - 1)
    return [float(dx), 0.0, float(xc[0] - dx / 2), 0.0, float(dy), float(yc[0] - dy / 2)]


def _time_encoding(values: np.ndarray, calendar: str) -> tuple[np.ndarray, dict[str, Any]]:
    days = TimeAxis.from_values(values).values
    return np.round(days * 86400).astype(np.int64), {
        "units": "seconds since 1970-01-01",
        "calendar": calendar,
        "standard_name": "time",
    }


def _time_dim(ds: xr.Dataset, names: list[str], spatial: tuple[str, str]) -> str | None:
    for v in names:
        for d in ds[v].dims:
            if d in spatial or d not in ds.coords:
                continue
            c = ds[d]
            if np.issubdtype(c.dtype, np.datetime64) or (
                c.dtype == object and c.size and hasattr(c.values.flat[0], "calendar")
            ):
                return str(d)
    return None


def build(
    source: str,
    out: str,
    variables: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    min_factor: int = 2,
    method: str = "mean",
    workers: int = 1,
    engine: str = "auto",
    overwrite: bool = False,
    band_mb: int = 1024,
) -> dict[str, Any]:
    """Build external overviews of `source` into the new Zarr store `out`.

    All variables must share one x/y grid. Time-dependent and static
    variables can be mixed; extra dimensions (levels, members) are kept.
    """
    import zarr
    from zarr.codecs import BloscCodec, BloscShuffle

    ds = open_dataset(source, engine)
    names = variables or [str(v) for v in ds.data_vars if find_spatial_dims(ds[v]) is not None]
    if not names:
        raise ValueError("no gridded variables to process")
    da0 = ds[names[0]]
    dims = find_spatial_dims(da0)
    if dims is None:
        raise ValueError(f"cannot find x/y dimensions of {names[0]!r}")
    x_dim, y_dim = dims
    for v in names[1:]:
        if (
            find_spatial_dims(ds[v]) != dims
            or ds[v].sizes[y_dim] != da0.sizes[y_dim]
            or ds[v].sizes[x_dim] != da0.sizes[x_dim]
        ):
            raise ValueError(f"{v!r} is on another grid than {names[0]!r}; build them separately")
    grid = Grid.from_dataarray(ds, da0, x_dim, y_dim)
    tdim = _time_dim(ds, names, dims)
    var_dims = {v: [str(d) for d in ds[v].dims if d not in dims] for v in names}
    t_idx = np.arange(1)
    if tdim:
        t_idx = TimeAxis.from_values(ds[tdim].values).range(start, end)
        if t_idx.size == 0:
            raise ValueError("no time steps in the requested range")
    ny, nx = grid.shape
    factors = level_factors(nx, ny, min_factor)
    if not factors:
        raise ValueError(f"the grid ({ny} × {nx}) is already small; no overviews needed")
    fmax = factors[-1]
    enc = ds[names[0]].encoding
    chunk_rows = (enc.get("preferred_chunks") or {}).get(y_dim) or (
        enc.get("chunks") or [None, None]
    )[-2]
    rows = int(chunk_rows or fmax)
    band = rows * fmax // math.gcd(rows, fmax)
    while band * nx * 4 > band_mb * 2**20 and band > fmax:
        band -= fmax
    band = max(band, fmax)

    if os.path.exists(out):
        if not overwrite:
            raise FileExistsError(f"{out} exists; use --overwrite to replace it")
        shutil.rmtree(out)
    root = zarr.open_group(out, mode="w", zarr_format=3)
    packing = {v: p for v in names if (p := _packing_of(ds[v])) is not None}
    calendar = TimeAxis.from_values(ds[tdim].values).calendar if tdim else None
    layout = []
    crs = grid.crs
    epsg = crs.to_epsg()
    for k, f in enumerate(factors):
        asset = str(int(math.log2(f)))
        xc, yc = coarsen_axis(grid.x, f), coarsen_axis(grid.y, f)
        g = root.create_group(asset)
        aff = _affine(xc, yc)
        lvl_attrs: dict[str, Any] = {
            "spatial:dimensions": [y_dim, x_dim],
            "spatial:shape": [yc.size, xc.size],
        }
        if aff:
            lvl_attrs["spatial:transform"] = aff
        g.attrs.update(lvl_attrs)
        entry: dict[str, Any] = {
            "asset": asset,
            **{k2: v for k2, v in lvl_attrs.items() if k2 != "spatial:dimensions"},
        }
        if k > 0:
            entry["derived_from"] = str(int(math.log2(factors[k - 1])))
            entry["transform"] = {"scale": [2.0, 2.0], "translation": [0.0, 0.0]}
        entry["geoloupe:factor"] = f
        layout.append(entry)
        coords: dict[str, np.ndarray] = {x_dim: xc, y_dim: yc}
        for d in {d for dd in var_dims.values() for d in dd}:
            if d == tdim:
                continue
            coords[d] = np.asarray(ds[d].values) if d in ds.coords else np.arange(ds.sizes[d])
        for name, vals in coords.items():
            a = g.create_array(
                name, shape=vals.shape, dtype=vals.dtype, chunks=vals.shape, dimension_names=[name]
            )
            a[:] = vals
            if name in ds.coords:
                a.attrs.update(
                    {k2: v for k2, v in ds[name].attrs.items() if isinstance(v, str | int | float)}
                )
        if tdim:
            assert calendar is not None
            enc_t, t_attrs = _time_encoding(ds[tdim].values[t_idx], calendar)
            a = g.create_array(
                tdim, shape=enc_t.shape, dtype="int64", chunks=enc_t.shape, dimension_names=[tdim]
            )
            a[:] = enc_t
            a.attrs.update(t_attrs)
        # CF grid mapping, so other readers (xarray/rioxarray, GDAL) know the CRS of every level
        sr = g.create_array("spatial_ref", shape=(), dtype="int8", fill_value=0)
        sr.attrs.update({"crs_wkt": crs.to_wkt(), "spatial_ref": crs.to_wkt(), **_cf_mapping(crs)})
        for v in names:
            lead_dims = var_dims[v]
            lead_shape = [int(t_idx.size) if d == tdim else int(ds.sizes[d]) for d in lead_dims]
            p = packing.get(v)
            dtype = p["dtype"] if p else "float32"
            fill = p["_FillValue"] if p else float("nan")
            arr = g.create_array(
                v,
                shape=(*lead_shape, yc.size, xc.size),
                chunks=(*([1] * len(lead_shape)), min(512, yc.size), min(512, xc.size)),
                dtype=dtype,
                fill_value=fill,
                dimension_names=[*lead_dims, y_dim, x_dim],
                # byte shuffle + zstd: smooth geophysical fields compress ~5-10×
                compressors=BloscCodec(cname="zstd", clevel=5, shuffle=BloscShuffle.shuffle),
            )
            attrs = {
                k2: val
                for k2, val in ds[v].attrs.items()
                if isinstance(val, str | int | float | list)
            }
            if p:
                attrs.update(
                    {
                        "_FillValue": fill,
                        "scale_factor": p["scale_factor"],
                        "add_offset": p["add_offset"],
                    }
                )
            attrs["geoloupe:resampling"] = method
            attrs["grid_mapping"] = "spatial_ref"
            attrs["coordinates"] = "spatial_ref"
            arr.attrs.update(attrs)
    root.attrs.update(
        {
            "zarr_conventions": CONVENTIONS,
            "multiscales": {
                "layout": layout,
                "resampling_method": _RESAMPLING_NAME.get(method, method),
            },
            **({"proj:code": f"EPSG:{epsg}"} if epsg else {"proj:wkt2": crs.to_wkt()}),
            "spatial:dimensions": [y_dim, x_dim],
            "geoloupe:source": source,
            "geoloupe:variables": names,
            "geoloupe:min_factor": min_factor,
            "title": f"Overviews of {os.path.basename(source.rstrip('/'))}",
        }
    )
    spec = BuildSpec(
        source, engine, out, names, x_dim, y_dim, tdim, var_dims, factors, method, band, packing
    )
    jobs = [(int(j), int(src_t)) for j, src_t in enumerate(t_idx)] if tdim else [(0, -1)]
    t0 = time.perf_counter()
    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(workers, mp_context=get_context("spawn")) as pool:
            futs = [pool.submit(_process_chunk, spec, jobs[i::workers]) for i in range(workers)]
            for fut in as_completed(futs):
                fut.result()
    else:
        _process_chunk(spec, jobs)
    zarr.consolidate_metadata(out)
    secs = time.perf_counter() - t0
    log.info("built %d levels × %d steps in %.1f s", len(factors), len(jobs), secs)
    return {"levels": factors, "time_steps": len(jobs), "seconds": secs, "band_rows": band}


def _process_chunk(spec: BuildSpec, jobs: list[tuple[int, int]]) -> None:
    """Compute all levels for some time steps (static variables with time step 0)."""
    import zarr

    if not logging.getLogger().handlers:  # spawned worker processes start without logging
        logging.basicConfig(
            level=logging.INFO,
            format=f"%(asctime)s worker {os.getpid()} %(message)s",
            datefmt="%H:%M:%S",
        )
    ds = open_dataset(spec.source, spec.engine)
    root = zarr.open_group(spec.out, mode="r+")
    levels = {f: cast("zarr.Group", root[str(int(math.log2(f)))]) for f in spec.factors}
    ny = ds.sizes[spec.y_dim]
    t_start = time.perf_counter()
    every = max(1, len(jobs) // 10)
    for k, (out_t, src_t) in enumerate(jobs):
        for v in spec.variables:
            vdims = spec.var_dims[v]
            if spec.time_dim not in vdims and out_t != 0:
                continue  # static: done once
            others = [d for d in vdims if d != spec.time_dim]
            for combo in itertools.product(*(range(ds.sizes[d]) for d in others)):
                sel: dict[str, Any] = dict(zip(others, combo, strict=True))
                idx_out = dict(sel)
                if spec.time_dim in vdims:
                    sel[spec.time_dim] = src_t
                    idx_out[spec.time_dim] = out_t
                lead = tuple(idx_out[d] for d in vdims)
                for r0 in range(0, ny, spec.band_rows):
                    r1 = min(ny, r0 + spec.band_rows)
                    da = (
                        ds[v]
                        .isel({**sel, spec.y_dim: slice(r0, r1)})
                        .transpose(spec.y_dim, spec.x_dim)
                    )
                    band = np.asarray(da.values, dtype=np.float32)
                    for f in spec.factors:
                        red = block_reduce(band, f, spec.method)
                        lr0 = r0 // f
                        arr = cast("zarr.Array", levels[f][v])
                        arr[(*lead, slice(lr0, lr0 + red.shape[0]), slice(None))] = _pack(
                            red, spec.packing.get(v)
                        )
        if (k + 1) % every == 0 or k + 1 == len(jobs):
            el = time.perf_counter() - t_start
            log.info("%d/%d time steps (%.1f s per step)", k + 1, len(jobs), el / (k + 1))
