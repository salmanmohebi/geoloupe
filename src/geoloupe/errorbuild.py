"""Precomputed error maps: ``geoloupe errors build``.

On the fly, error tiles are exact only while the native cells under a tile fit the read budget
(see errortiles.py); a global 1 km field seen whole does not. This module computes the error of
P against T once, per native cell of T and per time step, and writes it as a Zarr store on T's
grid, with block-mean overviews (the most frequent class for class maps). Every level of that
pyramid holds means of the native errors, so the map is exact at every zoom.

The error is computed on the finer of the two native grids, as error tiles are: the layer with
the finer cells gives the grid (it must be one dataset variable on 1-D x/y coordinates), and the
other layer is sampled at the centres of its cells (the first of its sources that has a value
there). P is converted into T's units first. The store records which error it holds (attribute
``geoloupe:error``); a server that has it in its configuration serves error panes asking for
the same P, T and definition from it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from .catalog import Catalog, Layer, Source
from .engine import read_window, resolve_extra_dims, time_index
from .errors import ErrorSpec, conversion, resolve
from .overviews import _cf_mapping, _time_encoding

log = logging.getLogger(__name__)

ATTR = "geoloupe:error"


def error_key(
    p: str,
    t: str,
    spec: ErrorSpec,
    p_dims: dict[str, str] | None = None,
    t_dims: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What a precomputed error holds; equal keys mean the same error map."""
    return {
        "p": p,
        "t": t,
        "metric": spec.metric,
        "formula": spec.formula,
        "params": {k: float(v) for k, v in sorted(spec.params.items())},
        "units": spec.units if spec.metric == "custom" else None,
        "p_dims": dict(sorted((p_dims or {}).items())),
        "t_dims": dict(sorted((t_dims or {}).items())),
    }


def sample_at(
    layer: Layer, label: str | None, lon: np.ndarray, lat: np.ndarray, dims: dict[str, str]
) -> np.ndarray:
    """Native values of a layer at points: per point, the first source (in the layer's order)
    whose native cell under the point has a value."""
    out = np.full(lon.shape, np.nan, np.float32)
    for src in layer.sources:
        lv = src.levels[0]
        ti = time_index(lv, src.time_for(label))
        if ti is None:
            continue
        rows, cols = lv.grid.locate(lon, lat)
        need = np.isnan(out) & (rows >= 0)
        if not need.any():
            continue
        r0, r1 = int(rows[need].min()), int(rows[need].max()) + 1
        c0, c1 = int(cols[need].min()), int(cols[need].max()) + 1
        extra = resolve_extra_dims(src, tuple(sorted(dims.items())))
        win = read_window(lv, src, ti, extra, r0, r1, c0, c1)
        vals = win[rows[need] - r0, cols[need] - c0]
        if src.scale != 1.0 or src.offset != 0.0:
            vals = vals * np.float32(src.scale) + np.float32(src.offset)
        out[need] = vals
    return out


def _grid_source(layer: Layer) -> Source | None:
    """The source whose native grid can carry the error map, or None."""
    if len(layer.sources) != 1 or layer.sources[0].levels[0].grid.curvi is not None:
        return None
    return layer.sources[0]


def _finest_m(layer: Layer) -> float:
    return min(s.levels[0].grid.cell_size_m() for s in layer.sources)


def choose_grid(P: Layer, T: Layer) -> tuple[str, Source]:
    """("P" or "T", source): the layer with the finer cells gives the grid of the error map."""
    for role, layer in (("P", P), ("T", T)):
        if layer.kind == "rgb":
            raise ValueError(f"{role} is an RGB layer: an error map needs one value per cell")
    order = [("P", P), ("T", T)] if _finest_m(P) < _finest_m(T) else [("T", T), ("P", P)]
    role, finer = order[0]
    src = _grid_source(finer)
    if src is None:
        raise ValueError(
            f"{role} ({finer.id}) has the finer cells but "
            + (
                f"combines {len(finer.sources)} sources"
                if len(finer.sources) != 1
                else "is on a curvilinear grid"
            )
            + f"; give one dataset variable on 1-D x/y coordinates as {role}"
        )
    return role, src


def build_errors(
    catalog: Catalog,
    p_id: str,
    t_id: str,
    out: str,
    metric: str = "bias",
    formula: str | None = None,
    params: dict[str, float] | None = None,
    units: str | None = None,
    start: str | None = None,
    end: str | None = None,
    p_dims: dict[str, str] | None = None,
    t_dims: dict[str, str] | None = None,
    overviews: bool = True,
    overwrite: bool = False,
    block_cells: int = 4_000_000,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Write the error of P against T on T's native grid to the Zarr store `out` (and its
    overviews to `out` + ``.overviews.zarr``)."""
    import zarr
    from zarr.codecs import BloscCodec

    P, T = catalog.layer(p_id), catalog.layer(t_id)
    on, src_t = choose_grid(P, T)  # src_t: the source whose grid the error map uses
    other, other_dims = (T, t_dims) if on == "P" else (P, p_dims)
    spec = resolve(metric, formula, params, units)
    conv = conversion(P.units, T.units)
    if conv.warning:
        log.warning(conv.warning)
    lv = src_t.levels[0]
    g = lv.grid
    ny, nx = g.shape
    x_dim, y_dim = g.x_dim, g.y_dim
    xc = np.asarray(lv.da[x_dim].values)
    yc = np.asarray(lv.da[y_dim].values)
    if lv.time is not None:
        t_idx = lv.time.range(start, end)
        if t_idx.size == 0:
            raise ValueError("no time steps of T in the requested range")
        labels: list[str | None] = [lv.time.labels[int(i)] for i in t_idx]
    else:
        t_idx, labels = np.array([-1]), [None]

    if os.path.exists(out):
        if not overwrite:
            raise FileExistsError(f"{out} exists; use --overwrite to replace it")
        shutil.rmtree(out)
    root = zarr.open_group(out, mode="w", zarr_format=3)
    for name, vals in ((x_dim, xc), (y_dim, yc)):
        a = root.create_array(
            name, shape=vals.shape, dtype=vals.dtype, chunks=vals.shape, dimension_names=[name]
        )
        a[:] = vals
        a.attrs.update(
            {k: v for k, v in lv.da[name].attrs.items() if isinstance(v, str | int | float)}
        )
    lead: list[str] = []
    if lv.time is not None and src_t.time_dim:
        tdim = src_t.time_dim
        enc_t, t_attrs = _time_encoding(np.asarray(lv.da[tdim].values)[t_idx], lv.time.calendar)
        a = root.create_array(
            tdim, shape=enc_t.shape, dtype="int64", chunks=enc_t.shape, dimension_names=[tdim]
        )
        a[:] = enc_t
        a.attrs.update(t_attrs)
        lead = [tdim]
    sr = root.create_array("spatial_ref", shape=(), dtype="int8", fill_value=0)
    sr.attrs.update(
        {"crs_wkt": g.crs.to_wkt(), "spatial_ref": g.crs.to_wkt(), **_cf_mapping(g.crs)}
    )
    rows = max(1, min(ny, block_cells // max(nx, 1)))
    chunk_rows = min(512, ny)
    rows = max(chunk_rows, rows // chunk_rows * chunk_rows)  # whole chunks per write
    arr = root.create_array(
        "error",
        shape=(len(labels), ny, nx) if lead else (ny, nx),
        chunks=(1, chunk_rows, min(512, nx)) if lead else (chunk_rows, min(512, nx)),
        dtype="float32",
        fill_value=float("nan"),
        dimension_names=[*lead, y_dim, x_dim],
        compressors=BloscCodec(cname="zstd", clevel=5, shuffle="shuffle"),
    )
    key = error_key(P.id, T.id, spec, p_dims, t_dims)
    t_units = T.units
    arr.attrs.update(
        {
            "long_name": f"{spec.metric if spec.metric != 'custom' else spec.formula}: "
            f"{P.title} against {T.title}",
            "units": spec.units_for(t_units),
            "grid_mapping": "spatial_ref",
            "coordinates": "spatial_ref",
            ATTR: json.dumps(key),
            **(
                {
                    "flag_values": [c["value"] for c in spec.classes],
                    "flag_meanings": " ".join(
                        c["name"].lower().replace(" ", "_") for c in spec.classes
                    ),
                }
                if spec.classes
                else {}
            ),
        }
    )
    root.attrs.update(
        {
            "title": f"Error of {P.title} against {T.title}",
            ATTR: json.dumps(key),
            "geoloupe:conversion": json.dumps({"scale": conv.scale, "offset": conv.offset}),
        }
    )

    # cell centres in lon/lat, one block of rows at a time
    to_ll = None if g.geographic else g.transformer_to_lonlat
    grid_dims = p_dims if on == "P" else t_dims
    extra_t = resolve_extra_dims(src_t, tuple(sorted((grid_dims or {}).items())))
    t0 = time.perf_counter()
    for k, label in enumerate(labels):
        ti = int(t_idx[k])
        for r0 in range(0, ny, rows):
            r1 = min(ny, r0 + rows)
            gv = read_window(lv, src_t, ti, extra_t, r0, r1, 0, nx)
            if src_t.scale != 1.0 or src_t.offset != 0.0:
                gv = gv * np.float32(src_t.scale) + np.float32(src_t.offset)
            X, Y = np.meshgrid(xc, yc[r0:r1])
            if to_ll is None:
                lon, lat = X, Y
            else:
                lon, lat = to_ll.transform(X, Y)
            sampled = sample_at(other, label, np.asarray(lon), np.asarray(lat), other_dims or {})
            pv, tv = (gv, sampled) if on == "P" else (sampled, gv)
            if not conv.identity:
                pv = pv * np.float32(conv.scale) + np.float32(conv.offset)
            e = spec.compute(pv, tv)
            if lead:
                arr[k, r0:r1, :] = e
            else:
                arr[r0:r1, :] = e
        if progress:
            progress(k + 1, len(labels))
    zarr.consolidate_metadata(out)
    secs = time.perf_counter() - t0
    result: dict[str, Any] = {
        "time_steps": len(labels),
        "shape": [ny, nx],
        "seconds": secs,
        "overviews": None,
        "key": key,
    }
    if overviews:
        from .overviews import build as build_overviews

        ov = out.rstrip("/").removesuffix(".zarr") + ".overviews.zarr"
        try:
            res = build_overviews(
                out, ov, method="mode" if spec.classes else "mean", overwrite=True
            )
            result["overviews"] = ov
            result["levels"] = res["levels"]
        except ValueError as e:  # a small grid needs no overviews
            log.info("no overviews: %s", e)
    log.info("error map: %d steps × %d × %d cells in %.1f s", len(labels), ny, nx, secs)
    return result


def find_precomputed(catalog: Catalog, key: dict[str, Any]) -> Layer | None:
    """The layer of a configured dataset holding exactly this error (see build_errors)."""
    want = json.dumps(key, sort_keys=True)
    for d in catalog.datasets.values():
        if d.status != "ok":
            continue
        for src in d.sources.values():
            raw = src.attrs.get(ATTR)
            if not raw:
                continue
            try:
                have = json.dumps(json.loads(raw), sort_keys=True)
            except (TypeError, ValueError):
                continue
            if have == want:
                lid = src.id
                return catalog.layer(lid) if lid in catalog.layers else None
    return None


def config_snippet(name: str, out: str, overviews: str | None) -> str:
    lines = [f"  {name}:", f"    path: {out}"]
    if overviews:
        lines.append(f"    overviews: {overviews}")
    return "datasets:\n" + "\n".join(lines)
