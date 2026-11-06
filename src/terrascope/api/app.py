"""HTTP API (FastAPI) and the bundled web client.

Routes (all under ``/api``; interactive docs at ``/api/docs``):

    GET /catalog                                   datasets, layers, errors
    GET /colormaps                                 colormap lookup tables
    GET /layers/{id}                               full layer metadata
    GET /layers/{id}/tiles/{z}/{x}/{y}.bin         float32 data tile (see docs/data-tiles.md)
    GET /layers/{id}/tiles/{z}/{x}/{y}.png         rendered tile (for QGIS, Leaflet, ...)
    GET /layers/{id}/tilejson.json                 TileJSON 3.0 for the PNG tiles
    GET /layers/{id}/point?lon=&lat=               native values at a location
    GET /layers/{id}/timeseries?lon=&lat=          native time series at a location
    GET /layers/{id}/stats                         value statistics (colour ranges)
    GET /sources/{dataset:variable}/export.nc      native-resolution NetCDF subset

Common query parameters: ``time`` (ISO 8601; default: the last time) and
``dims`` (extra dimensions, ``name:value,name:value``).
"""

from __future__ import annotations

import logging
import mimetypes
import threading
import time as _time
import zlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Annotated, Any

import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from terrascope import __version__
from terrascope.catalog import Catalog, Layer
from terrascope.colormaps import apply_classes, apply_colormap, colormap_table
from terrascope.engine import Selection, SelectionError, TileEngine
from terrascope.points import PointRegistry, PointsError
from terrascope.query import QueryError, export_subset, point, stats, timeseries, to_netcdf_bytes
from terrascope.tiling import Tile, TileError
from terrascope.timeaxis import TimeError, compact_axis
from terrascope.vectors import VectorError, VectorRegistry

log = logging.getLogger(__name__)

STATIC_DIR = Path(str(resources.files("terrascope").joinpath("static")))
# Module workers (MapLibre's .mjs worker) need a JavaScript MIME type everywhere.
mimetypes.add_type("text/javascript", ".mjs")


def parse_dims(dims: str | None) -> dict[str, str]:
    """'plev:500,member:3' -> {'plev': '500', 'member': '3'}."""
    out: dict[str, str] = {}
    for part in (dims or "").split(","):
        if not part.strip():
            continue
        name, sep, value = part.partition(":")
        if not sep:
            raise SelectionError(f"bad dims entry {part!r}; use name:value")
        out[name.strip()] = value.strip()
    return out


def parse_bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    if not bbox:
        return None
    try:
        w, s, e, n = (float(v) for v in bbox.split(","))
    except ValueError:
        raise SelectionError("bbox must be west,south,east,north in degrees") from None
    if not (s < n and w < e):
        raise SelectionError("bbox must satisfy west < east and south < north")
    return w, s, e, n


def encode_png(rgba: np.ndarray) -> bytes:
    """Minimal PNG encoder (8-bit RGBA, no filtering)."""
    import struct

    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[i].tobytes() for i in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def layer_json(
    engine: TileEngine, layer: Layer, points: dict[str, Any] | None = None
) -> dict[str, Any]:
    ts = engine.settings.tile_size
    sources = []
    for s in layer.sources:
        d = s.describe()
        d["min_zoom"] = engine.min_zoom(s)
        d["native_zoom"] = engine.native_zoom(s, ts)
        d["zoom_window"] = [s.min_zoom, s.max_zoom]
        sources.append(d)
    native = max(sd["native_zoom"] for sd in sources)
    compact = compact_axis(layer.times)
    st = layer.style
    return {
        "id": layer.id,
        "title": layer.title,
        "description": layer.description,
        "kind": layer.kind,
        "units": layer.units,
        "style": {
            "colormap": st.colormap,
            "vmin": st.vmin,
            "vmax": st.vmax,
            "resampling": st.resampling,
            "gamma": layer.gamma,
        },
        "classes": [
            {"value": v, "name": c["name"], "color": c["color"]}
            for v, c in sorted((layer.sources[0].classes or {}).items())
        ]
        or None,
        "bands": [dict(s.fixed_dims).get("band") for s in layer.sources]
        if layer.kind == "rgb"
        else None,
        "bounds": list(layer.bounds),
        "times": None if compact else layer.times,
        "time_axis": compact,
        "time_step": next(
            (
                s.time.step_label()
                for s in layer.sources
                if s.time is not None and s.fixed_time is None
            ),
            None,
        ),
        "dims": {
            k: [v if isinstance(v, int | float | str) else str(v) for v in vals]
            for k, vals in layer.dims.items()
            if not all(k in dict(s.fixed_dims) for s in layer.sources)  # fixed (e.g. RGB bands)
        },
        "tile_size": ts,
        "minzoom": 0,
        "maxzoom": int(min(22, max(0, np.ceil(native) + 1))),
        "sources": sources,
        "points": points,
    }


def warm_up(engine: TileEngine, catalog: Catalog, max_layers: int = 8) -> None:
    """Compute statistics of the main layers once, in the background.

    The first request in a fresh process pays for lazy imports (codecs),
    opening store metadata and cold file-system caches — seconds on a busy
    parallel file system. Doing it right after start makes the first view fast.
    """
    t0 = _time.perf_counter()
    layers = sorted(catalog.layers.values(), key=lambda lyr: lyr.kind != "composite")[:max_layers]
    for layer in layers:
        try:
            sel = Selection.make(layer.times[-1] if layer.times else None)
            cached_stats(engine, layer, sel, None)
        except Exception as e:  # warm-up must never break the server
            log.warning("warm-up of %s failed: %s", layer.id, e)
    log.info("warm-up of %d layers done in %.1f s", len(layers), _time.perf_counter() - t0)


def cached_stats(
    engine: TileEngine, layer: Layer, sel: Selection, bbox: tuple[float, float, float, float] | None
) -> dict[str, Any]:
    """Layer statistics, cached (they are asked for on every layer switch)."""
    key = ("stats", layer.id, sel, bbox)
    hit = engine.cache.get(key)
    if hit is None:
        hit = stats(engine, layer, sel, bbox)
        engine.cache.put(key, hit, 2048)
    return dict(hit)


def create_app(catalog: Catalog, warm: bool = True) -> FastAPI:
    """The TerraScope web application for a catalog."""
    engine = TileEngine(catalog)
    settings = catalog.settings

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if warm:
            threading.Thread(
                target=warm_up, args=(engine, catalog), daemon=True, name="warm-up"
            ).start()
            threading.Thread(target=vectors.preload, daemon=True, name="vectors").start()
        yield

    app = FastAPI(
        lifespan=lifespan,
        title=f"{catalog.config.title} API",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.catalog = catalog
    app.state.engine = engine

    # point observations (stations): layers of their own, and overlays on gridded layers
    registry = PointRegistry(catalog.config, set(catalog.layers))
    app.state.points = registry

    # featured views (the gallery); a view whose layer is missing (dataset failed to open) is dropped
    known = set(catalog.layers) | set(registry.layer_ids())
    views: list[dict[str, Any]] = []
    for i, view in enumerate(catalog.config.views):
        missing = [lid for lid in (view.layer, view.compare) if lid and lid not in known]
        if missing:
            log.warning("view %r skipped: unknown layer(s) %s", view.title, missing)
            continue
        views.append({"id": i, **view.model_dump(exclude_none=True, exclude_defaults=True)})
        views[-1].update(title=view.title, layer=view.layer)

    # vector overlays (regions, rivers, tracks): GeoJSON for the browser
    vectors = VectorRegistry(catalog.config)
    app.state.vectors = vectors

    def points_meta(layer_id: str) -> dict[str, Any] | None:
        hit = registry.resolve(layer_id)
        return None if hit is None else registry.meta(*hit)

    app.add_middleware(GZipMiddleware, minimum_size=2048)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET"],
        expose_headers=[
            "X-TerraScope-Sources",
            "X-TerraScope-Skipped",
            "X-TerraScope-Time-Ms",
            "X-TerraScope-Bands",
        ],
    )

    # ---------------------------------------------------------------- errors
    @app.exception_handler(KeyError)
    async def _not_found(_: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"detail": str(exc.args[0] if exc.args else exc)}, status_code=404)

    for exc_type in (SelectionError, TimeError, QueryError, TileError, PointsError, VectorError):

        @app.exception_handler(exc_type)
        async def _bad_request(_: Request, exc: Exception) -> JSONResponse:
            return JSONResponse({"detail": str(exc)}, status_code=400)

    def get_layer(layer_id: str) -> Layer:
        return catalog.layer(layer_id)

    def selection(layer: Layer, time: str | None, dims: str | None) -> Selection:
        t = time or (layer.times[-1] if layer.times else None)
        return Selection.make(t, parse_dims(dims))

    # --------------------------------------------------------------- catalog
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/catalog")
    def get_catalog() -> dict[str, Any]:
        return {
            "title": catalog.config.title,
            "description": catalog.config.description,
            "version": __version__,
            "views": views,
            "datasets": [
                {
                    "id": d.id,
                    "title": d.title,
                    "description": d.config.description,
                    "attribution": d.config.attribution,
                    "status": d.status,
                    "error": d.error,
                    "variables": list(d.sources)
                    or sorted({v for m in d.members for v in catalog.datasets[m].sources}),
                    "open_seconds": round(d.open_seconds, 3),
                }
                for d in catalog.datasets.values()
                if not d.hidden
            ],
            "layers": [
                {
                    "id": lyr.id,
                    "title": lyr.title,
                    "kind": lyr.kind,
                    "units": lyr.units,
                    "datasets": sorted({s.dataset_id for s in lyr.sources}),
                    "has_time": bool(lyr.times),
                    "dims": list(lyr.dims),
                }
                for lyr in catalog.layers.values()
                if not lyr.hidden
            ]
            + [
                {
                    "id": lid,
                    "title": f"{var} ({registry.sources[pid].title})",
                    "kind": "points",
                    "units": registry.meta(pid, var)["units"],
                    "datasets": [pid],
                    "has_time": True,
                    "dims": [],
                }
                for lid, (pid, var) in registry.layer_ids().items()
            ],
            "points": [
                {
                    "id": pid,
                    "title": pcfg.title or pid,
                    "attribution": pcfg.attribution,
                    "status": "error" if pid in registry.errors else "ok",
                    "error": registry.errors.get(pid),
                    "layers": pcfg.layers,
                    "variables": [v.name for v in registry.sources[pid].variables()]
                    if pid in registry.sources
                    else [],
                }
                for pid, pcfg in catalog.config.points.items()
            ],
            "vectors": vectors.info(),
        }

    @app.get("/api/vectors/{vid}.geojson")
    def vector_geojson(vid: str) -> Response:
        """A vector overlay as GeoJSON in longitude/latitude (read once, then from memory)."""
        layer = vectors.get(vid)
        return Response(
            layer.geojson,
            media_type="application/geo+json",
            headers={
                "Cache-Control": "public, max-age=3600",
                "X-TerraScope-Count": str(layer.count),
            },
        )

    @app.get("/api/colormaps")
    def colormaps() -> Response:
        return JSONResponse(colormap_table(), headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/layers/{layer_id}")
    def get_layer_meta(layer_id: str) -> dict[str, Any]:
        if layer_id in registry.layer_ids():
            return registry.layer_json(layer_id)
        return layer_json(engine, get_layer(layer_id), points_meta(layer_id))

    # ----------------------------------------------------------------- tiles
    @app.get("/api/layers/{layer_id}/tiles/{z}/{x}/{y}.bin")
    def data_tile(
        request: Request,
        layer_id: str,
        z: int,
        x: int,
        y: int,
        time: str | None = None,
        dims: str | None = None,
    ) -> Response:
        layer = get_layer(layer_id)
        t0 = _time.perf_counter()
        body, headers = engine.tile_bytes(
            layer, Tile(z, x, y, settings.tile_size), selection(layer, time, dims)
        )
        headers = {**headers, "X-TerraScope-Time-Ms": f"{(_time.perf_counter() - t0) * 1000:.1f}"}
        headers["Cache-Control"] = "public, max-age=3600"
        if body is None:
            return Response(status_code=204, headers=headers)
        if "gzip" in request.headers.get("accept-encoding", ""):
            headers["Content-Encoding"] = "gzip"
        else:
            import gzip

            body = gzip.decompress(body)
        return Response(body, media_type="application/octet-stream", headers=headers)

    def colour(
        layer: Layer,
        data: np.ndarray,
        bands: list[np.ndarray] | None,
        sel: Selection,
        colormap: str | None,
        vmin: float | None,
        vmax: float | None,
        log_scale: bool,
    ) -> np.ndarray | None:
        """RGBA image of rendered data: class colours, an RGB stretch or a colormap.

        None when no range is given and the layer has no data to compute one from.
        """
        try:
            classes = layer.sources[0].classes
            if classes and bands is None:  # categorical: class colours, no colour scale
                return apply_classes(data, classes, colormap or layer.style.colormap or "turbo")
            lo = vmin if vmin is not None else layer.style.vmin
            hi = vmax if vmax is not None else layer.style.vmax
            if lo is None or hi is None:
                st = cached_stats(engine, layer, sel, None)
                if not st.get("count"):
                    return None
                lo = st["p2"] if lo is None else lo
                hi = st["p98"] if hi is None else hi
            if bands is not None:  # RGB: stretch every channel to 0–255
                with np.errstate(invalid="ignore"):
                    planes = [
                        np.clip((b - lo) / (hi - lo), 0, 1) ** (1 / layer.gamma) for b in bands
                    ]
                rgba = np.zeros((*data.shape, 4), np.uint8)
                ok = np.all([np.isfinite(p) for p in planes], axis=0)
                for c, p in enumerate(planes):
                    rgba[..., c] = np.where(ok, p * 255 + 0.5, 0).astype(np.uint8)
                rgba[..., 3] = np.where(ok, 255, 0)
                return rgba
            return apply_colormap(
                data, colormap or layer.style.colormap or "viridis", lo, hi, log_scale
            )
        except KeyError as e:
            raise HTTPException(400, str(e.args[0])) from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    def png_response(rgba: np.ndarray | None) -> Response:
        if rgba is None:
            return Response(status_code=204)
        return Response(
            encode_png(rgba),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.get("/api/layers/{layer_id}/tiles/{z}/{x}/{y}.png")
    def png_tile(
        layer_id: str,
        z: int,
        x: int,
        y: int,
        time: str | None = None,
        dims: str | None = None,
        colormap: str | None = None,
        vmin: float | None = None,
        vmax: float | None = None,
        scale: Annotated[str, Query(pattern="^(linear|log)$")] = "linear",
    ) -> Response:
        layer = get_layer(layer_id)
        sel = selection(layer, time, dims)
        res = engine.render(layer, Tile(z, x, y, settings.tile_size), sel)
        return png_response(
            colour(layer, res.data, res.bands, sel, colormap, vmin, vmax, scale == "log")
        )

    @app.get("/api/layers/{layer_id}/preview.png")
    def preview(
        layer_id: str,
        center: str,
        zoom: Annotated[int, Query(ge=0, le=22)],
        width: Annotated[int, Query(ge=16, le=1024)] = 480,
        height: Annotated[int, Query(ge=16, le=1024)] = 240,
        time: str | None = None,
        dims: str | None = None,
        colormap: str | None = None,
        vmin: float | None = None,
        vmax: float | None = None,
        scale: Annotated[str, Query(pattern="^(linear|log)$")] = "linear",
    ) -> Response:
        """A Web Mercator image of any size centred on a point (thumbnails, previews).

        Stitched from the map tiles of zoom `zoom` (so it shares their cache), at most 5 × 5 tiles.
        """
        try:
            lon, lat = (float(v) for v in center.split(","))
        except ValueError:
            raise HTTPException(400, "center must be 'lon,lat'") from None
        layer = get_layer(layer_id)
        sel = selection(layer, time, dims)
        ts = settings.tile_size
        n = 1 << zoom
        lat = max(-85.05, min(85.05, lat))
        cx = (lon + 180) / 360 * n * ts
        cy = (
            (1 - np.log(np.tan(np.radians(lat)) + 1 / np.cos(np.radians(lat))) / np.pi) / 2 * n * ts
        )
        x0, y0 = int(np.floor(cx - width / 2)), int(np.floor(cy - height / 2))
        tx = range(x0 // ts, (x0 + width - 1) // ts + 1)
        ty = range(y0 // ts, (y0 + height - 1) // ts + 1)
        if len(tx) * len(ty) > 25:
            raise HTTPException(400, "preview too large for this zoom: lower width/height")
        nb = 3 if layer.kind == "rgb" else 1
        canvas = np.full((nb, len(ty) * ts, len(tx) * ts), np.nan, np.float32)
        for j, y in enumerate(ty):
            if not 0 <= y < n:
                continue  # beyond the poles of the map: empty
            for i, x in enumerate(tx):
                res = engine.render(layer, Tile(zoom, x % n, y, ts), sel)  # wraps in longitude
                planes = res.bands if res.bands is not None else [res.data]
                for b, plane in enumerate(planes[:nb]):
                    canvas[b, j * ts : (j + 1) * ts, i * ts : (i + 1) * ts] = plane
        ox, oy = x0 - tx[0] * ts, y0 - ty[0] * ts
        crop = canvas[:, oy : oy + height, ox : ox + width]
        bands = list(crop) if nb == 3 else None
        return png_response(
            colour(layer, crop[0], bands, sel, colormap, vmin, vmax, scale == "log")
        )

    @app.get("/api/layers/{layer_id}/tilejson.json")
    def tilejson(request: Request, layer_id: str) -> dict[str, Any]:
        layer = get_layer(layer_id)
        meta = layer_json(engine, layer)
        q = f"?{request.url.query}" if request.url.query else ""
        base = str(request.url_for("png_tile", layer_id=layer_id, z="{z}", x="{x}", y="{y}"))
        url = base.replace("%7B", "{").replace("%7D", "}") + q
        return {
            "tilejson": "3.0.0",
            "name": layer.title,
            "tiles": [url],
            "minzoom": meta["minzoom"],
            "maxzoom": meta["maxzoom"],
            "bounds": meta["bounds"],
            "attribution": "; ".join(
                {catalog.datasets[s.dataset_id].config.attribution or "" for s in layer.sources}
                - {""}
            ),
        }

    # --------------------------------------------------------------- queries
    @app.get("/api/layers/{layer_id}/point")
    def get_point(
        layer_id: str, lon: float, lat: float, time: str | None = None, dims: str | None = None
    ) -> dict[str, Any]:
        layer = get_layer(layer_id)
        return point(layer, lon, lat, selection(layer, time, dims))

    @app.get("/api/layers/{layer_id}/timeseries")
    def get_timeseries(
        layer_id: str,
        lon: float,
        lat: float,
        start: str | None = None,
        end: str | None = None,
        dims: str | None = None,
    ) -> dict[str, Any]:
        layer = get_layer(layer_id)
        sel = Selection.make(None, parse_dims(dims))
        return timeseries(layer, lon, lat, sel, start, end, settings.max_timeseries_steps)

    @app.get("/api/layers/{layer_id}/stats")
    def get_stats(
        layer_id: str, time: str | None = None, dims: str | None = None, bbox: str | None = None
    ) -> dict[str, Any]:
        point_layer = registry.layer_ids().get(layer_id)
        if point_layer is not None:
            pid, var = point_layer
            t = time or registry.sources[pid].times()[-1]
            return registry.sources[pid].stats(var, t)
        layer = get_layer(layer_id)
        return cached_stats(engine, layer, selection(layer, time, dims), parse_bbox(bbox))

    @app.get("/api/layers/{layer_id}/points")
    def get_points(
        layer_id: str, time: str | None = None, bbox: str | None = None, limit: int = 20_000
    ) -> dict[str, Any]:
        hit = registry.resolve(layer_id)
        if hit is None:
            raise HTTPException(404, f"layer {layer_id!r} has no point observations")
        pid, var = hit
        t = time or registry.sources[pid].times()[-1]
        return registry.sources[pid].at_time(var, t, parse_bbox(bbox), min(limit, 50_000))

    @app.get("/api/points/{source_id}/series")
    def get_point_series(
        source_id: str, station: str, variable: str, start: str, end: str
    ) -> dict[str, Any]:
        if source_id not in registry.sources:
            raise HTTPException(404, f"unknown point source {source_id!r}")
        return registry.sources[source_id].series(station, variable, start, end)

    @app.get("/api/sources/{source_id}/export.nc")
    def export(
        source_id: str,
        bbox: str,
        time: str | None = None,
        start: str | None = None,
        end: str | None = None,
        dims: str | None = None,
    ) -> Response:
        src = catalog.source(source_id)
        box = parse_bbox(bbox)
        assert box is not None
        t = time or (src.time.labels[-1] if src.time is not None else None)
        ds = export_subset(
            src, box, Selection.make(t, parse_dims(dims)), start, end, settings.max_export_mb
        )
        name = f"{source_id.replace(':', '_')}_{'_'.join(f'{b:.2f}' for b in box)}.nc"
        return Response(
            to_netcdf_bytes(ds),
            media_type="application/x-netcdf",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    # ------------------------------------------------------------ web client
    index = STATIC_DIR / "index.html"

    @app.get("/", include_in_schema=False)
    def root() -> Response:
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return HTMLResponse(
            "<h1>TerraScope API is running</h1><p>The web client is not built. Run "
            "<code>npm ci &amp;&amp; npm run build</code> in <code>frontend/</code>, or open "
            "<a href='api/docs'>the API docs</a>.</p>"
        )

    @app.get("/{path:path}", include_in_schema=False)
    def static_files(path: str) -> Response:
        if path.startswith("api/"):
            raise HTTPException(404, f"no API route /{path}")
        f = (STATIC_DIR / path).resolve()
        if STATIC_DIR.exists() and f.is_file() and STATIC_DIR.resolve() in f.parents:
            cache = (
                "public, max-age=31536000, immutable" if "/assets/" in f"/{path}" else "no-cache"
            )
            return FileResponse(f, headers={"Cache-Control": cache})
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        raise HTTPException(404)

    return app
