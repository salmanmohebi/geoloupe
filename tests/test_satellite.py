"""Satellite rasters: (Cloud-Optimized) GeoTIFF with internal overviews, bands, nodata, scale/offset."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from geoloupe.catalog import Catalog
from geoloupe.config import config_from_dict
from geoloupe.engine import Selection, TileEngine
from geoloupe.query import point
from geoloupe.tiling import Tile, lonlat_to_tile

rasterio = pytest.importorskip("rasterio")
from rasterio.enums import Resampling  # noqa: E402
from rasterio.transform import from_origin  # noqa: E402


def _cog(
    path: Path,
    bands: int = 1,
    descriptions: list[str] | None = None,
    scale: float = 1.0,
    offset: float = 0.0,
    nodata: int | None = 0,
) -> np.ndarray:
    """A 1024 × 1024 UTM 32N uint16 GeoTIFF (10 m) with internal overviews 2, 4, 8."""
    rng = np.random.default_rng(0)
    data = rng.integers(100, 5000, size=(bands, 1024, 1024), dtype=np.uint16)
    data[:, :16, :16] = 0  # nodata corner
    profile = {
        "driver": "GTiff",
        "width": 1024,
        "height": 1024,
        "count": bands,
        "dtype": "uint16",
        "crs": "EPSG:32632",
        "transform": from_origin(400_000, 5_100_000, 10, 10),
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
        "nodata": nodata,
        "compress": "deflate",
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)
        if descriptions:
            for i, d in enumerate(descriptions, start=1):
                dst.set_band_description(i, d)
        if scale != 1.0 or offset != 0.0:
            dst.scales = [scale] * bands
            dst.offsets = [offset] * bands
        dst.build_overviews([2, 4, 8], Resampling.average)
    return data


def _cat(tmp_path: Path, **datasets: dict) -> Catalog:
    return Catalog(config_from_dict({"datasets": datasets}, base_dir=tmp_path))


def test_cog_overviews_are_levels_and_used_by_zoom(tmp_path: Path) -> None:
    data = _cog(tmp_path / "B04.tif")
    cat = _cat(tmp_path, s2={"path": "B04.tif"})
    src = cat.layer("s2:B04").sources[0]
    assert [round(lv.grid.x.resolution) for lv in src.levels] == [10, 20, 40, 80]
    assert [lv.origin for lv in src.levels] == ["native", "overview", "overview", "overview"]
    eng = TileEngine(cat)
    w, s, e, n = cat.layer("s2:B04").bounds
    lon, lat = (w + e) / 2, (s + n) / 2
    low = eng.render(
        cat.layer("s2:B04"), Tile(10, *lonlat_to_tile(lon, lat, 10)), Selection.make(None)
    )
    high = eng.render(
        cat.layer("s2:B04"), Tile(15, *lonlat_to_tile(lon, lat, 15)), Selection.make(None)
    )
    assert low.contributions[0].level_index == 3 and high.contributions[0].level_index == 0
    # a click reads the native cell
    p = point(cat.layer("s2:B04"), lon, lat, Selection.make(None))
    cell = p["sources"][0]["cell"]
    assert p["value"] == float(data[0, cell["row"], cell["col"]])


def test_nodata_scale_offset_and_decode(tmp_path: Path) -> None:
    data = _cog(tmp_path / "tagged.tif", scale=0.0001, offset=-0.1)  # scale in the TIFF tags
    _cog(tmp_path / "plain.tif")  # scale only known from elsewhere (STAC)
    cat = _cat(
        tmp_path,
        tagged={"path": "tagged.tif"},
        plain={"path": "plain.tif", "decode": {"plain": {"scale": 0.0001, "offset": -0.1}}},
    )
    g = cat.layer("tagged:tagged").sources[0].native.grid
    x, y = g.x.centers[100], g.y.centers[200]
    import pyproj

    lon, lat = pyproj.Transformer.from_crs(32632, 4326, always_xy=True).transform(x, y)
    expected = data[0, 200, 100] * 0.0001 - 0.1
    for lid in ("tagged:tagged", "plain:plain"):
        assert point(cat.layer(lid), lon, lat, Selection.make(None))["value"] == pytest.approx(
            expected, abs=1e-6
        )
    x0, y0 = g.x.centers[3], g.y.centers[3]  # the nodata corner
    lon0, lat0 = pyproj.Transformer.from_crs(32632, 4326, always_xy=True).transform(x0, y0)
    assert point(cat.layer("tagged:tagged"), lon0, lat0, Selection.make(None))["value"] is None


def test_multiband_names_become_band_values(tmp_path: Path) -> None:
    _cog(tmp_path / "rgbn.tif", bands=4, descriptions=["red", "green", "blue", "nir"])
    cat = _cat(tmp_path, img={"path": "rgbn.tif"})
    layer = cat.layer("img:rgbn")
    assert layer.dims == {"band": ["red", "green", "blue", "nir"]}
    eng = TileEngine(cat)
    w, s, e, n = layer.bounds
    tile = Tile(14, *lonlat_to_tile((w + e) / 2, (s + n) / 2, 14))
    nir = eng.render(layer, tile, Selection.make(None, {"band": "nir"})).data
    red = eng.render(layer, tile, Selection.make(None, {"band": "red"})).data
    assert np.isfinite(nir).any() and not np.array_equal(nir, red, equal_nan=True)


def test_rgb_layers_auto_and_configured(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from geoloupe.api.app import create_app
    from geoloupe.engine import decode_data_tile

    _cog(tmp_path / "rgbn.tif", bands=4, descriptions=["red", "green", "blue", "nir"])
    _cog(tmp_path / "B08.tif")
    cfg = config_from_dict(
        {
            "datasets": {"img": {"path": "rgbn.tif"}, "nir": {"path": "B08.tif"}},
            "layers": {
                "false-colour": {
                    "rgb": True,
                    "sources": [
                        {"dataset": "nir", "variable": "B08"},
                        {"dataset": "img", "variable": "rgbn", "dims": {"band": "red"}},
                        {"dataset": "img", "variable": "rgbn", "dims": {"band": "green"}},
                    ],
                }
            },
        },
        base_dir=tmp_path,
    )
    cat = Catalog(cfg)
    auto = cat.layer("img:rgbn:rgb")  # 4 named bands → a true-colour layer
    assert auto.kind == "rgb" and [dict(s.fixed_dims)["band"] for s in auto.sources] == [
        "red",
        "green",
        "blue",
    ]
    client = TestClient(create_app(cat))
    meta = client.get("/api/layers/img:rgbn:rgb").json()
    assert (
        meta["kind"] == "rgb"
        and meta["bands"] == ["red", "green", "blue"]
        and "band" not in meta["dims"]
    )
    w, s, e, n = auto.bounds
    z = 14
    x, y = lonlat_to_tile((w + e) / 2, (s + n) / 2, z)
    r = client.get(
        f"/api/layers/img:rgbn:rgb/tiles/{z}/{x}/{y}.bin", headers={"Accept-Encoding": "identity"}
    )
    assert r.headers["x-geoloupe-bands"] == "3" and len(r.content) == 3 * 4 * 256 * 256
    planes = [
        decode_data_tile(r.content[k * 4 * 65536 : (k + 1) * 4 * 65536], 256) for k in range(3)
    ]
    direct = TileEngine(cat).render(auto, Tile(z, x, y), Selection.make(None)).bands
    for p, d in zip(planes, direct, strict=True):
        np.testing.assert_array_equal(p, d)
    assert not np.array_equal(planes[0], planes[2], equal_nan=True)
    png = client.get(
        f"/api/layers/false-colour/tiles/{z}/{x}/{y}.png", params={"vmin": 0, "vmax": 5000}
    )
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
    assert client.get("/api/layers/false-colour/stats").json()["count"] > 0
