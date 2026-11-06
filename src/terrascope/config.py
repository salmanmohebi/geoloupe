"""Configuration: which datasets to serve and how to combine them into layers.

A configuration is a YAML file (or the equivalent dict). Minimal example::

    datasets:
      era5:
        path: data/era5.zarr
      regional:
        path: data/regional-1km.zarr
        overviews: data/regional-1km.overviews.zarr

    layers:
      tas:
        title: Near-surface air temperature
        sources:                      # finest data wins, coarser data fills gaps
          - {dataset: regional, variable: tas}
          - {dataset: era5, variable: t2m}

Every gridded variable of every dataset is also available as its own layer
(id ``<dataset>:<variable>``), so ``layers`` is only needed for composites.

Strings may reference environment variables as ``${NAME}`` or ``${NAME:-default}``.
Relative paths are resolved against the directory of the configuration file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Resampling = Literal["mean", "nearest", "min", "max", "mode"]

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """The configuration is invalid (bad YAML, unknown keys, missing env variable, ...)."""


def expand_env(value: str) -> str:
    """Replace ``${NAME}`` / ``${NAME:-default}`` with environment values.

    An unset variable without a default is an error: a silently empty path
    usually points at the wrong data.
    """

    def repl(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise ConfigError(f"environment variable ${{{name}}} is not set (used in {value!r})")

    return _ENV_RE.sub(repl, value)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClassInfo(_Model):
    """One class of categorical data (land cover, masks, …)."""

    name: str
    color: str | None = None  # "#rrggbb"


class Style(_Model):
    """Display defaults for a variable or layer. Every field is optional."""

    title: str | None = None
    units: str | None = None
    colormap: str | None = None
    vmin: float | None = None
    vmax: float | None = None
    # How a source is aggregated when one screen pixel covers several grid cells
    # (on-the-fly and in overview pyramids). "mean" suits continuous fields;
    # use "nearest" for categorical data such as land cover classes.
    resampling: Resampling | None = None
    # Categorical data: class value → name and colour, e.g. {10: {name: Tree cover, color: "#006400"}}.
    # Merged with what the file says (CF flag_values/flag_meanings/flag_colors, GeoTIFF palette).
    classes: dict[int, ClassInfo] | None = None


class DatasetConfig(_Model):
    """One gridded dataset: a file, a set of files, or a store, opened with xarray."""

    title: str | None = None
    description: str | None = None
    attribution: str | None = None
    # Where the data is. Exactly one of `path` and `levels` is required.
    path: str | list[str] | None = None
    # Alternative to `path`: the same data at several resolutions (any order),
    # e.g. existing 30 arcsec, 0.025° and 0.25° copies of one region.
    levels: list[str] | None = None
    # Alternative to `path`: many files that tile an area (e.g. 1° × 1° DEM tiles, satellite
    # scenes), shown as one layer; the first file with data at a pixel wins.
    mosaic: list[str] | None = None
    # Name of the variable of single-variable files (GeoTIFF names come from the file name).
    variable_name: str | None = None
    # External overview pyramid(s) written by `terrascope overviews build`: one store, a list,
    # or a glob (e.g. one store per year built by parallel jobs: "ovr/*.zarr").
    overviews: str | list[str] | None = None
    engine: Literal["auto", "zarr", "netcdf", "grib", "geotiff"] = "auto"
    variables: list[str] | None = None
    # Overrides for when the metadata does not say it.
    crs: str | None = None
    x: str | None = None
    y: str | None = None
    time: str | None = None
    # Keep only part of the time axis, e.g. ["1940-01-01", "2026-10-30"]; either end may be null.
    # Useful for stores whose axis extends past the data (filled with missing values).
    time_range: tuple[str | None, str | None] | None = None
    time_tolerance: str | None = Field(
        default=None,
        description="Max distance for time matching, e.g. '12h'. Default: half a step for monthly or coarser data, else at most 12 h.",
    )
    storage_options: dict[str, Any] = Field(default_factory=dict)
    open_kwargs: dict[str, Any] = Field(default_factory=dict)
    styles: dict[str, Style] = Field(
        default_factory=dict, description="Per-variable display defaults."
    )
    # Per-variable scale/offset when the files do not carry them, e.g. Sentinel-2 COGs whose
    # reflectance scale is only in the STAC metadata: {B04: {scale: 0.0001, offset: -0.1}}.
    decode: dict[str, Decode] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_location(self) -> DatasetConfig:
        if sum(x is not None for x in (self.path, self.levels, self.mosaic)) != 1:
            raise ValueError("give exactly one of 'path', 'levels' and 'mosaic'")
        return self


class Decode(_Model):
    """Stored value → physical value: value × scale + offset (for files without CF metadata)."""

    scale: float = 1.0
    offset: float = 0.0


class SourceRef(_Model):
    """One member of a composite layer."""

    dataset: str
    variable: str
    # Unit conversion into the layer's units: value * scale + offset.
    scale: float = 1.0
    offset: float = 0.0
    # Fixed values of extra dimensions for this source, e.g. {band: 4} in an RGB layer.
    dims: dict[str, str | int | float] = Field(default_factory=dict)
    # One fixed time for a time-dependent source: a static field stored with a time axis (e.g. surface
    # geopotential in an hourly store) then behaves as static in this layer.
    time: str | None = None
    # Optional explicit zoom window (Web Mercator zoom) for this source.
    min_zoom: float | None = None
    max_zoom: float | None = None


class LayerConfig(Style):
    """A composite layer: several sources of one quantity at different resolutions."""

    description: str | None = None
    sources: list[SourceRef]
    # "resolution": finest available source first (default). "listed": config order.
    priority: Literal["resolution", "listed"] = "resolution"
    # RGB composite: the three sources are the red, green and blue channels (in this order).
    rgb: bool = False
    gamma: float = 1.0  # display gamma of an RGB layer

    @field_validator("sources")
    @classmethod
    def _non_empty(cls, v: list[SourceRef]) -> list[SourceRef]:
        if not v:
            raise ValueError("a layer needs at least one source")
        return v

    @model_validator(mode="after")
    def _rgb_needs_three(self) -> LayerConfig:
        if self.rgb and len(self.sources) != 3:
            raise ValueError("an RGB layer needs exactly three sources (red, green, blue)")
        return self


class PointColumns(_Model):
    """Column names of a point table. Unset names are detected (lat/latitude, date/time, …)."""

    id: str | None = None
    name: str | None = None
    lon: str | None = None
    lat: str | None = None
    elevation: str | None = None
    time: str | None = None
    # Long tables: the column holding the variable name, and the value column.
    # Wide tables (one column per variable) have neither.
    variable: str | None = None
    value: str | None = None
    # GeoParquet: a WKB point column instead of lon/lat columns (detected as "geometry").
    geometry: str | None = None


class PointVariable(_Model):
    """Unit conversion of one observed variable into the layer's units: value * scale + offset."""

    scale: float = 1.0
    offset: float = 0.0
    units: str | None = None


class PointsConfig(_Model):
    """Point observations (stations) drawn over gridded layers with the same colour scale."""

    title: str | None = None
    attribution: str | None = None
    path: str | list[str]  # a file, a folder (hive-partitioned Parquet), or several files/URLs
    format: Literal["auto", "parquet", "csv", "netcdf", "zarr"] = "auto"
    # Time step of very large tables whose times are not listed (e.g. "1D"); default: detected.
    time_step: str | None = None
    columns: PointColumns = Field(default_factory=PointColumns)
    # Equality filters applied to every query, e.g. {duplicate: false, qc_flag: 0}.
    filters: dict[str, Any] = Field(default_factory=dict)
    # Unit conversion per variable. For a wide CSV (one column per variable) it also selects the
    # columns: only these variables (and the id, name, position and time columns) are read.
    variables: dict[str, PointVariable] = Field(default_factory=dict)
    # Which layer shows which observed variable, e.g. {tas: tg, "regional:tas": tg, pr: rr}.
    layers: dict[str, str] = Field(default_factory=dict)


class Settings(_Model):
    """Server limits. The defaults suit a workstation or a single compute node."""

    # A tile is never computed from more than this many grid cells per source;
    # beyond that the source is skipped at this zoom (build overviews to see it).
    max_read_cells: int = 6_000_000
    tile_size: int = 256
    tile_cache_mb: int = 256
    # Upper bound on time steps in one time-series request.
    max_timeseries_steps: int = 20_000
    # Upper bound on the uncompressed size of a NetCDF export.
    max_export_mb: int = 512
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])


class ViewConfig(_Model):
    """A featured view: a ready-made map (layer, time, place, colour scale) listed in the gallery.

    Opening a view is the same as opening a shared link with this state.
    """

    title: str
    description: str | None = None
    layer: str
    time: str | None = None
    dims: dict[str, str | int | float] = Field(default_factory=dict)
    center: tuple[float, float] | None = None  # lon, lat
    zoom: float | None = None
    colormap: str | None = None
    vmin: float | None = None
    vmax: float | None = None
    log: bool = False
    basemap: Literal["minimal", "light", "dark"] | None = None
    globe: bool = False
    # A second layer to compare with: side by side ("split") or the difference ("diff").
    compare: str | None = None
    compare_mode: Literal["split", "diff"] = "split"
    compare_time: str | None = None
    # Station overlay: a points id, or "none" to hide stations.
    overlay: str | None = None


class Config(_Model):
    title: str = "TerraScope"
    description: str | None = None  # shown on the gallery start screen
    datasets: dict[str, DatasetConfig] = Field(default_factory=dict)
    layers: dict[str, LayerConfig] = Field(default_factory=dict)
    points: dict[str, PointsConfig] = Field(default_factory=dict)
    views: list[ViewConfig] = Field(default_factory=list)
    settings: Settings = Field(default_factory=Settings)
    # Directory used to resolve relative paths (not part of the YAML).
    base_dir: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _check_refs(self) -> Config:
        for lid, layer in self.layers.items():
            if ":" in lid:
                raise ValueError(
                    f"layer id {lid!r} must not contain ':' (reserved for dataset layers)"
                )
            for s in layer.sources:
                if s.dataset not in self.datasets:
                    raise ValueError(f"layer {lid!r} uses unknown dataset {s.dataset!r}")
        for did in self.datasets:
            if ":" in did:
                raise ValueError(f"dataset id {did!r} must not contain ':'")
        for pid in self.points:
            if ":" in pid or pid in self.datasets:
                raise ValueError(f"points id {pid!r} must not contain ':' or repeat a dataset id")
        return self

    def resolve(self, location: str) -> str:
        """Expand env variables; make local relative paths absolute (URLs are kept)."""
        location = expand_env(location)
        if "://" in location or os.path.isabs(location) or self.base_dir is None:
            return location
        return str((self.base_dir / location).resolve())


def load_config(path: str | os.PathLike[str]) -> Config:
    """Read and validate a YAML configuration file."""
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {p}: {e}") from e
    return config_from_dict(raw, base_dir=p.resolve().parent)


def config_from_dict(raw: dict[str, Any], base_dir: Path | None = None) -> Config:
    try:
        cfg = Config.model_validate(raw)
    except ValueError as e:
        raise ConfigError(str(e)) from e
    cfg.base_dir = base_dir
    return cfg
