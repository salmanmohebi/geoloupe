"""Overview pyramids, configuration, and format readers."""

from __future__ import annotations

import json

import numpy as np
import pytest
import xarray as xr
import zarr

from terrascope.catalog import Catalog
from terrascope.config import ConfigError, config_from_dict, expand_env, load_config
from terrascope.grid import Axis
from terrascope.overviews import block_reduce, build, coarsen_axis, level_factors
from terrascope.readers import OpenError, detect_engine, open_dataset

from .conftest import latlon_ds

# --------------------------------------------------------------- overviews


def test_block_reduce_mean_is_exact_with_nans() -> None:
    rng = np.random.default_rng(0)
    a = rng.normal(size=(9, 10)).astype(np.float32)
    a[a > 1.2] = np.nan
    out = block_reduce(a, 4, "mean")
    assert out.shape == (3, 3)
    for i in range(3):
        for j in range(3):
            block = a[i * 4 : i * 4 + 4, j * 4 : j * 4 + 4]
            exp = np.nanmean(block) if np.isfinite(block).any() else np.nan
            np.testing.assert_allclose(out[i, j], exp, rtol=1e-6, atol=1e-6, equal_nan=True)


def test_block_reduce_min_max_nearest() -> None:
    a = np.arange(16, dtype=np.float32).reshape(4, 4)
    np.testing.assert_array_equal(block_reduce(a, 2, "min"), [[0, 2], [8, 10]])
    np.testing.assert_array_equal(block_reduce(a, 2, "max"), [[5, 7], [13, 15]])
    np.testing.assert_array_equal(block_reduce(a, 2, "nearest"), [[5, 7], [13, 15]])


def test_level_factors() -> None:
    assert level_factors(43200, 20880) == [2, 4, 8, 16, 32, 64, 128]
    assert level_factors(43200, 20880, min_factor=4)[0] == 4
    assert level_factors(300, 200) == []
    with pytest.raises(ValueError):
        level_factors(1000, 1000, min_factor=3)


def test_coarsen_axis_regular_continues() -> None:
    a = Axis(np.arange(10) + 0.5)  # 10 cells → 3 blocks of 4
    np.testing.assert_allclose(coarsen_axis(a, 4), [2.0, 6.0, 10.0])


def test_build_overviews_values_and_metadata(tmp_path) -> None:
    ds = latlon_ds((0, 40, 12, 46), 0.01, times=3)
    ds["tas"][:, :5, :7] = np.nan
    src = tmp_path / "src.zarr"
    ds.to_zarr(src, zarr_format=3)
    res = build(str(src), str(tmp_path / "ovr.zarr"), start="2021-01-02", workers=1)
    assert res["levels"] == [2, 4] and res["time_steps"] == 2
    root = zarr.open_group(str(tmp_path / "ovr.zarr"), mode="r")
    ms = root.attrs["multiscales"]
    assert [e["asset"] for e in ms["layout"]] == ["1", "2"]
    assert ms["layout"][1]["derived_from"] == "1" and ms["layout"][1]["transform"]["scale"] == [
        2.0,
        2.0,
    ]
    assert root.attrs["proj:code"] == "EPSG:4326"
    assert {c["name"] for c in root.attrs["zarr_conventions"]} == {"multiscales", "proj", "spatial"}
    lvl = xr.open_zarr(tmp_path / "ovr.zarr", group="2")
    assert list(lvl.time.dt.strftime("%Y-%m-%d").values) == ["2021-01-02", "2021-01-03"]
    exp = block_reduce(ds["tas"].isel(time=1).values, 4, "mean")
    np.testing.assert_allclose(lvl["tas"].isel(time=0).values, exp, rtol=1e-6, equal_nan=True)
    # spatial:transform is consistent with the stored coordinates (rasterio order)
    a, _, c, _, e, f = lvl.attrs["spatial:transform"]
    np.testing.assert_allclose(lvl.lon.values[:2], [c + a / 2, c + 1.5 * a])
    np.testing.assert_allclose(lvl.lat.values[0], f + e / 2)


def test_build_overviews_keeps_integer_packing(tmp_path) -> None:
    ds = latlon_ds((0, 40, 12, 46), 0.01, times=2)
    enc = {"tas": {"dtype": "uint16", "scale_factor": 0.1, "add_offset": 0.0, "_FillValue": 65535}}
    ds.to_zarr(tmp_path / "p.zarr", zarr_format=3, encoding=enc)
    build(str(tmp_path / "p.zarr"), str(tmp_path / "p.ovr.zarr"), workers=2)
    raw = zarr.open_group(str(tmp_path / "p.ovr.zarr"), mode="r")["1"]["tas"]
    assert raw.dtype == np.uint16
    lvl = xr.open_zarr(tmp_path / "p.ovr.zarr", group="1")
    src = xr.open_zarr(tmp_path / "p.zarr")
    exp = block_reduce(src["tas"].isel(time=1).values.astype(np.float32), 2, "mean")
    np.testing.assert_allclose(lvl["tas"].isel(time=1).values, exp, atol=0.051)


def test_catalog_reads_multiscale_store_as_path(tmp_path) -> None:
    """A dataset path that is itself a multiscale group (levels 0, 1, ...)."""
    ds = latlon_ds((0, 40, 4, 44), 0.05, times=1)
    ds.to_zarr(tmp_path / "ms.zarr", group="0", zarr_format=3)
    ds.coarsen(lat=2, lon=2).mean().to_zarr(
        tmp_path / "ms.zarr", group="1", mode="a", zarr_format=3
    )
    g = zarr.open_group(str(tmp_path / "ms.zarr"), mode="a")
    g.attrs["multiscales"] = {"layout": [{"asset": "0"}, {"asset": "1", "derived_from": "0"}]}
    cat = Catalog(config_from_dict({"datasets": {"m": {"path": "ms.zarr"}}}, base_dir=tmp_path))
    src = cat.layer("m:tas").sources[0]
    assert [lv.origin for lv in src.levels] == ["native", "overview"]


# ------------------------------------------------------------------ config


def test_env_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TS_ROOT", "/data")
    assert expand_env("${TS_ROOT}/x.zarr") == "/data/x.zarr"
    assert expand_env("${TS_UNSET_VAR:-/fallback}/x") == "/fallback/x"
    with pytest.raises(ConfigError, match="TS_UNSET_VAR"):
        expand_env("${TS_UNSET_VAR}/x")


def test_config_validation_errors(tmp_path) -> None:
    with pytest.raises(ConfigError, match="unknown dataset"):
        config_from_dict(
            {"datasets": {}, "layers": {"t": {"sources": [{"dataset": "x", "variable": "v"}]}}}
        )
    with pytest.raises(ConfigError, match="exactly one"):
        config_from_dict({"datasets": {"a": {}}})
    with pytest.raises(ConfigError, match="Extra inputs"):
        config_from_dict({"datasets": {"a": {"path": "x", "typo_key": 1}}})
    p = tmp_path / "c.yaml"
    p.write_text("datasets: [unclosed")
    with pytest.raises(ConfigError):
        load_config(p)


def test_relative_paths_resolve_against_config(tmp_path) -> None:
    cfg = config_from_dict({"datasets": {"a": {"path": "sub/a.zarr"}}}, base_dir=tmp_path)
    assert cfg.resolve("sub/a.zarr") == str(tmp_path / "sub" / "a.zarr")
    assert cfg.resolve("s3://bucket/a.zarr") == "s3://bucket/a.zarr"


# ----------------------------------------------------------------- readers


def test_detect_engine(tmp_path) -> None:
    assert detect_engine("x.nc") == "netcdf"
    assert detect_engine("data/*.nc") == "netcdf"
    assert detect_engine("x.grib2") == "grib"
    assert detect_engine("x.tif") == "geotiff"
    assert detect_engine("refs.json") == "kerchunk"
    assert detect_engine("s3://b/x.zarr") == "zarr"
    with pytest.raises(OpenError):
        detect_engine("x.unknown")


def test_netcdf3_and_netcdf4_and_mfdataset(tmp_path) -> None:
    ds = latlon_ds((0, 40, 4, 44), 0.5, times=4)
    ds.to_netcdf(tmp_path / "a.nc", engine="scipy")  # NetCDF-3 classic
    ds.to_netcdf(tmp_path / "b.nc", engine="h5netcdf")
    for f in ("a.nc", "b.nc"):
        assert open_dataset(str(tmp_path / f))["tas"].shape == (4, 8, 8)
    for i in range(2):
        ds.isel(time=slice(2 * i, 2 * i + 2)).to_netcdf(tmp_path / f"part{i}.nc", engine="h5netcdf")
    multi = open_dataset(str(tmp_path / "part*.nc"))
    assert multi.sizes["time"] == 4


def test_geotiff(tmp_path) -> None:
    pytest.importorskip("rioxarray")
    lon = np.arange(5.005, 6, 0.01)
    lat = np.arange(46.995, 46, -0.01)
    da = xr.DataArray(
        np.random.default_rng(0).random((lat.size, lon.size)).astype("float32"),
        coords={"y": lat, "x": lon},
        dims=("y", "x"),
        name="dem",
    )
    da = da.rio.write_crs("EPSG:4326")
    da.rio.to_raster(tmp_path / "dem.tif")
    cat = Catalog(config_from_dict({"datasets": {"t": {"path": "dem.tif"}}}, base_dir=tmp_path))
    assert cat.datasets["t"].status == "ok", cat.datasets["t"].error
    src = next(iter(cat.datasets["t"].sources.values()))
    assert src.native.grid.crs.to_epsg() == 4326 and src.native.grid.shape == (100, 100)


def test_missing_path_is_a_clear_error(tmp_path) -> None:
    with pytest.raises(OpenError, match="not found"):
        open_dataset(str(tmp_path / "nope.zarr"))


def test_cli_info_on_dataset(tmp_path, capsys) -> None:
    from terrascope.cli import main

    latlon_ds((0, 40, 4, 44), 0.5).to_zarr(tmp_path / "x.zarr", zarr_format=3)
    assert main(["info", str(tmp_path / "x.zarr")]) == 0
    out = capsys.readouterr().out
    assert "tas" in out and "EPSG:4326" in out
    assert main(["info", "--json", str(tmp_path / "x.zarr")]) == 0
    layers = json.loads(capsys.readouterr().out)
    assert layers["x:tas"]["sources"][0]["crs"] == "EPSG:4326"


def test_overview_stores_per_period_via_glob(tmp_path) -> None:
    """Overviews built per period (e.g. one Slurm task per year) are combined with a glob."""
    from terrascope.engine import Selection, TileEngine
    from terrascope.tiling import Tile

    ds = latlon_ds((0, 40, 20, 50), 0.01, times=4)
    ds.to_zarr(tmp_path / "d.zarr", zarr_format=3)
    (tmp_path / "ovr").mkdir()
    build(str(tmp_path / "d.zarr"), str(tmp_path / "ovr" / "a.zarr"), end="2021-01-02")
    build(str(tmp_path / "d.zarr"), str(tmp_path / "ovr" / "b.zarr"), start="2021-01-03")
    cfg = config_from_dict(
        {
            "datasets": {"d": {"path": "d.zarr", "overviews": "ovr/*.zarr"}},
            "settings": {"max_read_cells": 200_000},
        },
        base_dir=tmp_path,
    )
    cat = Catalog(cfg)
    src = cat.layer("d:tas").sources[0]
    assert len(src.levels) == 1 + 2 * 2  # native + two levels from each store
    eng = TileEngine(cat)
    for t in ("2021-01-01", "2021-01-04"):  # one time from each store
        res = eng.render(cat.layer("d:tas"), Tile(3, 4, 2), Selection.make(t))
        assert not res.empty and res.contributions[0].level_index > 0
