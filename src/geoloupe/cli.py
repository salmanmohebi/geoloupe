"""Command line: ``geoloupe serve | gallery | info | demo-data | overviews build``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from geoloupe import __version__
from geoloupe.certs import use_system_certificates

log = logging.getLogger("geoloupe")


def config_for_paths(
    targets: list[str], engine: str = "auto", storage_options: dict | None = None
) -> dict:
    """A configuration for data paths given on the command line.

    Vector files (GeoJSON, Shapefile, GeoPackage, …) become overlays; tables (CSV, Parquet) and CF
    timeSeries NetCDF become point datasets; everything else gridded datasets. Names come from the
    file names. `storage_options` (S3 endpoint, profile, anonymous access) go to each entry.
    """
    extra = {"storage_options": dict(storage_options)} if storage_options else {}
    from geoloupe.points import looks_like_points
    from geoloupe.vectors import looks_like_vector

    raw: dict = {"datasets": {}, "points": {}, "vectors": {}}
    used: set[str] = set()
    for t in targets:
        base = Path(t.rstrip("/")).name.split(".")[0].replace(":", "-") or "data"
        name, k = base, 2
        while name in used:
            name, k = f"{base}-{k}", k + 1
        used.add(name)
        if looks_like_vector(t):
            raw["vectors"][name] = {"path": t, "visible": True, **extra}
        elif looks_like_points(t):
            raw["points"][name] = {"path": t, **extra}
        else:
            raw["datasets"][name] = {"path": t, "engine": engine, **extra}
    names = ", ".join(Path(t).name for t in targets[:3]) + (" …" if len(targets) > 3 else "")
    raw["title"] = f"GeoLoupe · {names}"
    return raw


def _catalog_from(targets: list[str], engine: str = "auto"):
    """A catalog from a YAML config or directly from data paths."""
    from geoloupe.catalog import Catalog
    from geoloupe.config import config_from_dict, load_config

    if len(targets) == 1 and targets[0].endswith((".yaml", ".yml")):
        return Catalog(load_config(targets[0]))
    if any(t.endswith((".yaml", ".yml")) for t in targets):
        raise ValueError("give either one configuration file or data paths, not both")
    return Catalog(config_from_dict(config_for_paths(targets, engine), base_dir=Path.cwd()))


def cmd_serve(a: argparse.Namespace) -> int:
    from geoloupe.server import serve

    cat = _catalog_from(a.target, a.engine)
    if getattr(a, "allow_open", False):
        cat.settings.allow_open = True
    for root in getattr(a, "open_root", None) or []:
        cat.settings.open_roots.append(str(Path(root).expanduser().resolve()))
    if cat.settings.allow_open or cat.settings.open_roots:
        log.warning(
            "'Add data' is on (%s): anyone who can reach this server can make it read those; "
            "keep it on localhost or behind a login",
            ", ".join(["remote URLs"] * cat.settings.allow_open + cat.settings.open_roots),
        )
    bad = [d for d in cat.datasets.values() if d.status != "ok"]
    for d in bad:
        log.error("dataset %s: %s", d.id, d.error)
    if not cat.layers and not cat.config.points:
        log.error("nothing to serve: no dataset could be opened")
        return 1
    host = a.host
    print(
        f"\n  GeoLoupe {__version__}: {len(cat.layers)} gridded layers from "
        f"{len(cat.datasets) - len(bad)} datasets, {len(cat.config.points)} point datasets"
    )
    print(
        f"  Open http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{a.port}/  (API docs: /api/docs)"
    )
    if host == "127.0.0.1":
        print(
            f"  Remote machine? Tunnel first: ssh -N -L {a.port}:localhost:{a.port} <this host>\n"
        )
    serve(cat, host=host, port=a.port, log_level=a.log_level)
    return 0


GALLERY = Path(__file__).with_name("gallery.yaml")


def cmd_gallery(a: argparse.Namespace) -> int:
    """Serve the gallery of open public datasets that ships with GeoLoupe."""
    if a.show_config:
        sys.stdout.write(GALLERY.read_text())
        return 0
    a.target, a.engine = [str(GALLERY)], "auto"
    return cmd_serve(a)


def cmd_info(a: argparse.Namespace) -> int:
    cat = _catalog_from(a.target, a.engine)
    if a.json:
        from geoloupe.api.app import create_app, layer_json

        app = create_app(cat)
        out = {lid: layer_json(app.state.engine, lyr) for lid, lyr in cat.layers.items()}
        for lj in out.values():
            lj["times"] = (
                [lj["times"][0], "...", lj["times"][-1]] if len(lj["times"]) > 3 else lj["times"]
            )
        print(json.dumps(out, indent=2, default=str))
        return 0
    for d in cat.datasets.values():
        if d.status != "ok":
            print(f"✗ {d.id}: {d.error}")
            continue
        print(f"✓ {d.id}  ({d.title}; opened in {d.open_seconds:.2f} s)")
        for s in d.sources.values():
            info = s.describe()
            t = (
                f"{info['time_steps']} × {info['time_step']} [{info['time_range'][0]} … {info['time_range'][1]}]"
                if info["time_steps"]
                else "static"
            )
            dims = ", ".join(f"{k}={len(v)}" for k, v in s.extra_dims.items())
            lv = " → ".join(x["resolution"].split(" (")[0] for x in info["levels"])
            print(
                f"    {s.variable:<16} {info['units'] or '':<10} {info['crs']:<10} {info['resolution']:<18} levels: {lv}"
            )
            print(f"    {'':<16} time: {t}{'; ' + dims if dims else ''}")
    composites = [lyr for lyr in cat.layers.values() if lyr.kind == "composite"]
    if composites:
        print("\ncomposite layers:")
        for lyr in composites:
            print(f"    {lyr.id:<16} {' + '.join(s.id for s in lyr.sources)}")
    ok = all(d.status == "ok" for d in cat.datasets.values())
    if cat.config.points:
        from geoloupe.points import PointRegistry

        reg = PointRegistry(cat.config, set(cat.layers))
        for pid in cat.config.points:
            if pid in reg.errors:
                print(f"✗ {pid}: {reg.errors[pid]}")
                ok = False
                continue
            src = reg.sources[pid]
            times = src.times()
            w, so, e, n = src.bounds()
            print(f"✓ {pid}  (points: {src.title})")
            print(
                f"    variables: {', '.join(v.name + (f' [{v.units}]' if v.units else '') for v in src.variables())}"
            )
            print(
                f"    time: {len(times)} [{times[0]} … {times[-1]}]; extent {w:.2f}…{e:.2f} E, {so:.2f}…{n:.2f} N"
            )
    if cat.config.vectors:
        from geoloupe.vectors import VectorError, VectorRegistry

        vreg = VectorRegistry(cat.config)
        for vid in cat.config.vectors:
            try:
                v = vreg.get(vid)
            except VectorError as e:
                print(f"✗ {vid}: {e}")
                ok = False
                continue
            w, so, e_, n = v.bounds
            print(f"✓ {vid}  (vectors: {cat.config.vectors[vid].title or vid})")
            print(
                f"    {v.count} features ({', '.join(v.geometry_types)}), "
                f"{len(v.geojson) / 1e6:.1f} MB GeoJSON; label: {v.label}; "
                f"extent {w:.2f}…{e_:.2f} E, {so:.2f}…{n:.2f} N"
            )
    return 0 if ok else 2


def cmd_demo(a: argparse.Namespace) -> int:
    from geoloupe.demo import write_demo

    cfg = write_demo(a.directory, days=a.days, overwrite=a.overwrite)
    print(f"Demo data written. Start the server with:\n\n    geoloupe serve {cfg}\n")
    return 0


def cmd_overviews(a: argparse.Namespace) -> int:
    from geoloupe.overviews import build

    res = build(
        a.source,
        a.output,
        variables=a.variables.split(",") if a.variables else None,
        start=a.start,
        end=a.end,
        min_factor=a.min_factor,
        method=a.method,
        workers=a.workers,
        engine=a.engine,
        overwrite=a.overwrite,
    )
    print(
        f"Wrote factors {res['levels']} for {res['time_steps']} time steps in {res['seconds']:.1f} s.\n"
        f"Add to the dataset config:  overviews: {a.output}"
    )
    return 0


def _open_args(s: argparse.ArgumentParser) -> None:
    s.add_argument(
        "--allow-open",
        action="store_true",
        help="let the browser open remote data (https://, s3://, gs:// URLs) with 'Add data'",
    )
    s.add_argument(
        "--open-root",
        action="append",
        metavar="DIR",
        help="let the browser open local files below DIR with 'Add data' (repeatable)",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="geoloupe", description="Explore large gridded datasets on a map."
    )
    p.add_argument("--version", action="version", version=f"geoloupe {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="start the web server")
    s.add_argument(
        "target",
        nargs="+",
        help="a config file (.yaml), or data paths: Zarr, NetCDF, GRIB, GeoTIFF, CSV/Parquet stations",
    )
    s.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to listen on all interfaces")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument(
        "--engine", default="auto", help="reader for a dataset path (default: from the extension)"
    )
    s.add_argument("--log-level", default="info")
    _open_args(s)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser(
        "gallery", help="explore open public data (ERA5, Sentinel-2, DEM, land cover, stations)"
    )
    s.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to listen on all interfaces")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--log-level", default="info")
    s.add_argument(
        "--show-config", action="store_true", help="print the gallery configuration and exit"
    )
    _open_args(s)
    s.set_defaults(func=cmd_gallery)

    s = sub.add_parser("info", help="check a config or dataset and list its layers")
    s.add_argument("target", nargs="+", help="a config file (.yaml) or data paths")
    s.add_argument("--engine", default="auto")
    s.add_argument("--json", action="store_true", help="print the full layer metadata as JSON")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("demo-data", help="write synthetic demo datasets and a config")
    s.add_argument("directory")
    s.add_argument("--days", type=int, default=365)
    s.add_argument("--overwrite", action="store_true")
    s.set_defaults(func=cmd_demo)

    s = sub.add_parser("overviews", help="overview pyramids")
    osub = s.add_subparsers(dest="ocmd", required=True)
    b = osub.add_parser("build", help="build external overviews of a dataset")
    b.add_argument("source")
    b.add_argument("output", help="new Zarr store, e.g. data.overviews.zarr")
    b.add_argument("--variables", help="comma-separated (default: all gridded variables)")
    b.add_argument("--start", help="first time (ISO 8601)")
    b.add_argument("--end", help="last time (ISO 8601)")
    b.add_argument(
        "--min-factor", type=int, default=2, help="finest overview factor (power of 2; default 2)"
    )
    b.add_argument("--method", default="mean", choices=["mean", "nearest", "min", "max", "mode"])
    b.add_argument("--workers", type=int, default=1, help="parallel processes (one time step each)")
    b.add_argument("--engine", default="auto")
    b.add_argument("--overwrite", action="store_true")
    b.set_defaults(func=cmd_overviews)

    a = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not a.verbose:  # rasterio logs every remote file open at INFO
        logging.getLogger("rasterio").setLevel(logging.WARNING)
    # Before any HTTPS connection (and before aiohttp is imported): see geoloupe.certs.
    log.debug("HTTPS certificates: %s", use_system_certificates())
    try:
        return int(a.func(a))
    except (FileExistsError, ValueError, OSError) as e:
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
