"""Vector overlays: regions, borders, rivers, tracks, sites drawn over the data.

Formats: GeoJSON, Shapefile, GeoPackage, FlatGeobuf, KML, GML (anything GDAL reads, through
pyogrio), and GeoParquet (through pyarrow); local or remote. The server reads a file once,
reprojects it to longitude/latitude, optionally filters and simplifies it, and sends GeoJSON. The
browser draws it with MapLibre (which simplifies per zoom) and hit-tests it for the hover read-out.

Vector files are meant to be overlays (thousands to a few hundred thousand features); a size
limit (``max_vector_mb``) keeps a request bounded, with a message that says how to reduce it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import threading
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from terrascope.config import Config, VectorConfig
from terrascope.readers import with_retries

log = logging.getLogger(__name__)

VECTOR_EXT = (".geojson", ".shp", ".gpkg", ".fgb", ".kml", ".gml", ".geoparquet", ".shp.zip")
_LABEL_NAMES = ("name", "name_en", "label", "title", "id", "nom", "nombre")


class VectorError(ValueError):
    """A vector file cannot be read or is too large to send."""


def looks_like_vector(path: str) -> bool:
    low = path.lower().split("?")[0]
    return low.endswith(VECTOR_EXT) or (low.endswith(".json") and "geo" in low)


@dataclass
class VectorLayer:
    """A loaded overlay: GeoJSON bytes plus what the client needs to style and label it."""

    id: str
    geojson: bytes
    count: int
    bounds: list[float]
    geometry_types: list[str]
    properties: list[str]
    label: str | None
    load_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)


def _clean(v: Any) -> Any:
    """A JSON-safe property value."""
    if v is None:
        return None
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    if isinstance(v, str | int | float | bool):
        return v
    return str(v)


def _read_ogr(loc: str, cfg: VectorConfig) -> tuple[Any, list[str], dict[str, list[Any]], Any]:
    """(geometries, field names, field values, CRS) through pyogrio (GDAL)."""
    try:
        import pyogrio.raw
    except ImportError as e:
        raise VectorError(
            "vector overlays need pyogrio and shapely; install terrascope[vector]"
        ) from e
    import shapely

    path = loc
    if loc.startswith(("http://", "https://")) and loc.lower().endswith(".zip"):
        path = "/vsizip//vsicurl/" + loc  # a zipped Shapefile on the web
    elif loc.lower().endswith(".zip"):
        path = "/vsizip/" + loc
    meta, _, wkb, values = pyogrio.raw.read(
        path, layer=cfg.layer, where=cfg.where, columns=cfg.properties
    )
    names = [str(n) for n in meta["fields"]]
    return shapely.from_wkb(wkb), names, dict(zip(names, values, strict=True)), meta.get("crs")


def _read_geoparquet(
    loc: str, cfg: VectorConfig
) -> tuple[Any, list[str], dict[str, list[Any]], Any]:
    import pyarrow.parquet as pq
    import shapely

    table = pq.read_table(loc)
    geo = json.loads((table.schema.metadata or {}).get(b"geo", b"{}") or b"{}")
    gcol = geo.get("primary_column", "geometry")
    if gcol not in table.column_names:
        raise VectorError(f"{loc}: no geometry column {gcol!r}")
    crs = (geo.get("columns", {}).get(gcol, {}) or {}).get("crs")
    names = [c for c in table.column_names if c != gcol]
    if cfg.properties is not None:
        names = [c for c in names if c in cfg.properties]
    geoms = shapely.from_wkb(table.column(gcol).to_numpy(zero_copy_only=False))
    values = {c: table.column(c).to_pylist() for c in names}
    if cfg.where:
        raise VectorError("'where' filters work for GDAL formats; filter GeoParquet beforehand")
    return geoms, names, values, json.dumps(crs) if isinstance(crs, dict) else crs


def load_vector(vid: str, cfg: VectorConfig, config: Config, max_mb: float) -> VectorLayer:
    """Read, reproject, simplify and encode one overlay."""
    import time

    import shapely
    from pyproj import CRS, Transformer

    t0 = time.perf_counter()
    loc = config.resolve(cfg.path)
    reader = _read_geoparquet if loc.lower().endswith((".parquet", ".geoparquet")) else _read_ogr
    try:
        geoms, names, values, crs = with_retries(
            lambda: reader(loc, cfg), "://" in loc, f"vectors {vid!r}"
        )
    except VectorError:
        raise
    except Exception as e:
        raise VectorError(f"cannot read {loc}: {type(e).__name__}: {e}") from e

    keep = ~shapely.is_missing(geoms) & ~shapely.is_empty(geoms)
    geoms = geoms[keep]
    values = {k: [v for v, ok in zip(col, keep, strict=True) if ok] for k, col in values.items()}
    warnings: list[str] = []

    src = CRS.from_user_input(cfg.crs or crs or "OGC:CRS84")
    if not src.equals(CRS.from_epsg(4326), ignore_axis_order=True):
        tr = Transformer.from_crs(src, "OGC:CRS84", always_xy=True)
        geoms = shapely.transform(
            geoms, lambda xy: np.column_stack(tr.transform(xy[:, 0], xy[:, 1]))
        )
    if cfg.simplify:
        geoms = shapely.simplify(geoms, cfg.simplify, preserve_topology=True)
    geoms = shapely.set_precision(geoms, 1e-6, mode="pointwise")  # ~10 cm: smaller JSON

    label = cfg.label or next(
        (n for n in names if n.lower() in _LABEL_NAMES),
        None,
    )
    texts = shapely.to_geojson(geoms)
    parts = []
    for i, g in enumerate(texts):
        props = {k: _clean(values[k][i]) for k in names}
        parts.append(
            f'{{"type":"Feature","id":{i},"properties":{json.dumps(props)},"geometry":{g}}}'
        )
    body = ('{"type":"FeatureCollection","features":[' + ",".join(parts) + "]}").encode()
    mb = len(body) / 1e6
    if mb > max_mb:
        raise VectorError(
            f"{vid!r} is {mb:.0f} MB as GeoJSON (limit {max_mb:g} MB): set 'simplify' (degrees, e.g. "
            "0.001), a 'where' filter, or fewer 'properties'; or raise settings.max_vector_mb"
        )
    if len(geoms):
        x0, y0, x1, y1 = (float(v) for v in shapely.total_bounds(geoms))
        bounds = [round(x0, 6), round(y0, 6), round(x1, 6), round(y1, 6)]
    else:
        bounds = [-180.0, -90.0, 180.0, 90.0]
        warnings.append("no features")
    types = sorted({str(t) for t in shapely.get_type_id(geoms)})
    type_names = {"0": "Point", "1": "LineString", "3": "Polygon", "4": "MultiPoint",
                  "5": "MultiLineString", "6": "MultiPolygon", "7": "GeometryCollection",
                  "2": "LinearRing"}  # fmt: skip
    return VectorLayer(
        vid,
        body,
        len(geoms),
        bounds,
        [type_names.get(t, t) for t in types],
        names,
        label,
        time.perf_counter() - t0,
        warnings,
    )


class VectorRegistry:
    """The overlays of a configuration, loaded on first use (or by the start-up warm-up)."""

    def __init__(self, config: Config):
        self.config = config
        self.loaded: dict[str, VectorLayer] = {}
        self.errors: dict[str, str] = {}
        self._locks = {vid: threading.Lock() for vid in config.vectors}

    def add(self, vid: str, cfg: VectorConfig) -> VectorLayer:
        """Add and load one overlay while the server runs (raises VectorError)."""
        self.config.vectors[vid] = cfg
        self._locks.setdefault(vid, threading.Lock())
        return self.get(vid)

    def get(self, vid: str) -> VectorLayer:
        if vid not in self.config.vectors:
            raise KeyError(f"unknown vector layer {vid!r}")
        with self._locks[vid]:
            if vid in self.loaded:
                return self.loaded[vid]
            try:
                layer = load_vector(
                    vid, self.config.vectors[vid], self.config, self.config.settings.max_vector_mb
                )
            except VectorError as e:
                self.errors[vid] = str(e)
                log.error("vectors %r: %s", vid, e)
                raise
            self.errors.pop(vid, None)
            self.loaded[vid] = layer
            log.info(
                "vectors %r: %d features, %.1f MB in %.2f s",
                vid,
                layer.count,
                len(layer.geojson) / 1e6,
                layer.load_seconds,
            )
            return layer

    def preload(self) -> None:
        for vid in self.config.vectors:
            with contextlib.suppress(VectorError):  # reported in self.errors and the log
                self.get(vid)

    def info(self) -> list[dict[str, Any]]:
        out = []
        for vid, c in self.config.vectors.items():
            lyr = self.loaded.get(vid)
            out.append(
                {
                    "id": vid,
                    "title": c.title or vid,
                    "attribution": c.attribution,
                    "visible": c.visible,
                    "style": {
                        "color": c.color,
                        "width": c.width,
                        "fill": c.fill,
                        "fill_opacity": c.fill_opacity,
                    },
                    "label": c.label if lyr is None else lyr.label,
                    "status": "error" if vid in self.errors else "ok",
                    "error": self.errors.get(vid),
                    "count": None if lyr is None else lyr.count,
                    "bounds": None if lyr is None else lyr.bounds,
                }
            )
        return out
