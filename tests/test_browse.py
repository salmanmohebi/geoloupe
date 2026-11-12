"""Browsing S3 buckets for "Add data": listing, kinds, access options, errors, and the API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from geoloupe import browse
from geoloupe.api.app import create_app
from geoloupe.browse import BrowseError, access_options, kind_of, list_location, normalise
from geoloupe.catalog import Catalog
from geoloupe.cli import config_for_paths
from geoloupe.config import config_from_dict
from geoloupe.readers import gdal_path


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """An in-memory "bucket"; records the options each listing used."""
    fsspec = pytest.importorskip("fsspec")
    fs = fsspec.filesystem("memory")
    fs.store.clear()
    fs.pseudo_dirs.clear()
    fs.pseudo_dirs.append("")
    for p in [
        "/bkt/data/era.zarr/zarr.json",
        "/bkt/data/era.zarr/t2m/zarr.json",
        "/bkt/data/scene.tif",
        "/bkt/data/obs.parquet",
        "/bkt/data/regions.geojson",
        "/bkt/data/README.txt",
        "/bkt/data/more/x.nc",
    ]:
        fs.pipe(p, b"x")
    used: list[dict[str, Any]] = []

    def fake(so: dict[str, Any]) -> Any:
        used.append(so)
        return fs

    monkeypatch.setattr(browse, "_filesystem", fake)
    return used


def test_lists_folders_first_with_what_opens(bucket: list[dict[str, Any]]) -> None:
    out = list_location("s3://bkt/data")
    assert out["url"] == "s3://bkt/data/" and out["parent"] == "s3://bkt/" and not out["zarr"]
    rows = [(e["name"], e["dir"], e["kind"]) for e in out["entries"]]
    assert rows == [
        ("era.zarr", True, "zarr"),
        ("more", True, None),
        ("obs.parquet", False, "table"),
        ("README.txt", False, None),
        ("regions.geojson", False, "vector"),
        ("scene.tif", False, "geotiff"),
    ]
    assert out["entries"][0]["url"] == "s3://bkt/data/era.zarr/"
    assert bucket[-1] == {"anon": True}  # public by default
    some = list_location("s3://bkt/data/", match="O")  # any case, folders too
    assert [e["name"] for e in some["entries"]] == ["more", "obs.parquet", "regions.geojson"]
    assert some["total"] == 3
    inner = list_location("bkt/data/era.zarr/", {"profile": "work", "anon": False})
    assert inner["zarr"] and bucket[-1] == {"profile": "work", "anon": False}


def test_errors_say_what_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    class Denied:
        def ls(self, *a: object, **k: object) -> None:
            raise PermissionError("403")

    monkeypatch.setattr(browse, "_filesystem", lambda so: Denied())
    with pytest.raises(BrowseError, match=r"not public\. Choose 'With the server's credentials'"):
        list_location("s3://private/")
    with pytest.raises(BrowseError, match="give a bucket"):
        list_location("s3://")

    class NoKeys:
        def ls(self, *a: object, **k: object) -> None:
            raise type("NoCredentialsError", (Exception,), {})("Unable to locate credentials")

    monkeypatch.setattr(browse, "_filesystem", lambda so: NoKeys())
    with pytest.raises(BrowseError, match="no AWS credentials on the server"):
        list_location("s3://private/", {"anon": False})


def test_kinds_names_and_options() -> None:
    assert kind_of("a.zarr", True) == "zarr" and kind_of("a.nc", True) is None
    assert kind_of("x.TIF", False) == "geotiff" and kind_of("refs.json", False) == "reference"
    assert kind_of("column_descriptions.json", False) is None  # plain JSON is not data
    assert kind_of("notes.md", False) is None
    assert normalise("bkt/a/") == "s3://bkt/a/"
    assert normalise("https://s3.console.aws.amazon.com/s3/buckets/bkt?region=x") == "s3://bkt"
    assert access_options() == {}
    assert access_options("minio.example.org", anon=True) == {
        "client_kwargs": {"endpoint_url": "https://minio.example.org"},
        "anon": True,
    }
    assert access_options(profile="work", anon=True) == {"profile": "work", "anon": False}


def test_public_objects_of_s3_compatible_stores_are_read_over_https() -> None:
    so = access_options("https://os.example.org/", anon=True)
    assert gdal_path("s3://bkt/a/scene.tif", so) == "https://os.example.org/bkt/a/scene.tif"
    with_keys = access_options("https://os.example.org", profile="work")
    assert gdal_path("s3://bkt/a/scene.tif", with_keys) == "s3://bkt/a/scene.tif"
    # an endpoint without an explicit choice keeps the s3:// path (keys may come from the environment)
    assert (
        gdal_path("s3://bkt/x.tif", {"client_kwargs": {"endpoint_url": "https://e"}})
        == "s3://bkt/x.tif"
    )
    assert gdal_path("s3://bkt/x.tif") == "https://bkt.s3.amazonaws.com/x.tif"


def test_config_for_paths_keeps_the_access() -> None:
    so = access_options("https://os.example.org", anon=True)
    raw = config_for_paths(
        ["s3://b/a.zarr", "s3://b/obs.csv", "s3://b/r.geojson"], storage_options=so
    )
    assert raw["datasets"]["a"]["storage_options"] == so
    assert raw["points"]["obs"]["storage_options"] == so
    assert raw["vectors"]["r"]["storage_options"] == so
    assert "storage_options" not in config_for_paths(["s3://b/a.zarr"])["datasets"]["a"]


def _client(tmp_path: Path, allow: bool) -> tuple[TestClient, Catalog]:
    raw = {"datasets": {}, "settings": {"allow_open": allow}}
    cat = Catalog(config_from_dict(raw, base_dir=tmp_path))
    return TestClient(create_app(cat, warm=False)), cat


def test_api_browse_needs_allow_open(tmp_path: Path, bucket: list[dict[str, Any]]) -> None:
    off, _ = _client(tmp_path, False)
    assert off.get("/api/browse", params={"url": "s3://bkt/data/"}).status_code == 403
    assert off.get("/api/s3/profiles").status_code == 403
    on, _ = _client(tmp_path, True)
    r = on.get(
        "/api/browse", params={"url": "s3://bkt/data/", "endpoint": "os.example.org", "anon": True}
    )
    assert r.status_code == 200 and r.json()["entries"][0]["kind"] == "zarr"
    assert bucket[-1]["client_kwargs"] == {"endpoint_url": "https://os.example.org"}
    assert on.get("/api/browse", params={"url": "s3://"}).status_code == 400
    assert isinstance(on.get("/api/s3/profiles").json()["profiles"], list)


def test_api_open_passes_the_access_to_the_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, cat = _client(tmp_path, True)
    seen: dict[str, Any] = {}

    def add(found: dict[str, Any]) -> tuple[list[str], list[str]]:
        seen.update(found)
        return ["era:t2m"], []

    monkeypatch.setattr(cat, "add_datasets", add)
    body = {"path": "s3://bkt/data/era.zarr/", "endpoint": "https://os.example.org", "anon": True}
    r = c.post("/api/datasets", json=body)
    assert r.status_code == 200 and r.json()["layers"] == ["era:t2m"]
    (cfg,) = seen.values()
    assert cfg.storage_options == {
        "client_kwargs": {"endpoint_url": "https://os.example.org"},
        "anon": True,
    }
    bad = c.post("/api/datasets", json={"path": "https://x.org/a.zarr", "profile": "work"})
    assert bad.status_code == 400 and "s3:// locations only" in bad.json()["detail"]


def test_viewer_links_and_web_pages() -> None:
    from geoloupe.browse import unwrap_viewer_link
    from geoloupe.readers import OpenError, detect_engine

    link = (
        "https://os.example.org/b/test/preview.html?store=s3://b/test/sst.zarr"
        "&endpoint=https://os.example.org/&var=sst&time=2016-02-13T09:00:00.000Z&z=6.42"
    )
    assert unwrap_viewer_link(link) == ("s3://b/test/sst.zarr", "https://os.example.org")
    assert unwrap_viewer_link("https://x.org/v.html?url=https://x.org/a.zarr") == (
        "https://x.org/a.zarr",
        None,
    )
    assert unwrap_viewer_link("https://x.org/a.zarr") == ("https://x.org/a.zarr", None)
    assert unwrap_viewer_link("https://x.org/page?id=3") == ("https://x.org/page?id=3", None)
    # a query string is not a glob, and a page is not data
    assert detect_engine("https://x.org/data.nc?token=abc") == "netcdf"
    assert detect_engine("https://x.org/scene.tif?X-Amz-Signature=1") == "geotiff"
    with pytest.raises(OpenError, match="is a web page, not data"):
        detect_engine("https://x.org/preview.html?var=sst")
    assert detect_engine("s3://b/files/*.nc") == "netcdf"  # globs of files still work
    assert detect_engine("s3://b/years.zarr/1980") == "zarr"  # a group inside a store


def test_api_opens_the_data_of_a_viewer_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, cat = _client(tmp_path, True)
    seen: dict[str, Any] = {}

    def add(found: dict[str, Any]) -> tuple[list[str], list[str]]:
        seen.update(found)
        return ["sst:sst"], []

    monkeypatch.setattr(cat, "add_datasets", add)
    link = "https://os.example.org/b/t/preview.html?store=s3://b/t/sst.zarr&endpoint=https://os.example.org/"
    assert c.post("/api/datasets", json={"path": link}).json()["layers"] == ["sst:sst"]
    (cfg,) = seen.values()
    assert cfg.path == "s3://b/t/sst.zarr"
    assert cfg.storage_options == {
        "client_kwargs": {"endpoint_url": "https://os.example.org"},
        "anon": True,
    }


def test_locations_as_pasted() -> None:
    from geoloupe.browse import clean_location, looks_like_address

    assert clean_location("  'https://x.org/a.zarr'\n") == "https://x.org/a.zarr"
    assert clean_location('"s3://b/a.zarr"') == "s3://b/a.zarr"
    assert clean_location("<HTTPS://x.org/a.zarr>") == "https://x.org/a.zarr"
    assert clean_location("data/a.nc") == "data/a.nc"
    assert looks_like_address("os.zhdk.cloud.switch.ch/bucket/a.zarr")
    assert looks_like_address("www.example.org/a.nc")
    assert not looks_like_address("data/a.nc") and not looks_like_address("a.nc")
    assert not looks_like_address("results.v2/a.nc")  # a local folder with a dot: not a host
    assert not looks_like_address("/abs/path.nc") and not looks_like_address("https://x.org/a")


def test_api_pasted_addresses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c, cat = _client(tmp_path, True)
    monkeypatch.setattr(cat, "add_datasets", lambda found: (["a:v"], []))
    quoted = "'https://os.example.org/b/t/preview.html?store=s3://b/t/a.zarr&endpoint=https://os.example.org/'"
    assert c.post("/api/datasets", json={"path": quoted}).status_code == 200
    r = c.post("/api/datasets", json={"path": "os.example.org/b/t/a.zarr"})
    assert r.status_code == 400 and "start it with https://" in r.json()["detail"]
    r = c.post("/api/datasets", json={"path": "a.zarr"})
    assert r.status_code == 403 and "is not a URL" in r.json()["detail"]


def test_datasets_without_a_map_say_what_they_hold(tmp_path: Path) -> None:
    import numpy as np
    import pandas as pd
    import xarray as xr

    from geoloupe.catalog import no_map_reason
    from geoloupe.config import DatasetConfig

    t = pd.date_range("1980-01-01", periods=4, freq="h")
    series = xr.Dataset({"t2m": ("time", np.ones(4, np.float32))}, {"time": t})
    msg = no_map_reason(series, DatasetConfig(path="x.zarr"))
    assert "a time series of one place, not a map" in msg and "t2m(time)" in msg
    # an empty top level with one group per year (like a store of yearly series)
    store = tmp_path / "years.zarr"
    xr.Dataset().to_zarr(store)
    for y in ("1980", "1981"):
        series.to_zarr(store, group=y)
    msg = no_map_reason(xr.open_zarr(store), DatasetConfig(path=str(store)))
    assert "only groups: 1980, 1981" in msg and "…/1980" in msg
