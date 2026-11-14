"""Time stacks: one GeoTIFF per time step, times from the file names."""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from geoloupe.catalog import Catalog
from geoloupe.config import config_from_dict
from geoloupe.engine import Selection, TileEngine
from geoloupe.query import QueryError, export_subset, point, timeseries
from geoloupe.readers import OpenError
from geoloupe.stack import Pattern, _list_https, find_files, stack_name
from geoloupe.tiling import Tile, lonlat_to_tile

rasterio = pytest.importorskip("rasterio")
from rasterio.enums import Resampling  # noqa: E402
from rasterio.transform import from_origin  # noqa: E402

PATTERN = "{YYYY}/tas_{DD}_{MM}_{YYYY}.tif"  # CHELSA's order: day before month


def _tif(path: Path, value: int, size: int = 512) -> None:
    """A uint16 GeoTIFF (lon/lat, 0.01°) holding `value` (×0.1 K, offset 0) with nodata in a corner
    and internal overviews 2 and 4, as the CHELSA files (packed values, scale tag)."""
    data = np.full((1, size, size), value, dtype=np.uint16)
    data[:, :8, :8] = 65535
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "width": size,
        "height": size,
        "count": 1,
        "dtype": "uint16",
        "crs": "EPSG:4326",
        "transform": from_origin(6.0, 48.0, 0.01, 0.01),
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
        "nodata": 65535,
        "compress": "deflate",
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)
        dst.scales = [0.1]
        dst.offsets = [0.0]
        dst.build_overviews([2, 4], Resampling.average)


def _days(root: Path, days: list[str]) -> None:
    """One file per day 'YYYY-MM-DD'; its value is 2800 + day of the month (280.x K)."""
    for d in days:
        t = datetime.fromisoformat(d)
        _tif(root / f"{t.year}" / f"tas_{t.day:02d}_{t.month:02d}_{t.year}.tif", 2800 + t.day)


def _cat(tmp_path: Path, settings: dict | None = None, **ds: object) -> Catalog:
    raw: dict = {"datasets": {"tas": {"path": PATTERN, **ds}}}
    if settings:
        raw["settings"] = settings
    return Catalog(config_from_dict(raw, base_dir=tmp_path))


# ------------------------------------------------------------------ patterns
def test_pattern_times_and_names() -> None:
    p = Pattern("x/{YYYY}/tas_{DD}_{MM}_{YYYY}.tif")
    assert p.step == "D"
    assert p.time_of("x/2003/tas_12_08_2003.tif") == datetime(2003, 8, 12)
    assert p.time_of("x/2004/tas_12_08_2003.tif") is None  # the year must agree
    assert p.time_of("x/2003/tas_31_04_2003.tif") is None  # no 31 April
    assert p.format(datetime(2003, 8, 2)) == "x/2003/tas_02_08_2003.tif"
    monthly = Pattern("CHELSA_tas_{MM}_{YYYY}_V.2.1.tif")
    assert monthly.step == "M"
    assert [t.month for t in monthly.times_between("1980-01-15", "1980-04-01")] == [1, 2, 3, 4]
    doy = Pattern("MOD_{YYYY}{DOY}.tif")
    assert doy.time_of("MOD_2020060.tif") == datetime(2020, 2, 29)
    assert Pattern("era_{YYYY}{MM}{DD}{hh}.tif").time_of("era_2023071506.tif") == datetime(
        2023, 7, 15, 6
    )
    with pytest.raises(OpenError, match="YYYY"):
        Pattern("tas_{MM}.tif")
    assert stack_name("https://h/b/CHELSA_tas_{DD}_{MM}_{YYYY}_V.2.1.tif") == "CHELSA_tas_V"


# ------------------------------------------------------------------ local stacks
def test_stack_layer_reads_the_file_of_each_time(tmp_path: Path) -> None:
    _days(tmp_path, ["2003-08-03", "2003-08-01", "2003-08-02"])
    _tif(tmp_path / "2003" / "tas_02_08_2004.tif", 9999)  # year folder disagrees: not matched
    _tif(tmp_path / "2003" / "readme.tif", 9999)
    cat = _cat(tmp_path)
    assert cat.datasets["tas"].status == "ok", cat.datasets["tas"].error
    layer = cat.layer("tas:tas")
    assert layer.times == ["2003-08-01", "2003-08-02", "2003-08-03"]
    src = layer.sources[0]
    assert [lv.origin for lv in src.levels] == ["native", "overview", "overview"]
    assert src.series_limit == 366
    eng = TileEngine(cat)
    tile = Tile(9, *lonlat_to_tile(8.5, 45.5, 9))
    for day in (1, 2, 3):
        res = eng.render(layer, tile, Selection.make(f"2003-08-0{day}"))
        assert np.nanmedian(res.data) == pytest.approx(280 + day / 10, abs=1e-3)
    # an overview is read when zoomed out, with the full image's scale (0.1)
    low = eng.render(layer, Tile(5, *lonlat_to_tile(8.5, 45.5, 5)), Selection.make("2003-08-02"))
    assert low.contributions[0].level_index >= 1
    assert np.nanmedian(low.data) == pytest.approx(280.2, abs=1e-3)
    # nodata stays missing
    assert np.isnan(np.asarray(src.native.da.isel(time=0, y=0, x=0).values))
    p = point(layer, 8.5, 45.5, Selection.make("2003-08-03"))
    assert p["value"] == pytest.approx(280.3, abs=1e-4)
    ts = timeseries(layer, 8.5, 45.5, Selection.make(None), None, None, 1000)
    assert ts["series"][0]["values"] == pytest.approx([280.1, 280.2, 280.3], abs=1e-4)


def test_time_range_builds_names_and_missing_files_are_empty(tmp_path: Path) -> None:
    _days(tmp_path, ["2003-08-01", "2003-08-03"])  # 2 August is missing
    cat = _cat(tmp_path, time_range=["2003-08-01", "2003-08-03"])
    layer = cat.layer("tas:tas")
    assert layer.times == ["2003-08-01", "2003-08-02", "2003-08-03"]
    ts = timeseries(layer, 8.5, 45.5, Selection.make(None), None, None, 1000)
    v = ts["series"][0]["values"]
    assert v[0] == pytest.approx(280.1, abs=1e-4) and v[1] is None
    eng = TileEngine(cat)
    res = eng.render(layer, Tile(9, *lonlat_to_tile(8.5, 45.5, 9)), Selection.make("2003-08-02"))
    assert not np.isfinite(res.data).any()


def test_listing_keeps_only_times_in_range(tmp_path: Path) -> None:
    _days(tmp_path, ["2002-12-31", "2003-01-01", "2003-01-02"])
    times, files = find_files(str(tmp_path / PATTERN), ("2003-01-01", None))
    assert [str(t)[:10] for t in times] == ["2003-01-01", "2003-01-02"]
    assert files[0].endswith("2003/tas_01_01_2003.tif")
    with pytest.raises(OpenError, match="no files match"):
        find_files(str(tmp_path / "{YYYY}/pr_{DD}_{MM}_{YYYY}.tif"))


def test_pattern_in_the_current_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for y in (2001, 2002):
        _tif(tmp_path / f"tas_{y}.tif", 2800, size=64)
    monkeypatch.chdir(tmp_path)
    times, files = find_files("tas_{YYYY}.tif")
    assert files == ["tas_2001.tif", "tas_2002.tif"]
    assert str(times[1])[:10] == "2002-01-01"


def test_series_and_exports_of_stacks_are_limited(tmp_path: Path) -> None:
    _days(tmp_path, ["2003-08-01", "2003-08-02", "2003-08-03"])
    cat = _cat(tmp_path, settings={"max_stack_steps": 2})
    layer = cat.layer("tas:tas")
    with pytest.raises(QueryError, match="one file per step"):
        timeseries(layer, 8.5, 45.5, Selection.make(None), None, None, 1000)
    ts = timeseries(layer, 8.5, 45.5, Selection.make(None), "2003-08-02", "2003-08-03", 1000)
    assert len(ts["series"][0]["values"]) == 2
    with pytest.raises(QueryError, match="one file per step"):
        export_subset(
            layer.sources[0],
            (8.0, 45.0, 9.0, 46.0),
            Selection.make(None),
            "2003-08-01",
            "2003-08-03",
            512,
        )


def test_layer_metadata_and_tiles_over_http(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from geoloupe.api.app import create_app

    _days(tmp_path, ["2003-08-01", "2003-08-02"])
    client = TestClient(create_app(_cat(tmp_path, settings={"max_stack_steps": 100}), warm=False))
    meta = client.get("/api/layers/tas:tas").json()
    assert meta["series_limit"] == 100
    assert meta["times"] == ["2003-08-01", "2003-08-02"]
    x, y = lonlat_to_tile(8.5, 45.5, 8)
    r = client.get(f"/api/layers/tas:tas/tiles/8/{x}/{y}.bin", params={"time": "2003-08-02"})
    assert r.status_code == 200


# ------------------------------------------------------------------ S3 listing over HTTPS
def _page(prefix: str, dirs: list[str], keys: list[str], token: str | None) -> bytes:
    ns = ' xmlns="http://s3.amazonaws.com/doc/2006-03-01/"' if token else ""  # with and without
    parts = [f"<ListBucketResult{ns}><Prefix>{prefix}</Prefix>"]
    parts += [f"<CommonPrefixes><Prefix>{prefix}{d}/</Prefix></CommonPrefixes>" for d in dirs]
    parts += [f"<Contents><Key>{prefix}{k}</Key></Contents>" for k in keys]
    parts.append(f"<IsTruncated>{'true' if token else 'false'}</IsTruncated>")
    if token:
        parts.append(f"<NextContinuationToken>{token}</NextContinuationToken>")
    parts.append("</ListBucketResult>")
    return "".join(parts).encode()


def test_https_listing_follows_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.parse
    import urllib.request

    seen: list[str] = []

    def fake(url: str, timeout: float = 0) -> io.BytesIO:
        seen.append(url)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        assert url.startswith("https://os.example.org/bucket?")
        assert q["prefix"] == "chelsa/daily/" and q["delimiter"] == "/"
        if "continuation-token" not in q:
            return io.BytesIO(_page(q["prefix"], ["2003"], ["a.tif"], "t1"))
        return io.BytesIO(_page(q["prefix"], ["2004"], ["b.tif"], None))

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    got = _list_https("https://os.example.org/bucket/chelsa/daily/")
    assert got == [("2003", True), ("a.tif", False), ("2004", True), ("b.tif", False)]
    assert len(seen) == 2
