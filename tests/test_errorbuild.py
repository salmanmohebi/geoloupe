"""Precomputed error maps (geoloupe errors build) and their use by the server."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from fastapi.testclient import TestClient

from geoloupe.api.app import create_app
from geoloupe.catalog import Catalog
from geoloupe.cli import main
from geoloupe.config import config_from_dict
from geoloupe.engine import decode_data_tile
from geoloupe.errorbuild import ATTR, build_errors, error_key, find_precomputed
from geoloupe.errors import resolve
from geoloupe.tiling import lonlat_to_tile

from .conftest import latlon_ds

BOX = (4.0, 42.0, 16.0, 50.0)


@pytest.fixture
def stores(tmp_path: Path) -> dict[str, str]:
    """T on a 0.1° grid (K), P = T ± 1 in a checkerboard of cells, in °C; both as Zarr."""
    t = latlon_ds(BOX, 0.1, seed=3)
    i, j = np.meshgrid(np.arange(t.sizes["lat"]), np.arange(t.sizes["lon"]), indexing="ij")
    p = t.copy(deep=True)
    p["tas"] = t["tas"] + np.where((i + j) % 2 == 0, 1.0, -1.0).astype(np.float32) - 273.15
    p["tas"].attrs = {"units": "degC"}
    out = {"model": str(tmp_path / "model.zarr"), "obs": str(tmp_path / "obs.zarr")}
    p.to_zarr(out["model"], mode="w")
    t.to_zarr(out["obs"], mode="w")
    return out


def catalog_of(stores: dict[str, str], tmp_path: Path, extra: dict | None = None) -> Catalog:
    raw = {"datasets": {k: {"path": v} for k, v in stores.items()}}
    raw["datasets"].update(extra or {})
    return Catalog(config_from_dict(raw, base_dir=tmp_path))


def test_build_writes_native_errors(stores: dict[str, str], tmp_path: Path) -> None:
    cat = catalog_of(stores, tmp_path)
    out = str(tmp_path / "err.zarr")
    res = build_errors(cat, "model:tas", "obs:tas", out, metric="abs", start="2021-01-02")
    assert res["time_steps"] == 3 and res["shape"] == [80, 120]
    ds = xr.open_zarr(out)
    assert ds["error"].shape == (3, 80, 120)
    np.testing.assert_allclose(ds["error"].values, 1.0, atol=1e-4)  # |P − T| = 1 K everywhere
    assert ds["error"].attrs["units"] == "K"
    key = json.loads(ds["error"].attrs[ATTR])
    assert key == error_key("model:tas", "obs:tas", resolve("abs"))
    assert str(ds["time"].values[0])[:10] == "2021-01-02"
    with pytest.raises(FileExistsError):
        build_errors(cat, "model:tas", "obs:tas", out, metric="abs")


def test_build_events_as_classes(stores: dict[str, str], tmp_path: Path) -> None:
    cat = catalog_of(stores, tmp_path)
    out = str(tmp_path / "ev.zarr")
    build_errors(
        cat, "model:tas", "obs:tas", out, metric="events", params={"h": 200.0}, end="2021-01-01"
    )
    ds = xr.open_zarr(out)
    assert set(np.unique(ds["error"].values)) == {1.0}  # hits only
    assert ds["error"].attrs["flag_meanings"].split() == [
        "hit",
        "miss",
        "false_alarm",
        "correct_no_event",
    ]


def test_server_uses_the_precomputed_error(stores: dict[str, str], tmp_path: Path) -> None:
    out = str(tmp_path / "err.zarr")
    build_errors(catalog_of(stores, tmp_path), "model:tas", "obs:tas", out, metric="abs")
    cat = catalog_of(stores, tmp_path, {"err": {"path": out}})
    assert find_precomputed(cat, error_key("model:tas", "obs:tas", resolve("abs"))) is not None
    assert find_precomputed(cat, error_key("model:tas", "obs:tas", resolve("bias"))) is None
    client = TestClient(create_app(cat))
    x, y = lonlat_to_tile(10.0, 46.0, 1)  # zoomed far out: on the fly would not be exact
    base = {"p": "model:tas", "t": "obs:tas", "pt": "2021-01-02", "tt": "2021-01-02"}
    r = client.get(f"/api/error/tiles/1/{x}/{y}.bin", params={**base, "metric": "abs"})
    assert r.status_code == 200 and r.headers["X-GeoLoupe-Precomputed"] == "err:error"
    vals = decode_data_tile(r.content, 256)
    np.testing.assert_allclose(vals[np.isfinite(vals)], 1.0, atol=1e-4)
    # another definition, or different times of P and T: computed on the fly
    r2 = client.get(f"/api/error/tiles/1/{x}/{y}.bin", params={**base, "metric": "bias"})
    assert "X-GeoLoupe-Precomputed" not in r2.headers
    r3 = client.get(
        f"/api/error/tiles/1/{x}/{y}.bin", params={**base, "tt": "2021-01-03", "metric": "abs"}
    )
    assert "X-GeoLoupe-Precomputed" not in r3.headers


def test_build_uses_the_finer_grid(composite_catalog: Catalog, tmp_path: Path) -> None:
    # P regional (0.1°), T global (2°): the error map is on P's finer grid
    out = str(tmp_path / "fine.zarr")
    build_errors(composite_catalog, "reg:tas", "glob:tas", out, end="2021-01-01")
    assert xr.open_zarr(out)["error"].shape[1:] == (80, 120)
    # the finer layer must be one variable: a composite cannot give the grid
    with pytest.raises(ValueError, match="combines 2 sources"):
        build_errors(composite_catalog, "tas", "glob:tas", str(tmp_path / "x.zarr"))


def test_cli(stores: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("datasets:\n" + "".join(f"  {k}:\n    path: {v}\n" for k, v in stores.items()))
    out = str(tmp_path / "cli.zarr")
    rc = main(
        [
            "errors",
            "build",
            str(cfg),
            "model:tas",
            "obs:tas",
            out,
            "--metric",
            "rel",
            "--params",
            "eps:1",
        ]
    )
    assert rc == 0
    text = capsys.readouterr().out
    assert "Add to the configuration" in text and f"path: {out}" in text
    assert json.loads(xr.open_zarr(out)["error"].attrs[ATTR])["params"] == {"eps": 1.0}
