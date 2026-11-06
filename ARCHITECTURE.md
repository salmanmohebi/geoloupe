# GeoLoupe architecture

This document describes how GeoLoupe works, why it is built this way, and which alternatives were
considered. The survey of existing tools is in [docs/research.md](docs/research.md); the individual
decisions are logged in [docs/decisions.md](docs/decisions.md).

## 1. Goals and constraints

GeoLoupe should let a researcher open large gridded datasets and explore them like a web map:
pan, zoom, step through time, read values, compare datasets. The hard requirements are:

1. **Seamless multi-resolution.** A coarse global dataset and a fine regional one (or several) must
   form one map: coarse when zoomed out, fine where it exists when zoomed in, without visible seams
   and without the user switching layers.
2. **Never load what is not visible.** A 20 880 × 43 200 × 13 864 grid (a 30″ ≈ 1 km daily climate dataset) cannot be
   read for a zoomed-out view. Every request must have a bounded cost.
3. **Scientific honesty.** The user must be able to tell what they see: which dataset, at which
   resolution, aggregated or native. Clicked values must be the stored values.
4. **Formats as they are.** Data stays where and how it is (Zarr, NetCDF, GRIB, GeoTIFF, local or
   object storage). Preprocessing is optional and only adds speed.
5. **Simple to run** on a laptop, a workstation, or an HPC compute node behind an SSH tunnel or
   JupyterHub proxy.

## 2. Overview

```
             Browser                                        Python server (FastAPI)
┌─────────────────────────────────┐        ┌─────────────────────────────────────────────────────┐
│ React UI (panels, time, charts, │        │ API  /api/layers/{id}/tiles/{z}/{x}/{y}.bin  .png    │
│  gallery) · state (zustand, URL)│  HTTP  │      /preview.png /point /timeseries /stats /catalog  │
│ MapLibre GL JS v6               │◄──────►│                     │                               │
│   raster source "geoloupe://" │ float  │ TileEngine: per tile and per source                   │
│        │                        │ tiles  │   level choice → read budget → resample → composite  │
│ protocol handler:               │        │                     │                               │
│   fetch float tile → cache      │        │ Catalog: datasets → sources (levels) → layers         │
│   → colormap (CPU) → ImageBitmap│        │                     │                               │
│ hover values from the cache     │        │ Readers: xarray (zarr, netcdf, grib, geotiff, refs)   │
└─────────────────────────────────┘        └─────────────────────┬───────────────────────────────┘
                                                                  │ lazy window reads
                                      ┌───────────────────────────┴──────────────────────────┐
                                      │ native datasets (never modified) + external overviews │
                                      │ local disk, Lustre, S3/GCS/HTTP (fsspec)              │
                                      └──────────────────────────────────────────────────────┘
```

Four ideas carry the design:

| Idea | What it gives |
|---|---|
| **Float data tiles, coloured in the browser** | Colormap and range changes are instant (no new requests); hover shows the value of the displayed pixel; difference maps are computed in the browser from two cached tiles. |
| **Per-tile level choice with a read budget** | Every tile reads at most `max_read_cells` grid cells. The engine picks the coarsest level that is still as fine as a screen pixel; if even that is too expensive, the source is skipped at this zoom and the UI says so. |
| **Per-pixel compositing of several datasets** | A layer can stack sources of different resolution. Per pixel, the finest source with data wins; coarser sources fill the rest. This is the "Google Maps" behaviour, and no existing server does it across datasets. |
| **External overviews in the native grid** | Optional pyramids (block means at 2×, 4×, … coarser) stored next to, not inside, the source. Values stay traceable to source cells; one pyramid serves any display projection. |

## 3. Data model

```
Config (YAML)          Catalog (runtime)
─────────────          ─────────────────────────────────────────────────────────────
datasets:      ──►     DatasetEntry  (status ok/error, attrs)
  path / levels          └─ Source  = one variable of one dataset
  overviews                    ├─ levels: [Level, …]  finest first
  styles                       │    Level = lazy DataArray + Grid + TimeAxis + origin
                               ├─ extra_dims (e.g. plev, member)
layers:        ──►     Layer = [Source, …] in priority order + style + union of times
  sources                (every gridded variable is also its own layer "<dataset>:<variable>")
```

* **Grid** ([grid.py](src/geoloupe/grid.py)) describes a rectilinear grid: 1-D cell centres for x and
  y and a CRS. It finds the spatial dimensions (CF `axis`, `standard_name`, common names), the CRS (config
  override, CF `grid_mapping` with `crs_wkt`/`spatial_ref` or CF parameters, or lat/lon coordinates),
  and turns coordinates into *fractional cell positions* (regular axes by arithmetic, irregular ones such
  as Gaussian latitudes by binary search). Longitudes wrap: global grids in 0…360 or −180…180 both
  work, and so do regional grids that cross the antimeridian.
* **TimeAxis** ([timeaxis.py](src/geoloupe/timeaxis.py)) labels times in ISO 8601 and matches
  requests to the nearest time within a tolerance (default: half a step for monthly or coarser data, at
  most 12 h otherwise, so sparsely sampled daily data never show a neighbouring day). It works in each axis'
  own calendar, so cftime calendars (`noleap`, `360_day`) from climate models are supported.
* **Levels** come from (a) the dataset itself, if it is a multiscale Zarr group, (b) an explicit list
  of copies at other resolutions (`levels:`), or (c) an external overview store (`overviews:`). Each
  level has its own time axis: overviews may exist for only part of the period, and the engine then
  falls back to finer levels for the other times.

### Point observations

Point sources ([points.py](src/geoloupe/points.py)) are tables (CSV, Parquet, GeoParquet; long or wide,
columns detected) or CF timeSeries NetCDF/Zarr. Every variable is a point layer `<source>:<variable>`, and
any point layer can be drawn over a gridded layer. For a layer and a day, the server returns the
stations in a compact JSON list, read from Parquet with filter push-down (only that day; hive `year=`
partitions pruned) and cached per day; time axes of very large tables come from Parquet statistics. The browser draws them as
circles with the layer's colour scale (a MapLibre `interpolate` expression built from the same lookup
table), so observation and grid are compared by eye; hover adds the numbers and their difference.

## 4. A tile request, step by step

`GET /api/layers/tas/tiles/7/67/45.bin?time=1978-07-15`

1. **Cache.** The engine's LRU cache (bytes-bounded) is keyed by (layer, tile, time, dims). Hits return
   immediately.
2. **For each source of the layer, in priority order** (finest first by default):
   1. *Zoom window and time.* Sources outside their configured zoom window, or without data at this
      time (within the tolerance), are skipped and the reason is recorded.
   2. *Level choice.* The screen pixel size is computed in the source's CRS units (lat/lon grids:
      degrees in x and y; projected grids: a pyproj transform at the tile centre). The preferred level
      is the **coarsest level whose cells are no larger than a pixel in both directions**, so no detail
      that the screen could show is lost. Levels without the requested time are not considered.
   3. *Read budget.* The read window (the grid cells the tile covers, clipped to the grid) is computed
      from the cell positions of the tile's pixel edges. If it exceeds `max_read_cells` the next
      coarser level is tried; if none fits, the source is skipped with reason `cost`. A regional
      source is cheap even at zoom 0, because the window is clipped to its extent.
   4. *Read.* One `isel` per contiguous column run (two runs at the antimeridian), through xarray's
      lazy backend arrays — no dask graph. CF decoding (`scale_factor`, `_FillValue`) gives floats with
      NaN for missing data.
   5. *Resample to the tile.*
      * **Lat/lon grids (separable fast path).** Web Mercator is separable, so the cells of each output
        row depend only on latitude and the cells of each column only on longitude. Each pixel takes
        the **mean of the cells whose centres lie inside it**, computed exactly for all pixels at once
        with an integral image (summed-area table, float64) of values and valid counts. When a pixel is
        smaller than a cell, it takes the cell under its centre. `min`/`max` and `nearest` are also
        available; `nearest` is the default for categorical variables (`flag_values`, land cover).
      * **Projected grids** (LAEA, rotated pole, UTM, …): pixel centres are transformed with pyproj and
        the containing cell is taken (nearest). Build overviews for proper means at low zoom.
   6. *Unit transform* (`scale`, `offset` from the layer config).
   7. *Composite.* Pixels still empty take this source's finite values; the source index is recorded
      per pixel. When no pixel is empty, the remaining sources are not read at all.
3. **Encode.** Body = 256 × 256 float32 values, byte-shuffled, followed by 256 × 256 uint8 source
   indices; gzip; headers `X-GeoLoupe-Sources` (source:level:pixels) and `X-GeoLoupe-Skipped`
   (source:reason). An empty tile is HTTP 204 with the same headers. See
   [docs/data-tiles.md](docs/data-tiles.md).

In the browser, MapLibre decides which tiles are visible and requests `geoloupe://<spec>/{z}/{x}/{y}`.
The protocol handler fetches the data tile (from its own cache if possible), colours it with the
current colour scale and returns an `ImageBitmap`. A "spec" is an immutable description of what to
draw (layer, time, dims, colour scale, optional subtrahend); every change makes a new spec and a new
MapLibre source, added on top of the old one, which is removed only after the new tiles have loaded.
That double buffering keeps time animation and colour changes free of flicker.

### Curvilinear grids

A grid with 2-D longitude/latitude keeps its cells as they are: `Grid.locate()` finds the nearest cell
centre of every pixel with a KD-tree on 3-D unit vectors (no special cases at the date line or the
poles) and leaves pixels empty beyond ¾ of a cell. The resulting per-pixel (row, col) plan is the same
as for projected grids, so reading, compositing, queries and exports are shared. Coarser levels are
views of every 2nd, 4th, … cell, so low zooms stay within the read budget.

Small grids (≤ `field_cache_cells`, e.g. ERA5's 1440 × 721 or a 1° ocean model) are read whole
once per time step and every tile cuts its window from memory. Stores whose chunks hold a whole
field, or many time steps (CMIP6), would otherwise be decompressed again for every tile.

### Mosaics and STAC

A `mosaic:` (a list of files) or a `stac:` search is expanded when the catalog opens into hidden
member datasets, one per file, opened in parallel. Each variable becomes one composite layer over the
members, in order, so the first file with data at a pixel wins (for STAC: the best item by
`sortby`). RGB layers record the channel of each source; the engine composites each channel over its
sources the same way, so true colour works across many scenes.

### Vector overlays

`vectors:` files are read once per server (pyogrio/GDAL, or pyarrow for GeoParquet), reprojected to
lon/lat, optionally filtered and simplified, and sent as one GeoJSON document
(`/api/vectors/{id}.geojson`, gzip, cached by the browser). MapLibre tiles and simplifies it per zoom
on the client (geojson-vt) and draws it between the data and the station circles. Polygons always get
a fill layer, transparent when no fill colour is set, so that hovering inside a region finds it. This
suits overlays up to a few hundred thousand features (limit `max_vector_mb`); larger collections need
vector tiles (roadmap).

### Featured views and previews

A configuration can list `views:` (layer, time, place, colour scale, optional comparison). The
client shows them as a start screen (the gallery) when the URL has no state. A view is exactly the
state of a shareable link: opening it sets the URL hash, and the normal start-up restores it, so there
is no second code path. Each card has a preview from `GET /api/layers/{id}/preview.png?center=lon,lat&zoom=z&width&height`:
a Web Mercator window of any size centred on a point, stitched from the same (cached) tiles as the
map and coloured once (colormap, RGB stretch or class colours).

## 5. What the user sees, and what it means

GeoLoupe shows three kinds of numbers. They are deliberately different:

| Where | Value | Resolution |
|---|---|---|
| Map pixel and **hover** read-out | The value of the displayed pixel: a mean of the source cells inside the pixel (or an overview level, itself a block mean) | Display resolution; the read-out names the source and level |
| **Click** (inspector table, time series) | The stored value of the native grid cell under the point, for every source | Native resolution |
| **Export** (NetCDF) | The native cells inside the box, with the source attributes | Native resolution |

The **provenance badge** at the top of the map always lists the sources and levels on screen, with
their share of pixels and whether they are native or overview data, and says when a source is hidden
because it is too expensive at this zoom.

Known approximations (documented, not hidden):

* Block means on lat/lon grids are **not area-weighted**. Across a block of height Δ (radians) the
  cos(latitude) cell areas vary by about tan(φ)·Δ: 0.9 % for a 0.5° block at 45°, 3 % for a 1° block at
  60°. The effect on the mean is smaller still (it is a weight error, not a value error) and grows toward
  the poles. Overviews use the same unweighted mean; area weighting is on the roadmap.
* A screen pixel takes the cells whose **centres** fall inside it, not an area-weighted share of
  partly covered cells (that would be conservative remapping). At the edges of fine data this can
  shift a boundary by up to half a cell.
* **Difference maps** (A − B) subtract two layers sampled on the same screen pixels, each at its own
  best level. They show where A and B differ at the displayed resolution; they are not a regridding.
* **Overview edges.** When a grid size is not a multiple of the factor, the last overview row/column
  is a block mean of the remaining cells but is placed as if the block were full, up to f/2 native
  cells beyond the grid edge (e.g. 0.67° past 90° S at factor 4 of a 0.25° grid with 522 rows).
* **Compositing** assumes the sources describe the same quantity in the same units (unit conversion
  is configured per source). It does not blend at the boundary: the fine source simply wins where it
  has data.

## 6. Technology choices and alternatives

| Component | Choice | Alternatives considered | Why |
|---|---|---|---|
| Data access | **xarray** (lazy, no dask by default) with zarr-python 3, h5netcdf/scipy, cfgrib, rioxarray, kerchunk refs | Format-specific readers; GDAL for everything | xarray is the common model of the Pangeo ecosystem, decodes CF metadata and supports every needed format. Dask graphs add overhead for small window reads, so it is only used for multi-file NetCDF. |
| Tile engine | **Own engine** on numpy + pyproj (~500 lines) | TiTiler / rio-tiler `XarrayReader`; xpublish-tiles | Neither returns float data tiles with automatic level selection, and neither composites several datasets of different resolution per pixel. rio-tiler warps through GDAL for every tile and has no multiscale choice for xarray. xpublish-tiles (the closest match, with excellent grid coverage) renders PNG on the server. The engine copies their best ideas (cost budget, advertised minimum zoom, separable lat/lon path). |
| Multi-resolution storage | **External overviews**, block means in the native grid, Zarr v3 with GeoZarr `multiscales` + `proj` + `spatial` v0.1 metadata | Web Mercator pyramids (ndpyramid); on-the-fly coarsening only; overviews inside the source store | Native grid keeps values traceable and serves any projection. External stores work with read-only archives and do not duplicate level 0. The GeoZarr conventions let other tools (xpublish-tiles, zarr-layer, OpenLayers GeoZarr, titiler-eopf) read the same pyramid. On-the-fly coarsening alone cannot serve a 900 M-cell grid at zoom 0. |
| API | **FastAPI** | Starlette alone; xpublish plugin; Django | Typed endpoints, automatic OpenAPI docs at `/api/docs`, the standard in this ecosystem (TiTiler, xpublish). |
| Tile format | **float32, byte-shuffled, gzip** + uint8 source map | PNG (server colormap); `.npy` (TiTiler); LERC; RGB-packed floats for MapLibre `raster-dem` | Exact values, NaN for missing data, simple to decode, about 2× better compression than unshuffled floats. PNG would make every colour change a refetch and loses the values. |
| Map library | **MapLibre GL JS v6** + custom protocol | deck.gl `TileLayer` + custom shader; OpenLayers WebGLTile; CesiumJS; Leaflet | MapLibre handles tiling, LOD, caching, globe projection and vector basemaps with labels above the data. The protocol handler is about 80 lines. OpenLayers' WebGL tiles are the strongest float-raster alternative (GPU colormaps, any CRS) and are the documented fallback. deck.gl adds weight for no gain at this stage; Cesium is for full 3D; Leaflet has no WebGL. |
| Colouring | **CPU** in the protocol handler (≈0.3 ms per tile) | WebGL2 custom layer; `raster-dem` + `color-relief` | Simple, exact NaN transparency, same LUT as the server. A GPU path is on the roadmap if profiling shows a need. |
| UI | **React 19 + zustand + uPlot** | Vanilla TS; Svelte; Plotly/ECharts | React is what most contributors know; zustand is a 1 kB store; uPlot draws long daily series (13 000+ points) fast and small. |
| Colormaps | One **LUT file** (matplotlib, cmocean, Crameri) for server and client | matplotlib at runtime | Identical colours everywhere, no matplotlib dependency at run time. |

## 7. Performance

Measured on one compute node (72-core Arm CPU, data on a busy Lustre file system) with a large private
store — a 30″ ≈ 1 km global daily dataset (20 880 × 43 200 cells × 13 864 days, uint16, sharded Zarr
v3) over a 0.25° reanalysis — and with public data read over the internet. Full numbers, conditions
and the script: [docs/benchmarks.md](docs/benchmarks.md).

| Request | Time |
|---|---|
| Composite tile, zoom 2–4, 1 km data from overviews | 14–33 ms |
| Composite tile, zoom 4–5, 1 km data native (≤ 6 M cells read, no overviews) | 76–95 ms |
| Composite tile, zoom ≥ 6, 1 km data native | 6–14 ms |
| Tile from the server cache | 3–20 ms (incl. gzip of the response) |
| Point query (native cells of all sources) | 9–10 ms |
| One year of daily values at a point, two sources | 4.7 s cold, 0.3–0.5 s warm |
| Remote Sentinel-2 COG tile over HTTPS (internal overview chosen per zoom) | 0.3–0.9 s cold |
| Remote ARCO-ERA5 tile over HTTPS (1.3 million hourly steps) | 1.0–1.5 s cold |
| 16 cold Sentinel-2 RGB tiles (3 remote COGs each), in parallel | 2.0 s (13.3 s with a global read lock) |
| Opening the whole gallery (ERA5 Zarr, 6 DEM + 4 Sentinel-2 + 1 WorldCover COGs, 21 station CSVs) | 5–15 s |
| Overview build, two variables, one year of 1 km daily data, 32 workers | 18.5 min, 12 GB |

Data tiles are 10–100 kB after gzip (a PNG of the same tile: ~30 kB). The main levers are the chunk
layout (720 × 720 inner chunks make windows cheap; whole-globe chunks make point series read a full
field per time step) and overviews for low zoom. A fresh process on a busy parallel file system pays
tens of seconds for imports and first reads, so the server warms up its main layers in a background
thread at start.

## 8. Deployment

* **Local:** `geoloupe serve config.yaml` (uvicorn). The web client is a static bundle inside the
  Python package, so no Node.js is needed to run it.
* **HPC:** run on a compute node; open through an SSH tunnel, or through jupyter-server-proxy. All
  client URLs are relative, so a path prefix such as `/user/<name>/proxy/8000/` works.
* **Notebook:** `geoloupe.show({"era5": ds})` starts the server in a background thread and displays
  the map inline (any in-memory or lazily opened xarray Dataset).
* **Security:** the API is read-only. It serves only datasets named in the configuration; static file
  paths are confined to the bundled client. There is no authentication (bind to `127.0.0.1`, the
  default, or put it behind an authenticating proxy). CORS is open by default for read-only use and is
  configurable.

### Opening data at run time

`POST /api/datasets` ("Add data") is off unless the server allows remote URLs (`--allow-open`) or
local folders (`--open-root`); local paths are resolved (symlinks, `..`) and must stay below a
root. The new dataset is opened by a small catalog of its own and merged into the running one, so
data already open is not touched; vector and point files go to their registries.

## 9. Extension points

| To add … | Change |
|---|---|
| A file format | a branch in [readers.py](src/geoloupe/readers.py) returning an xarray Dataset |
| A grid type (curvilinear, UGRID, HEALPix) | a new planner in [engine.py](src/geoloupe/engine.py) next to `plan_separable` / `plan_projected` |
| An aggregation | `resample()` in the engine and `block_reduce()` in [overviews.py](src/geoloupe/overviews.py) |
| A colormap | an entry in [scripts/build_colormaps.py](scripts/build_colormaps.py), then regenerate |
| A tile matrix set (polar stereographic, EPSG:4326 tiles) | `tiling.Tile` (MapLibre v6.15 supports custom projections) |
| An API endpoint | [api/app.py](src/geoloupe/api/app.py) |
