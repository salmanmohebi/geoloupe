"""Point observations: tables (CSV, Parquet, GeoParquet), CF timeSeries NetCDF, point layers, API."""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from fastapi.testclient import TestClient

from geoloupe.api.app import create_app
from geoloupe.catalog import Catalog
from geoloupe.config import config_from_dict
from geoloupe.points import PointsError, looks_like_points, open_points

from .conftest import latlon_ds


def _wide_csv(path: Path) -> None:
    days = pd.date_range("2021-01-01", periods=3)
    rows = [
        (sid, f"S{sid}", lon, lat, 500, d, 270.0 + k + i, 1.0 * k)
        for i, (sid, lon, lat) in enumerate(
            [("A", 5.0, 45.0), ("B", 10.0, 47.0), ("C", -70.0, -30.0)]
        )
        for k, d in enumerate(days)
    ]
    pd.DataFrame(
        rows,
        columns=["station_id", "station_name", "lon", "lat", "elevation_m", "date", "tas", "pr"],
    ).to_csv(path, index=False)


def _long_parquet(root: Path) -> None:
    """A large station archive: year=YYYY/part-0.parquet, long format, values in °C."""
    for year in (2020, 2021):
        d = root / f"year={year}"
        d.mkdir(parents=True)
        days = (
            pd.date_range(f"{year}-12-30", periods=2)
            if year == 2020
            else pd.date_range(f"{year}-01-01", periods=2)
        )
        rows = []
        for day in days:
            rows.append(("X1", "One", 47.0, 8.0, 400, day, "tg", 1.5, False))
            rows.append(("X1", "One", 47.0, 8.0, 400, day, "rr", 3.0, False))
            rows.append(("X2", "Dup", 47.1, 8.1, 410, day, "tg", 9.0, True))
        pd.DataFrame(
            rows,
            columns=[
                "station_id",
                "station_name",
                "lat",
                "lon",
                "elevation_m",
                "date",
                "element",
                "value",
                "duplicate",
            ],
        ).to_parquet(d / "part-0.parquet")


def _src(tmp_path: Path, **cfg: object):
    c = config_from_dict({"datasets": {}, "points": {"p": cfg}}, base_dir=tmp_path)
    return open_points("p", c.points["p"], c)


def test_wide_csv(tmp_path: Path) -> None:
    _wide_csv(tmp_path / "st.csv")
    src = _src(tmp_path, path="st.csv")
    assert [v.name for v in src.variables()] == [
        "tas",
        "pr",
    ]  # elevation is a coordinate, not a variable
    assert src.times() == ["2021-01-01", "2021-01-02", "2021-01-03"]
    assert src.bounds() == (-70.0, -30.0, 10.0, 47.0)
    r = src.at_time("tas", "2021-01-02")
    assert r["count"] == 3 and {f["id"] for f in r["features"]} == {"A", "B", "C"}
    assert next(f for f in r["features"] if f["id"] == "B")["value"] == 272.0
    assert {
        f["id"] for f in src.at_time("tas", "2021-01-02", bbox=(0, 40, 20, 50))["features"]
    } == {"A", "B"}
    s = src.series("A", "pr", "2021-01-01", "2021-01-03")
    assert s["times"] == ["2021-01-01", "2021-01-02", "2021-01-03"] and s["values"] == [
        0.0,
        1.0,
        2.0,
    ]
    with pytest.raises(PointsError):
        src.series("nope", "pr", "2021-01-01", "2021-01-03")


def test_noaa_style_columns_are_detected(tmp_path: Path) -> None:
    """NCEI daily summaries: STATION, NAME, LATITUDE, LONGITUDE, ELEVATION, DATE, TMAX, TMAX_ATTRIBUTES."""
    pd.DataFrame(
        {
            "STATION": ["SZ1", "SZ1", "SZ2"],
            "NAME": ["BASEL", "BASEL", "SANTIS"],
            "LATITUDE": [47.55, 47.55, 47.25],
            "LONGITUDE": [7.58, 7.58, 9.35],
            "ELEVATION": [316, 316, 2502],
            "DATE": ["1978-07-14", "1978-07-15", "1978-07-15"],
            "TMAX": [281, 290, 120],
            "TMAX_ATTRIBUTES": [",,E", ",,E", ",,E"],
        }
    ).to_csv(tmp_path / "ncei.csv", index=False)
    src = _src(tmp_path, path="ncei.csv", variables={"TMAX": {"scale": 0.1, "units": "degC"}})
    assert [v.name for v in src.variables()] == ["TMAX"]
    r = src.at_time("TMAX", "1978-07-15")
    assert {f["name"]: f["value"] for f in r["features"]} == {"BASEL": 29.0, "SANTIS": 12.0}
    assert next(f for f in r["features"] if f["name"] == "SANTIS")["elevation"] == 2502
    auto = _src(tmp_path, path="ncei.csv")
    assert [v.name for v in auto.variables()] == ["TMAX"]  # *_ATTRIBUTES strings are skipped


def test_long_parquet_with_units_filters_and_partitions(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    _long_parquet(tmp_path / "stations.parquet")
    src = _src(
        tmp_path,
        path="stations.parquet",
        filters={"duplicate": False},
        variables={"tg": {"offset": 273.15, "units": "K"}},
    )
    r = src.at_time("tg", "2021-01-02")
    assert [f["id"] for f in r["features"]] == ["X1"]  # the duplicate is filtered out
    assert r["features"][0]["value"] == pytest.approx(274.65)
    s = src.series("X1", "tg", "2020-12-31", "2021-01-01")  # across two partitions
    assert s["times"] == ["2020-12-31", "2021-01-01"]
    np.testing.assert_allclose(s["values"], [274.65, 274.65])
    assert src.times() == ["2020-12-30", "2020-12-31", "2021-01-01", "2021-01-02"]
    assert {v.name for v in _src(tmp_path, path="stations.parquet").variables()} == {"tg", "rr"}


def test_geoparquet_points(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")

    def wkb(x: float, y: float) -> bytes:
        return struct.pack("<BIdd", 1, 1, x, y)

    df = pd.DataFrame(
        {
            "id": ["a", "b"],
            "geometry": [wkb(8.0, 47.0), wkb(9.0, 46.0)],
            "time": pd.to_datetime(["2021-05-01"] * 2),
            "pm25": [12.0, 30.0],
        }
    )
    df.to_parquet(tmp_path / "aq.parquet")
    src = _src(tmp_path, path="aq.parquet")
    r = src.at_time("pm25", "2021-05-01")
    assert sorted((f["lon"], f["lat"], f["value"]) for f in r["features"]) == [
        (8.0, 47.0, 12.0),
        (9.0, 46.0, 30.0),
    ]


def _dsg(path: Path) -> None:
    t = pd.date_range("2021-01-01", periods=4)
    ds = xr.Dataset(
        {
            "tas": (
                ("station", "time"),
                np.arange(8.0).reshape(2, 4) + 270,
                {"units": "K", "long_name": "air temperature"},
            ),
            "lat": ("station", [47.0, 46.0], {"standard_name": "latitude"}),
            "lon": ("station", [8.0, 9.0], {"standard_name": "longitude"}),
            "station_id": ("station", ["CH1", "CH2"], {"cf_role": "timeseries_id"}),
            "station_name": ("station", ["Bern", "Lugano"]),
        },
        coords={"time": t},
        attrs={"featureType": "timeSeries", "Conventions": "CF-1.10"},
    )
    ds.to_netcdf(path, engine="h5netcdf")


def test_cf_dsg_timeseries_netcdf(tmp_path: Path) -> None:
    _dsg(tmp_path / "obs.nc")
    assert looks_like_points(str(tmp_path / "obs.nc"))
    src = _src(tmp_path, path="obs.nc")
    v = src.variables()
    assert [x.name for x in v] == ["tas"] and v[0].units == "K"
    r = src.at_time("tas", "2021-01-03")
    assert {f["name"]: f["value"] for f in r["features"]} == {"Bern": 272.0, "Lugano": 276.0}
    s = src.series("CH2", "tas", "2021-01-02", "2021-01-04")
    assert s["values"] == [275.0, 276.0, 277.0] and s["name"] == "Lugano"


def test_not_dsg_netcdf_is_not_points(tmp_path: Path) -> None:
    latlon_ds((0, 40, 4, 44), 1.0).to_netcdf(tmp_path / "grid.nc", engine="h5netcdf")
    assert not looks_like_points(str(tmp_path / "grid.nc"))
    assert looks_like_points("x.csv") and looks_like_points("y.parquet")


def test_api_point_layers_and_overlays(tmp_path: Path) -> None:
    _wide_csv(tmp_path / "st.csv")
    ds = latlon_ds((-180, -90, 180, 90), 2.0, times=3)
    cfg = config_from_dict(
        {
            "datasets": {"g": {"path": "memory://g"}},
            "points": {
                "st": {
                    "path": "st.csv",
                    "variables": {"tas": {"units": "K"}, "pr": {"units": "mm"}},
                    "layers": {"g:tas": "tas", "nope": "tas"},
                }
            },
        },
        base_dir=tmp_path,
    )
    client = TestClient(create_app(Catalog(cfg, datasets={"g": ds})))
    cat = client.get("/api/catalog").json()
    kinds = {lyr["id"]: lyr["kind"] for lyr in cat["layers"]}
    assert (
        kinds["st:tas"] == "points" and kinds["st:pr"] == "points" and kinds["g:tas"] == "dataset"
    )
    assert cat["points"][0]["variables"] == ["tas", "pr"]
    # a point layer of its own
    meta = client.get("/api/layers/st:pr").json()
    assert meta["kind"] == "points" and meta["times"] == ["2021-01-01", "2021-01-02", "2021-01-03"]
    assert meta["units"] == "mm" and meta["points"]["layer"] == "st:pr"
    assert (
        client.get("/api/layers/st:pr/points", params={"time": "2021-01-03"}).json()["count"] == 3
    )
    st = client.get("/api/layers/st:tas/stats", params={"time": "2021-01-02"}).json()
    assert st["count"] == 3 and st["min"] == 271.0
    # the overlay of a gridded layer
    gmeta = client.get("/api/layers/g:tas").json()
    assert gmeta["points"]["layer"] == "st:tas"
    assert (
        client.get("/api/layers/g:tas/points", params={"time": "2021-01-03"}).json()["count"] == 3
    )
    s = client.get(
        "/api/points/st/series",
        params={"station": "C", "variable": "tas", "start": "2021-01-01", "end": "2021-01-03"},
    ).json()
    assert s["values"] == [272.0, 273.0, 274.0]


def test_missing_point_file_is_reported(tmp_path: Path) -> None:
    cfg = config_from_dict(
        {"datasets": {}, "points": {"st": {"path": "missing.csv"}}}, base_dir=tmp_path
    )
    cat = TestClient(create_app(Catalog(cfg))).get("/api/catalog").json()
    assert cat["points"][0]["status"] == "error" and "not found" in cat["points"][0]["error"]
