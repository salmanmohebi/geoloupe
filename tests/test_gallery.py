"""The public gallery (featured views) and the features it relies on."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from fastapi.testclient import TestClient

from terrascope.api.app import create_app
from terrascope.catalog import Catalog
from terrascope.cli import GALLERY, main
from terrascope.config import config_from_dict, load_config
from terrascope.points import open_points
from terrascope.timeaxis import TimeAxis, merge_labels


def _grid(lon0: float, lat0: float, n: int, step: float, times: int = 0) -> xr.Dataset:
    lon = lon0 + step * (np.arange(n) + 0.5)
    lat = lat0 + step * (np.arange(n)[::-1] + 0.5)
    rng = np.random.default_rng(1)
    if times:
        t = pd.date_range("2000-01-01", periods=times, freq="D")
        v = rng.normal(size=(times, n, n)).astype(np.float32)
        return xr.Dataset({"v": (("time", "lat", "lon"), v)}, {"time": t, "lat": lat, "lon": lon})
    v = rng.normal(size=(n, n)).astype(np.float32)
    return xr.Dataset({"v": (("lat", "lon"), v)}, {"lat": lat, "lon": lon})


def test_gallery_config_is_valid_and_views_point_at_layers() -> None:
    cfg = load_config(GALLERY)  # parsing only, no network
    assert cfg.views and cfg.description
    prefixes = set(cfg.datasets) | set(cfg.points)
    for v in cfg.views:
        for lid in filter(None, (v.layer, v.compare)):
            assert lid in cfg.layers or lid.split(":")[0] in prefixes, (v.title, lid)
    # public data only: every file is a URL anyone can read
    paths = [p for d in cfg.datasets.values() for p in (d.mosaic or [d.path])]
    paths += [
        p
        for pc in cfg.points.values()
        for p in (pc.path if isinstance(pc.path, list) else [pc.path])
    ]
    paths += [v.path for v in cfg.vectors.values()]
    assert paths and all(str(p).startswith("https://") for p in paths)


def test_cli_prints_the_gallery_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gallery", "--show-config"]) == 0
    assert "views:" in capsys.readouterr().out


def test_views_are_listed_and_missing_layers_dropped() -> None:
    cfg = config_from_dict(
        {
            "title": "Demo",
            "description": "Things to look at.",
            "datasets": {"d": {"path": "memory://d"}},
            "views": [
                {
                    "title": "Here",
                    "layer": "d:v",
                    "center": [1, 1],
                    "zoom": 5,
                    "vmin": -1,
                    "vmax": 1,
                },
                {"title": "Gone", "layer": "nothing:here"},
            ],
        }
    )
    j = TestClient(create_app(Catalog(cfg, {"d": _grid(0, 0, 8, 0.25)}), warm=False)).get(
        "/api/catalog"
    )
    body = j.json()
    assert body["description"] == "Things to look at."
    assert body["views"] == [
        {
            "id": 0,
            "title": "Here",
            "layer": "d:v",
            "center": [1, 1],
            "zoom": 5,
            "vmin": -1,
            "vmax": 1,
        }
    ]


def test_time_range_cuts_the_time_axis() -> None:
    cfg = config_from_dict(
        {"datasets": {"d": {"path": "memory://d", "time_range": ["2000-01-03", "2000-01-05"]}}}
    )
    cat = Catalog(cfg, {"d": _grid(0, 0, 4, 1, times=10)})
    assert cat.layer("d:v").times == ["2000-01-03", "2000-01-04", "2000-01-05"]
    bad = config_from_dict(
        {"datasets": {"d": {"path": "memory://d", "time_range": ["1990", "1991"]}}}
    )
    assert Catalog(bad, {"d": _grid(0, 0, 4, 1, times=10)}).datasets["d"].status == "error"


def test_composite_can_use_a_mosaic(tmp_path: Path) -> None:
    _grid(0, 0, 20, 0.05).to_netcdf(tmp_path / "a.nc")
    _grid(1, 0, 20, 0.05).to_netcdf(tmp_path / "b.nc")
    _grid(-5, -5, 20, 1.0).to_netcdf(tmp_path / "coarse.nc")
    cfg = config_from_dict(
        {
            "datasets": {
                "fine": {"mosaic": ["a.nc", "b.nc"], "variable_name": "v"},
                "coarse": {"path": "coarse.nc"},
            },
            "layers": {
                "both": {
                    "sources": [
                        {"dataset": "coarse", "variable": "v"},
                        {"dataset": "fine", "variable": "v"},
                    ]
                }
            },
        },
        base_dir=tmp_path,
    )
    ids = [s.id for s in Catalog(cfg).layer("both").sources]
    assert sorted(ids[:2]) == ["fine.0:v", "fine.1:v"] and ids[2] == "coarse:v"  # finest first


def test_wide_csv_reads_only_listed_variables(tmp_path: Path) -> None:
    pd.DataFrame(
        {
            "STATION": ["A", "A"],
            "DATE": ["2020-01-01", "2020-01-02"],
            "LATITUDE": [46.0, 46.0],
            "LONGITUDE": [7.0, 7.0],
            "TMAX": [10, 20],
            "TMAX_ATTRIBUTES": [",,E", ",,E"],
            "SNWD": [0, 5],
        }
    ).to_csv(tmp_path / "st.csv", index=False)
    cfg = config_from_dict(
        {"points": {"st": {"path": "st.csv", "variables": {"TMAX": {"scale": 0.1}}}}},
        base_dir=tmp_path,
    )
    src = open_points("st", cfg.points["st"], cfg)
    cols = set(src._load_csv().columns)  # type: ignore[attr-defined]
    assert "TMAX_ATTRIBUTES" not in cols and "SNWD" not in cols and "TMAX" in cols
    assert [v.name for v in src.variables()] == ["TMAX"]


def test_time_labels_and_shared_axes() -> None:
    hourly = TimeAxis.from_values(pd.date_range("2020-01-01", periods=3, freq="h").values)
    daily = TimeAxis.from_values(pd.date_range("1899-12-31", periods=2, freq="D").values)
    assert hourly.labels == ["2020-01-01T00:00", "2020-01-01T01:00", "2020-01-01T02:00"]
    assert daily.labels == ["1899-12-31", "1900-01-01"]
    assert merge_labels([hourly, hourly]) == hourly.labels
    other = TimeAxis.from_values(pd.date_range("2019-12-31T23:00", periods=2, freq="h").values)
    assert merge_labels([hourly, other])[:2] == ["2019-12-31T23:00", "2020-01-01T00:00"]


def test_preview_is_centred_and_wraps_in_longitude() -> None:
    import io

    pytest.importorskip("rasterio")
    from rasterio.io import MemoryFile

    # data only in a 2° box around (179°E, 0°): east of it is the date line
    ds = _grid(178, -1, 8, 0.25)
    c = TestClient(create_app(Catalog.from_xarray({"d": ds}), warm=False))
    r = c.get(
        "/api/layers/d:v/preview.png",
        params={"center": "179,0", "zoom": 6, "width": 300, "height": 120, "vmin": -1, "vmax": 1},
    )
    assert r.status_code == 200
    with MemoryFile(io.BytesIO(r.content)) as m, m.open() as d:
        alpha = d.read(4)
    assert alpha.shape == (120, 300)
    cols = np.flatnonzero(alpha.max(axis=0))
    # the box spans 178–180°E: centred on 179°, it fills the middle of the image (≈ ±45 px at z6)
    assert abs((cols.min() + cols.max()) / 2 - 150) < 3
    r = c.get(
        "/api/layers/d:v/preview.png", params={"center": "180,0", "zoom": 6, "vmin": -1, "vmax": 1}
    )
    assert r.status_code == 200  # tiles beyond the date line wrap around
    too_big = {"center": "0,0", "zoom": 10, "width": 1025, "height": 100}
    assert c.get("/api/layers/d:v/preview.png", params=too_big).status_code == 422


def test_fixed_time_source_makes_a_static_layer() -> None:
    cfg = config_from_dict(
        {
            "datasets": {"fine": {"path": "memory://fine"}, "coarse": {"path": "memory://coarse"}},
            "layers": {
                "h": {
                    "sources": [
                        {"dataset": "fine", "variable": "v"},
                        {"dataset": "coarse", "variable": "v", "time": "2000-01-02"},
                    ]
                }
            },
        }
    )
    cat = Catalog(cfg, {"fine": _grid(0, 0, 8, 0.1), "coarse": _grid(-5, -5, 10, 1, times=3)})
    meta = TestClient(create_app(cat, warm=False)).get("/api/layers/h").json()
    assert meta["times"] == [] and meta["time_step"] is None
    assert meta["sources"][1]["fixed_time"] == "2000-01-02"


def test_remote_opens_retry_network_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    from terrascope import readers

    monkeypatch.setattr(readers.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError(OSError(-3, "Temporary failure in name resolution"))
        return "ok"

    assert readers.with_retries(flaky, True, "x") == "ok" and calls["n"] == 3

    def missing() -> None:
        calls["n"] += 1
        raise readers.OpenError("not found: https://x/y.tif") from FileNotFoundError("y.tif")

    calls["n"] = 0
    with pytest.raises(readers.OpenError):
        readers.with_retries(missing, True, "x")
    assert calls["n"] == 1  # a missing file is not retried
    calls["n"] = 0
    with pytest.raises(urllib.error.URLError):
        readers.with_retries(flaky, False, "x")  # local data: no retries
    assert calls["n"] == 1
