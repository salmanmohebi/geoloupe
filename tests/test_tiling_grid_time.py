from __future__ import annotations

import cftime
import numpy as np
import pyproj
import pytest
import xarray as xr

from terrascope.grid import Axis, Grid, GridError, detect_crs, find_spatial_dims
from terrascope.tiling import MAX_LAT, Tile, TileError, lonlat_to_tile, zoom_for_resolution
from terrascope.timeaxis import TimeAxis, TimeError

# ------------------------------------------------------------------ tiling


def test_tile_bounds_world() -> None:
    w, s, e, n = Tile(0, 0, 0).bounds()
    assert (w, e) == (-180.0, 180.0)
    assert n == pytest.approx(MAX_LAT) and s == pytest.approx(-MAX_LAT)


def test_tile_children_share_edges() -> None:
    parent = Tile(3, 4, 2)
    child = Tile(4, 8, 4)
    assert child.bounds()[0] == pytest.approx(parent.bounds()[0])
    assert child.bounds()[3] == pytest.approx(parent.bounds()[3])


def test_invalid_tile() -> None:
    with pytest.raises(TileError):
        Tile(2, 4, 0)


def test_lonlat_to_tile_roundtrip() -> None:
    for lon, lat, z in [(8.5, 47.4, 10), (-70.6, -33.4, 7), (179.9, 84.0, 3)]:
        x, y = lonlat_to_tile(lon, lat, z)
        w, s, e, n = Tile(z, x, y).bounds()
        assert w <= lon < e and s <= lat <= n


def test_zoom_for_resolution() -> None:
    # 0.25° ≈ 27.8 km at the equator ≈ zoom 2.5 for 256 px tiles
    assert zoom_for_resolution(27_830) == pytest.approx(2.49, abs=0.02)


# -------------------------------------------------------------------- grid


def test_axis_positions_ascending_descending() -> None:
    asc = Axis(np.array([0.5, 1.5, 2.5]))
    desc = Axis(np.array([2.5, 1.5, 0.5]))
    q = np.array([0.0, 0.5, 1.0, 2.99])
    np.testing.assert_allclose(asc.position(q), [0.0, 0.5, 1.0, 2.99])
    np.testing.assert_allclose(desc.position(q), [3.0, 2.5, 2.0, 0.01])


def test_axis_irregular_gaussian_like() -> None:
    a = Axis(np.array([0.0, 1.0, 3.0, 7.0]))
    assert not a.regular
    # cell 2 spans [2, 5): its middle 3.5 is at position 2.5
    assert a.position(np.array([3.5]))[0] == pytest.approx(2.5)


def test_axis_rejects_non_monotonic() -> None:
    with pytest.raises(GridError):
        Axis(np.array([0.0, 2.0, 1.0]))


def test_global_longitude_wrap() -> None:
    a = Axis(np.arange(0.125, 360, 0.25), periodic=360.0)  # 0..360 convention
    assert a.covers_full_period()
    p = a.position(np.array([-0.125, 359.875]))
    np.testing.assert_allclose(p, [1439.5, 1439.5])


def test_regional_longitude_wrap_keeps_seam_away() -> None:
    a = Axis(np.arange(350.05, 370, 0.1), periodic=360.0)  # region across 0° in 0..360 style
    assert not a.covers_full_period()
    # -4.95° E is 355.05° here: the centre of cell 50
    assert a.position(np.array([-4.95]))[0] == pytest.approx(50.5)
    assert a.position(np.array([5.05]))[0] == pytest.approx(150.5)


def test_find_dims_and_crs_latlon(global_ds: xr.Dataset) -> None:
    da = global_ds["tas"]
    assert find_spatial_dims(da) == ("lon", "lat")
    assert detect_crs(global_ds, da, "lon", "lat", None).to_epsg() == 4326


def test_crs_from_cf_grid_mapping() -> None:
    crs = pyproj.CRS.from_epsg(3035)
    ds = xr.Dataset(
        {"z": (("y", "x"), np.zeros((3, 4)), {"grid_mapping": "crs"})},
        coords={"x": np.arange(4.0) * 1000, "y": np.arange(3.0)[::-1] * 1000},
    )
    ds["crs"] = xr.DataArray(0, attrs=crs.to_cf())
    g = Grid.from_dataarray(ds, ds["z"], "x", "y")
    assert g.crs.to_epsg() == 3035 and not g.geographic


def test_unknown_crs_is_an_error() -> None:
    ds = xr.Dataset(
        {"z": (("y", "x"), np.zeros((2, 2)))}, coords={"x": [0.0, 1.0], "y": [0.0, 1.0]}
    )
    with pytest.raises(GridError, match="crs"):
        Grid.from_dataarray(ds, ds["z"], "x", "y")


@pytest.mark.parametrize(
    ("res", "label"),
    [(0.25, "0.25°"), (1 / 120, "30″"), (0.01, "0.01°"), (1 / 12, "5′"), (2.5 / 60, "2.5′")],
)
def test_resolution_labels(res: float, label: str) -> None:
    lon = np.arange(-10 + res / 2, 10, res)
    lat = np.arange(10 - res / 2, -10, -res)
    ds = xr.Dataset(
        {"v": (("lat", "lon"), np.zeros((lat.size, lon.size)))}, coords={"lat": lat, "lon": lon}
    )
    g = Grid.from_dataarray(ds, ds["v"], "lon", "lat")
    assert g.resolution_label().startswith(label)


# -------------------------------------------------------------------- time


def test_time_axis_numpy_daily() -> None:
    t = TimeAxis.from_values(
        np.array(["2021-01-01", "2021-01-02", "2021-01-03"], dtype="datetime64[ns]")
    )
    assert t.labels == ["2021-01-01", "2021-01-02", "2021-01-03"]
    assert t.index("2021-01-02") == 1
    assert t.index("2021-01-02T10:00") == 1  # within half a day
    assert t.index("2021-01-05") is None
    assert t.step_label() == "1 day"
    np.testing.assert_array_equal(t.range("2021-01-02", None), [1, 2])


def test_time_axis_subdaily_labels() -> None:
    t = TimeAxis.from_values(np.array(["2021-01-01T00", "2021-01-01T06"], dtype="datetime64[ns]"))
    assert t.labels == ["2021-01-01T00:00", "2021-01-01T06:00"]
    assert t.step_label() == "6 hours"


def test_time_axis_cftime_noleap_and_360day() -> None:
    noleap = np.array([cftime.DatetimeNoLeap(2001, 2, 28), cftime.DatetimeNoLeap(2001, 3, 1)])
    t = TimeAxis.from_values(noleap)
    assert t.calendar == "noleap" and t.index("2001-03-01") == 1
    d360 = np.array([cftime.Datetime360Day(2001, 2, d) for d in (29, 30)])
    t3 = TimeAxis.from_values(d360)
    assert t3.index("2001-02-30") == 1  # a date that only exists in this calendar
    with pytest.raises(TimeError):
        t3.index("2001-02-31")


def test_bad_time_string() -> None:
    t = TimeAxis.from_values(np.array(["2021-01-01"], dtype="datetime64[ns]"))
    with pytest.raises(TimeError):
        t.index("yesterday")


def test_default_tolerance_does_not_jump_to_neighbouring_days() -> None:
    every4 = np.array(["2021-01-01", "2021-01-05", "2021-01-09"], dtype="datetime64[ns]")
    t = TimeAxis.from_values(every4)
    assert t.index("2021-01-05") == 1
    assert t.index("2021-01-06") is None  # not silently the 5th
    monthly = np.array(["2021-01-15", "2021-02-15", "2021-03-15"], dtype="datetime64[ns]")
    assert TimeAxis.from_values(monthly).index("2021-02-03") == 1  # any day of the month
    assert TimeAxis.from_values(every4, tolerance="2D").index("2021-01-06") == 1  # explicit


def test_long_regular_axes_are_sent_compactly() -> None:
    import pandas as pd
    import xarray as xr
    from fastapi.testclient import TestClient

    from terrascope.api.app import create_app
    from terrascope.catalog import Catalog
    from terrascope.timeaxis import compact_axis

    t = pd.date_range("2000-01-01", periods=30_000, freq="h")
    ds = xr.Dataset(
        {"v": (("time", "lat", "lon"), np.zeros((t.size, 2, 2), np.float32))},
        coords={"time": t, "lat": [0.5, 1.5], "lon": [0.5, 1.5]},
    )
    meta = TestClient(create_app(Catalog.from_xarray({"d": ds}))).get("/api/layers/d:v").json()
    assert meta["times"] is None
    assert meta["time_axis"] == {
        "start": "2000-01-01T00:00",
        "end": t[-1].strftime("%Y-%m-%dT%H:%M"),
        "count": 30_000,
        "step_seconds": 3600,
        "format": "datetime",
    }
    labels = [x.strftime("%Y-%m-%d") for x in pd.date_range("2000-01-01", periods=25_000)]
    labels[100] = "2000-04-11"  # one odd step: must stay a list
    assert compact_axis(labels) is None
