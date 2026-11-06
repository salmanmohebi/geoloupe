"""HTTP API end to end (FastAPI TestClient)."""

from __future__ import annotations

import gzip
import io

import numpy as np
import pytest
import xarray as xr
from fastapi.testclient import TestClient

from terrascope.api.app import create_app, parse_dims
from terrascope.catalog import Catalog
from terrascope.engine import SelectionError, decode_data_tile


@pytest.fixture
def client(composite_catalog: Catalog) -> TestClient:
    return TestClient(create_app(composite_catalog))


def test_catalog_lists_layers_and_datasets(client: TestClient) -> None:
    r = client.get("/api/catalog")
    assert r.status_code == 200
    body = r.json()
    ids = {lyr["id"] for lyr in body["layers"]}
    assert {"tas", "glob:tas", "reg:tas"} <= ids
    assert all(d["status"] == "ok" for d in body["datasets"])


def test_layer_metadata(client: TestClient) -> None:
    m = client.get("/api/layers/tas").json()
    assert m["kind"] == "composite"
    assert m["times"][0] == "2021-01-01" and len(m["times"]) == 4
    assert [s["dataset"] for s in m["sources"]] == ["reg", "glob"]
    assert m["sources"][0]["native_zoom"] > m["sources"][1]["native_zoom"]
    assert m["maxzoom"] == 5  # 0.1° ≈ 11 km: native at tile zoom 3.8, plus one


def test_data_tile_roundtrip(client: TestClient, composite_catalog: Catalog) -> None:
    r = client.get("/api/layers/tas/tiles/6/33/22.bin", params={"time": "2021-01-02"})
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"  # decoded transparently by httpx
    tile = decode_data_tile(r.content, 256)
    assert np.isfinite(tile).all()
    assert r.headers["x-terrascope-sources"].split(",")[0].startswith("0:0:")
    from terrascope.engine import Selection, TileEngine
    from terrascope.tiling import Tile

    direct = TileEngine(composite_catalog).render(
        composite_catalog.layer("tas"), Tile(6, 33, 22), Selection.make("2021-01-02")
    )
    np.testing.assert_array_equal(tile, direct.data)
    from terrascope.engine import decode_source_map

    smap = decode_source_map(r.content, 256)
    assert smap is not None
    np.testing.assert_array_equal(smap, direct.source_map)
    assert set(np.unique(smap)) == {0, 1}  # regional data plus the global fill of the hole


def test_data_tile_without_gzip(client: TestClient) -> None:
    r = client.get(
        "/api/layers/tas/tiles/6/33/22.bin",
        params={"time": "2021-01-02"},
        headers={"Accept-Encoding": "identity"},
    )
    assert "content-encoding" not in r.headers and len(r.content) == 256 * 256 * 5


def test_empty_tile_is_204(client: TestClient) -> None:
    r = client.get("/api/layers/reg:tas/tiles/5/5/12.bin", params={"time": "2021-01-01"})
    assert r.status_code == 204


def test_png_tile(client: TestClient) -> None:
    r = client.get(
        "/api/layers/tas/tiles/3/4/2.png", params={"colormap": "viridis", "vmin": 250, "vmax": 310}
    )
    assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    bad = client.get(
        "/api/layers/tas/tiles/3/4/2.png", params={"colormap": "nope", "vmin": 0, "vmax": 1}
    )
    assert bad.status_code == 400


def test_tilejson(client: TestClient) -> None:
    tj = client.get("/api/layers/tas/tilejson.json", params={"colormap": "magma"}).json()
    assert tj["tilejson"] == "3.0.0"
    assert tj["tiles"][0].endswith("/api/layers/tas/tiles/{z}/{x}/{y}.png?colormap=magma")


def test_point_returns_native_values(client: TestClient, regional_ds: xr.Dataset) -> None:
    lon, lat = float(regional_ds.lon[50]), float(regional_ds.lat[40])
    r = client.get(
        "/api/layers/tas/point", params={"lon": lon, "lat": lat, "time": "2021-01-03"}
    ).json()
    expected = float(regional_ds["tas"].isel(time=2, lat=40, lon=50))
    assert r["value"] == pytest.approx(expected, rel=1e-6)
    assert r["source"] == 0
    assert r["sources"][1]["status"] == "ok"  # the global value is reported too


def test_point_in_hole_falls_back_to_coarse(client: TestClient, regional_ds: xr.Dataset) -> None:
    lon, lat = float(regional_ds.lon[15]), float(regional_ds.lat[15])
    r = client.get(
        "/api/layers/tas/point", params={"lon": lon, "lat": lat, "time": "2021-01-01"}
    ).json()
    assert r["sources"][0]["value"] is None and r["source"] == 1


def test_timeseries(client: TestClient, regional_ds: xr.Dataset) -> None:
    lon, lat = float(regional_ds.lon[3]), float(regional_ds.lat[4])
    r = client.get(
        "/api/layers/tas/timeseries", params={"lon": lon, "lat": lat, "start": "2021-01-02"}
    ).json()
    s0 = r["series"][0]
    assert s0["times"] == ["2021-01-02", "2021-01-03", "2021-01-04"]
    np.testing.assert_allclose(
        s0["values"], regional_ds["tas"].isel(lat=4, lon=3).values[1:], rtol=1e-6
    )


def test_stats(client: TestClient) -> None:
    st = client.get("/api/layers/glob:tas/stats", params={"time": "2021-01-01"}).json()
    assert st["count"] > 1000 and st["p2"] < st["median"] < st["p98"]
    assert len(st["histogram"]["counts"]) == 64


def test_export_netcdf(client: TestClient, regional_ds: xr.Dataset) -> None:
    r = client.get(
        "/api/sources/reg:tas/export.nc", params={"bbox": "5,45,6,46", "time": "2021-01-02"}
    )
    assert r.status_code == 200
    ds = xr.open_dataset(io.BytesIO(r.content), engine="h5netcdf")
    assert ds["tas"].shape == (1, 10, 10)
    ref = regional_ds["tas"].sel(time="2021-01-02", lon=slice(5, 6), lat=slice(46, 45))
    np.testing.assert_allclose(ds["tas"].values[0], ref.values, rtol=1e-6)


def test_errors(client: TestClient) -> None:
    assert client.get("/api/layers/nope").status_code == 404
    r = client.get("/api/layers/tas/tiles/3/4/2.bin", params={"time": "not-a-date"})
    assert r.status_code == 400 and "parse" in r.json()["detail"]
    assert client.get("/api/layers/tas/tiles/3/99/2.bin").status_code == 400
    assert client.get("/api/layers/tas/stats", params={"bbox": "1,2"}).status_code == 400


def test_failed_dataset_is_reported(tmp_path) -> None:
    from terrascope.config import config_from_dict

    cfg = config_from_dict(
        {"datasets": {"missing": {"path": "does-not-exist.zarr"}}}, base_dir=tmp_path
    )
    body = TestClient(create_app(Catalog(cfg))).get("/api/catalog").json()
    assert body["datasets"][0]["status"] == "error" and "not found" in body["datasets"][0]["error"]


def test_parse_dims() -> None:
    assert parse_dims("plev:500, member:3") == {"plev": "500", "member": "3"}
    with pytest.raises(SelectionError):
        parse_dims("plev")


def test_gzip_is_valid() -> None:
    from terrascope.engine import encode_data_tile

    a = np.linspace(250, 300, 256 * 256, dtype=np.float32).reshape(256, 256)
    raw = encode_data_tile(a)
    assert len(gzip.compress(raw)) < len(gzip.compress(a.tobytes()))  # shuffling helps


def test_viewer_runs_in_background(global_ds: xr.Dataset) -> None:
    """terrascope.show(): a real uvicorn server in a thread, as used in notebooks."""
    import httpx

    import terrascope

    viewer = terrascope.show({"era5": global_ds})
    try:
        assert viewer.url.startswith("http://127.0.0.1:")
        r = httpx.get(viewer.url + "api/catalog", timeout=10)
        assert [lyr["id"] for lyr in r.json()["layers"]] == ["era5:tas"]
        assert "layer=era5:tas" in viewer.link("era5:tas", "2021-01-02")
        tile = httpx.get(viewer.url + "api/layers/era5:tas/tiles/1/1/0.bin", timeout=10)
        assert tile.status_code == 200
    finally:
        viewer.stop()


def test_viewer_url_behind_jupyterhub(
    monkeypatch: pytest.MonkeyPatch, global_ds: xr.Dataset
) -> None:
    from terrascope.server import Viewer

    monkeypatch.setenv("JUPYTERHUB_SERVICE_PREFIX", "/user/alice/")
    v = Viewer(Catalog.from_xarray({"d": global_ds}), port=9999)
    assert v.url == "/user/alice/proxy/9999/"
