"""'Add data' while the server runs: off by default, URLs and local roots only when allowed."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from fastapi.testclient import TestClient

from geoloupe.api.app import create_app
from geoloupe.catalog import Catalog
from geoloupe.config import config_from_dict


def _grid(path: Path) -> None:
    lon, lat = np.arange(5.05, 9, 0.1), np.arange(47.95, 44, -0.1)
    v = np.ones((lat.size, lon.size), np.float32)
    xr.Dataset({"h": (("lat", "lon"), v)}, {"lat": lat, "lon": lon}).to_netcdf(path)


def _client(tmp_path: Path, **settings: object) -> TestClient:
    _grid(tmp_path / "base.nc")
    raw = {"datasets": {"base": {"path": "base.nc"}}, "settings": settings}
    return TestClient(create_app(Catalog(config_from_dict(raw, base_dir=tmp_path)), warm=False))


def test_off_by_default(tmp_path: Path) -> None:
    c = _client(tmp_path)
    assert c.get("/api/catalog").json()["allow_open"] is False
    assert c.post("/api/datasets", json={"path": str(tmp_path / "base.nc")}).status_code == 403
    r = c.post("/api/datasets", json={"path": "https://example.org/x.zarr"})
    assert r.status_code == 403 and "--allow-open" in r.json()["detail"]


def test_local_files_below_the_root(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _grid(data / "more.nc")
    c = _client(tmp_path, open_roots=[str(data)])
    r = c.post("/api/datasets", json={"path": "more.nc"})  # relative to the first root
    assert r.status_code == 200 and r.json()["layers"] == ["more:h"]
    assert "more:h" in [lyr["id"] for lyr in c.get("/api/catalog").json()["layers"]]
    assert c.get("/api/layers/more:h/tiles/5/16/11.bin").status_code == 200
    again = c.post("/api/datasets", json={"path": str(data / "more.nc")}).json()
    assert again["layers"] == ["more-2:h"]  # ids stay unique
    # outside the root: refused, also through '..'
    assert c.post("/api/datasets", json={"path": str(tmp_path / "base.nc")}).status_code == 403
    assert c.post("/api/datasets", json={"path": "../base.nc"}).status_code == 403
    assert c.post("/api/datasets", json={"path": "missing.nc"}).status_code == 404
    assert c.post("/api/datasets", json={"path": "https://example.org/x"}).status_code == 403


def test_points_vectors_and_broken_files(tmp_path: Path) -> None:
    pytest.importorskip("pyogrio")
    data = tmp_path / "data"
    data.mkdir()
    pd.DataFrame(
        {"station": ["a", "b"], "lon": [6.0, 7.0], "lat": [46.0, 46.5], "date": ["2020-01-01"] * 2,
         "tmax": [3.0, 5.0]}
    ).to_csv(data / "obs.csv", index=False)  # fmt: skip
    fc = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"name": "x"},
          "geometry": {"type": "Point", "coordinates": [6.5, 46.2]}}]}  # fmt: skip
    (data / "sites.geojson").write_text(json.dumps(fc))
    (data / "broken.nc").write_text("not a netcdf file")
    c = _client(tmp_path, open_roots=[str(data)])
    assert c.post("/api/datasets", json={"path": "obs.csv"}).json()["layers"] == ["obs:tmax"]
    v = c.post("/api/datasets", json={"path": "sites.geojson", "name": "my sites"}).json()
    assert v["vectors"] == ["my-sites"]
    assert c.get("/api/vectors/my-sites.geojson").json()["features"][0]["properties"]["name"] == "x"
    before = c.get("/api/catalog").json()
    r = c.post("/api/datasets", json={"path": "broken.nc"})
    assert r.status_code == 400 and "broken" in r.json()["detail"]
    after = c.get("/api/catalog").json()
    assert [d["id"] for d in after["datasets"]] == [d["id"] for d in before["datasets"]]  # not kept


@pytest.mark.parametrize(
    ("args", "remote"),
    [
        ([], True),  # localhost: on by default
        (["--no-open"], False),
        (["--host", "0.0.0.0"], False),  # reachable by others: only when asked
        (["--host", "0.0.0.0", "--allow-open"], True),
    ],
)
def test_serve_opens_remote_data_by_default_on_localhost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], remote: bool
) -> None:
    import geoloupe.server
    from geoloupe.cli import main

    seen: dict[str, object] = {}

    def fake_serve(cat, host: str, port: int, log_level: str) -> None:
        seen["allow_open"] = cat.settings.allow_open
        seen["datasets"] = {d: e.config.storage_options for d, e in cat.datasets.items()}

    monkeypatch.setattr(geoloupe.server, "serve", fake_serve)
    path = tmp_path / "a.nc"
    _grid(path)
    assert main(["serve", str(path), *args]) == 0
    assert seen["allow_open"] is remote


def test_serve_s3_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from geoloupe import cli

    got: dict[str, object] = {}

    def catalog_from(targets: list[str], engine: str, so: dict | None = None) -> None:
        got["so"] = so
        raise SystemExit(0)  # stop before serving

    monkeypatch.setattr(cli, "_catalog_from", catalog_from)
    with pytest.raises(SystemExit):
        cli.main(["serve", "s3://bucket/x.zarr", "--endpoint", "os.example.org", "--anon"])
    assert got["so"] == {"client_kwargs": {"endpoint_url": "https://os.example.org"}, "anon": True}
