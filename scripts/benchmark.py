"""Time tiles, point queries and time series for a config (cold and warm cache).

    uv run python scripts/benchmark.py my-config.yaml --layer tas --time 2021-07-15 \
        --lon 8.5 --lat 46.5

Prints one line per request: what was read (source, level) and how long it took.
"""

from __future__ import annotations

import argparse
import statistics
import time

from terrascope.catalog import Catalog
from terrascope.config import load_config
from terrascope.engine import Selection, TileEngine
from terrascope.query import point, timeseries
from terrascope.tiling import Tile, lonlat_to_tile


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--layer", required=True)
    ap.add_argument("--time")
    ap.add_argument("--lon", type=float, default=8.5)
    ap.add_argument("--lat", type=float, default=46.5)
    ap.add_argument("--zooms", default="0,2,3,4,5,6,8,10,12")
    ap.add_argument("--series-days", type=int, default=365)
    a = ap.parse_args()

    t0 = time.perf_counter()
    cat = Catalog(load_config(a.config))
    print(f"catalog opened in {time.perf_counter() - t0:.2f} s")
    for d in cat.datasets.values():
        print(f"  {d.id}: {d.status} {d.error or ''} ({d.open_seconds:.2f} s)")
    eng = TileEngine(cat)
    layer = cat.layer(a.layer)
    t = a.time or layer.times[-1]
    sel = Selection.make(t)
    for s in layer.sources:
        print(
            f"  source {s.id}: levels {[lv.grid.resolution_label() for lv in s.levels]}; "
            f"min tile zoom {eng.min_zoom(s):.1f}"
        )
    print(f"\n{'zoom':>4} {'tiles':>5} {'cold ms/tile':>13} {'warm ms':>8}  contributions")
    for z in (int(v) for v in a.zooms.split(",")):
        x, y = lonlat_to_tile(a.lon, a.lat, z)
        tiles = [
            Tile(z, (x + dx) % (1 << z), min(max(y + dy, 0), (1 << z) - 1))
            for dx in (0, 1)
            for dy in (0, 1)
        ]
        tiles = list(dict.fromkeys(tiles))
        times, desc = [], ""
        for tile in tiles:
            t1 = time.perf_counter()
            res = eng.render(layer, tile, sel)
            times.append((time.perf_counter() - t1) * 1000)
            if tile == tiles[0]:
                desc = ", ".join(
                    f"{layer.sources[c.source_index].dataset_id}@L{c.level_index}:{c.pixels}"
                    for c in res.contributions
                ) + (
                    "; skipped "
                    + ",".join(f"{layer.sources[k].dataset_id}={r}" for k, r in res.skipped)
                    if res.skipped
                    else ""
                )
        t2 = time.perf_counter()
        eng.tile_bytes(layer, tiles[0], sel)
        eng.tile_bytes(layer, tiles[0], sel)
        warm = (time.perf_counter() - t2) * 1000 / 2
        print(f"{z:>4} {len(tiles):>5} {statistics.median(times):>13.0f} {warm:>8.1f}  {desc}")
    t1 = time.perf_counter()
    p = point(layer, a.lon, a.lat, sel)
    print(
        f"\npoint: {p['value']} {layer.units} from source {p['source']} in {(time.perf_counter() - t1) * 1000:.0f} ms"
    )
    if layer.times:
        i = layer.times.index(t) if t in layer.times else len(layer.times) - 1
        lo = layer.times[max(0, i - a.series_days // 2)]
        hi = layer.times[min(len(layer.times) - 1, i + a.series_days // 2)]
        t1 = time.perf_counter()
        ts = timeseries(layer, a.lon, a.lat, Selection.make(None), lo, hi, 100_000)
        n = [len(s["times"]) for s in ts["series"]]
        print(f"time series {lo}..{hi}: {n} steps in {(time.perf_counter() - t1):.2f} s")


if __name__ == "__main__":
    main()
