"""The catalog: datasets → sources (one variable at several resolutions) → layers.

* A **level** is one variable at one resolution: a lazy DataArray and its grid.
* A **source** is one variable of one dataset with all its levels, finest
  first. Levels come from the dataset itself, an explicit list of copies at
  other resolutions, or an external overview pyramid.
* A **layer** is what the user picks in the UI: one or more sources of the
  same quantity. Every gridded variable is a layer of its own
  (``<dataset>:<variable>``); composite layers from the config combine
  several datasets, e.g. a 1 km regional field over a 30 km global one.

Datasets that fail to open do not stop the server: they are listed with
their error message so the UI can show what is wrong.
"""

from __future__ import annotations

import glob
import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import xarray as xr

from geoloupe import stac
from geoloupe.config import (
    Config,
    DatasetConfig,
    Decode,
    LayerConfig,
    SourceRef,
    Style,
    config_from_dict,
)
from geoloupe.grid import Grid, GridError, find_curvilinear, find_spatial_dims
from geoloupe.readers import (
    OpenError,
    detect_engine,
    geotiff_overviews,
    multiscale_groups,
    open_dataset,
    with_retries,
)
from geoloupe.styles import classes_from_attrs, default_style
from geoloupe.timeaxis import TimeAxis, TimeError, merge_labels

log = logging.getLogger(__name__)


@dataclass
class Level:
    """One variable at one resolution."""

    da: xr.DataArray
    grid: Grid
    time: TimeAxis | None
    origin: str  # "native", "copy", "overview" or "subsampled" (curvilinear, on the fly)
    location: str

    @property
    def cell_size(self) -> float:
        """Larger of the x/y cell sizes, in CRS units."""
        return max(self.grid.resolution)


@dataclass
class Source:
    """One variable of one dataset, with all its resolution levels (finest first)."""

    dataset_id: str
    variable: str
    levels: list[Level]
    time_dim: str | None
    extra_dims: dict[str, np.ndarray]
    attrs: dict[str, Any]
    style: Style
    dataset_title: str
    scale: float = 1.0
    offset: float = 0.0
    min_zoom: float | None = None
    max_zoom: float | None = None
    # fixed selections, e.g. (("band", "4"),) for one channel of an RGB layer
    fixed_dims: tuple[tuple[str, str], ...] = ()
    fixed_time: str | None = None  # a time-dependent source shown as a static snapshot

    def time_for(self, requested: str | None) -> str | None:
        """The time to read: the fixed one, or the requested one."""
        return self.fixed_time or requested

    @property
    def id(self) -> str:
        return f"{self.dataset_id}:{self.variable}"

    @property
    def native(self) -> Level:
        return self.levels[0]

    @property
    def time(self) -> TimeAxis | None:
        return self.native.time

    @property
    def units(self) -> str | None:
        return self.style.units or self.attrs.get("units")

    @property
    def resampling(self) -> str:
        if self.style.resampling:
            return self.style.resampling
        return "nearest" if self.classes else "mean"  # never average class codes

    @property
    def classes(self) -> dict[int, dict[str, str | None]] | None:
        """Classes of categorical data: the file's (CF flags, palette) updated by the config."""
        out = classes_from_attrs(self.attrs) or {}
        for v, c in (self.style.classes or {}).items():
            out[int(v)] = {"name": c.name, "color": c.color or out.get(int(v), {}).get("color")}
        return out or None

    def with_ref(self, ref: SourceRef) -> Source:
        """This source as a member of a composite (its unit conversion applied after decoding)."""
        return replace(
            self,
            scale=self.scale * ref.scale,
            offset=self.offset * ref.scale + ref.offset,
            min_zoom=ref.min_zoom,
            max_zoom=ref.max_zoom,
            fixed_dims=tuple(sorted((k, str(v)) for k, v in ref.dims.items())),
            fixed_time=ref.time,
        )

    def describe(self) -> dict[str, Any]:
        g = self.native.grid
        return {
            "id": self.id,
            "dataset": self.dataset_id,
            "dataset_title": self.dataset_title,
            "variable": self.variable,
            "long_name": self.attrs.get("long_name") or self.attrs.get("standard_name"),
            "units": self.attrs.get("units"),
            "crs": g.crs.to_string(),
            "shape": list(g.shape),
            "resolution": g.resolution_label(),
            "cell_size_m": g.cell_size_m(),
            "bounds": list(g.bounds_lonlat),
            "levels": [
                {
                    "resolution": lv.grid.resolution_label(),
                    "shape": list(lv.grid.shape),
                    "origin": lv.origin,
                    "times": None if lv.time is None else len(lv.time),
                }
                for lv in self.levels
            ],
            "time_steps": None if self.time is None else len(self.time),
            "time_range": None
            if self.time is None
            else [self.time.labels[0], self.time.labels[-1]],
            "time_step": None if self.time is None else self.time.step_label(),
            "calendar": None if self.time is None else self.time.calendar,
            "fixed_time": self.fixed_time,  # one time used for every map time (static fields)
            "transform": {"scale": self.scale, "offset": self.offset},
        }


@dataclass
class Layer:
    id: str
    title: str
    description: str | None
    sources: list[Source]
    style: Style
    kind: str  # "dataset", "composite" or "rgb" (sources = red, green, blue)
    times: list[str] = field(default_factory=list)
    dims: dict[str, list[Any]] = field(default_factory=dict)
    gamma: float = 1.0
    hidden: bool = False  # not listed (member of a mosaic), but reachable by id
    # RGB layers: the channel (0 red, 1 green, 2 blue) of each source; None = sources 0, 1, 2.
    # Several sources per channel (mosaics) are composited per channel, the first value wins.
    channels: list[int] | None = None

    def channel_heads(self) -> list[Source]:
        """The first source of each RGB channel."""
        chans = self.channels or list(range(len(self.sources)))
        return [self.sources[chans.index(c)] for c in range(3)]

    @property
    def units(self) -> str | None:
        return self.style.units or self.sources[0].units

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        b = np.array([s.native.grid.bounds_lonlat for s in self.sources])
        return (
            float(b[:, 0].min()),
            float(b[:, 1].min()),
            float(b[:, 2].max()),
            float(b[:, 3].max()),
        )


@dataclass
class DatasetEntry:
    id: str
    config: DatasetConfig
    status: str = "ok"  # "ok" or "error"
    error: str | None = None
    sources: dict[str, Source] = field(default_factory=dict)
    attrs: dict[str, Any] = field(default_factory=dict)
    open_seconds: float = 0.0
    hidden: bool = False  # a member of a mosaic (its layers are reached through the mosaic)
    members: list[str] = field(default_factory=list)  # for a mosaic: its member datasets

    @property
    def title(self) -> str:
        return self.config.title or str(self.attrs.get("title") or self.id)


class Catalog:
    """All datasets and layers of one GeoLoupe configuration."""

    def __init__(self, config: Config, datasets: dict[str, xr.Dataset] | None = None):
        self.config = config
        self.settings = config.settings
        self.datasets: dict[str, DatasetEntry] = {}
        self.layers: dict[str, Layer] = {}
        in_memory = datasets or {}
        # Mosaics and STAC datasets become hidden member datasets (one per file) plus one layer
        # per variable.
        jobs: dict[str, DatasetConfig] = {}
        members_of: dict[str, list[str]] = {}
        failed: dict[str, str] = {}
        for did, dcfg in config.datasets.items():
            if dcfg.stac is not None:
                members_of[did] = []
                scfg = dcfg.stac
                try:
                    assets = with_retries(lambda: stac.resolve(scfg), True, did)  # noqa: B023
                except (stac.StacError, OSError) as e:
                    failed[did] = str(e)
                    log.error("dataset %r: %s", did, e)
                    continue
                for k, a in enumerate(assets):
                    decode = dict(dcfg.decode)
                    if a.key not in decode and (a.scale, a.offset) != (1.0, 0.0):
                        decode[a.key] = Decode(scale=a.scale, offset=a.offset)
                    jobs[f"{did}.{k}"] = dcfg.model_copy(
                        update={
                            "stac": None,
                            "path": a.href,
                            "variable_name": a.key,
                            "decode": decode,
                        }
                    )
                    members_of[did].append(f"{did}.{k}")
            elif dcfg.mosaic is not None:
                members_of[did] = []
                for k, loc in enumerate(dcfg.mosaic):
                    jobs[f"{did}.{k}"] = dcfg.model_copy(
                        update={
                            "mosaic": None,
                            "path": loc,
                            "variable_name": dcfg.variable_name or did,
                        }
                    )
                    members_of[did].append(f"{did}.{k}")
            else:
                jobs[did] = dcfg
        # Open in parallel: remote files take a round trip or two each (I/O bound).
        with ThreadPoolExecutor(max_workers=16) as pool:
            entries = dict(
                zip(
                    jobs,
                    pool.map(
                        lambda kv: self._load_dataset(kv[0], kv[1], in_memory.get(kv[0])),
                        jobs.items(),
                    ),
                    strict=True,
                )
            )
        for did, dcfg in config.datasets.items():
            if did not in members_of:
                self.datasets[did] = entries[did]
                continue
            members = members_of[did]
            for m in members:
                entries[m].hidden = True
                self.datasets[m] = entries[m]
            ok = [entries[m] for m in members if entries[m].status == "ok"]
            parent = DatasetEntry(did, dcfg, members=members)
            parent.status = "ok" if ok else "error"
            parent.error = (
                None
                if ok
                else failed.get(did) or "; ".join(f"{m}: {entries[m].error}" for m in members)
            )
            parent.open_seconds = max((entries[m].open_seconds for m in members), default=0.0)
            n_bad = len(members) - len(ok)
            if n_bad and ok:
                log.warning(
                    "mosaic %r: %d of %d files could not be opened", did, n_bad, len(members)
                )
            self.datasets[did] = parent
        self._build_layers()

    def add_datasets(self, datasets: dict[str, DatasetConfig]) -> tuple[list[str], list[str]]:
        """Open more datasets while the server runs: (new visible layer ids, error messages).

        The new datasets are opened through a small catalog of their own, so the ones already
        open are not touched.
        """
        sub = Catalog(
            self.config.model_copy(
                update={
                    "datasets": datasets,
                    "layers": {},
                    "points": {},
                    "vectors": {},
                    "views": [],
                }
            )
        )
        failed = {d.id for d in sub.datasets.values() if d.status != "ok" and not d.hidden}

        def keep(did: str) -> bool:  # failed datasets (and their mosaic members) are not kept
            return not any(did == f or did.startswith(f + ".") for f in failed)

        self.datasets.update({k: v for k, v in sub.datasets.items() if keep(k)})
        layers = {k: v for k, v in sub.layers.items() if all(keep(s.dataset_id) for s in v.sources)}
        self.layers.update(layers)
        self.config.datasets.update({k: v for k, v in datasets.items() if keep(k)})
        errors = [f"{f}: {sub.datasets[f].error}" for f in sorted(failed)]
        return [lid for lid, lyr in layers.items() if not lyr.hidden], errors

    # ---------------------------------------------------------------- creation
    @classmethod
    def from_xarray(
        cls,
        datasets: dict[str, xr.Dataset],
        layers: dict[str, dict[str, Any]] | None = None,
        title: str = "GeoLoupe",
        settings: dict[str, Any] | None = None,
    ) -> Catalog:
        """A catalog of in-memory (or already opened) xarray datasets."""
        raw: dict[str, Any] = {
            "title": title,
            "datasets": {k: {"path": f"memory://{k}"} for k in datasets},
            "layers": layers or {},
        }
        if settings:
            raw["settings"] = settings
        return cls(config_from_dict(raw), datasets=datasets)

    def _open(
        self, dcfg: DatasetConfig, location: str | list[str], group: str | None = None
    ) -> xr.Dataset:
        loc: str | list[str] = (
            [self.config.resolve(p) for p in location]
            if isinstance(location, list)
            else self.config.resolve(location)
        )
        return open_dataset(loc, dcfg.engine, dcfg.storage_options, dcfg.open_kwargs, group=group)

    def _load_dataset(
        self, did: str, dcfg: DatasetConfig, ds_mem: xr.Dataset | None
    ) -> DatasetEntry:
        entry = DatasetEntry(did, dcfg)
        t0 = time.perf_counter()
        try:
            remote = "://" in " ".join(
                str(x) for x in (dcfg.path, dcfg.levels, dcfg.overviews) if x is not None
            )
            levels_ds = with_retries(lambda: self._open_levels(dcfg, ds_mem), remote, did)
            entry.attrs = dict(levels_ds[0][1].attrs)
            entry.sources = self._make_sources(did, dcfg, levels_ds, entry.title)
            if not entry.sources:
                raise GridError("no gridded variables found (need 1-D x/y or lon/lat coordinates)")
        except (OpenError, GridError, TimeError, OSError, ValueError, KeyError) as e:
            entry.status, entry.error = "error", str(e)
            log.error("dataset %r: %s", did, e)
        entry.open_seconds = time.perf_counter() - t0
        if entry.status == "ok":
            log.info(
                "dataset %r: %d variables in %.2f s", did, len(entry.sources), entry.open_seconds
            )
        return entry

    def _open_levels(
        self, dcfg: DatasetConfig, ds_mem: xr.Dataset | None
    ) -> list[tuple[str, xr.Dataset, str]]:
        """(origin, dataset, location) for every resolution level available."""
        out: list[tuple[str, xr.Dataset, str]] = []
        if ds_mem is not None:
            out.append(("native", ds_mem, "memory"))
        elif dcfg.levels is not None:
            for loc in dcfg.levels:
                out.append(("copy", self._open(dcfg, loc), self.config.resolve(loc)))
        else:
            assert dcfg.path is not None
            ploc: str | list[str] = (
                dcfg.path if isinstance(dcfg.path, list) else self.config.resolve(dcfg.path)
            )
            maybe_zarr = isinstance(ploc, str) and (
                dcfg.engine == "zarr" or (dcfg.engine == "auto" and _is_zarr(ploc))
            )
            groups = (
                multiscale_groups(ploc, dcfg.storage_options)
                if maybe_zarr and isinstance(ploc, str)
                else None
            )
            if groups:
                for i, g in enumerate(groups):
                    out.append(
                        (
                            "native" if i == 0 else "overview",
                            self._open(dcfg, ploc, group=g),
                            f"{ploc}/{g}",
                        )
                    )
            else:
                out.append(("native", self._open(dcfg, dcfg.path), str(ploc)))
                if isinstance(ploc, str) and (
                    dcfg.engine == "geotiff"
                    or (dcfg.engine == "auto" and detect_engine(ploc) == "geotiff")
                ):
                    # internal overviews of a Cloud-Optimized GeoTIFF are resolution levels
                    for i, f in enumerate(geotiff_overviews(ploc, dcfg.storage_options)):
                        ocfg = dcfg.model_copy(
                            update={"open_kwargs": {**dcfg.open_kwargs, "overview_level": i}}
                        )
                        out.append(
                            ("overview", self._open(ocfg, dcfg.path), f"{ploc} (overview ×{f})")
                        )
        for oloc in self._overview_locations(dcfg):
            groups = multiscale_groups(oloc, dcfg.storage_options)
            if not groups:
                # Overviews only speed things up: serve the dataset without them.
                log.warning(
                    "overviews %s missing or not a multiscale store; build them with "
                    "`geoloupe overviews build`. Continuing without.",
                    oloc,
                )
            for g in groups or []:
                out.append(("overview", self._open(dcfg, oloc, group=g), f"{oloc}/{g}"))
        if dcfg.variable_name:
            out = [(o, _rename_single(ds, dcfg.variable_name), loc) for o, ds, loc in out]
        return out

    def _overview_locations(self, dcfg: DatasetConfig) -> list[str]:
        """Overview stores of a dataset: a path, a list, or glob patterns."""
        if not dcfg.overviews:
            return []
        items = dcfg.overviews if isinstance(dcfg.overviews, list) else [dcfg.overviews]
        out: list[str] = []
        for item in items:
            loc = self.config.resolve(item)
            if any(ch in loc for ch in "*?[") and "://" not in loc:
                matches = sorted(glob.glob(loc))
                if not matches:
                    log.warning("no overview stores match %s", loc)
                out.extend(matches)
            else:
                out.append(loc)
        return out

    def _make_sources(
        self,
        did: str,
        dcfg: DatasetConfig,
        levels_ds: list[tuple[str, xr.Dataset, str]],
        title: str,
    ) -> dict[str, Source]:
        base = levels_ds[0][1]
        names = dcfg.variables or [str(v) for v in base.data_vars]
        missing = [v for v in names if v not in base.data_vars]
        if missing:
            raise KeyError(
                f"variables not in the dataset: {missing}; available: {list(base.data_vars)}"
            )
        sources: dict[str, Source] = {}
        axes: dict[tuple[Any, ...], TimeAxis] = {}  # one TimeAxis per distinct time coordinate
        for var in names:
            da0 = base[var]
            dims = find_spatial_dims(da0, dcfg.x, dcfg.y)
            # (`d in coords`, not coords.get(d): xarray returns a default index for bare dims)
            rectilinear = dims is not None and all(
                d in da0.coords and da0.coords[d].ndim == 1 for d in dims
            )
            # 2-D longitude/latitude (ocean models, WRF, swaths); for these, `x` and `y` in the
            # config may name the longitude and latitude coordinates. They are the truth unless
            # the 1-D axes come with a CRS (projected grids carry 2-D lon/lat only as a help):
            # model grids often have 1-D *nominal* axes (CMIP6 GFDL: x, y "degrees").
            curv = find_curvilinear(da0, dcfg.x, dcfg.y)
            has_crs = bool(
                dcfg.crs or "grid_mapping" in da0.attrs or "grid_mapping" in da0.encoding
            )
            forced = bool(dcfg.x and dcfg.y and dcfg.x in da0.dims and dcfg.y in da0.dims)
            if rectilinear and (has_crs or forced):
                curv = None
            if curv is not None:
                dims = (curv[0], curv[1])
            elif not rectilinear or dims is None:
                if dcfg.variables:
                    raise GridError(
                        f"{var!r} has no recognisable x/y dimensions or 2-D longitude/latitude "
                        f"{da0.dims}; set 'x' and 'y'"
                    )
                continue
            levels: list[Level] = []
            for origin, ds, loc in levels_ds:
                if var not in ds.data_vars:
                    continue
                da = ds[var]
                # Overviews are built from this dataset, so they share its CRS by construction.
                override = dcfg.crs
                if origin == "overview" and levels:
                    override = levels[0].grid.crs.to_wkt()
                try:
                    if curv is not None:
                        grid = Grid.from_curvilinear(
                            dims[0], dims[1], da.coords[curv[2]].values, da.coords[curv[3]].values
                        )
                    else:
                        grid = Grid.from_dataarray(ds, da, *dims, crs_override=override)
                except GridError as e:
                    if not levels:
                        raise
                    log.warning("skipping level %s of %s:%s: %s", loc, did, var, e)
                    continue
                tdim = self._time_dim(da, dims, dcfg.time)
                if tdim and dcfg.time_range:
                    da = da.sel({tdim: slice(*dcfg.time_range)})
                    if da.sizes[tdim] == 0:
                        raise ValueError(f"time_range {dcfg.time_range} selects no time of {var!r}")
                taxis = _time_axis(da[tdim].values, dcfg.time_tolerance, axes) if tdim else None
                levels.append(Level(da, grid, taxis, origin, loc))
            if not levels:
                continue
            if curv is not None:  # coarser levels on the fly: every 2nd, 4th, … cell
                lv0 = levels[0]
                f = 2
                while max(lv0.grid.shape) // f >= 128:
                    sub = lv0.da.isel(
                        {dims[1]: slice(None, None, f), dims[0]: slice(None, None, f)}
                    )
                    levels.append(
                        Level(
                            sub, lv0.grid.subsample(f), lv0.time, "subsampled", f"every {f}th cell"
                        )
                    )
                    f *= 2
            native_crs = levels[0].grid.crs
            same = [lv for lv in levels if lv.grid.crs.equals(native_crs, ignore_axis_order=True)]
            for lv in levels:
                if not any(lv is k for k in same):
                    log.warning(
                        "skipping level %s of %s:%s: CRS differs from the first level",
                        lv.location,
                        did,
                        var,
                    )
            levels = same
            levels.sort(key=lambda lv: lv.cell_size)
            tdim = self._time_dim(da0, dims, dcfg.time)
            extra = {
                str(d): np.asarray(da0[d].values) if d in da0.coords else np.arange(da0.sizes[d])
                for d in da0.dims
                if d not in (*dims, tdim)
            }
            given = dcfg.styles.get(var, Style())
            style = default_style(var, da0.attrs).model_copy(
                update={  # attributes, not model_dump(): nested ClassInfo must stay objects
                    k: getattr(given, k)
                    for k in Style.model_fields
                    if getattr(given, k) is not None
                }
            )
            dec = dcfg.decode.get(var)
            sources[var] = Source(
                did,
                var,
                levels,
                tdim,
                extra,
                dict(da0.attrs),
                style,
                title,
                scale=dec.scale if dec else 1.0,
                offset=dec.offset if dec else 0.0,
            )
        return sources

    @staticmethod
    def _time_dim(da: xr.DataArray, spatial: tuple[str, str], override: str | None) -> str | None:
        if override:
            return override if override in da.dims else None
        for d in da.dims:
            if d in spatial or d not in da.coords:
                continue
            c = da.coords[d]
            if (
                np.issubdtype(c.dtype, np.datetime64)
                or c.attrs.get("axis") == "T"
                or (c.dtype == object and c.size and hasattr(c.values.flat[0], "calendar"))
            ):
                return str(d)
        return None

    def _build_layers(self) -> None:
        for did, entry in self.datasets.items():
            if entry.members:  # a mosaic: one layer per variable over all member files
                variables: list[str] = []
                for m in entry.members:
                    variables += [v for v in self.datasets[m].sources if v not in variables]
                for var in variables:
                    tiles = [
                        self.datasets[m].sources[var]
                        for m in entry.members
                        if var in self.datasets[m].sources
                    ]
                    style = tiles[0].style
                    self.layers[f"{did}:{var}"] = self._make_layer(
                        f"{did}:{var}",
                        entry.title,
                        f"mosaic of {len(tiles)} files",
                        tiles,
                        style,
                        "composite",
                    )
                    rgb = _auto_rgb_bands(tiles[0])
                    if rgb:  # mosaics of 3-band images (e.g. STAC "visual") in true colour
                        chans = [replace(t, fixed_dims=(("band", b),)) for b in rgb for t in tiles]
                        lyr = self._make_layer(
                            f"{did}:{var}:rgb",
                            f"{entry.title} ({var}, RGB)",
                            f"mosaic of {len(tiles)} files, bands {', '.join(rgb)} as red, green, blue",
                            chans,
                            style,
                            "rgb",
                        )
                        lyr.channels = [c for c in range(3) for _ in tiles]
                        self.layers[lyr.id] = lyr
                continue
            for var, src in entry.sources.items():
                lid = f"{did}:{var}"
                title = src.style.title or src.attrs.get("long_name") or var
                self.layers[lid] = self._make_layer(
                    lid, str(title), None, [src], src.style, "dataset"
                )
                self.layers[lid].hidden = entry.hidden
                rgb = _auto_rgb_bands(src)
                if rgb:  # 3- or 4-band images also get a true-colour layer
                    chans = [replace(src, fixed_dims=(("band", b),)) for b in rgb]
                    self.layers[f"{lid}:rgb"] = self._make_layer(
                        f"{lid}:rgb",
                        f"{title} (RGB)",
                        f"bands {', '.join(rgb)} as red, green, blue",
                        chans,
                        src.style,
                        "rgb",
                    )
                    self.layers[f"{lid}:rgb"].hidden = entry.hidden
        for lid, lcfg in self.config.layers.items():
            srcs: list[Source] = []
            channels: list[int] = []
            problems: list[str] = []
            for c, ref in enumerate(lcfg.sources):
                entry = self.datasets[ref.dataset]
                # a mosaic stands for all its member files
                found = [
                    e.sources[ref.variable]
                    for e in ([self.datasets[m] for m in entry.members] or [entry])
                    if ref.variable in e.sources
                ]
                if entry.status != "ok":
                    problems.append(f"dataset {ref.dataset!r} failed to open")
                elif not found:
                    problems.append(f"{ref.dataset!r} has no gridded variable {ref.variable!r}")
                else:
                    srcs += [src.with_ref(ref) for src in found]
                    channels += [c] * len(found)
            if problems:
                log.error("layer %r: %s", lid, "; ".join(problems))
            if not srcs:
                continue
            if lcfg.rgb and len(set(channels)) != 3:
                continue  # a channel failed to open (reported above)
            if lcfg.priority == "resolution" and not lcfg.rgb:
                srcs.sort(key=lambda s: s.native.grid.cell_size_m())
            style = _merge_style(srcs[0].style, lcfg)
            self.layers[lid] = self._make_layer(
                lid,
                lcfg.title or lid,
                lcfg.description,
                srcs,
                style,
                "rgb" if lcfg.rgb else "composite",
            )
            self.layers[lid].gamma = lcfg.gamma
            if lcfg.rgb:
                self.layers[lid].channels = channels

    @staticmethod
    def _make_layer(
        lid: str, title: str, desc: str | None, srcs: list[Source], style: Style, kind: str
    ) -> Layer:
        axes = [s.time for s in srcs if s.time is not None and s.fixed_time is None]
        dims: dict[str, list[Any]] = {}
        for s in srcs:
            for d, vals in s.extra_dims.items():
                cur = dims.setdefault(d, [])
                for v in vals.tolist():
                    if v not in cur:
                        cur.append(v)
        return Layer(lid, title, desc, srcs, style, kind, merge_labels(axes), dims)

    # ------------------------------------------------------------------ access
    def layer(self, layer_id: str) -> Layer:
        try:
            return self.layers[layer_id]
        except KeyError:
            raise KeyError(f"unknown layer {layer_id!r}") from None

    def source(self, source_id: str) -> Source:
        did, _, var = source_id.partition(":")
        entry = self.datasets.get(did)
        if entry is None or var not in entry.sources:
            raise KeyError(f"unknown source {source_id!r}")
        return entry.sources[var]


def _is_zarr(loc: str) -> bool:
    try:
        return detect_engine(loc) == "zarr"
    except OpenError:
        return False  # unknown format: the open reports it


def _time_axis(
    raw: np.ndarray, tolerance: str | None, cache: dict[tuple[Any, ...], TimeAxis]
) -> TimeAxis:
    """TimeAxis of a coordinate, shared by all variables with identical times (numpy datetimes)."""
    raw = np.asarray(raw)
    if not np.issubdtype(raw.dtype, np.datetime64):
        return TimeAxis.from_values(raw, tolerance)
    key = (raw.dtype.str, raw.shape, hashlib.blake2b(raw.tobytes(), digest_size=16).digest())
    if key not in cache:
        cache[key] = TimeAxis.from_values(raw, tolerance)
    return cache[key]


def _merge_style(base: Style, layer: LayerConfig) -> Style:
    upd = {k: getattr(layer, k) for k in Style.model_fields if getattr(layer, k) is not None}
    return base.model_copy(update=upd)


def _auto_rgb_bands(src: Source) -> list[str] | None:
    """Band labels for an automatic RGB layer: named red/green/blue bands, else bands 1–3 of a 3- or
    4-band image (e.g. a Sentinel-2 TCI or an RGB(A) aerial photo)."""
    bands = src.extra_dims.get("band")
    if bands is None or set(src.extra_dims) != {"band"}:
        return None
    labels = [str(b) for b in bands.tolist()]
    lower = [b.lower() for b in labels]
    if all(c in lower for c in ("red", "green", "blue")):
        return [labels[lower.index(c)] for c in ("red", "green", "blue")]
    if len(labels) in (3, 4):
        return labels[:3]
    return None


def _rename_single(ds: xr.Dataset, name: str) -> xr.Dataset:
    """Rename the one data variable of a file (grid-mapping variables are not counted)."""
    mappings = {str(ds[v].attrs.get("grid_mapping")) for v in ds.data_vars} | {"spatial_ref", "crs"}
    data = [str(v) for v in ds.data_vars if str(v) not in mappings]
    if len(data) != 1 or data[0] == name:
        return ds
    return ds.rename({data[0]: name})
