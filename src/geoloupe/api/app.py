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
import re
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
from pydantic import BaseModel

from geoloupe import __version__
from geoloupe.catalog import Catalog, Layer
from geoloupe.colormaps import apply_classes, apply_colormap, colormap_table
from geoloupe.config import DatasetConfig, PointsConfig, VectorConfig
from geoloupe.engine import Selection, SelectionError, TileEngine
from geoloupe.errorbuild import error_key, find_precomputed
from geoloupe.errors import conversion as unit_conversion
from geoloupe.errors import resolve as resolve_error
from geoloupe.errortiles import (
    ErrorQuery,
    ErrorQueryError,
    error_parts,
    error_range,
    error_stats,
    error_tile_bytes,
)
from geoloupe.points import PointRegistry, PointsError
from geoloupe.query import QueryError, export_subset, point, stats, timeseries, to_netcdf_bytes
from geoloupe.tiling import Tile, TileError
from geoloupe.timeaxis import TimeError, compact_axis
from geoloupe.vectors import VectorError, VectorRegistry

log = logging.getLogger(__name__)

STATIC_DIR = Path(str(resources.files("geoloupe").joinpath("static")))
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
        "bands": [str(dict(s.fixed_dims).get("band") or s.variable) for s in layer.channel_heads()]
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


class OpenRequest(BaseModel):
    """Body of POST /api/datasets."""

    path: str
    name: str | None = None
    engine: str = "auto"
    # S3 access (see geoloupe.browse.access_options): endpoint of an S3-compatible store, a profile
    # of the server's AWS config, or anonymous (public) access. No keys: those stay on the server.
    endpoint: str | None = None
    profile: str | None = None
    anon: bool | None = None
    region: str | None = None


def create_app(catalog: Catalog, warm: bool = True) -> FastAPI:
    """The GeoLoupe web application for a catalog."""
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
        missing = [lid for lid in view.layer_ids() if lid not in known]
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
            "X-GeoLoupe-Sources",
            "X-GeoLoupe-Skipped",
            "X-GeoLoupe-Time-Ms",
            "X-GeoLoupe-Bands",
        ],
    )

    # ---------------------------------------------------------------- errors
    @app.exception_handler(KeyError)
    async def _not_found(_: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"detail": str(exc.args[0] if exc.args else exc)}, status_code=404)

    for exc_type in (
        SelectionError,
        TimeError,
        QueryError,
        TileError,
        PointsError,
        VectorError,
        ErrorQueryError,
    ):

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
                for d in list(catalog.datasets.values())
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
                for lyr in list(catalog.layers.values())
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
                for pid, pcfg in list(catalog.config.points.items())
            ],
            "vectors": vectors.info(),
            "allow_open": settings.allow_open,
            "open_roots": settings.open_roots,
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
                "X-GeoLoupe-Count": str(layer.count),
            },
        )

    open_lock = threading.Lock()

    @app.post("/api/datasets")
    def add_data(req: OpenRequest) -> dict[str, Any]:
        """Open a file or URL while the server runs ("Add data" in the browser).

        Off by default: remote URLs need `allow_open`, local files must lie below `open_roots`.
        """
        from geoloupe.browse import clean_location, looks_like_address, unwrap_viewer_link
        from geoloupe.cli import config_for_paths

        loc = clean_location(req.path)
        if not loc:
            raise HTTPException(400, "give a path or a URL")
        loc, link_endpoint = unwrap_viewer_link(loc)  # a viewer's link: open its data
        if link_endpoint and not req.endpoint:
            req = req.model_copy(
                update={
                    "endpoint": link_endpoint,
                    "anon": req.anon if req.anon is not None else True,
                }
            )
        remote = loc.startswith(("http://", "https://", "s3://", "gs://"))
        if remote and not settings.allow_open:
            raise HTTPException(403, "opening URLs is off: start the server with --allow-open")
        if not remote:
            roots = [Path(r).expanduser().resolve() for r in settings.open_roots]
            if looks_like_address(loc):
                raise HTTPException(
                    400,
                    f"{loc!r} looks like a web address: start it with https:// (or s3:// for a "
                    "bucket, with its endpoint in Browse S3 if it is not on AWS)",
                )
            if not roots:
                log.info("refused local path %r (no --open-root)", loc)
                raise HTTPException(
                    403,
                    f"{loc!r} is not a URL, and opening local files is off: give a URL starting "
                    "with https:// or s3://, or start the server with --open-root DIR",
                )
            p = Path(loc).expanduser()
            p = (p if p.is_absolute() else roots[0] / p).resolve()
            if not any(p == r or p.is_relative_to(r) for r in roots):
                raise HTTPException(
                    403, f"local files must be inside {', '.join(str(r) for r in roots)}"
                )
            if not p.exists():
                raise HTTPException(404, f"not found: {p}")
            loc = str(p)
        from geoloupe.browse import access_options, normalise

        if remote and loc.startswith("s3://"):
            loc = normalise(loc)
        so = access_options(req.endpoint, req.profile, req.anon, req.region)
        if so and not loc.startswith("s3://"):
            raise HTTPException(400, "endpoint, profile and access apply to s3:// locations only")
        raw = config_for_paths([loc], req.engine, storage_options=so)
        out: dict[str, list[str]] = {"layers": [], "vectors": [], "errors": []}
        with open_lock:
            cfg = catalog.config
            taken = set(cfg.datasets) | set(cfg.points) | set(cfg.vectors)

            def fresh(name: str) -> str:
                base = re.sub(r"[^A-Za-z0-9_-]+", "-", req.name or name).strip("-") or "data"
                new, k = base, 2
                while new in taken:
                    new, k = f"{base}-{k}", k + 1
                taken.add(new)
                return new

            try:
                if raw["datasets"]:
                    found = {
                        fresh(k): DatasetConfig.model_validate(v)
                        for k, v in raw["datasets"].items()
                    }
                    layers, errors = catalog.add_datasets(found)
                    out["layers"] += layers
                    out["errors"] += errors
                for k, v in raw["points"].items():
                    pid, pcfg = fresh(k), PointsConfig.model_validate(v)
                    cfg.points[pid] = pcfg
                    if registry.add(pid, pcfg, cfg, set(catalog.layers)):
                        out["layers"] += [
                            lid for lid, (src, _) in registry.layer_ids().items() if src == pid
                        ]
                    else:
                        out["errors"].append(f"{pid}: {registry.errors.pop(pid)}")
                        del cfg.points[pid]
                for k, v in raw["vectors"].items():
                    vid = fresh(k)
                    try:
                        vectors.add(vid, VectorConfig.model_validate(v))
                        out["vectors"].append(vid)
                    except VectorError as e:
                        out["errors"].append(f"{vid}: {e}")
                        cfg.vectors.pop(vid, None)
                        vectors.errors.pop(vid, None)
            except ValueError as e:  # an invalid option (e.g. engine)
                raise HTTPException(400, str(e)) from None
        if not out["layers"] and not out["vectors"]:
            raise HTTPException(400, "; ".join(out["errors"]) or "nothing to show in this file")
        log.info("opened %s: %s", loc, out)
        return out

    @app.get("/api/browse")
    def browse(
        url: str,
        endpoint: str | None = None,
        profile: str | None = None,
        anon: bool | None = None,
        region: str | None = None,
        match: str | None = None,
    ) -> dict[str, Any]:
        """List an s3:// bucket or prefix for "Add data" (needs `allow_open`)."""
        from geoloupe.browse import BrowseError, access_options, list_location

        if not settings.allow_open:
            raise HTTPException(403, "browsing buckets is off: start the server with --allow-open")
        try:
            return list_location(url, access_options(endpoint, profile, anon, region), match)
        except BrowseError as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/api/s3/profiles")
    def s3_profiles() -> dict[str, list[str]]:
        """Names of the AWS profiles on the server (no secrets), for "Add data"."""
        from geoloupe.browse import profiles

        if not settings.allow_open:
            raise HTTPException(403, "opening URLs is off: start the server with --allow-open")
        return {"profiles": profiles()}

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
        headers = {**headers, "X-GeoLoupe-Time-Ms": f"{(_time.perf_counter() - t0) * 1000:.1f}"}
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
        layer = get_layer(layer_id)
        sel = selection(layer, time, dims)
        nb = 3 if layer.kind == "rgb" else 1

        def render(tile: Tile) -> list[np.ndarray]:
            res = engine.render(layer, tile, sel)
            return res.bands if res.bands is not None else [res.data]

        crop = stitch(render, center, zoom, width, height, nb)
        bands = list(crop) if nb == 3 else None
        return png_response(
            colour(layer, crop[0], bands, sel, colormap, vmin, vmax, scale == "log")
        )

    def stitch(
        render: Any, center: str, zoom: int, width: int, height: int, nb: int = 1
    ) -> np.ndarray:
        """(nb, height, width) values of a Web Mercator image centred on a point, stitched from
        the tiles of zoom `zoom` (at most 5 × 5)."""
        try:
            lon, lat = (float(v) for v in center.split(","))
        except ValueError:
            raise HTTPException(400, "center must be 'lon,lat'") from None
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
        canvas = np.full((nb, len(ty) * ts, len(tx) * ts), np.nan, np.float32)
        for j, y in enumerate(ty):
            if not 0 <= y < n:
                continue  # beyond the poles of the map: empty
            for i, x in enumerate(tx):
                planes = render(Tile(zoom, x % n, y, ts))  # wraps in longitude
                for b, plane in enumerate(planes[:nb]):
                    canvas[b, j * ts : (j + 1) * ts, i * ts : (i + 1) * ts] = plane
        ox, oy = x0 - tx[0] * ts, y0 - ty[0] * ts
        return canvas[:, oy : oy + height, ox : ox + width]

    @app.get("/api/error/preview.png")
    def error_preview(
        p: str,
        t: str,
        center: str,
        zoom: Annotated[int, Query(ge=0, le=22)],
        width: Annotated[int, Query(ge=16, le=1024)] = 480,
        height: Annotated[int, Query(ge=16, le=1024)] = 240,
        pt: str | None = None,
        tt: str | None = None,
        pd: str | None = None,
        td: str | None = None,
        metric: str = "bias",
        formula: str | None = None,
        params: str | None = None,
        units: str | None = None,
        colormap: str | None = None,
        vmin: float | None = None,
        vmax: float | None = None,
    ) -> Response:
        """A preview image of an error map (gallery thumbnails of error panes); without a range,
        the default colours of the error definition, fitted to the image."""
        q = error_query(p, t, pt, tt, pd, td, metric, formula, params, units)

        def render(tile: Tile) -> list[np.ndarray]:
            parts = error_parts(engine, q, tile)
            return [parts.values() if parts is not None else np.full((tile.size,) * 2, np.nan)]

        data = stitch(render, center, zoom, width, height)[0]
        if q.spec.classes:
            classes: dict[int, dict[str, str | None]] = {
                int(c["value"]): {"name": c["name"], "color": c["color"]} for c in q.spec.classes
            }
            return png_response(apply_classes(data, classes, "turbo"))
        lo, hi, log_scale, cmap = error_range(q.spec.scale, data)
        return png_response(
            apply_colormap(
                data,
                colormap or cmap,
                vmin if vmin is not None else lo,
                vmax if vmax is not None else hi,
                log_scale,
            )
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

    # ------------------------------------------------------------ error maps
    def error_query(
        p: str,
        t: str,
        pt: str | None,
        tt: str | None,
        pd: str | None,
        td: str | None,
        metric: str,
        formula: str | None,
        params: str | None,
        units: str | None,
    ) -> ErrorQuery:
        """The error of layer p (time pt, dims pd) against layer t (time tt, dims td)."""
        P, T = get_layer(p), get_layer(t)
        values: dict[str, float] = {}
        for k, v in parse_dims(params).items():
            try:
                values[k] = float(v)
            except ValueError:
                raise ErrorQueryError(f"parameter {k!r} must be a number") from None
        try:
            spec = resolve_error(metric, formula, values, units)
        except ValueError as e:  # includes FormulaError
            raise ErrorQueryError(str(e)) from None
        return ErrorQuery(
            P,
            T,
            selection(P, pt, pd),
            selection(T, tt, td),
            spec,
            unit_conversion(P.units, T.units),
        )

    @app.get("/api/error/tiles/{z}/{x}/{y}.bin")
    def error_tile(
        request: Request,
        z: int,
        x: int,
        y: int,
        p: str,
        t: str,
        pt: str | None = None,
        tt: str | None = None,
        pd: str | None = None,
        td: str | None = None,
        metric: str = "bias",
        formula: str | None = None,
        params: str | None = None,
        units: str | None = None,
    ) -> Response:
        """Error tile of P against T, computed from native cells (see geoloupe/errortiles.py)."""
        q = error_query(p, t, pt, tt, pd, td, metric, formula, params, units)
        t0 = _time.perf_counter()
        tile = Tile(z, x, y, settings.tile_size)
        # a precomputed error pyramid (geoloupe errors build) is exact at every zoom
        pre = find_precomputed(
            catalog, error_key(q.p.id, q.t.id, q.spec, parse_dims(pd), parse_dims(td))
        )
        if (
            pre is not None
            and q.p_sel.time == q.t_sel.time
            and (not pre.times or q.t_sel.time in pre.times)
        ):
            body, headers = engine.tile_bytes(pre, tile, Selection.make(q.t_sel.time))
            headers = {
                "X-GeoLoupe-Exact": "1",
                "X-GeoLoupe-Precomputed": pre.id,
                "X-GeoLoupe-Cell-M": f"{pre.sources[0].native.grid.cell_size_m():.1f}",
            }
        else:
            body, headers = error_tile_bytes(engine, q, tile)
        headers = {**headers, "X-GeoLoupe-Time-Ms": f"{(_time.perf_counter() - t0) * 1000:.1f}"}
        headers["Cache-Control"] = "public, max-age=3600"
        if body is None:
            return Response(status_code=204, headers=headers)
        if "gzip" in request.headers.get("accept-encoding", ""):
            headers["Content-Encoding"] = "gzip"
        else:
            import gzip

            body = gzip.decompress(body)
        return Response(body, media_type="application/octet-stream", headers=headers)

    @app.get("/api/error/stats")
    def get_error_stats(
        p: str,
        t: str,
        bbox: str,
        z: int,
        pt: str | None = None,
        tt: str | None = None,
        pd: str | None = None,
        td: str | None = None,
        metric: str = "bias",
        formula: str | None = None,
        params: str | None = None,
        units: str | None = None,
    ) -> dict[str, Any]:
        """Error statistics over the native cells in bbox (mean, mean |e|, RMS, min, max; class
        counts and event scores for class maps)."""
        q = error_query(p, t, pt, tt, pd, td, metric, formula, params, units)
        box = parse_bbox(bbox)
        assert box is not None
        return error_stats(engine, q, box, z)

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
            "<h1>GeoLoupe API is running</h1><p>The web client is not built. Run "
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
