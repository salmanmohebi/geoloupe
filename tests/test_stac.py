"""STAC datasets (search and item) as mosaics; RGB layers whose channels are mosaics."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from geoloupe import stac
from geoloupe.catalog import Catalog
from geoloupe.config import config_from_dict
from geoloupe.engine import Selection, TileEngine
from geoloupe.tiling import Tile

rasterio = pytest.importorskip("rasterio")
from rasterio.transform import from_origin  # noqa: E402

COG = "image/tiff; application=geotiff; profile=cloud-optimized"


def _tif(path: Path, lon0: float, value: int, bands: int = 1) -> str:
    data = np.full((bands, 100, 100), value, np.uint16)
    with rasterio.open(path, "w", driver="GTiff", width=100, height=100, count=bands,
                       dtype="uint16", crs="EPSG:4326", transform=from_origin(lon0, 47.0, 0.01, 0.01),
                       nodata=0) as dst:  # fmt: skip
        dst.write(data)
    return str(path)


def _item(tmp: Path, iid: str, lon0: float, value: int, cloud: float, boa: bool) -> dict[str, Any]:
    props: dict[str, Any] = {"datetime": "2023-08-07T10:00:00Z", "eo:cloud_cover": cloud}
    if boa:
        props["earthsearch:boa_offset_applied"] = True
    red = {"href": _tif(tmp / f"{iid}_red.tif", lon0, value), "type": COG, "roles": ["data"],
           "raster:bands": [{"scale": 0.0001, "offset": -0.1}]}  # fmt: skip
    tci = {"href": _tif(tmp / f"{iid}_tci.tif", lon0, value, 3), "type": COG, "roles": ["visual"]}
    thumb = {"href": "https://example.org/t.jpg", "type": "image/jpeg", "roles": ["thumbnail"]}
    return {"type": "Feature", "id": iid, "properties": props,
            "assets": {"red": red, "visual": tci, "thumbnail": thumb}}  # fmt: skip


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """A fake STAC API with two pages: the cloudier scene first, to test the sorting."""
    b = _item(tmp_path, "b", 7.5, 200, cloud=5.0, boa=False)  # 7.5–8.5° E
    a = _item(tmp_path, "a", 7.0, 100, cloud=1.0, boa=True)  # 7.0–8.0° E
    calls: list[dict[str, Any]] = []

    def fetch(url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        calls.append({"url": url, "body": body})
        if url.endswith("/search"):
            nxt = {"rel": "next", "href": "https://stac.test/page2"}
            return {"features": [b], "links": [nxt]}
        if url == "https://stac.test/page2":
            return {"features": [a], "links": []}
        if url == "https://stac.test/items/a":
            return a
        raise AssertionError(url)

    monkeypatch.setattr(stac, "_fetch_json", fetch)
    return calls


def _cat(**stac_cfg: Any) -> Catalog:
    return Catalog(config_from_dict({"datasets": {"s2": {"stac": stac_cfg}}}))


Z = 7


def _pixel(lon: float, lat: float = 46.5) -> tuple[int, int, int, int]:
    """Tile x, y at zoom Z and the pixel column, row of a point (Web Mercator, 256 px)."""
    n = 2**Z
    fx = (lon + 180) / 360 * n
    fy = (1 - np.log(np.tan(np.radians(lat)) + 1 / np.cos(np.radians(lat))) / np.pi) / 2 * n
    return int(fx), int(fy), int((fx % 1) * 256), int((fy % 1) * 256)


def _values(cat: Catalog, layer_id: str, lons: tuple[float, ...]) -> list[list[float]]:
    """Rendered values at lat 46.5° for each longitude: one list per band."""
    x, y, _, row = _pixel(lons[0])
    res = TileEngine(cat).render(cat.layer(layer_id), Tile(Z, x, y), Selection.make(None))
    planes = res.bands if res.bands is not None else [res.data]
    assert all(_pixel(lon)[:2] == (x, y) for lon in lons)  # one tile holds all points
    return [[float(p[row, _pixel(lon)[2]]) for lon in lons] for p in planes]


def test_search_becomes_mosaics_best_item_first(api: list[dict[str, Any]]) -> None:
    cat = _cat(url="https://stac.test", collections=["s2"], bbox=[7, 46, 8.5, 47],
               datetime="2023-08-01/2023-08-31")  # fmt: skip
    body = api[0]["body"]
    assert body["datetime"] == "2023-08-01T00:00:00Z/2023-08-31T23:59:59Z" and body["bbox"] == [
        7,
        46,
        8.5,
        47,
    ]
    shown = sorted(lid for lid, lyr in cat.layers.items() if not lyr.hidden)
    assert shown == ["s2:red", "s2:visual", "s2:visual:rgb"]  # no thumbnail, member layers hidden
    red = cat.layer("s2:red")
    # least cloudy first; offset dropped where the provider already applied it
    assert [(s.dataset_id, s.scale, s.offset) for s in red.sources] == [
        ("s2.0", 0.0001, 0.0),
        ("s2.2", 0.0001, -0.1),
    ]
    # scene a (100 × 0.0001) where it exists, also in the overlap; scene b (200 × 0.0001 − 0.1) east
    np.testing.assert_allclose(
        _values(cat, "s2:red", (7.2, 7.75, 8.3))[0], [0.01, 0.01, -0.08], atol=1e-6
    )


def test_rgb_mosaic_composites_per_channel(api: list[dict[str, Any]]) -> None:
    cat = _cat(url="https://stac.test", limit=5)
    rgb = cat.layer("s2:visual:rgb")
    assert rgb.kind == "rgb" and rgb.channels == [0, 0, 1, 1, 2, 2]
    # every channel: scene a (100) where it exists, scene b (200) east of 8° E
    assert _values(cat, "s2:visual:rgb", (7.2, 7.75, 8.3)) == [[100, 100, 200]] * 3


def test_rgb_layer_from_three_mosaics_and_item_mode(
    api: list[dict[str, Any]], tmp_path: Path
) -> None:
    raw = {
        "datasets": {"s2": {"stac": {"url": "https://stac.test", "assets": ["red"]}}},
        "layers": {"grey": {"rgb": True, "sources": [{"dataset": "s2", "variable": "red"}] * 3}},
    }
    lyr = Catalog(config_from_dict(raw)).layer("grey")
    assert lyr.channels == [0, 0, 1, 1, 2, 2] and len(lyr.sources) == 6
    one = _cat(item="https://stac.test/items/a", assets=["red"])
    assert [s.dataset_id for s in one.layer("s2:red").sources] == ["s2.0"]


def test_empty_search_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stac, "_fetch_json", lambda url, body=None: {"features": [], "links": []})
    entry = _cat(url="https://stac.test").datasets["s2"]
    assert entry.status == "error" and "found no items" in (entry.error or "")
    with pytest.raises(ValueError, match="exactly one of 'url'"):
        config_from_dict({"datasets": {"s": {"stac": {}}}})
