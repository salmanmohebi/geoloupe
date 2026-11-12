"""Exact error maps on the server (geoloupe/errortiles.py and /api/error/...)."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr
from fastapi.testclient import TestClient

from geoloupe.api.app import create_app
from geoloupe.catalog import Catalog
from geoloupe.engine import Selection, TileEngine, decode_data_tile
from geoloupe.errors import conversion, resolve
from geoloupe.errortiles import ErrorQuery, ErrorQueryError, error_parts, error_stats
from geoloupe.tiling import Tile, lonlat_to_tile

from .conftest import latlon_ds

BOX = (4.0, 42.0, 16.0, 50.0)


@pytest.fixture
def pair() -> tuple[xr.Dataset, xr.Dataset]:
    """T on a 0.1° grid, and P = T ± 1 in a checkerboard of native cells (in °C)."""
    t = latlon_ds(BOX, 0.1, seed=3)
    i, j = np.meshgrid(np.arange(t.sizes["lat"]), np.arange(t.sizes["lon"]), indexing="ij")
    checker = np.where((i + j) % 2 == 0, 1.0, -1.0).astype(np.float32)
    p = t.copy(deep=True)
    p["tas"] = t["tas"] + checker - 273.15
    p["tas"].attrs = {"units": "degC"}
    return p, t


@pytest.fixture
def catalog(pair: tuple[xr.Dataset, xr.Dataset]) -> Catalog:
    p, t = pair
    return Catalog.from_xarray({"model": p, "obs": t})


def query(catalog: Catalog, metric: str = "abs", **params: float) -> ErrorQuery:
    P, T = catalog.layer("model:tas"), catalog.layer("obs:tas")
    sel = Selection.make("2021-01-02")
    return ErrorQuery(P, T, sel, sel, resolve(metric, None, params), conversion(P.units, T.units))


def zoomed_out_tile() -> Tile:
    x, y = lonlat_to_tile(10.0, 46.0, 2)  # a pixel spans ~3 × 2.5 native cells
    return Tile(2, x, y)


def test_error_of_averages_is_not_average_error(catalog: Catalog) -> None:
    """The checkerboard cancels in screen-pixel means, but every native cell is 1 K off."""
    engine = TileEngine(catalog)
    tile = zoomed_out_tile()
    parts = error_parts(engine, query(catalog, "abs"), tile)
    assert parts is not None and parts.exact and parts.factor == 4
    vals = parts.values()
    ok = np.isfinite(vals)
    assert ok.sum() > 1000
    np.testing.assert_allclose(vals[ok], 1.0, atol=1e-4)  # exact: |P − T| = 1 in every cell

    # what the browser preview computes from the two display tiles: nearly 0
    sel = Selection.make("2021-01-02")
    p = engine.render(catalog.layer("model:tas"), tile, sel).data + 273.15
    t = engine.render(catalog.layer("obs:tas"), tile, sel).data
    assert np.nanmean(np.abs(p - t)) < 0.5

    bias = error_parts(engine, query(catalog, "bias"), tile)
    assert bias is not None and abs(float(np.nanmean(bias.values()))) < 0.2


def test_units_are_converted(catalog: Catalog) -> None:
    q = query(catalog, "abs")
    assert q.conv.offset == pytest.approx(273.15)
    stats = error_stats(TileEngine(catalog), q, (6.0, 43.0, 14.0, 49.0), 2)
    assert stats["exact"] and stats["count"] > 1000
    assert stats["mean_abs"] == pytest.approx(1.0, abs=1e-4)
    assert stats["rms"] == pytest.approx(1.0, abs=1e-4)


def test_stats_of_bias_give_mae_and_rmse(catalog: Catalog) -> None:
    stats = error_stats(TileEngine(catalog), query(catalog, "bias"), (6.0, 43.0, 14.0, 49.0), 3)
    assert stats["mean"] == pytest.approx(0.0, abs=0.05)  # +1 and −1 cancel
    assert stats["mean_abs"] == pytest.approx(1.0, abs=1e-4)  # MAE
    assert stats["rms"] == pytest.approx(1.0, abs=1e-4)  # RMSE
    assert stats["min"] == pytest.approx(-1.0, abs=1e-4)
    assert stats["max"] == pytest.approx(1.0, abs=1e-4)


def test_event_classes_and_scores(catalog: Catalog) -> None:
    # T ≈ 280 K and P (in K) are above h = 200 everywhere: hits only
    q = query(catalog, "events", h=200.0)
    engine = TileEngine(catalog)
    parts = error_parts(engine, q, zoomed_out_tile())
    assert parts is not None and parts.classes is not None
    vals = parts.values()
    assert set(np.unique(vals[np.isfinite(vals)])) == {1.0}  # the majority class, not a mean
    stats = error_stats(engine, q, (6.0, 43.0, 14.0, 49.0), 2)
    assert stats["classes"]["1"] > 0  # every class is listed, also with no cells
    assert stats["classes"]["2"] == stats["classes"]["3"] == stats["classes"]["4"] == 0
    assert stats["pod"] == 1.0 and stats["far"] == 0.0 and stats["csi"] == 1.0
    # above everything: no events at all
    s2 = error_stats(engine, query(catalog, "events", h=1000.0), (6.0, 43.0, 14.0, 49.0), 2)
    assert s2["classes"]["4"] == s2["count"] and s2["pod"] is None and s2["csi"] is None


def test_budget_marks_tiles_not_exact(catalog: Catalog) -> None:
    catalog.settings.error_max_subpixels = 256  # no sub-pixels: screen pixels only
    parts = error_parts(TileEngine(catalog), query(catalog, "abs"), zoomed_out_tile())
    assert parts is not None and parts.factor == 1 and not parts.exact


def test_zoomed_in_needs_no_subpixels(catalog: Catalog) -> None:
    x, y = lonlat_to_tile(10.0, 46.0, 7)  # pixels much smaller than the 0.1° cells
    parts = error_parts(TileEngine(catalog), query(catalog, "abs"), Tile(7, x, y))
    assert parts is not None and parts.factor == 1 and parts.exact


def test_rgb_layers_are_refused(catalog: Catalog) -> None:
    P = catalog.layer("model:tas")
    rgb = type("L", (), {"kind": "rgb", "id": "x"})()
    with pytest.raises(ErrorQueryError, match="RGB"):
        ErrorQuery(P, rgb, Selection(), Selection(), resolve("bias"), conversion("K", "K"))  # type: ignore[arg-type]


# ------------------------------------------------------------------- HTTP
@pytest.fixture
def client(catalog: Catalog) -> TestClient:
    return TestClient(create_app(catalog))


def test_error_tile_endpoint(client: TestClient) -> None:
    t = zoomed_out_tile()
    r = client.get(
        f"/api/error/tiles/{t.z}/{t.x}/{t.y}.bin",
        params={
            "p": "model:tas",
            "t": "obs:tas",
            "pt": "2021-01-02",
            "tt": "2021-01-02",
            "metric": "abs",
        },
        headers={"accept-encoding": "gzip"},
    )
    assert r.status_code == 200
    assert r.headers["X-GeoLoupe-Exact"] == "1" and r.headers["X-GeoLoupe-Subpixels"] == "4"
    vals = decode_data_tile(r.content, 256)  # the test client removes the gzip encoding
    np.testing.assert_allclose(vals[np.isfinite(vals)], 1.0, atol=1e-4)


def test_error_tile_custom_formula_and_errors(client: TestClient) -> None:
    t = zoomed_out_tile()
    url = f"/api/error/tiles/{t.z}/{t.x}/{t.y}.bin"
    base = {"p": "model:tas", "t": "obs:tas", "pt": "2021-01-02", "tt": "2021-01-02"}
    ok = client.get(url, params={**base, "metric": "custom", "formula": "abs(P - T) > 0.5"})
    assert ok.status_code == 200
    bad = client.get(url, params={**base, "metric": "custom", "formula": "P - X"})
    assert bad.status_code == 400 and "unknown name 'X'" in bad.json()["detail"]
    bad = client.get(url, params={**base, "metric": "rel", "params": "eps:x"})
    assert bad.status_code == 400 and "must be a number" in bad.json()["detail"]
    bad = client.get(url, params={**base, "metric": "nope"})
    assert bad.status_code == 400 and "unknown error" in bad.json()["detail"]
    missing = client.get(url, params={**base, "p": "nope:tas"})
    assert missing.status_code == 404


def test_error_stats_endpoint(client: TestClient) -> None:
    r = client.get(
        "/api/error/stats",
        params={
            "p": "model:tas",
            "t": "obs:tas",
            "pt": "2021-01-02",
            "tt": "2021-01-02",
            "metric": "bias",
            "bbox": "6,43,14,49",
            "z": 3,
        },
    )
    assert r.status_code == 200
    s = r.json()
    assert s["exact"] and s["mean_abs"] == pytest.approx(1.0, abs=1e-4)
    assert s["rms"] == pytest.approx(1.0, abs=1e-4)


def test_error_preview_png(client: TestClient) -> None:
    base = {"p": "model:tas", "t": "obs:tas", "pt": "2021-01-02", "tt": "2021-01-02"}
    for metric in ("abs", "bias", "ratio", "events"):
        r = client.get(
            "/api/error/preview.png",
            params={
                **base,
                "metric": metric,
                "center": "10,46",
                "zoom": 3,
                "width": 64,
                "height": 32,
            },
        )
        assert r.status_code == 200 and r.headers["content-type"] == "image/png", metric
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
