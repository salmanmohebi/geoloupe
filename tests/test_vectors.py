"""Vector overlays: reading, reprojection, filters, limits, API and CLI."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from fastapi.testclient import TestClient

from terrascope.api.app import create_app
from terrascope.catalog import Catalog
from terrascope.cli import config_for_paths
from terrascope.config import ConfigError, config_from_dict
from terrascope.vectors import VectorError, VectorRegistry

pytest.importorskip("pyogrio")
shapely = pytest.importorskip("shapely")

FC = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"name": "Lake", "area": 12.5, "note": None},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[6, 46], [7, 46], [7, 46.5], [6, 46]]],
            },
        },
        {
            "type": "Feature",
            "properties": {"name": "River", "area": None, "note": "long"},
            "geometry": {"type": "LineString", "coordinates": [[6, 45], [6.5, 45.5], [7, 45.2]]},
        },
        {
            "type": "Feature",
            "properties": {"name": "Hut", "area": 0.0, "note": ""},
            "geometry": {"type": "Point", "coordinates": [7.5, 46.1]},
        },
    ],
}


def _grid() -> xr.Dataset:
    lon, lat = np.arange(5.05, 9, 0.1), np.arange(47.95, 44, -0.1)
    return xr.Dataset({"v": (("lat", "lon"), np.ones((lat.size, lon.size), np.float32))},
                      {"lat": lat, "lon": lon})  # fmt: skip


def _cfg(tmp_path: Path, **vectors: dict) -> Catalog:
    raw = {"datasets": {"d": {"path": "memory://d"}}, "vectors": vectors}
    return Catalog(config_from_dict(raw, base_dir=tmp_path), {"d": _grid()})


def test_geojson_overlay_through_the_api(tmp_path: Path) -> None:
    (tmp_path / "places.geojson").write_text(json.dumps(FC))
    cat = _cfg(tmp_path, places={"path": "places.geojson", "fill": "#3366cc", "visible": True})
    c = TestClient(create_app(cat, warm=False))
    r = c.get("/api/vectors/places.geojson")
    assert r.status_code == 200 and r.headers["content-type"] == "application/geo+json"
    fc = r.json()
    assert [f["geometry"]["type"] for f in fc["features"]] == ["Polygon", "LineString", "Point"]
    assert fc["features"][0]["properties"] == {"name": "Lake", "area": 12.5, "note": None}
    info = c.get("/api/catalog").json()["vectors"][0]
    assert info["id"] == "places" and info["visible"] and info["label"] == "name"
    assert info["count"] == 3 and info["style"]["fill"] == "#3366cc"
    assert c.get("/api/vectors/nothing.geojson").status_code == 404


def test_reprojection_filter_and_properties(tmp_path: Path) -> None:
    import pyogrio.raw
    from pyproj import Transformer

    tr = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    pts = [shapely.Point(*tr.transform(lon, lat)) for lon, lat in [(6.0, 46.0), (8.0, 47.0)]]
    pyogrio.raw.write(
        tmp_path / "sites.gpkg",
        shapely.to_wkb(np.array(pts, dtype=object)),
        [np.array(["a", "b"], dtype=object), np.array([1, 2])],
        ["name", "rank"],
        geometry_type="Point",
        crs="EPSG:3857",
        driver="GPKG",
    )
    cat = _cfg(tmp_path, s={"path": "sites.gpkg", "where": "rank > 1", "properties": ["rank"]})
    layer = VectorRegistry(cat.config).get("s")
    fc = json.loads(layer.geojson)
    assert layer.count == 1 and fc["features"][0]["properties"] == {"rank": 2}
    np.testing.assert_allclose(fc["features"][0]["geometry"]["coordinates"], [8.0, 47.0], atol=1e-6)


def test_geoparquet_overlay(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    geoms = [shapely.LineString([(0, 0), (1, 1)]), shapely.LineString([(2, 2), (3, 1)])]
    table = pa.table(
        {"name": ["x", "y"], "geometry": shapely.to_wkb(np.array(geoms, dtype=object))}
    )
    geo = {
        "version": "1.0.0",
        "primary_column": "geometry",
        "columns": {"geometry": {"encoding": "WKB"}},
    }
    table = table.replace_schema_metadata({b"geo": json.dumps(geo).encode()})
    pq.write_table(table, tmp_path / "lines.parquet")
    layer = VectorRegistry(_cfg(tmp_path, l={"path": "lines.parquet"}).config).get("l")
    assert (
        layer.count == 2 and layer.geometry_types == ["LineString"] and layer.bounds == [0, 0, 3, 2]
    )


def test_simplify_and_size_limit(tmp_path: Path) -> None:
    t = np.linspace(0, 2 * np.pi, 2000)
    wiggly = {
        "type": "LineString",
        "coordinates": np.column_stack([t, 0.001 * np.sin(50 * t)]).tolist(),
    }
    (tmp_path / "w.geojson").write_text(
        json.dumps({"type": "Feature", "properties": {}, "geometry": wiggly})
    )
    full = VectorRegistry(_cfg(tmp_path, w={"path": "w.geojson"}).config).get("w")
    simple = VectorRegistry(_cfg(tmp_path, w={"path": "w.geojson", "simplify": 0.01}).config).get(
        "w"
    )
    assert len(simple.geojson) < len(full.geojson) / 20
    cat = Catalog(
        config_from_dict(
            {"datasets": {"d": {"path": "memory://d"}}, "vectors": {"w": {"path": "w.geojson"}},
             "settings": {"max_vector_mb": 0.001}},
            base_dir=tmp_path,
        ),
        {"d": _grid()},
    )  # fmt: skip
    c = TestClient(create_app(cat, warm=False))
    r = c.get("/api/vectors/w.geojson")
    assert r.status_code == 400 and "simplify" in r.json()["detail"]
    assert c.get("/api/catalog").json()["vectors"][0]["status"] == "error"
    with pytest.raises(VectorError, match="cannot read"):
        VectorRegistry(_cfg(tmp_path, x={"path": "missing.geojson"}).config).get("x")


def test_config_checks_and_cli_paths(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown vectors"):
        config_from_dict({"vectors": {}, "views": [{"title": "t", "layer": "a", "vectors": ["r"]}]})
    with pytest.raises(ConfigError, match="letters"):
        config_from_dict({"vectors": {"a b": {"path": "x.geojson"}}})
    raw = config_for_paths(["grid.zarr", "regions.geojson", "catchments.gpkg", "obs.csv"])
    assert set(raw["vectors"]) == {"regions", "catchments"} and set(raw["points"]) == {"obs"}
    assert raw["vectors"]["regions"]["visible"] is True


def test_remote_vector_formats_are_allowed_for_gdal() -> None:
    # GDAL's allowed-extension list is process-wide: once a COG has been opened, remote vector
    # files must still be readable (regression: every remote overlay failed in the gallery)
    from terrascope.readers import _GDAL_CLOUD_DEFAULTS
    from terrascope.vectors import VECTOR_EXT

    allowed = _GDAL_CLOUD_DEFAULTS["CPL_VSIL_CURL_ALLOWED_EXTENSIONS"].split(",")
    for ext in VECTOR_EXT:
        if ext != ".geoparquet":  # read with pyarrow, not GDAL
            assert "." + ext.rsplit(".", 1)[-1] in allowed, ext
    for sidecar in (".shx", ".dbf", ".prj"):
        assert sidecar in allowed
