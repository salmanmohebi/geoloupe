"""Synthetic demo data: a coarse global dataset and a fine regional one.

The fields are built like a real downscaling product, so the multi-resolution
composite looks seamless:

* a smooth large-scale atmosphere (zonal climate, seasons, travelling weather
  waves) is an analytic function of (lon, lat, day), evaluated on each grid;
* terrain is a procedural function too (a few mountain ranges plus fractal
  detail, spectrally truncated per grid so coarse terrain ≈ block mean of
  fine terrain);
* temperature = atmosphere − 6.5 K/km × elevation, so the 1 km field shows
  valleys and peaks while matching the 1° field on average;
* precipitation = weather-wave moisture × orographic enhancement.

Datasets written (``terrascope demo-data DIR``):

=========================== ========================================= ==============
``global-1deg.zarr``        tas, pr; 1° global, daily                   Zarr v3
``alps-1km.zarr``           tas, pr, elevation; 0.01° Alps, daily       Zarr v3
``global-plev-monthly.nc``  ta(plev); 2° global, monthly, 3 levels      NetCDF-4
``europe-laea-5km.nc``      elevation in EPSG:3035 (LAEA Europe), static NetCDF-4 + CF grid mapping
``demo.yaml``               a TerraScope config for all of the above
=========================== ========================================= ==============
"""

from __future__ import annotations

import logging
import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyproj
import xarray as xr

log = logging.getLogger(__name__)

LAPSE = 6.5e-3  # K per m
ALPS = (5.8, 44.0, 16.0, 48.6)  # west, south, east, north

_RNG = np.random.default_rng(20211)
# Procedural terrain: random plane waves with a power-law spectrum (fixed seed).
_N_WAVES = 400
_WAVE_K = np.exp(_RNG.uniform(np.log(0.15), np.log(60.0), _N_WAVES))  # cycles per degree
_WAVE_DIR = _RNG.uniform(0, 2 * np.pi, _N_WAVES)
_WAVE_PHASE = _RNG.uniform(0, 2 * np.pi, _N_WAVES)
_WAVE_AMP = _WAVE_K**-0.45  # flat spectrum: relief at every scale down to ~2 cells
_WAVE_AMP /= np.sqrt((_WAVE_AMP**2).sum() / 2)  # unit standard deviation over all waves
# Mountain ranges: (lon, lat, half-length deg, half-width deg, angle deg, height m)
_RANGES = [
    (10.5, 46.4, 5.5, 0.9, 20, 2300),  # Alps
    (7.0, 45.3, 1.5, 0.8, 80, 1600),  # western Alps
    (13.5, 47.2, 2.5, 0.6, 0, 1200),  # eastern Alps
    (-120, 45, 15, 3, 75, 1500),  # Rockies
    (-70, -25, 25, 2.5, 88, 3500),  # Andes
    (85, 31, 15, 4, 10, 4500),  # Himalaya / Tibet
    (40, 10, 6, 4, 60, 1800),  # Ethiopian highlands
    (25, -28, 8, 5, 30, 1200),  # southern Africa
]


def elevation(lon: np.ndarray, lat: np.ndarray, cell_deg: float) -> np.ndarray:
    """Synthetic elevation (m) at lon/lat, without detail finer than ~2 cells."""
    lon = np.asarray(lon, dtype=np.float64)
    lat = np.asarray(lat, dtype=np.float64)
    z = np.zeros(np.broadcast(lon, lat).shape)
    for clon, clat, hl, hw, ang, h in _RANGES:
        a = np.radians(ang)
        dx = (lon - clon) * np.cos(np.radians(clat))
        dy = lat - clat
        u = dx * np.cos(a) + dy * np.sin(a)
        v = -dx * np.sin(a) + dy * np.cos(a)
        z += h * np.exp(-((u / hl) ** 2) - (v / hw) ** 2)
    keep = 1.0 / (2.5 * cell_deg) > _WAVE_K  # spectral truncation (anti-aliasing)
    if keep.any():
        kx = _WAVE_K[keep] * np.cos(_WAVE_DIR[keep])
        ky = _WAVE_K[keep] * np.sin(_WAVE_DIR[keep])
        rough = np.zeros_like(z)
        for i in range(kx.size):
            rough += _WAVE_AMP[keep][i] * np.sin(
                2 * np.pi * (kx[i] * lon + ky[i] * lat) + _WAVE_PHASE[keep][i]
            )
        z += 1000 * rough * np.clip(z / 1500, 0.0, 1.2)  # relief only in the mountain ranges
    return np.clip(z, 0, None).astype(np.float32)


def _weather(lon: np.ndarray, lat: np.ndarray, day: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Smooth large-scale temperature (K, at sea level) and moisture index, shape (t, y, x)."""
    lon = lon[None, None, :]
    lat = lat[None, :, None]
    d = day[:, None, None]
    phi = np.radians(lat)
    season = np.cos(2 * np.pi * (d - 200) / 365.25)  # +1 in mid-July
    t = 301.0 - 42.0 * np.sin(phi) ** 2 + 13.0 * np.sin(phi) * season
    rng = np.random.default_rng(7)
    moist = np.zeros(np.broadcast(lon, lat, d).shape)
    for k in range(1, 9):
        amp_t = 4.5 / k**0.7
        speed = rng.uniform(4, 12)  # degrees per day eastwards
        ph = rng.uniform(0, 2 * np.pi)
        band = np.exp(-(((np.abs(lat) - 48) / 18) ** 2))
        wave = np.sin(np.radians(k * (lon - speed * d)) * 1.0 + ph + 0.05 * k * lat / 10)
        t = t + amp_t * band * wave
        moist = moist + band * np.cos(np.radians(k * (lon - speed * d)) + ph + 0.4) / k**0.5
    itcz = np.exp(-(((lat - 8 * season) / 9) ** 2))
    moist = moist + 2.2 * itcz
    return t.astype(np.float32), moist.astype(np.float32)


@lru_cache(maxsize=4)
def _elevation_grid(lon_key: bytes, lat_key: bytes, cell_deg: float) -> np.ndarray:
    lon2, lat2 = np.meshgrid(np.frombuffer(lon_key), np.frombuffer(lat_key))
    return elevation(lon2, lat2, cell_deg)


def _fields(
    lon: np.ndarray, lat: np.ndarray, day: np.ndarray, cell_deg: float
) -> dict[str, np.ndarray]:
    z = _elevation_grid(
        lon.astype(np.float64).tobytes(), lat.astype(np.float64).tobytes(), cell_deg
    )
    t0, moist = _weather(lon, lat, day)
    tas = t0 - LAPSE * z[None]
    oro = 1.0 + z[None] / 1200.0
    pr = np.clip(moist - 0.55, 0, None) ** 1.6 * 9.0 * oro
    return {"tas": tas.astype(np.float32), "pr": pr.astype(np.float32), "elevation": z}


def _cf_attrs(ds: xr.Dataset) -> xr.Dataset:
    ds["lon"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["lat"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    if "tas" in ds:
        ds["tas"].attrs.update(
            standard_name="air_temperature", long_name="Near-surface air temperature", units="K"
        )
    if "pr" in ds:
        ds["pr"].attrs.update(
            standard_name="precipitation_amount", long_name="Precipitation", units="mm day-1"
        )
    if "elevation" in ds:
        ds["elevation"].attrs.update(
            standard_name="surface_altitude", long_name="Elevation", units="m"
        )
    return ds


def _doy(days: pd.DatetimeIndex) -> np.ndarray:
    return (
        days.dayofyear.to_numpy().astype(np.float64)
        + 365.25 * (days.year - days.year[0]).to_numpy()
    )


def _grid(box: tuple[float, float, float, float], res: float) -> tuple[np.ndarray, np.ndarray]:
    w, s, e, n = box
    return np.arange(w + res / 2, e - res / 4, res), np.arange(n - res / 2, s + res / 4, -res)


def make_dataset(
    days: pd.DatetimeIndex,
    box: tuple[float, float, float, float],
    res: float,
    with_elevation: bool,
    title: str,
) -> xr.Dataset:
    """tas and pr (and optionally the static elevation) on a lat/lon grid."""
    lon, lat = _grid(box, res)
    f = _fields(lon, lat, _doy(days), res)
    data: dict[str, Any] = {
        "tas": (("time", "lat", "lon"), f["tas"]),
        "pr": (("time", "lat", "lon"), f["pr"]),
    }
    if with_elevation:
        data["elevation"] = (("lat", "lon"), f["elevation"])
    ds = xr.Dataset(
        data,
        coords={"time": days, "lat": lat, "lon": lon},
        attrs={"title": title, "Conventions": "CF-1.10"},
    )
    return _cf_attrs(ds)


def make_global(days: pd.DatetimeIndex, res: float = 1.0) -> xr.Dataset:
    return make_dataset(
        days, (-180, -90, 180, 90), res, False, f"TerraScope demo: synthetic global {res:g}° daily"
    )


def make_regional(
    days: pd.DatetimeIndex, res: float = 0.01, box: tuple[float, float, float, float] = ALPS
) -> xr.Dataset:
    return make_dataset(
        days, box, res, True, f"TerraScope demo: synthetic Alps {res:g}° daily (downscaled)"
    )


def make_stations(
    days: pd.DatetimeIndex, n: int = 160, box: tuple[float, ...] = ALPS, seed: int = 3
) -> pd.DataFrame:
    """Synthetic weather stations in the regional box (a wide table: one column per variable).

    Each station measures the same analytic atmosphere at its own elevation (terrain plus a
    random offset: a station rarely sits at its grid cell's mean height) plus measurement noise,
    so station − grid differences look like real validation results.
    """
    rng = np.random.default_rng(seed)
    w, s, e, nn = box
    lon = rng.uniform(w + 0.2, e - 0.2, n)
    lat = rng.uniform(s + 0.2, nn - 0.2, n)
    z = elevation(lon, lat, 0.01) + rng.normal(0, 80, n)
    z = np.clip(z, 0, None)
    rows = []
    doy = _doy(days)
    for i in range(n):
        t0, moist = _weather(lon[i : i + 1], lat[i : i + 1], doy)
        tas = t0[:, 0, 0] - LAPSE * z[i] + rng.normal(0, 0.8, days.size)
        pr = (
            np.clip(moist[:, 0, 0] - 0.55, 0, None) ** 1.6
            * 9.0
            * (1 + z[i] / 1200)
            * rng.gamma(4, 0.25, days.size)
        )
        sid = f"TS{i:04d}"
        for k, d in enumerate(days):
            rows.append(
                (
                    sid,
                    f"Station {i + 1}",
                    round(lon[i], 4),
                    round(lat[i], 4),
                    round(float(z[i])),
                    d,
                    round(float(tas[k]), 2),
                    round(float(pr[k]), 2),
                )
            )
    return pd.DataFrame(
        rows,
        columns=["station_id", "station_name", "lon", "lat", "elevation_m", "date", "tas", "pr"],
    )


def make_plev_monthly(year: int, res: float = 2.0) -> xr.Dataset:
    months = pd.date_range(f"{year}-01-01", periods=12, freq="MS") + pd.Timedelta(days=14)
    lon = np.arange(-180 + res / 2, 180, res)
    lat = np.arange(-90 + res / 2, 90, res)  # south → north on purpose (both orders occur)
    plev = np.array([850.0, 500.0, 250.0])
    t0, _ = _weather(lon, lat, months.dayofyear.to_numpy().astype(np.float64))
    height = 44330.0 * (1 - (plev / 1013.25) ** 0.1903)  # standard atmosphere, m
    ta = t0[:, None] - LAPSE * height[None, :, None, None]
    ta = np.where(plev[None, :, None, None] <= 250, np.maximum(ta, 216.0), ta)
    ds = xr.Dataset(
        {"ta": (("time", "plev", "lat", "lon"), ta.astype(np.float32))},
        coords={"time": months, "plev": plev, "lat": lat, "lon": lon},
        attrs={"title": "TerraScope demo: synthetic monthly air temperature on pressure levels"},
    )
    ds["plev"].attrs.update(units="hPa", long_name="pressure level", positive="down", axis="Z")
    ds["ta"].attrs.update(standard_name="air_temperature", long_name="Air temperature", units="K")
    return _cf_attrs(ds)


def make_laea(res_m: float = 5000.0) -> xr.Dataset:
    """Elevation on the EPSG:3035 (LAEA Europe) grid, with a CF grid mapping."""
    crs = pyproj.CRS.from_epsg(3035)
    x = np.arange(2_600_000 + res_m / 2, 6_600_000, res_m)
    y = np.arange(5_400_000 - res_m / 2, 1_400_000, -res_m)
    xx, yy = np.meshgrid(x, y)
    lon, lat = pyproj.Transformer.from_crs(crs, 4326, always_xy=True).transform(xx, yy)
    z = elevation(lon, lat, res_m / 111_000)
    ds = xr.Dataset(
        {
            "elevation": (
                ("y", "x"),
                z,
                {"long_name": "Elevation", "units": "m", "grid_mapping": "crs"},
            )
        },
        coords={"x": x, "y": y},
        attrs={"title": "TerraScope demo: synthetic elevation on the LAEA Europe grid (EPSG:3035)"},
    )
    ds["x"].attrs.update(standard_name="projection_x_coordinate", units="m", axis="X")
    ds["y"].attrs.update(standard_name="projection_y_coordinate", units="m", axis="Y")
    ds["crs"] = xr.DataArray(0, attrs={**crs.to_cf(), "crs_wkt": crs.to_wkt()})
    return ds


CONFIG = """\
# TerraScope demo configuration (synthetic data written by `terrascope demo-data`).
title: TerraScope demo

datasets:
  global:
    title: Global reanalysis (synthetic, 1°)
    path: global-1deg.zarr
    attribution: Synthetic data
  alps:
    title: Alps downscaled (synthetic, 0.01° ≈ 1 km)
    path: alps-1km.zarr
    overviews: alps-1km.overviews.zarr
    attribution: Synthetic data
    styles:
      elevation: {{colormap: terrain, vmin: 0, vmax: 3500}}
  plev:
    title: Pressure levels (synthetic, 2°, monthly)
    path: global-plev-monthly.nc
  laea:
    title: Europe elevation on LAEA grid (synthetic, 5 km, EPSG:3035)
    path: europe-laea-5km.nc
    styles:
      elevation: {{colormap: terrain, vmin: 0, vmax: 3500}}

points:
  stations:
    title: Weather stations (synthetic)
    path: stations.csv
    columns: {{variable: null}}          # wide table: one column per variable
    variables:
      tas: {{units: K}}
      pr: {{units: mm day-1}}
    layers: {{tas: tas, "alps:tas": tas, pr: pr, "alps:pr": pr}}

layers:
  tas:
    title: Near-surface air temperature (1 km where available)
    description: >-
      Composite of the 0.01° Alps dataset over the 1° global dataset. Zoom into
      the Alps and the 1 km data takes over; zoom out and it is aggregated.
    sources:
      - {{dataset: alps, variable: tas}}
      - {{dataset: global, variable: tas}}
  pr:
    title: Precipitation (1 km where available)
    vmin: 0
    vmax: 30
    sources:
      - {{dataset: alps, variable: pr}}
      - {{dataset: global, variable: pr}}
"""


# int16 packing (CF scale_factor / add_offset): half the size, 0.01 precision.
PACKING = {
    "tas": {"dtype": "int16", "scale_factor": 0.01, "add_offset": 273.15, "_FillValue": -32768},
    "pr": {"dtype": "int16", "scale_factor": 0.01, "add_offset": 0.0, "_FillValue": -32768},
    "elevation": {"dtype": "int16", "scale_factor": 1.0, "add_offset": 0.0, "_FillValue": -32768},
}


def write_demo(
    directory: str | os.PathLike[str],
    days: int = 365,
    start: str = "2021-01-01",
    regional_res: float = 0.01,
    overwrite: bool = False,
    block: int = 30,
) -> Path:
    """Write the demo datasets and config into `directory`; returns the config path."""
    from terrascope.overviews import build

    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    cfg = out / "demo.yaml"
    if cfg.exists() and not overwrite:
        raise FileExistsError(f"{cfg} exists; use --overwrite to replace the demo data")
    times = pd.date_range(start, periods=days, freq="D")
    log.info("writing global 1° dataset (%d days)", days)
    _write_blocks(
        lambda t: make_global(t),
        times,
        block,
        out / "global-1deg.zarr",
        {"time": 1, "lat": 180, "lon": 360},
    )
    log.info("writing Alps %.3g° dataset (%d days)", regional_res, days)
    _write_blocks(
        lambda t: make_regional(t, regional_res),
        times,
        block,
        out / "alps-1km.zarr",
        {"time": 1, "lat": 256, "lon": 512},
    )
    log.info("writing synthetic stations")
    make_stations(times).to_csv(out / "stations.csv", index=False)
    log.info("writing pressure-level and LAEA datasets")
    make_plev_monthly(times[0].year).to_netcdf(out / "global-plev-monthly.nc", engine="h5netcdf")
    make_laea().to_netcdf(out / "europe-laea-5km.nc", engine="h5netcdf")
    log.info("building overviews of the Alps dataset")
    build(str(out / "alps-1km.zarr"), str(out / "alps-1km.overviews.zarr"), overwrite=True)
    cfg.write_text(CONFIG.format())
    return cfg


def _write_blocks(
    make, times: pd.DatetimeIndex, block: int, path: Path, chunks: dict[str, int]
) -> None:
    """Generate and write `times` in blocks (bounded memory), appending along time."""
    if path.exists():
        shutil.rmtree(path)
    for i in range(0, len(times), block):
        ds = make(times[i : i + block])
        if i == 0:
            enc = {}
            for v in ds.data_vars:
                c = tuple(min(chunks.get(str(d), ds.sizes[d]), ds.sizes[d]) for d in ds[v].dims)
                enc[v] = {"chunks": c, **PACKING.get(str(v), {})}
            enc["time"] = {"units": f"days since {times[0]:%Y-%m-%d}", "dtype": "int32"}
            ds.to_zarr(path, mode="w", zarr_format=3, consolidated=True, encoding=enc)
        else:
            ds.drop_vars([v for v in ds.data_vars if "time" not in ds[v].dims]).to_zarr(
                path, append_dim="time", zarr_format=3, consolidated=True
            )
