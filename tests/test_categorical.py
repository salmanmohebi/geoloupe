"""Categorical data: classes from CF flags, GeoTIFF palettes and config; nearest and majority (mode)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from fastapi.testclient import TestClient

from terrascope.api.app import create_app
from terrascope.catalog import Catalog
from terrascope.config import config_from_dict
from terrascope.engine import Selection, TileEngine
from terrascope.overviews import block_reduce
from terrascope.tiling import Tile


def _landcover() -> xr.Dataset:
    lon = np.arange(0.005, 2, 0.01)
    lat = np.arange(46.995, 45, -0.01)
    rng = np.random.default_rng(0)
    codes = rng.choice([10, 20, 80], size=(lat.size, lon.size)).astype(np.uint8)
    da = xr.DataArray(
        codes,
        coords={"lat": lat, "lon": lon},
        dims=("lat", "lon"),
        attrs={
            "flag_values": [10, 20, 80],
            "flag_meanings": "tree_cover shrubland water",
            "flag_colors": "#006400 #ffbb22 #0064c8",
        },
    )
    return xr.Dataset({"lc": da})


def test_cf_flags_become_classes_and_nearest_sampling() -> None:
    cat = Catalog.from_xarray({"wc": _landcover()})
    src = cat.layer("wc:lc").sources[0]
    assert src.classes == {
        10: {"name": "tree cover", "color": "#006400"},
        20: {"name": "shrubland", "color": "#ffbb22"},
        80: {"name": "water", "color": "#0064c8"},
    }
    assert src.resampling == "nearest"
    data = TileEngine(cat).render(cat.layer("wc:lc"), Tile(5, 16, 11), Selection.make(None)).data
    assert set(np.unique(data[np.isfinite(data)])) <= {
        10.0,
        20.0,
        80.0,
    }  # never an average of codes
    meta = TestClient(create_app(cat)).get("/api/layers/wc:lc").json()
    assert meta["classes"][2] == {"value": 80, "name": "water", "color": "#0064c8"}


def test_geotiff_palette_with_names_from_config(tmp_path: Path) -> None:
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    codes = np.array([[10, 50], [80, 0]], dtype=np.uint8).repeat(64, 0).repeat(64, 1)
    with rasterio.open(
        tmp_path / "wc.tif",
        "w",
        driver="GTiff",
        width=128,
        height=128,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=from_origin(7, 47, 0.001, 0.001),
        nodata=0,
    ) as dst:
        dst.write(codes, 1)
        dst.write_colormap(1, {10: (0, 100, 0, 255), 50: (250, 0, 0, 255), 80: (0, 100, 200, 255)})
    cfg = config_from_dict(
        {
            "datasets": {
                "wc": {
                    "path": "wc.tif",
                    "variable_name": "landcover",
                    "styles": {
                        "landcover": {
                            "classes": {
                                10: {"name": "Tree cover"},
                                50: {"name": "Built-up"},
                                80: {"name": "Water"},
                            }
                        }
                    },
                }
            }
        },
        base_dir=tmp_path,
    )
    cat = Catalog(cfg)
    classes = cat.layer("wc:landcover").sources[0].classes
    assert classes == {
        10: {"name": "Tree cover", "color": "#006400"},
        50: {"name": "Built-up", "color": "#fa0000"},
        80: {"name": "Water", "color": "#0064c8"},
    }


def test_mode_overviews_keep_the_majority_class() -> None:
    a = np.array(
        [
            [10, 10, 20, 20],
            [10, 80, 20, np.nan],
            [80, 80, np.nan, np.nan],
            [80, 10, np.nan, np.nan],
        ],
        dtype=np.float32,
    )
    np.testing.assert_array_equal(block_reduce(a, 2, "mode"), [[10, 20], [80, np.nan]])


def test_png_tiles_use_class_colours() -> None:
    import io

    pytest.importorskip("rasterio")
    from rasterio.io import MemoryFile

    cat = Catalog.from_xarray({"wc": _landcover()})
    r = TestClient(create_app(cat, warm=False)).get("/api/layers/wc:lc/tiles/5/16/11.png")
    assert r.status_code == 200
    with MemoryFile(io.BytesIO(r.content)) as m, m.open() as d:
        rgba = d.read()
    colours = {tuple(px) for px in rgba[:3].reshape(3, -1).T[rgba[3].ravel() > 0]}
    assert colours <= {(0, 100, 0), (255, 187, 34), (0, 100, 200)}  # only the class colours
    assert len(colours) == 3
