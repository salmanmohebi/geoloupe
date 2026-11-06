from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from geoloupe.catalog import Catalog


def latlon_ds(
    box: tuple[float, float, float, float],
    res: float,
    times: int = 4,
    seed: int = 0,
    descending_lat: bool = True,
    lon0_360: bool = False,
    name: str = "tas",
) -> xr.Dataset:
    """Random-but-smooth field on a regular lat/lon grid with daily times."""
    w, s, e, n = box
    lon = np.arange(w + res / 2, e, res)
    lat = np.arange(s + res / 2, n, res)
    if descending_lat:
        lat = lat[::-1]
    rng = np.random.default_rng(seed)
    data = (
        280
        + 20 * np.cos(np.radians(lat))[None, :, None]
        + np.sin(np.radians(lon))[None, None, :] * 5
        + rng.normal(0, 1, (times, lat.size, lon.size))
    ).astype(np.float32)
    if lon0_360:
        lon = np.mod(lon, 360)
        order = np.argsort(lon)
        lon = lon[order]
        data = data[:, :, order]
    t = pd.date_range("2021-01-01", periods=times, freq="D")
    ds = xr.Dataset(
        {name: (("time", "lat", "lon"), data, {"units": "K", "long_name": "air temperature"})},
        coords={
            "time": t,
            "lat": ("lat", lat, {"units": "degrees_north"}),
            "lon": ("lon", lon, {"units": "degrees_east"}),
        },
    )
    return ds


@pytest.fixture
def global_ds() -> xr.Dataset:
    return latlon_ds((-180, -90, 180, 90), 2.0, seed=1)


@pytest.fixture
def regional_ds() -> xr.Dataset:
    ds = latlon_ds((4.0, 42.0, 16.0, 50.0), 0.1, seed=2)
    # a hole (e.g. a lake or missing data) that the coarse source must fill
    ds["tas"][:, 10:20, 10:20] = np.nan
    return ds


@pytest.fixture
def composite_catalog(global_ds: xr.Dataset, regional_ds: xr.Dataset) -> Catalog:
    return Catalog.from_xarray(
        {"glob": global_ds, "reg": regional_ds},
        layers={
            "tas": {
                "title": "tas",
                "sources": [
                    {"dataset": "glob", "variable": "tas"},
                    {"dataset": "reg", "variable": "tas"},
                ],
            }
        },
    )
