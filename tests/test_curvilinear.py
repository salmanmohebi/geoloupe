"""Curvilinear grids (2-D longitude/latitude): detection, rendering against brute force, queries."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from fastapi.testclient import TestClient

from geoloupe.api.app import create_app
from geoloupe.catalog import Catalog
from geoloupe.engine import Selection, TileEngine
from geoloupe.query import export_subset
from geoloupe.tiling import Tile


def _rotated(ny: int = 120, nx: int = 260, lon0: float = 8.0, lat0: float = 46.0,
             step: float = 0.01, angle: float = 30.0) -> tuple[np.ndarray, np.ndarray]:  # fmt: skip
    """A regular grid rotated by `angle` degrees around (lon0, lat0): truly 2-D coordinates."""
    j, i = np.meshgrid(np.arange(nx) - nx / 2, np.arange(ny) - ny / 2)
    a = np.radians(angle)
    lon = lon0 + step * (j * np.cos(a) - i * np.sin(a)) / np.cos(np.radians(lat0))
    lat = lat0 + step * (j * np.sin(a) + i * np.cos(a))
    return lon, lat


def _dataset(lon: np.ndarray, lat: np.ndarray, times: int = 2) -> xr.Dataset:
    t = pd.date_range("2020-01-01", periods=times, freq="D")
    v = np.stack([lon + 10 * lat + k for k in range(times)]).astype(np.float32)
    return xr.Dataset(
        {"v": (("time", "y", "x"), v, {"units": "1"})},
        coords={
            "time": t,
            "lon": (("y", "x"), lon, {"standard_name": "longitude", "units": "degrees_east"}),
            "lat": (("y", "x"), lat, {"standard_name": "latitude", "units": "degrees_north"}),
        },
    )


def _brute(lon: np.ndarray, lat: np.ndarray, plon: float, plat: float) -> tuple[int, int]:
    d = np.hypot((lon - plon) * np.cos(np.radians(plat)), lat - plat)
    r, c = np.unravel_index(np.nanargmin(d), d.shape)
    return int(r), int(c)


def test_detected_with_subsampled_levels() -> None:
    lon, lat = _rotated()
    cat = Catalog.from_xarray({"d": _dataset(lon, lat)})
    src = cat.layer("d:v").sources[0]
    assert src.native.grid.curvi is not None
    assert [lv.origin for lv in src.levels] == ["native", "subsampled"]  # 260 / 2 ≥ 128
    assert src.levels[1].grid.shape == (60, 130)
    w, s, e, n = src.native.grid.bounds_lonlat
    assert w < 7 and e > 9 and s < 45.5 and n > 46.5
    assert "km" in src.native.grid.resolution_label() or " m" in src.native.grid.resolution_label()


def test_tile_pixels_are_the_nearest_cells() -> None:
    lon, lat = _rotated()
    cat = Catalog.from_xarray({"d": _dataset(lon, lat)})
    tile = Tile(9, 267, 182)  # (8° E, 46° N) at zoom 9: cells ≈ 1 km, pixels ≈ 200 m
    data = TileEngine(cat).render(cat.layer("d:v"), tile, Selection.make("2020-01-02")).data
    plon, plat = tile.lonlat_grid()
    finite = np.argwhere(np.isfinite(data))
    assert len(finite) > 1000
    rng = np.random.default_rng(0)
    for i, j in finite[rng.choice(len(finite), 40, replace=False)]:
        r, c = _brute(lon, lat, plon[i, j], plat[i, j])
        assert data[i, j] == np.float32(lon[r, c] + 10 * lat[r, c] + 1)
    # around the rotated outline the map stays empty: no stretched edge cells
    big = TileEngine(cat).render(cat.layer("d:v"), Tile(7, 66, 45), Selection.make("2020-01-01"))
    inside = np.isfinite(big.data)
    assert 0.02 < inside.mean() < 0.9


def test_wrf_style_coordinates_and_point_query() -> None:
    lon, lat = _rotated(angle=0.0)
    t = pd.date_range("2021-06-01", periods=3, freq="h")
    ds = xr.Dataset(
        {"T2": (("Time", "south_north", "west_east"), np.full((3, *lon.shape), 290.0, np.float32))},
        coords={
            "Time": t,
            "XLONG": (("Time", "south_north", "west_east"), np.stack([lon] * 3), {"units": "degree_east"}),
            "XLAT": (("Time", "south_north", "west_east"), np.stack([lat] * 3), {"units": "degree_north"}),
        },
    )  # fmt: skip
    cat = Catalog.from_xarray({"wrf": ds})
    layer = cat.layer("wrf:T2")
    assert layer.sources[0].native.grid.curvi is not None and len(layer.times) == 3
    c = TestClient(create_app(cat, warm=False))
    q = {"lon": 8.3, "lat": 46.1, "time": "2021-06-01T01:00"}
    cell = c.get("/api/layers/wrf:T2/point", params=q).json()["sources"][0]
    assert cell["status"] == "ok" and cell["value"] == 290.0
    assert abs(cell["cell"]["lon"] - 8.3) < 0.02 and abs(cell["cell"]["lat"] - 46.1) < 0.02
    out = c.get("/api/layers/wrf:T2/point", params={**q, "lon": 20.0}).json()["sources"][0]
    assert out["status"] != "ok" and out.get("value") is None


def test_grid_across_the_date_line() -> None:
    lon, lat = _rotated(lon0=180.0, lat0=60.0, angle=10.0, step=0.05)
    cat = Catalog.from_xarray({"d": _dataset(lon, lat, times=1)})
    w, _, e, _ = cat.layer("d:v").sources[0].native.grid.bounds_lonlat
    assert w < 180 < e and e - w < 40  # across the date line, but not the whole globe
    eng = TileEngine(cat)
    sel = Selection.make("2020-01-01")
    west = eng.render(cat.layer("d:v"), Tile(6, 63, 18), sel).data  # 174.4–180° E
    east = eng.render(cat.layer("d:v"), Tile(6, 0, 18), sel).data  # 180–174.4° W
    assert np.isfinite(west).sum() > 100 and np.isfinite(east).sum() > 100


def test_low_zoom_uses_the_subsampled_level_and_export_keeps_coordinates() -> None:
    lon, lat = _rotated()
    cat = Catalog.from_xarray({"d": _dataset(lon, lat)}, settings={"max_read_cells": 20_000})
    res = TileEngine(cat).render(cat.layer("d:v"), Tile(6, 33, 22), Selection.make("2020-01-01"))
    assert res.contributions and res.contributions[0].level_index == 1  # every 2nd cell
    sub = export_subset(cat.layer("d:v").sources[0], (7.9, 45.95, 8.1, 46.05),
                        Selection.make("2020-01-01"), None, None, 64)  # fmt: skip
    assert sub["v"].shape[0] == 1 and "lon" in sub.coords and sub["lon"].ndim == 2
    inside = (
        (sub["lon"] >= 7.9) & (sub["lon"] <= 8.1) & (sub["lat"] >= 45.95) & (sub["lat"] <= 46.05)
    )
    assert int(inside.sum()) > 50


def test_time_labels_round_to_the_minute() -> None:
    import cftime

    from geoloupe.timeaxis import TimeAxis

    noisy = [
        cftime.DatetimeNoLeap(1850, 1, 15, 12, 59, 59, 999000),
        cftime.DatetimeNoLeap(1850, 2, 14),
    ]
    assert TimeAxis.from_values(np.array(noisy)).labels == ["1850-01-15T13:00", "1850-02-14T00:00"]
    ns = np.array(["2000-01-01T05:59:59.9", "2000-01-02T06:00"], dtype="datetime64[ns]")
    assert TimeAxis.from_values(ns).labels == ["2000-01-01T06:00", "2000-01-02T06:00"]


def test_remote_zarr_without_extension_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error
    import urllib.request

    from geoloupe.readers import OpenError, detect_engine

    class Ok:
        status = 200

        def __enter__(self) -> Ok:
            return self

        def __exit__(self, *a: object) -> None:
            return None

    def urlopen(req: urllib.request.Request, timeout: float = 0) -> Ok:
        if req.full_url.startswith("https://zarr.test/") and req.full_url.endswith("/.zmetadata"):
            return Ok()
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert detect_engine("https://zarr.test/CMIP6/tos/gn/v20190308") == "zarr"
    with pytest.raises(OpenError, match="set 'engine'"):
        detect_engine("https://other.test/some/thing")


def test_nominal_axes_lose_to_2d_coordinates_unless_there_is_a_crs() -> None:
    lon, lat = _rotated(ny=40, nx=60)
    ds = _dataset(lon, lat, times=1).assign_coords(
        x=("x", np.linspace(7, 9, 60), {"units": "degrees", "standard_name": "projection_x_coordinate"}),
        y=("y", np.linspace(45.5, 46.5, 40), {"units": "degrees", "standard_name": "projection_y_coordinate"}),
    )  # fmt: skip
    nominal = Catalog.from_xarray({"d": ds}).layer("d:v").sources[0].native.grid
    assert nominal.curvi is not None  # CMIP6-style nominal axes: the 2-D coordinates are the truth
    ds["v"].attrs["grid_mapping"] = "crs"
    ds["crs"] = xr.DataArray(0, attrs={"grid_mapping_name": "latitude_longitude"})
    exact = Catalog.from_xarray({"d": ds}).layer("d:v").sources[0].native.grid
    assert exact.curvi is None  # an explicit CRS: the 1-D axes are exact
