"""Tile engine: resampling against brute force, compositing, levels, budgets, CRSs."""

from __future__ import annotations

import numpy as np
import pyproj
import pytest
import xarray as xr

from terrascope.catalog import Catalog
from terrascope.engine import (
    Selection,
    SelectionError,
    TileEngine,
    decode_data_tile,
    encode_data_tile,
)
from terrascope.tiling import Tile

from .conftest import latlon_ds


def brute_force_mean(ds: xr.Dataset, var: str, tile: Tile, t: int = 0) -> np.ndarray:
    """Reference: mean of the cells whose centres fall inside each pixel; else the cell under the pixel centre."""
    da = ds[var].isel(time=t)
    lon = np.asarray(ds["lon"].values, dtype=np.float64)
    lon = ((lon + 180) % 360) - 180
    lat = np.asarray(ds["lat"].values, dtype=np.float64)
    vals = np.asarray(da.values, dtype=np.float64)
    lon_e, lat_e = tile.lon_edges(), tile.lat_edges()
    dlon = np.median(np.abs(np.diff(np.sort(lon))))
    dlat = np.median(np.abs(np.diff(np.sort(lat))))
    out = np.full((tile.size, tile.size), np.nan)
    for i in range(tile.size):
        rows = np.nonzero((lat < lat_e[i]) & (lat >= lat_e[i + 1]))[0]
        if rows.size == 0:
            c = (lat_e[i] + lat_e[i + 1]) / 2
            rows = np.nonzero(np.abs(lat - c) <= dlat / 2)[0][:1]
        for j in range(tile.size):
            cols = np.nonzero((lon >= lon_e[j]) & (lon < lon_e[j + 1]))[0]
            if cols.size == 0:
                c = (lon_e[j] + lon_e[j + 1]) / 2
                cols = np.nonzero(np.abs(lon - c) <= dlon / 2)[0][:1]
            if rows.size and cols.size:
                block = vals[np.ix_(rows, cols)]
                if np.isfinite(block).any():
                    out[i, j] = np.nanmean(block)
    return out


def single(ds: xr.Dataset, **settings: object) -> tuple[TileEngine, Catalog]:
    cat = Catalog.from_xarray({"d": ds}, settings=settings or None)
    return TileEngine(cat), cat


@pytest.mark.parametrize(
    "tile", [Tile(0, 0, 0, 64), Tile(2, 1, 1, 64), Tile(4, 8, 5, 64), Tile(6, 33, 22, 64)]
)
@pytest.mark.parametrize("descending", [True, False])
def test_mean_resampling_matches_brute_force(tile: Tile, descending: bool) -> None:
    ds = latlon_ds((-180, -90, 180, 90), 1.0, times=1, descending_lat=descending)
    eng, cat = single(ds, tile_size=64)
    res = eng.render(cat.layer("d:tas"), tile, Selection.make("2021-01-01"))
    np.testing.assert_allclose(
        res.data, brute_force_mean(ds, "tas", tile), rtol=1e-5, equal_nan=True
    )


def test_longitudes_0_360_equal_minus180_180() -> None:
    a = latlon_ds((-180, -90, 180, 90), 2.0, times=1, seed=5)
    b = latlon_ds((-180, -90, 180, 90), 2.0, times=1, seed=5, lon0_360=True)
    assert float(b["lon"].min()) > 0
    for tile in [
        Tile(0, 0, 0, 64),
        Tile(3, 3, 3, 64),
        Tile(3, 4, 3, 64),
    ]:  # tiles at both sides of 0°
        ra = single(a, tile_size=64)[0].render(
            Catalog.from_xarray({"d": a}).layer("d:tas"), tile, Selection.make("2021-01-01")
        )
        rb = single(b, tile_size=64)[0].render(
            Catalog.from_xarray({"d": b}).layer("d:tas"), tile, Selection.make("2021-01-01")
        )
        np.testing.assert_allclose(ra.data, rb.data, equal_nan=True)


def test_antimeridian_tile_reads_both_ends() -> None:
    ds = latlon_ds((-180, -90, 180, 90), 1.0, times=1)
    eng, cat = single(ds, tile_size=64)
    tile = Tile(1, 0, 0, 64)  # western hemisphere, north
    res = eng.render(cat.layer("d:tas"), tile, Selection.make("2021-01-01"))
    assert np.isfinite(res.data).all()


def test_composite_fine_over_coarse(
    composite_catalog: Catalog, regional_ds: xr.Dataset, global_ds: xr.Dataset
) -> None:
    eng = TileEngine(composite_catalog)
    layer = composite_catalog.layer("tas")
    assert [s.dataset_id for s in layer.sources] == ["reg", "glob"]  # finest first
    tile = Tile(6, 33, 22)  # over the Alps box, finer than the regional grid
    res = eng.render(layer, tile, Selection.make("2021-01-02"))
    assert np.isfinite(res.data).all()
    used = {c.source_index for c in res.contributions}
    assert used == {0, 1}  # regional + global filling the hole
    reg_only = eng.sample(layer.sources[0], tile, Selection.make("2021-01-02"))[0]
    glob_only = eng.sample(layer.sources[1], tile, Selection.make("2021-01-02"))[0]
    assert reg_only is not None and glob_only is not None
    fine = np.isfinite(reg_only)
    np.testing.assert_array_equal(res.data[fine], reg_only[fine])
    np.testing.assert_array_equal(res.data[~fine], glob_only[~fine])


def test_composite_outside_region_uses_coarse(composite_catalog: Catalog) -> None:
    eng = TileEngine(composite_catalog)
    res = eng.render(
        composite_catalog.layer("tas"), Tile(5, 5, 12), Selection.make("2021-01-01")
    )  # Americas
    assert [c.source_index for c in res.contributions] == [1]


def test_listed_priority_keeps_config_order(global_ds: xr.Dataset, regional_ds: xr.Dataset) -> None:
    cat = Catalog.from_xarray(
        {"glob": global_ds, "reg": regional_ds},
        layers={
            "x": {
                "priority": "listed",
                "sources": [
                    {"dataset": "glob", "variable": "tas"},
                    {"dataset": "reg", "variable": "tas"},
                ],
            }
        },
    )
    assert [s.dataset_id for s in cat.layer("x").sources] == ["glob", "reg"]


def test_unit_transform_in_composite(global_ds: xr.Dataset) -> None:
    cat = Catalog.from_xarray(
        {"g": global_ds},
        layers={"c": {"sources": [{"dataset": "g", "variable": "tas", "offset": -273.15}]}},
    )
    eng = TileEngine(cat)
    k = eng.render(cat.layer("g:tas"), Tile(1, 1, 0), Selection.make("2021-01-01")).data
    c = eng.render(cat.layer("c"), Tile(1, 1, 0), Selection.make("2021-01-01")).data
    np.testing.assert_allclose(c, k - 273.15, rtol=1e-5)


def test_read_budget_skips_and_overview_rescues(tmp_path) -> None:
    ds = latlon_ds((0, 40, 20, 50), 0.01, times=1)  # 2000 × 1000 cells
    eng, cat = single(ds, max_read_cells=200_000)
    layer = cat.layer("d:tas")
    res = eng.render(layer, Tile(3, 4, 2), Selection.make("2021-01-01"))  # whole box in one tile
    assert res.empty and res.skipped == [(0, "cost")]
    assert eng.min_zoom(layer.sources[0]) > 3
    # with overviews the same tile is served from a coarse level
    from terrascope.overviews import build

    src = tmp_path / "d.zarr"
    ds.to_zarr(src, zarr_format=3)
    build(str(src), str(tmp_path / "d.ovr.zarr"))
    cat2 = Catalog(
        _cfg(tmp_path, {"path": "d.zarr", "overviews": "d.ovr.zarr"}, max_read_cells=200_000)
    )
    eng2 = TileEngine(cat2)
    res2 = eng2.render(cat2.layer("d:tas"), Tile(3, 4, 2), Selection.make("2021-01-01"))
    assert not res2.empty and res2.contributions[0].level_index > 0


def test_level_choice_finest_needed(tmp_path) -> None:
    from terrascope.overviews import build

    ds = latlon_ds((0, 40, 20, 50), 0.01, times=1)
    ds.to_zarr(tmp_path / "d.zarr", zarr_format=3)
    build(str(tmp_path / "d.zarr"), str(tmp_path / "d.ovr.zarr"))
    cat = Catalog(_cfg(tmp_path, {"path": "d.zarr", "overviews": "d.ovr.zarr"}))
    eng = TileEngine(cat)
    src = cat.layer("d:tas").sources[0]
    assert [round(lv.grid.x.resolution, 4) for lv in src.levels] == [0.01, 0.02, 0.04]
    sel = Selection.make("2021-01-01")
    # zoom 4: pixel ≈ 0.088° × 0.058° → 0.04°; zoom 5: 0.044° × 0.031° → 0.02°; zoom 12 → native
    assert eng.plan(src, Tile(4, 8, 5), sel.time)[0].level_index == 2
    assert eng.plan(src, Tile(5, 16, 11), sel.time)[0].level_index == 1
    assert eng.plan(src, Tile(12, 2100, 1500), sel.time)[0].level_index == 0


def test_time_selection_and_missing_time(composite_catalog: Catalog) -> None:
    eng = TileEngine(composite_catalog)
    layer = composite_catalog.layer("tas")
    res = eng.render(layer, Tile(6, 33, 22), Selection.make("2030-01-01"))
    assert res.empty and {r for _, r in res.skipped} == {"time"}
    with pytest.raises(SelectionError):
        eng.render(layer, Tile(6, 33, 22), Selection.make(None))


def test_extra_dimension_selection() -> None:
    ds = latlon_ds((-180, -90, 180, 90), 5.0, times=2)
    ds = xr.concat([ds, ds + 100], dim="plev").assign_coords(plev=[850.0, 500.0])
    ds = ds.transpose("time", "plev", "lat", "lon")
    eng, cat = single(ds)
    layer = cat.layer("d:tas")
    assert layer.dims == {"plev": [850.0, 500.0]}
    lo = eng.render(layer, Tile(1, 0, 0), Selection.make("2021-01-01", {"plev": "850"})).data
    hi = eng.render(layer, Tile(1, 0, 0), Selection.make("2021-01-01", {"plev": "500"})).data
    np.testing.assert_allclose(hi - lo, 100, rtol=1e-5)
    near = eng.render(layer, Tile(1, 0, 0), Selection.make("2021-01-01", {"plev": "600"})).data
    np.testing.assert_allclose(near, hi)


def test_projected_grid_nearest_matches_pyproj() -> None:
    crs = pyproj.CRS.from_epsg(3035)
    x = np.arange(3_000_000, 5_000_000, 10_000.0) + 5_000
    y = np.arange(3_500_000, 2_000_000, -10_000.0) - 5_000
    data = np.add.outer(np.arange(y.size) * 1000.0, np.arange(x.size)).astype(
        np.float32
    )  # unique per cell
    ds = xr.Dataset({"v": (("y", "x"), data, {"grid_mapping": "crs"})}, coords={"x": x, "y": y})
    ds["crs"] = xr.DataArray(0, attrs=crs.to_cf())
    eng, cat = single(ds, tile_size=32)
    tile = Tile(6, 34, 22, 32)  # central Europe
    out = eng.render(cat.layer("d:v"), tile, Selection.make(None)).data
    lon, lat = tile.lonlat_grid()
    gx, gy = pyproj.Transformer.from_crs(4326, crs, always_xy=True).transform(lon, lat)
    col = np.floor((gx - (x[0] - 5000)) / 10_000).astype(int)
    row = np.floor(((y[0] + 5000) - gy) / 10_000).astype(int)
    ok = (col >= 0) & (col < x.size) & (row >= 0) & (row < y.size)
    assert ok.sum() > 100
    np.testing.assert_array_equal(out[ok], data[row[ok], col[ok]])
    assert np.isnan(out[~ok]).all()


def test_nearest_resampling_for_categorical() -> None:
    lon = np.arange(-179.5, 180, 1.0)
    lat = np.arange(89.5, -90, -1.0)
    classes = (np.add.outer(np.arange(lat.size), np.arange(lon.size)) % 7).astype(np.float32)
    ds = xr.Dataset({"landcover": (("lat", "lon"), classes)}, coords={"lat": lat, "lon": lon})
    eng, cat = single(ds)
    src = cat.layer("d:landcover").sources[0]
    assert src.resampling == "nearest"
    out = eng.render(cat.layer("d:landcover"), Tile(0, 0, 0), Selection.make(None)).data
    assert set(np.unique(out[np.isfinite(out)])) <= set(range(7))


def test_data_tile_roundtrip() -> None:
    a = np.random.default_rng(0).normal(size=(256, 256)).astype(np.float32)
    a[0, :10] = np.nan
    np.testing.assert_array_equal(decode_data_tile(encode_data_tile(a), 256), a)


def test_tile_cache_and_headers(composite_catalog: Catalog) -> None:
    eng = TileEngine(composite_catalog)
    layer = composite_catalog.layer("tas")
    body, headers = eng.tile_bytes(layer, Tile(6, 33, 22), Selection.make("2021-01-01"))
    assert body is not None and headers["X-TerraScope-Sources"].startswith("0:0:")
    body2, _ = eng.tile_bytes(layer, Tile(6, 33, 22), Selection.make("2021-01-01"))
    assert body2 is body  # served from the cache
    empty, _ = eng.tile_bytes(
        composite_catalog.layer("reg:tas"), Tile(5, 5, 12), Selection.make("2021-01-01")
    )
    assert empty is None


def _cfg(tmp_path, dataset: dict, **settings: object):
    from terrascope.config import config_from_dict

    return config_from_dict({"datasets": {"d": dataset}, "settings": settings}, base_dir=tmp_path)
