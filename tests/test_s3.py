"""Public S3: anonymous by default, HTTPS where possible, credentials as the fallback."""

from __future__ import annotations

from typing import Any

import pytest

from geoloupe import readers
from geoloupe.points import _filesystem
from geoloupe.readers import (
    OpenError,
    gdal_path,
    public_https,
    s3_anonymous,
    s3_https,
    s3_options,
)


def test_public_by_default_credentials_when_asked() -> None:
    assert s3_anonymous(None) and s3_anonymous({"client_kwargs": {"region_name": "eu-central-1"}})
    assert not s3_anonymous({"anon": False}) and not s3_anonymous({"profile": "work"})
    assert not s3_anonymous({"key": "AK", "secret": "S"})
    assert s3_options("s3://b/k", None) == {"anon": True}
    assert s3_options("s3://b/k", {"anon": False}) == {"anon": False}
    assert s3_options("gs://b/k", None) == {}  # only S3 gets the anonymous default


def test_public_objects_are_read_over_https() -> None:
    assert s3_https("s3://noaa-ghcn-pds/csv/by_station/X.csv") == (
        "https://noaa-ghcn-pds.s3.amazonaws.com/csv/by_station/X.csv"
    )
    assert gdal_path("s3://b/scene.tif") == "https://b.s3.amazonaws.com/scene.tif"
    assert gdal_path("s3://b/scene.tif", {"anon": False}) == "s3://b/scene.tif"  # GDAL /vsis3/
    custom = {"client_kwargs": {"endpoint_url": "https://minio.example"}}
    assert gdal_path("s3://b/scene.tif", custom) == "s3://b/scene.tif"  # not AWS: keep s3://
    assert public_https("https://x/y.tif") is None and public_https("/data/y.tif") is None


def test_anonymous_first_then_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    def fake_open(location: str, engine: str, so: dict[str, Any], kw: Any, group: Any) -> str:
        calls.append(so.get("anon"))
        if so.get("anon"):
            raise OpenError("access denied")
        return "opened with credentials"

    monkeypatch.setattr(readers, "_open", fake_open)
    assert readers.open_dataset("s3://private/data.zarr", "zarr") == "opened with credentials"
    assert calls == [True, False]
    calls.clear()
    readers.open_dataset("s3://private/data.zarr", "zarr", {"anon": False})
    assert calls == [False]  # explicit: no anonymous try

    def never(location: str, engine: str, so: dict[str, Any], kw: Any, group: Any) -> str:
        raise OpenError("not found")

    monkeypatch.setattr(readers, "_open", never)
    with pytest.raises(OpenError, match="anon: false"):
        readers.open_dataset("s3://nowhere/data.zarr", "zarr")


def test_parquet_files_over_https_folders_through_s3fs() -> None:
    pytest.importorskip("s3fs")
    fs, paths = _filesystem(["s3://b/a.parquet", "s3://b/c.parquet"], {})
    assert type(fs).__name__ == "HTTPFileSystem"
    assert paths == ["https://b.s3.amazonaws.com/a.parquet", "https://b.s3.amazonaws.com/c.parquet"]
    fs, paths = _filesystem(["s3://b/by_year/"], {})  # a hive folder needs listing
    assert type(fs).__name__ == "S3FileSystem" and paths == ["b/by_year/"]
    assert _filesystem(["/local/x.parquet"], {}) == (None, ["/local/x.parquet"])
