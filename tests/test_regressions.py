"""Regression tests for bugs found in review (2026-11-06)."""

from __future__ import annotations

from pathlib import Path

import cftime
import numpy as np
import pandas as pd
import pyproj
import xarray as xr

from terrascope.catalog import Catalog
from terrascope.config import config_from_dict
from terrascope.engine import Selection, TileEngine
from terrascope.grid import Grid
from terrascope.overviews import build
from terrascope.query import point
from terrascope.tiling import Tile
from terrascope.timeaxis import TimeAxis

from .conftest import latlon_ds


def _rotated_ds() -> xr.Dataset:
    """A EURO-CORDEX-like rotated-pole grid (CF rotated_latitude_longitude)."""
    crs = pyproj.CRS.from_cf(
        {
            "grid_mapping_name": "rotated_latitude_longitude",
            "grid_north_pole_latitude": 39.25,
            "grid_north_pole_longitude": -162.0,
        }
    )
    rlon = np.arange(-28.0, 18.0, 0.44) + 0.22
    rlat = np.arange(-23.0, 21.0, 0.44) + 0.22
    data = np.add.outer(np.arange(rlat.size) * 1000.0, np.arange(rlon.size)).astype(np.float32)
    ds = xr.Dataset(
        {"tas": (("rlat", "rlon"), data, {"grid_mapping": "rotated_pole"})},
        coords={"rlon": rlon, "rlat": rlat},
    )
    ds["rlon"].attrs.update(standard_name="grid_longitude", units="degrees")
    ds["rlat"].attrs.update(standard_name="grid_latitude", units="degrees")
    ds["rotated_pole"] = xr.DataArray(0, attrs=crs.to_cf())
    return ds


def test_rotated_pole_grid_is_placed_in_europe() -> None:
    ds = _rotated_ds()
    cat = Catalog.from_xarray({"cordex": ds})
    g = cat.layer("cordex:tas").sources[0].native.grid
    assert not g.geographic and g.angular
    w, s, e, n = g.bounds_lonlat
    assert w < 8.5 < e and s < 47.4 < n  # Zurich is inside, not the Gulf of Guinea
    p = point(cat.layer("cordex:tas"), 8.54, 47.37, Selection.make(None))
    assert p["value"] is not None
    # the value is the cell that pyproj puts Zurich in
    rx, ry = pyproj.Transformer.from_crs(4326, g.crs, always_xy=True).transform(8.54, 47.37)
    col = int(np.floor((rx - (ds.rlon.values[0] - 0.22)) / 0.44))
    row = int(np.floor((ry - (ds.rlat.values[0] - 0.22)) / 0.44))
    assert p["value"] == float(ds["tas"].values[row, col])
    tile = TileEngine(cat).render(cat.layer("cordex:tas"), Tile(4, 8, 5), Selection.make(None))
    assert not tile.empty
    assert "km" in g.resolution_label() and "°" in g.resolution_label()


def test_overviews_kept_for_projected_and_cf_latlon_grids(tmp_path: Path) -> None:
    crs = pyproj.CRS.from_epsg(3035)
    x = np.arange(3_000_000, 5_000_000, 2_000.0) + 1_000
    y = np.arange(3_500_000, 2_500_000, -2_000.0) - 1_000
    ds = xr.Dataset(
        {"z": (("y", "x"), np.ones((y.size, x.size), np.float32), {"grid_mapping": "crs"})},
        coords={"x": x, "y": y},
    )
    ds["crs"] = xr.DataArray(0, attrs=crs.to_cf())
    ds.to_zarr(tmp_path / "p.zarr", zarr_format=3)
    build(str(tmp_path / "p.zarr"), str(tmp_path / "p.ovr.zarr"))
    ll = latlon_ds((0, 40, 20, 50), 0.01, times=1)
    ll["tas"].attrs["grid_mapping"] = "crs"
    ll["crs"] = xr.DataArray(0, attrs={"grid_mapping_name": "latitude_longitude"})
    ll.to_zarr(tmp_path / "l.zarr", zarr_format=3)
    build(str(tmp_path / "l.zarr"), str(tmp_path / "l.ovr.zarr"))
    cat = Catalog(
        config_from_dict(
            {
                "datasets": {
                    "p": {"path": "p.zarr", "overviews": "p.ovr.zarr"},
                    "l": {"path": "l.zarr", "overviews": "l.ovr.zarr"},
                }
            },
            base_dir=tmp_path,
        )
    )
    assert len(cat.layer("p:z").sources[0].levels) > 1
    assert len(cat.layer("l:tas").sources[0].levels) > 1
    # the overview levels carry a CF grid mapping for other readers too
    lvl = xr.open_zarr(tmp_path / "p.ovr.zarr", group="1")
    assert pyproj.CRS.from_wkt(lvl["spatial_ref"].attrs["crs_wkt"]).to_epsg() == 3035


def test_no_stray_column_at_the_seam_of_regional_grids() -> None:
    ds = latlon_ds((-25, 30, 45, 70), 0.5, times=1)  # Europe; the seam is at 190° E = -170°
    cat = Catalog.from_xarray({"eu": ds})
    eng = TileEngine(cat)
    for tile in (Tile(0, 0, 0, 64), Tile(1, 0, 0, 64), Tile(3, 0, 2, 64)):
        data = eng.render(cat.layer("eu:tas"), tile, Selection.make("2021-01-01")).data
        lon = tile.lon_centers()
        outside = (lon < -25.5) | (lon > 45.5)
        assert np.isnan(data[:, outside]).all(), f"data outside the grid in {tile}"


def test_polar_projection_bounds_reach_the_pole() -> None:
    crs = pyproj.CRS.from_epsg(3413)
    x = np.linspace(-3_000_000, 3_000_000, 61)
    y = np.linspace(3_000_000, -3_000_000, 61)
    ds = xr.Dataset(
        {"v": (("y", "x"), np.zeros((61, 61)), {"grid_mapping": "crs"})}, coords={"x": x, "y": y}
    )
    ds["crs"] = xr.DataArray(0, attrs=crs.to_cf())
    g = Grid.from_dataarray(ds, ds["v"], "x", "y")
    w, s, e, n = g.bounds_lonlat
    assert n == 90.0 and (w, e) == (-180.0, 180.0) and 50 < s < 55  # grid reaches ~51.6° N


def test_cftime_standard_calendar_outside_pandas_range() -> None:
    t = TimeAxis.from_values(np.array([cftime.DatetimeGregorian(2299, 1, d) for d in (1, 2, 3)]))
    assert t.index("2299-01-02") == 1
    t = TimeAxis.from_values(
        np.array([cftime.DatetimeProlepticGregorian(850, 1, d) for d in (1, 2)])
    )
    assert t.index("0850-01-02") == 1


def test_mixed_calendar_composite_skips_the_source_without_that_date() -> None:
    std = latlon_ds((-180, -90, 180, 90), 5.0, times=3)
    std = std.assign_coords(time=pd.date_range("2020-02-28", periods=3))
    noleap = std.copy().assign_coords(
        time=[
            cftime.DatetimeNoLeap(2020, 2, 27),
            cftime.DatetimeNoLeap(2020, 2, 28),
            cftime.DatetimeNoLeap(2020, 3, 1),
        ]
    )
    cat = Catalog.from_xarray(
        {"a": std, "b": noleap},
        layers={
            "c": {
                "priority": "listed",
                "sources": [
                    {"dataset": "b", "variable": "tas"},
                    {"dataset": "a", "variable": "tas"},
                ],
            }
        },
    )
    res = TileEngine(cat).render(cat.layer("c"), Tile(0, 0, 0, 32), Selection.make("2020-02-29"))
    assert [c.source_index for c in res.contributions] == [1]
    assert res.skipped == [(0, "time")]
    p = point(cat.layer("c"), 10.0, 10.0, Selection.make("2020-02-29"))
    assert p["source"] == 1


def test_monthly_values_match_their_month() -> None:
    first = TimeAxis.from_values(pd.date_range("2021-01-01", periods=12, freq="MS").values)
    assert first.labels[first.index("2021-01-20")] == "2021-01-01"
    mid = TimeAxis.from_values(
        (pd.date_range("1978-01-01", periods=12, freq="MS") + pd.Timedelta(days=15)).values
    )
    assert mid.labels[mid.index("1978-07-01")] == "1978-07-16"
    assert mid.index("1979-01-10") is None
    yearly = TimeAxis.from_values(pd.date_range("2000-01-01", periods=5, freq="YS").values)
    assert yearly.labels[yearly.index("2003-11-30")] == "2003-01-01"


def test_single_row_grid_uses_the_other_axis_size() -> None:
    lon = np.arange(0.05, 10, 0.1)
    ds = xr.Dataset(
        {"v": (("lat", "lon"), np.zeros((1, lon.size)))}, coords={"lat": [45.0], "lon": lon}
    )
    g = Grid.from_dataarray(ds, ds["v"], "lon", "lat")
    assert abs((g.y.hi - g.y.lo) - g.x.resolution) < 1e-9


def test_packed_integers_without_cf_decoding(tmp_path: Path) -> None:
    ds = latlon_ds((0, 40, 10, 50), 0.5, times=2)
    ds["tas"][0, 0, 0] = np.nan
    enc = {"tas": {"dtype": "uint16", "scale_factor": 0.1, "add_offset": 0.0, "_FillValue": 65535}}
    ds.to_zarr(tmp_path / "p.zarr", zarr_format=3, encoding=enc)
    cfg = {"datasets": {"p": {"path": "p.zarr", "open_kwargs": {"mask_and_scale": False}}}}
    cat = Catalog(config_from_dict(cfg, base_dir=tmp_path))
    layer = cat.layer("p:tas")
    lon, lat = float(ds.lon[3]), float(ds.lat[2])
    p = point(layer, lon, lat, Selection.make("2021-01-01"))
    expected = round(float(ds["tas"].values[0, 2, 3]) / 0.1) * 0.1
    assert abs(p["value"] - expected) < 1e-4  # float32
    corner = point(layer, float(ds.lon[0]), float(ds.lat[0]), Selection.make("2021-01-01"))
    assert corner["value"] is None  # the fill value is missing data, not 6553.5
