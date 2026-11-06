# Benchmarks

Measured on 2026-11-05/06 on one HPC compute node (288 Arm cores, 856 GB RAM). Local data on a shared,
busy Lustre file system: cold numbers vary by a factor of 2–5 from run to run. Reproduce with:

```bash
uv run python scripts/benchmark.py my-config.yaml --layer tas --time 2021-07-15 --lon 8.5 --lat 46.5
```

## Data

| Dataset | Grid | Time | Storage |
|---|---|---|---|
| A 0.25° daily reanalysis (private copy) | 721 × 1440, float32 | 13 879 days | one chunk per day and variable (whole globe) |
| A 30″ ≈ 1 km daily climate dataset (private) | 20 880 × 43 200, uint16 + scale_factor | 13 864 days | sharded Zarr v3: one shard per day, 720 × 720 inner chunks |
| Its overviews (TerraScope) | factors 4 … 128, uint16 | 365 days, two variables | 12 GB, 512 × 512 chunks, Blosc zstd + shuffle (ratio ≈ 9) |
| Sentinel-2 L2A scene (public, AWS, over HTTPS) | 10 980 × 10 980 COGs, UTM 32N, internal overviews 2–16× | single date | 1024 × 1024 blocks |
| ARCO-ERA5 (public, Google Cloud, over HTTPS) | 721 × 1440 | 1 323 648 hourly steps | Zarr v3, one chunk per hour |

## Tiles (composite layer = the 1 km dataset over the reanalysis, 256 px tiles)

Cold = first request for that tile in a running server (data not in the tile cache; file-system cache
may be warm). Warm = served from the tile cache.

| Tile zoom | With overviews | Level used | Without overviews | Level used |
|---|---|---|---|---|
| 0 | 396 ms (first request of the process) | 1 km data at 64× + reanalysis north of 84° N | 324 ms | reanalysis (1 km data skipped: cost) |
| 2 | 33 ms | 16× | 11 ms | reanalysis (1 km data skipped: cost) |
| 3 | 20 ms | 8× | – | – |
| 4 | 14 ms | 4× | 95 ms | native (5.8 M cells read) |
| 5 | 76 ms | native | – | – |
| 6 | 14 ms | native | 11 ms | native |
| 8–12 | 6–7 ms | native | – | – |
| cached | 3–20 ms (incl. gzip of the response) | | | |

Transfer sizes (gzip): native 1 km tile at zoom 8: 35 kB; low-zoom tiles (block means): ~100 kB;
reanalysis tile at zoom 5 (upsampled): 10 kB; PNG of the zoom-8 tile for comparison: 29 kB.

**Remote public data** (over the internet from the compute node):

| Request | Time |
|---|---|
| Sentinel-2 COG tile, cold (overview level chosen per zoom: 160 m at z8 … 10 m at z14) | 0.3–0.9 s |
| 50 Sentinel-2 tiles requested in parallel | 6.3 s (results identical to serial reads) |
| ARCO-ERA5: open the store (1.3 million time steps) | 14 s |
| ARCO-ERA5 tile (one hourly field) | 1.0–1.5 s |
| ARCO-ERA5 layer metadata (compact time axis) | 1 kB, 0.1 s (a full label list would be 25 MB) |

In the browser (headless Chromium, software WebGL), a full view (≈ 20 tiles) is complete in 3–4 s
including page load; panning at native zoom is limited by network round trips, not by the server.

## Queries

| Request | Time |
|---|---|
| Open a catalog with the two large stores + overviews | 1.2–1.7 s (warm file system); 10 s cold |
| Point value, all sources, native cells | 9–10 ms |
| Time series, 352 days, two sources at one point, cold | 4.7 s (6.3 s before parallel block reads) |
| Same, warm file-system cache | 0.25–0.45 s per source |
| Layer statistics (colour range), first / cached | 7.5 s in a fresh process (lazy imports, cold files); 30–80 ms after the start-up warm-up; < 1 ms cached |
| Station archive (Parquet, 38 yearly partitions, ~1.4 billion rows): one day for one variable | 0.16 s (7 052 stations) |
| Same archive: layer metadata (time axis from Parquet statistics) | 1.1 s |
| Python imports + catalog in a fresh process on a Lustre-hosted environment | up to 35 s + 10 s when the file system is busy |

The cold-start cost is why the server warms up its main layers in a background thread right after it
starts.

## Public data over the internet (the gallery)

`terrascope gallery` on the same node, all data read from public buckets over HTTPS (AWS eu-central-1
and us-west-2, Google Cloud, NOAA). Network latency dominates; numbers vary between runs.

| Step | Time |
|---|---|
| Open all datasets (ERA5 Zarr, 6 DEM + 4 Sentinel-2 + 1 WorldCover COGs, opened in parallel) | 4.9 s in a quiet run; 8–14 s per remote store in a slow one |
| Load 21 GHCN-Daily station CSVs (1 million rows; 9 of ~40 columns kept) | 3.9 s, 109 MB in memory |
| Open ARCO-ERA5 (6 variables, 761 160 hourly steps) — before / after vectorised time labels and one shared time axis per store | 15.3 s / 0.9 s |
| 16 cold false-colour tiles at zoom 11 (3 remote COGs each), requested in parallel — with rioxarray's global read lock / without | 13.3 s / 2.0 s |
| Vector overlays (Natural Earth 1:10m GeoJSON from GitHub): 4 596 provinces simplified to 0.02° / 1 454 rivers / 1 355 lakes | 3.3–7.3 s (6.2 MB) / 0.4–1.1 s (6.0 MB) / 0.4 s (3.8 MB) |
| 11 gallery previews (480 × 240 px, stitched from up to 9 tiles each) | 6.6–7.4 s for all, in the browser |
| Open a featured view until all tiles are loaded (browser) | 2.3–4.6 s; 7.2 s for the side-by-side Sentinel-2 view |

## Overview builds

| Build | Time | Output |
|---|---|---|
| 1 km daily dataset, two variables, 365 days, `--min-factor 4`, `--workers 32` | 18.5 min (≈ 25 s per day and variable per worker; read-bound) | 12 GB; values within half a packing step of a direct block mean |
| Demo data (`terrascope demo-data`, 365 days, incl. overviews) | 1.5 min | 350 MB |

## Tests

| Suite | Count | Time |
|---|---|---|
| Python (`pytest`): engine vs brute force, compositing, levels, budgets, CRSs, calendars, overviews, readers, API, stations, satellite rasters, classes, gallery, vectors, regressions | 121 | 7–90 s (file-system dependent) |
| Web client (`vitest`): tile decoding (byte layout shared with Python), colour mapping, RGB, classes, Mercator math, URL state, timelines, gallery links, comparison scales, overlays | 28 | < 2 s |
| Browser smoke test, demo data (`e2e/smoke.mjs`): global, Alps 1 km + hover, inspector, split, difference, LAEA grid, station hover + click | 7 scenarios | ~35 s |
| Browser smoke tests on public remote data (Sentinel-2 true and false colour, ARCO-ERA5, NOAA GHCN stations) | 4 scenarios | ~60 s |
| Gallery test (`e2e/gallery.mjs`): start screen and previews, then every featured view from its card | 11 views | ~60 s |
