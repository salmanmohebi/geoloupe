# Ecosystem research

This is the survey of existing tools that GeoLoupe's design is based on. It answers, for each
relevant project: what it does, how it handles large and multi-resolution data, what we can reuse,
and what we learn from it. [ARCHITECTURE.md](../ARCHITECTURE.md) explains the decisions that follow
from it; [decisions.md](decisions.md) lists them one by one.

**How this was compiled.** Versions and dates were checked on 2026-11-05 in the PyPI and npm
registries and in the project repositories. For xpublish-tiles, rio-tiler/TiTiler, MapLibre and the
Zarr conventions the source code was read, not only the README. Statements that could not be checked
are marked *(not verified)*. The ecosystem moves fast (several of these projects release monthly), so
re-check version-specific claims before relying on them.

## Summary

| Need | Best existing option | Gap that GeoLoupe fills |
|---|---|---|
| Dynamic tiles from xarray | xpublish-tiles (LOD, cost control, many grid types), TiTiler (`titiler.xarray`) | Neither returns **float data tiles** with per-tile level choice; neither composites **several datasets of different resolution per pixel** |
| Multi-resolution storage | Zarr v3 + GeoZarr `multiscales` / `proj` / `spatial` conventions (v0.1, pilot) | GeoLoupe writes them as **external overviews** in the native grid (source never modified) |
| Browser rendering of float rasters | OpenLayers WebGLTile + GeoZarr source, `@carbonplan/zarr-layer`, deck.gl-raster | These read one store at a time on the client; GeoLoupe keeps cross-dataset compositing on the server and uses MapLibre for the map |
| Point values and time series | OGC API – EDR (xpublish-edr) | GeoLoupe's `/point` and `/timeseries` follow the same model; an EDR facade is on the roadmap |
| Catalog metadata | STAC + datacube + render extensions | GeoLoupe's YAML registry maps onto these (roadmap: STAC import/export) |

## 1. xpublish-tiles, xpublish, xpublish-edr

### xpublish-tiles (earth-mover/xpublish-tiles)
- Repo: https://github.com/earth-mover/xpublish-tiles. PyPI `xpublish-tiles` **0.13.1 (2026-11-03)**. First
  release 0.1.1 on 2025-08-12; 74 releases since then, so it moves very fast (0.5.0 2026-05-13, 0.10.0
  2026-10-06). Apache-2.0, Python >= 3.12. Pre-1.0, so expect API churn. Maintained by Earthmover
  (it also serves as the open core of their commercial "Flux" gateway, which offers WMS/EDR/OPeNDAP;
  whether Flux runs this exact code is **not verified**).
- **What it does:** two xpublish plugins. `TilesPlugin` = OGC API – Tiles (README: "feature complete").
  `WMSPlugin` = OGC WMS (README: "still in active development"). It renders xarray Datasets/DataTrees on
  request. There is no pre-rendering step. It reads Zarr v2/v3 and Icechunk (CLI: `zarr:///…`, `icechunk:///…`).
- **Rendering architecture (from source):** CPU only. Dependencies are `datashader>=0.19.1`, `numba`,
  `numbagg`, `pyproj`, `morecantile`, `rasterix`, `scipy`, `pillow`, `matplotlib`, `xarray>=2025.12`,
  `zarr>=3.1.2`, `icechunk>=2`. There is no GDAL and no rasterio.
  - Rectilinear and curvilinear grids are rasterised with `datashader.Canvas.quadmesh`. Unstructured
    grids use `Canvas.trimesh` (UGRID, or Delaunay on 1-D vertex coordinates via scipy). Categorical
    data uses custom numba kernels (`render/kernels.py`, footprint mode). Points and polygons use datashader
    and spatialpandas.
  - It reimplements EPSG:4326 → EPSG:3857 as a *separable* transform when possible, so it does not
    transform every pixel. It can detect "approximately rectilinear" curvilinear grids and render them
    as rectilinear. Data is loaded with `xarray` `load_async`.
  - Before rendering, it coarsens the data on the fly (mean, `max_pixel_factor = 3` input cells per
    output pixel).
- **Grids (README table):** raster/affine (`spatial:transform`), rectilinear, curvilinear, unstructured
  (UGRID / triangular), geostationary (scan angles + CF grid_mapping), polar radar sweeps (raster only),
  cubed sphere and HEALPix (polygons style only), and vector data cubes (xvec `GeometryIndex` or CF
  geometry containers).
- **Output formats (from source, `types.py`):** `TileFormat = PNG | JPEG | MVT`
  (`application/vnd.mapbox-vector-tile`, one layer per variable, each feature carries `value` and
  optionally `color`). The legend endpoint gives PNG/JPEG or `application/json` (color stops).
  **There is no raw float output (no npy, no float32 GeoTIFF).** It is a server-side colormap engine.
- **Multiscale / LOD (from source, `multiscale.py`):** yes. For a `DataTree`, it scans every node that
  has data variables and a `spatial:transform` attribute (on the array or the group). It takes the pixel
  size as `abs(spatial:transform[0])` and the CRS from `proj:code` or `proj:wkt2` on the group. Then it
  picks a level per tile with `overview_selection_strategy`: `"nearest"` (default; closest in log space,
  ties go to the coarser level), `"coarser"`, or `"finer"`. Note: the level is detected from
  per-level `spatial:transform` attributes. It does **not** read the `multiscales.layout` entries for
  this. Overview levels may hold fewer variables than the finest level.
- **How it limits expensive requests (from source, `config.py` with donfig, env prefix `XPUBLISH_TILES_`):**
  - `max_renderable_size` = 1024**3 bytes (comment: "~10,000 × 10,000 float64 pixels; takes the
    pipeline ~1 s"). If a tile would need more, it raises `TileTooBigError` and returns **HTTP 413**.
  - It computes a `minzoom` that avoids `TileTooBigError` and advertises it in the TileJSON/metadata,
    so a client never asks for the expensive zooms. Coarse overviews lower this `minzoom`.
  - `max_renderable_vertices` = 5,000,000 (MVT features), `async_load_timeout_per_tile` = 20 s,
    `num_concurrent_data_loads` = 4, `num_threads` = 8, `grid_cache_max_size` = 16.
  - Non-spatial dimensions that the request does not select are reduced automatically: the last index,
    or index 0 for a vertical dimension. Selectors look like `time=nearest::2000-01-01T04:00` and
    `step=pad::3h`.
- **OGC:** OGC API – Tiles (TileJSON plus OGC tileset metadata; legends via an `ogc-rel:legend` link)
  and WMS 1.3 (in development). Dimension extents use the EDR-style `R<count>/<start>/<step>` notation.
  Any morecantile TMS works; the examples use `WebMercatorQuad`.
- Ops note (Development Seed datacube guide): the first request is slow because of numba JIT, so warm
  it up at startup and tune `NUMBA_NUM_THREADS`.

### xpublish (core)
- https://github.com/xpublish-community/xpublish. PyPI **0.5.2 (2026-06-18)**. A FastAPI app that serves
  xarray Datasets **and DataTrees** (a plain Dataset is wrapped as a one-node tree). Plugins add routers.
  Examples: xpublish-zarr, OPeNDAP, xpublish-wms (0.13.2, 2025-05-26), xpublish-tiles, xpublish-edr.
  It can use Dask for on-demand computation.

### xpublish-edr
- https://github.com/xpublish-community/xpublish-edr. PyPI **0.12.1 (2026-11-05)**. It implements
  OGC API – EDR **1.1** conformance classes (`core`, `collections`, `json`, `covjson`, `queries`) with
  `/position`, `/area` and `/cube`, also under `/collections/{id}/…`. `POST` on position/area is a
  non-standard extension for large geometries. Output `f=`: `cf_covjson`, `csv`, `geojson`, `netcdf`,
  `parquet` (plus `geotiff` for cube). It understands CF and GeoZarr `proj:`/`spatial:` metadata.
  Limits: it needs regular 1-D X/Y grids (or an affine that can be turned into them). No curvilinear
  selection, and no `proj:projjson`. There is no trajectory or corridor query.

---

## 2. TiTiler / titiler.xarray / rio-tiler `XarrayReader`

- Versions: `titiler.xarray` **2.4.0 (2026-09-21)**. TiTiler 2.0.0 came out on 2026-03-16 and broke the
  API: it removed the 256 default tilesize and the `@{scale}x` suffix, added `tilesize=`, and moved the
  selector method into `sel` (`sel=time=nearest::2020-01-06`). `rio-tiler` **9.4.13 (2026-11-02)**; 9.0.0
  came out on 2026-03-11. Repos: https://github.com/developmentseed/titiler,
  https://github.com/cogeotiff/rio-tiler.
- **How a tile is made (from source, `rio_tiler/io/xarray.py`, `XarrayReader.tile → part`):**
  1. `da.rio.clip_box(tile bbox in dataset CRS)`.
  2. If `da.nbytes > MAX_ARRAY_SIZE`, it raises `MaxArraySizeError`. The default is 1e9 bytes, set by
     env `RIO_TILER_MAX_ARRAY_SIZE`.
  3. It computes `calculate_default_transform` (it clamps lat to ±85.06 for 4326 → 3857), then
     `da.rio.reproject(dst_crs, shape=(tilesize,tilesize), transform=…, resampling=Resampling[reproject_method])`.
     So it warps with **GDAL through rioxarray**. The default `reproject_method` is `"nearest"`.
  4. It returns a masked array (`ImageData`). Only one extra (non-spatial) dimension is allowed, which
     becomes the bands. Other dimensions must be selected with `sel`.
- **Low zoom over big arrays:** rio-tiler has **no automatic pyramid / overview selection for xarray**.
  For COGs it has this, through GDAL overviews. At low zoom it reads the full-resolution clip into memory.
  For a large global grid this fails with `MaxArraySizeError` or becomes very slow. The only pyramid
  support in titiler.xarray is the `group=` query parameter: the *client* picks the zarr group,
  e.g. one group per zoom level as in the `pyramid.zarr` test fixture. There is no `multiscales`
  handling in titiler or rio-tiler (grep finds nothing).
  - **titiler-eopf** (https://github.com/EOPF-Explorer/titiler-eopf, tag v0.12.4) *does* add this. Its
    `reader.py` reads `dt.attrs["multiscales"]["layout"]`, computes the target resolution of each tile
    in the dataset CRS, and picks a level with `zoom_level_strategy = AUTO|LOWER|UPPER` (50/100/0 %
    threshold between adjacent levels), the same idea as GDAL overview selection for COGs.
- **Output formats:** PNG, JPEG, JP2, WEBP, GTiff, and **`npy`** (media type `application/x-binary`).
  In rio-tiler `render()`, NPY = `numpy.save()` of the data array **with the mask appended as the last
  band** (so the dtype is upcast to match the data). That is a raw float tile over HTTP, which is close
  to GeoLoupe's idea, but the format is NumPy `.npy`, not a typed buffer made for the browser.
- **titiler-multidim** (https://github.com/developmentseed/titiler-multidim, tag v0.11.0): the
  reference app for `titiler.xarray` (NASA VEDA staging). New: 1 to 20 ordered `url=` values per request
  make a **per-pixel mosaic** (first valid pixel wins, `pixel_selection` can change this). This is a
  simple form of GeoLoupe's "fine on top of coarse" compositing, but it works only on one shared grid
  selection.
- **titiler-cmr** (https://github.com/developmentseed/titiler-cmr, tag v1.1.4): TiTiler with NASA CMR
  granule search. It has `/xarray` and `/rasterio` backends, time-series endpoints, and NumpyTile output.
  It is not on PyPI (deployed with CDK).
- Development Seed's datacube guide calls TiTiler vs xpublish-tiles "breadth versus depth": TiTiler
  has the bigger format/deploy/cache ecosystem, and xpublish-tiles is stronger for unstructured grids,
  CF and chunk-aware loading
  (https://developmentseed.org/datacube-guide/latest/visualization/xpublish/xpublish-tiles.html).

---

## 3. Zarr multiscale conventions (exact schemas)

### GeoZarr status (OGC)
- Repo https://github.com/zarr-developers/geozarr-spec (last commit 2026-07-06, "Define versioning
  expectations for pilot conventions"). The OGC GeoZarr SWG no longer writes one big spec. GeoZarr
  is now a **suite of Zarr conventions**: `multiscales`, `proj` (geo-proj) and `spatial`. Under
  consideration: CF-in-Zarr, DGGS (https://github.com/zarr-conventions/dggs), TileMatrixSet.
- Roadmap at https://geozarr.org/roadmap (approved Feb 2026): Release Candidate May 2026 → OAB review +
  30–60 day public comment "Summer 2026" → SWG adoption vote Sep 2026. **The tracker still shows most
  items open (RC 1/8, OAB 0/4, vote 0/3). I found no evidence that GeoZarr V1 is an adopted OGC
  standard as of 2026-11-05.** All three conventions are tagged **v0.1** and labelled
  "**Pilot**" maturity. Their READMEs say "breaking changes should be expected until … `v1`. We
  anticipate releasing `v1` before the end of 2026."
- Implementations already reading these conventions: OpenLayers `ol/source/GeoZarr` (10.8+),
  `@carbonplan/zarr-layer`, `@developmentseed/deck.gl-zarr` + `@developmentseed/geozarr`, xpublish-tiles,
  xpublish-edr, titiler-eopf, and `geozarr-toolkit` (PyPI 0.1.2, 2026-04-01; models and validation).
  Writers: `topozarr` (CarbonPlan) and the EOPF data-model.

### `zarr-conventions/multiscales` v0.1 (https://github.com/zarr-conventions/multiscales)
- UUID `d35379db-88df-4056-af3a-620245f8e347`. Schema
  `https://raw.githubusercontent.com/zarr-conventions/multiscales/refs/tags/v0.1/schema.json`.
  Group-level only. There is no inheritance to child groups. A convention is declared with a
  `zarr_conventions` array (UUID + schema_url + spec_url + name). The version lives only in the URL tag;
  there is no `version` attribute.
- `multiscales` object:
  - `layout` (**required**, `object[]`): one entry per level.
    - `asset` (**required**, string): relative path to the child group or array (`"0"`, `"0/data"`,
      `"resolutions/full"`). It must not start with `/` or contain `..`.
    - `derived_from` (optional, string): path of the source level. Levels form a directed graph, and
      upsampled levels are allowed (scale < 1).
    - `transform` (**required if `derived_from` is present**): *relative* transform from the source
      level, `{"scale": number[], "translation": number[]}`. Both keys are optional. Scale 2.0 means the
      level is 2× coarser. Translation is in the coordinate space of the current level.
    - `resampling_method` (optional, string, per level): e.g. `"nearest"`, `"average"`, `"bilinear"`,
      `"mode"`, `"max"`, `"min"`, `"med"`, `"sum"`, `"rms"`, `"gauss"`, … Any string is allowed.
    - *Absolute* position goes **outside** `transform` as `spatial:transform` (6 numbers) and
      `spatial:shape` (`[height, width]`) on the layout entry. Additional properties are allowed.
  - `resampling_method` (optional): the default for all levels.
- Rules: child group names SHALL match the `asset` values. Consolidated metadata SHOULD be used.
  Unknown fields SHOULD be ignored, and readers that do not know the convention must still read the
  arrays.
- **Minimal example from the spec** (README, "Geospatial Data" section; the `examples/geospatial-pyramid.json`
  file adds `spatial:transform` per layout entry and `"resampling_method": "average"`):
```json
{
  "zarr_format": 3,
  "node_type": "group",
  "attributes": {
    "zarr_conventions": [
      {"schema_url": "https://raw.githubusercontent.com/zarr-conventions/multiscales/refs/tags/v0.1/schema.json",
       "spec_url": "https://github.com/zarr-conventions/multiscales/blob/v0.1/README.md",
       "uuid": "d35379db-88df-4056-af3a-620245f8e347", "name": "multiscales",
       "description": "Multiscale layout of zarr datasets"},
      {"schema_url": "https://raw.githubusercontent.com/zarr-conventions/proj/refs/tags/v0.1/schema.json",
       "spec_url": "https://github.com/zarr-conventions/proj/blob/v0.1/README.md",
       "uuid": "f17cb550-5864-4468-aeb7-f3180cfb622f", "name": "proj",
       "description": "Coordinate reference system information for geospatial data"},
      {"schema_url": "https://raw.githubusercontent.com/zarr-conventions/spatial/refs/tags/v0.1/schema.json",
       "spec_url": "https://github.com/zarr-conventions/spatial/blob/v0.1/README.md",
       "uuid": "689b58e2-cf7b-45e0-9fff-9cfc0883d6b4", "name": "spatial",
       "description": "Spatial coordinate information"}
    ],
    "multiscales": {
      "layout": [
        {"asset": "0", "transform": {"scale": [1.0, 1.0], "translation": [0.0, 0.0]}},
        {"asset": "1", "derived_from": "0", "transform": {"scale": [2.0, 2.0], "translation": [0.0, 0.0]}}
      ]
    },
    "proj:code": "EPSG:32633",
    "spatial:dimensions": ["Y", "X"],
    "spatial:transform": [10.0, 0.0, 500000.0, 0.0, -10.0, 5000000.0],
    "spatial:bbox": [500000.0, 4900000.0, 600000.0, 5000000.0]
  }
}
```

### `spatial` v0.1 (https://github.com/zarr-conventions/spatial) and `proj` v0.1 (https://github.com/zarr-conventions/proj)
- `spatial:dimensions` (`[Y, X]` names; required on arrays, optional on groups as a default),
  `spatial:bbox` `[xmin, ymin, xmax, ymax]`, `spatial:transform_type` (default `"affine"`, the only
  type in v0.1), `spatial:transform` (6 numbers), `spatial:shape` `[height, width]`,
  `spatial:registration` `"pixel"` (default) | `"node"`.
- **`spatial:transform` uses rasterio/`affine` order `[a, b, c, d, e, f]`**
  (`x = a·col + b·row + c`, `y = d·col + e·row + f`). This is **not** GDAL GeoTransform order
  (GDAL is `[c, a, b, f, d, e]`). Index (0,0) is the top-left *corner* of the top-left pixel.
- `proj:code` (e.g. `"EPSG:4326"`) or `proj:wkt2` or `proj:projjson` sit on the group. Array attributes
  override group attributes.
- Practical rule for GeoLoupe: write `proj:code` + `spatial:dimensions` on the multiscale group. Write
  `spatial:transform` + `spatial:shape` **both** on each layout entry **and** on each level's group (or
  arrays). xpublish-tiles finds levels from per-node `spatial:transform` attributes. zarr-layer uses the
  layout entries' `spatial:shape` to avoid opening each level.

### ndpyramid (carbonplan) layout — legacy
- PyPI `ndpyramid` **0.4.0 (2024-11-22)**. The repo only gets dependency bumps in 2026. CarbonPlan's
  new tool is **topozarr** (below).
- `pyramid_reproject` / `pyramid_resample` / `pyramid_regrid` reproject every level to
  `"web-mercator"` (EPSG:3857) or `"equidistant-cylindrical"` (EPSG:4326, extent (-180, 90, 180, -90)).
  Level *i* = zoom *i*, `pixels_per_tile` default 128, children `"0"`, `"1"`, …. Root attributes (from source):
```json
{"multiscales": [{
   "datasets": [{"path": "0", "level": 0, "crs": "EPSG:3857", "pixels_per_tile": 128},
                {"path": "1", "level": 1, "crs": "EPSG:3857", "pixels_per_tile": 128}],
   "type": "reduce",
   "metadata": {"method": "pyramid_reproject", "version": "<ndpyramid version>", "args": [], "kwargs": {}}
}]}
```
  This is an array of objects with a `datasets` key, i.e. the old OME-NGFF 0.1-style shape. **It is
  not the zarr-conventions `layout` format.** The levels are Web-Mercator tiles, not the native grid.

### topozarr (new, CarbonPlan)
- https://github.com/carbonplan/topozarr, PyPI **0.2.2 (2026-11-03)**. `create_pyramid(ds, levels=…,
  x_dim=…, y_dim=…)` writes a Zarr v3 multiscale store with each level 2× coarser, **in the native CRS**
  (CRS set with `xproj`). It follows the GeoZarr conventions and targets `zarr-layer`. This is the closest
  existing writer to GeoLoupe's "pyramids in native grid" design.

### OME-NGFF (OME-Zarr) multiscales
- https://github.com/ome/ngff-spec. The current tag is **0.6** (tag commit dated 2026-09-14); 0.5.x is
  the Zarr-v3 release before it. Metadata sits under `attributes.ome` with `"version": "0.6"`. In 0.6
  (RFC-5), `axes` moved into named `coordinateSystems`, and each dataset's `coordinateTransformations`
  have `input`/`output`. Excerpt from `examples/multiscales_strict/multiscales_example.json`:
```json
{"zarr_format": 3, "node_type": "group", "attributes": {"ome": {"version": "0.6", "multiscales": [{
  "name": "example",
  "coordinateSystems": [{"name": "intrinsic", "axes": [
     {"name": "t", "type": "time", "unit": "millisecond"}, {"name": "c", "type": "channel"},
     {"name": "z", "type": "space", "unit": "micrometer"}, {"name": "y", "type": "space", "unit": "micrometer"},
     {"name": "x", "type": "space", "unit": "micrometer"}]}],
  "datasets": [
    {"path": "s0", "coordinateTransformations": [{"type": "scale", "scale": [0.1, 1.0, 0.5, 0.5, 0.5],
       "input": {"path": "s0"}, "output": {"name": "intrinsic"}}]},
    {"path": "s1", "coordinateTransformations": [{"type": "sequence", "input": {"path": "s1"},
       "output": {"name": "intrinsic"}, "transformations": [
         {"type": "scale", "scale": [0.1, 1.0, 1.0, 1.0, 1.0]},
         {"type": "translation", "translation": [0, 0, 0.25, 0.25, 0.25]}]}]}],
  "type": "gaussian",
  "metadata": {"method": "skimage.transform.pyramid_gaussian", "version": "0.16.1"}}]}}}
```
  Difference from the geo convention: OME stores *absolute* scale/translation per level (in physical
  units). zarr-conventions/multiscales stores a *relative* transform to `derived_from` plus an optional
  absolute `spatial:transform`.

---

## 4. Browser-side zarr rendering

| Library | Version (npm) | Host | Needs pre-built Web-Mercator pyramid? | Native EPSG:4326 / other CRS | Maturity |
|---|---|---|---|---|---|
| `@carbonplan/maps` | 3.6.0 (2025-10-31) | own regl / Mapbox v1, MapLibre via `/core` | **Yes** (ndpyramid, 3857) | `/core` "currently only supports web-mercator" | Mature, mostly done; succeeded by zarr-layer |
| `@carbonplan/zarr-layer` | **0.11.0 (2026-10-02)**, created 2025-12 | MapLibre / Mapbox `CustomLayerInterface` | No. Uses `multiscales` (zarr-conventions) **or** legacy ndpyramid; "high resolution datasets require multiscales" | **Yes**: 4326, 3857, any proj4 / `proj:wkt2` / `proj:projjson`, GPU mesh reprojection (uses `@developmentseed/raster-reproject`) | Pre-1.0, very active. Globe on MapLibre including poles (direct ECEF path); terrain draping needs maplibre-gl ≥ 6.13 |
| `zarr-gl` | 0.7.0 (2026-01-22) | Mapbox / MapLibre custom layer | Yes (ndpyramid) | No (lon/lat reprojection was on its roadmap) | README: "you should use carbonplan/zarr-layer instead" |
| `@developmentseed/deck.gl-raster` / `-geotiff` / `-zarr` | **0.9.0 (2026-10-23)**, created 2025-12 | deck.gl (`TileLayer`) | No. Pairs the native Zarr chunk grid / COG overviews with `TileLayer`, reads GeoZarr `multiscales` | **Yes**: per-chunk GPU reprojection, plus `@developmentseed/epsg` (all 7352 EPSG codes as WKT2, 309 kB) | Pre-1.0, monorepo (affine, proj, morecantile, geozarr, geotiff ports). Used by lonboard |
| OpenLayers `ol/source/GeoZarr` | ol **10.11.0 (2026-10-04)**; source added in 10.8.0 (2026-02-11) | OpenLayers WebGL tile layer | No. Reads `multiscales` layout (and an older `tile_matrix_set` form); needs `proj:` + `spatial:` | Yes (OL raster reprojection), `proj:projjson`/`wkt2` since 10.10 | Marked "experimental"; non-spatial dim selection 10.10; generic n-D cubes 10.11 |
| `zarrita` (zarrita.js) | 0.7.5 (2026-09-01) | — (Zarr reader) | n/a | n/a | The de-facto JS Zarr v2/v3 reader (sharding, range coalescing); used by all of the above |
| Viv (`@hms-dbmi/viv`) | 0.23.0 (2026-09-23) | deck.gl (orthographic) | Reads OME-Zarr / OME-TIFF multiscales as they are | No geographic CRS (bio-imaging pixels/µm) | Mature in bio-imaging |
| vizarr | PyPI 0.1.1 (2025-03-04) | Viv + anywidget (Jupyter) | OME-Zarr multiscales | No geo CRS | Small, slow-moving |

- Notes (from the zarr-layer README, more current than the DevSeed comparison page): it picks the level
  **per map tile** (fine near the camera, coarse at the horizon). It never draws two levels over each
  other. It supports `queryData(geojson, selector)` for point / line / polygon and time-series
  extraction on the client. Custom GLSL fragments make band math possible (e.g. NDVI).
- Viv (**not verified in detail this session**) uploads 8/16/32-bit and float textures and applies
  contrast limits and colormaps on the GPU. That is the same "data tiles + client colormap" design that
  GeoLoupe chose, proven at bio-imaging scale.
- Applications (not libraries): **Browzarr** (three.js; 3D volumetric raycasting; Zarr v2/v3, NetCDF
  via wasm, Icechunk) and **GridLook** (DKRZ/MPI-M; three.js/Vue; HEALPix, ICON triangles, rotated
  grids; 1.0 in April 2026, per DevSeed guide). Comparison page:
  https://developmentseed.org/datacube-guide/latest/visualization/client-side-comparison.html

---

## 5. MapLibre GL JS and deck.gl

- **MapLibre GL JS current major = v6.** `maplibre-gl` **6.16.0 (2026-10-25)**. v6.0.0 = 2026-07-22,
  last v5 = 5.24.0 (2026-04-23), v5.0.0 = 2024-12-31, v4.0.0 = 2024-02-01.
  - v6 breaking changes that matter: **ESM-only** (`maplibre-gl.mjs`; there is no UMD `<script>` and no
    CSP bundle; use `import * as maplibregl from 'maplibre-gl'`). **WebGL2 required** (WebGL1 removed).
    `map.transform` was removed. Style-spec v25. Events are now classes.
  - New and useful in 6.x: `RasterTileSource#setPremultiplyAlpha(false)` (keeps raw RGBA when alpha
    carries data); experimental `CustomLayerInterface.renderToTerrainTile` (6.13); experimental
    `addProjection` / `identity` projection / `{bbox}` URL token for custom planar CRS tile grids
    (6.15.0); globe centre can now reach the poles (6.x).
- **Globe:** stable since **v5.0.0** (`projection: {type: 'globe'}` in the style, or `map.setProjection`).
  It switches to Mercator at high zoom automatically. Still being polished in 6.x (pole holes in
  fill/line layers fixed, precision fixes).
- **`addProtocol` (from source, `src/source/protocol_crud.ts`, `src/util/config.ts`):** promise-based
  since v4.0.0 (#3433). The signature is the same in v4, v5 and v6:
```ts
maplibregl.addProtocol(customProtocol: string, loadFn: AddProtocolAction): void
type AddProtocolAction = (requestParameters: RequestParameters, abortController: AbortController)
  => Promise<GetResourceResponse<AddProtocolResponseData>>;
type GetResourceResponse<T> = { data: T; cacheControl?: string|null; expires?: Date|string|null; etag?: string };
type AddProtocolResponseData = ArrayBuffer | ImageBitmap | HTMLImageElement | string | object;  // typed since 6.11.0
```
  - So the handler is an `async (params, abortController) => ({data})` function.
    `removeProtocol(name)` also exists. Since v4 the protocol can also be registered **inside the
    worker** via `maplibregl.importScriptInWorkers(url)`.
  - **Raster sources can be fed from a protocol:** return `{data: ArrayBuffer}` of *encoded* PNG/JPEG/WebP
    bytes (works since v4), or return a decoded `ImageBitmap` / `HTMLImageElement`. Returning these
    directly is officially typed and documented since **6.11.0** (#8515); the docs example draws on
    an `OffscreenCanvas` and returns `{data: await createImageBitmap(canvas)}`. The official COG example
    (`@geomatico/maplibre-cog-protocol` 0.10.0) uses the same mechanism.
  - Raster sources are always 8-bit RGBA. Float32 data must therefore be (a) colorised in the
    protocol handler (CPU, ideally in a worker) → ImageBitmap; or (b) packed into RGB and drawn with
    the **`color-relief` layer** (added 5.6.0) on a `raster-dem` source with `encoding: "custom"`
    (`redFactor/greenFactor/blueFactor/baseShift`). Then the colormap is a style expression evaluated
    on the GPU, and it can change with `setPaintProperty` without re-fetching (24-bit quantisation,
    `resampling` paint property since 5.20); or (c) drawn by a WebGL2 custom layer or deck.gl layer with
    float textures.
- **deck.gl:** current major **v9**, `deck.gl` **9.4.1 (2026-10-28)**. 9.4.0 (2026-09-05) is "expected
  to be the final release in the v9 series" (a v10 is coming). 9.1 (2025-01-21) added MapLibre v5
  globe support. 9.4 added a new **`@deck.gl/maplibre`** module (forked from `@deck.gl/mapbox`) with class
  **`MapLibreOverlay`** for MapLibre **v4.5.1, v5 and v6**. `interleaved: true` shares MapLibre's WebGL2
  context and puts layers in the style stack with `beforeId`; the mode cannot change after
  construction. `MapboxOverlay` from `@deck.gl/mapbox` still exists. Limits: globe goes through the
  experimental `GlobeView`, and deck layers are **not draped on MapLibre terrain**. WebGPU: "All layers
  … now support WebGPU" in 9.4, but it is "experimental and not yet recommended for production".
  Sources: https://deck.gl/docs/whats-new, https://deck.gl/docs/api-reference/maplibre/overview

---

## 6. OGC APIs

- **OGC API – EDR** (https://www.ogc.org/standards/ogcapi-edr/): approved versions 1.0 (19-086r4),
  1.0.1 (r5), 1.1 (19-086r6), **1.2 (19-086r9)** (approval date not verified), plus Part 2
  Publish-Subscribe 1.0 (23-057r1). Query types: `position`, `radius`, `area`, `cube`, `trajectory`,
  `corridor`, `items`, `locations`, `instances`, with `datetime`, `z` and `parameter-name` filters.
  Output is usually CoverageJSON or GeoJSON. **This is the right standard for GeoLoupe's point-click
  value and time-series queries on gridded data** ("value at lon/lat for all times" = one `position`
  request). xpublish-edr and pygeoapi already implement it.
- **OGC API – Tiles** Part 1 Core **1.0 (OGC 20-057, 2022)** with TileMatrixSet 2.0. It defines map
  tiles, vector tiles **and coverage (data) tiles**, so raw-value tiles fit the standard. It supports any
  TMS (e.g. `WebMercatorQuad`, `WorldCRS84Quad`) and tileset metadata that describes dimensions.
  GeoLoupe's float32 tiles could be published as an OGC "coverage tileset". Time is a query parameter
  (`datetime`), not a separate tile per time step.
- **OGC API – Maps** Part 1 Core **1.0** (OGC 20-058; approved 2024-06-12, published 2024-08-09). Rendered
  images for a bbox/size/CRS (the modern WMS). Good for static map export and figures, not for
  interaction. OpenLayers 10.8 added an `OGCMap` source for it.

---

## 7. Short notes

- **lonboard** (0.17.0, 2026-10-01): deck.gl in Jupyter (anywidget + GeoArrow). It has had `RasterLayer`
  for COG/PMTiles since 0.14–0.16 (2026) and now uses deck.gl-raster's `RasterTileLayer` upstream.
  *Lesson:* binary columnar transfer (Arrow) + GPU rendering beats JSON; a notebook widget can be a
  second front end for GeoLoupe's tile API.
- **HoloViz** (datashader 0.19.1, holoviews 1.23.2, hvplot 0.12.2, panel 1.10.0, geoviews 1.16.0):
  server-side re-rasterisation on every zoom/pan (`rasterize`/`datashade` + RangeXY streams). This is
  stateful and per user (Bokeh session). *Lesson:* datashader's quadmesh is the proven kernel
  (xpublish-tiles uses it), but stateless HTTP tiles scale to many users and stateful sessions do not.
- **Xee** (0.1.2, 2026-07-14): opens Google Earth Engine ImageCollections as lazy xarray; the pixel
  grid is chosen at open time. *Lesson:* "server picks scale/projection per request" works well for
  remote archives. GeoLoupe does the reverse (xarray → tiles).
- **xvec** (0.5.2, 2025-11-22): vector data cubes (geometry index on an xarray dimension). *Lesson:*
  station tables (GHCNd) and polygons can be xarray objects with time series. xpublish-tiles already
  renders xvec cubes as MVT.
- **VirtualiZarr** (2.8.0, 2026-10-14) and **kerchunk** (0.2.10, 2026-03-30): virtual Zarr references to
  NetCDF/HDF5/GRIB/TIFF chunks, written to Icechunk or kerchunk JSON/Parquet. *Lesson:* raw archives can
  be opened without copying, but you still need real pyramid levels for low zoom. Virtual references
  cannot downsample.
- **Icechunk** (2.3.1, 2026-10-27; 1.0 on 2025-07-10, 2.0 on 2026-04-08): transactional, versioned Zarr
  store (snapshots, branches, tags). *Lesson:* add pyramid levels or new time steps atomically, and
  put the snapshot id in tile URLs / ETags for safe HTTP caching.
- **STAC** (pystac 1.16.0, 2026-11-04; datacube ext **v2.3.0**, zarr ext v1.1.0, **render ext v2.1.0**):
  catalog metadata. The datacube extension describes `cube:dimensions` and `cube:variables`; the render
  extension describes default colormap / rescale / band math per asset (TiTiler reads it). *Lesson:*
  GeoLoupe's dataset registry (colormap defaults, valid range, units) maps well to STAC + render.
- **Neuroglancer precomputed**: one `info` JSON with a `scales` array (per level: `key`, `size`,
  `resolution` in physical units, `voxel_offset`, `chunk_sizes`, `encoding` = raw/jpeg/png/
  compressed_segmentation/jxl/…, optional `sharding`). *Lesson:* a single small manifest per pyramid
  plus sharding (few large objects instead of millions of chunks) works at petabyte scale. Zarr v3
  sharding gives the same benefit.
- **NASA Worldview / GIBS**: GIBS serves **pre-rendered** WMTS/TMS tiles in EPSG:4326, 3857, 3413 and
  3031, with a `TIME` dimension, for more than 1000 layers. Worldview is the OpenLayers client
  (**not re-verified this session**). *Lesson:* support more than one TMS (polar views), keep time as
  a first-class tile dimension, and publish colormap/legend metadata per layer.
- **earth.nullschool.net**: sends a coarse **global grid as data** (JSON/binary, about 1°) to the
  browser, then interpolates, projects (D3, many projections) and animates particles on the client
  (**not re-verified**). *Lesson:* for coarse global layers (ERA5), sending the whole level-N array once
  is cheaper than tiling. Client-side colormaps and animation make the map feel fast.
- **CesiumJS** (1.147.0, 2026-11-02): 3D globe and terrain. Imagery providers are 4326/3857 tile schemes.
  `zarr-cesium` (NOC-OI) renders Zarr on it. *Lesson:* only needed for real 3D/volumetric use;
  MapLibre globe covers 2.5D.
- **OpenLayers** (10.11.0, 2026-10-04): `WebGLTile` layer + `DataTile` / `GeoTIFF` / **`GeoZarr`**
  sources take typed arrays (incl. Float32) per tile. Style expressions (`['band', 1]`, `interpolate`,
  `palette`) run on the GPU; raster reprojection is built in; `OGCMap` source exists (10.8). *Lesson:*
  this is the most complete existing "float data tiles + GPU colormap + any CRS" implementation in a map
  library. Use it as a reference design, or as a fallback renderer.
- **Leaflet** (latest 1.9.4 from 2023; **2.0.0-beta.1 on 2026-11-02**, ESM): no WebGL. Float rasters
  only through CPU plugins (georaster-layer). *Lesson:* not a fit for GeoLoupe's core.
- **napari** (0.10.0, 2026-10-30): desktop n-D viewer with multiscale + async slicing (loads the level
  that matches the canvas resolution). *Lesson:* the same level-picking rule as tile LOD. Its
  "contrast limits + colormap on GPU" UX is the one scientists expect.
- **PyVista** (0.49.2, 2026-10-28) / **ParaView**: VTK-based 3D scientific visualisation. On the web via
  trame (server-side rendering or vtk.js). *Lesson:* good for 3D/volume, too heavy as a map backend.
- **EOxServer** (1.5.5, 2026-09-28): Django server for OGC WMS/WMTS/WCS/EO-WCS/OpenSearch (EOX, used
  in ESA projects). *Lesson:* standards coverage is nice, but EO-specific product and coverage models
  are heavy. EOX itself now uses titiler-eopf + OpenLayers GeoZarr for new work.
- **Pangeo**: the community and stack (xarray + Zarr + Dask + Jupyter on cloud/HPC) that most of these
  tools come from. *Lesson:* stay compatible with that stack (xarray DataTree, Zarr v3, GeoZarr
  conventions, Icechunk) so users can bring their own stores.

---

## 8. Recent things (2024–2026) likely worth knowing

1. **EOPF Sentinel Zarr Explorer** (ESA, went live Feb 2026, https://explorer.eopf.copernicus.eu):
   Sentinel products as GeoZarr (multiscales + proj + spatial). Two render paths on the same data:
   **titiler-eopf** (server, multiscale-aware level selection) and **OpenLayers `GeoZarr` source**
   (client, EOxElements). Write-up: https://eox.at/2026/02/visualizing-geozarr/. This is the largest
   real deployment of the conventions GeoLoupe plans to write.
2. **carbonplan/zarr-layer + topozarr** (Dec 2025 → now): native-CRS multiscale writer + MapLibre
   custom layer with globe, terrain, proj4 and client queries. This is the most direct "competitor or
   building block" for GeoLoupe's front end.
3. **developmentseed/deck.gl-raster monorepo** (Dec 2025 →, 0.9.0): COG + GeoZarr in deck.gl with GPU
   reprojection, plus TS ports of affine, morecantile and proj tools. Blog:
   https://developmentseed.org/deck.gl-raster/blog/initial-geozarr/
4. **OpenLayers GeoZarr source** (10.8 Feb 2026 → 10.11 Oct 2026), see above.
5. **xpublish-tiles** (Aug 2025 →, 0.13.1): the main Python server for non-rectilinear grids
   (HEALPix, UGRID, curvilinear) with GeoZarr multiscale support and 413-based cost control.
6. **MapLibre v6** (Jul 2026): ESM-only + WebGL2-only. New GeoLoupe code should target v6 directly.
   `@deck.gl/maplibre` `MapLibreOverlay` (deck.gl 9.4, Sep 2026).
7. **titiler-multidim** multi-URL per-pixel mosaics, and **titiler-cmr** (NASA VEDA / CMR).
8. **Development Seed "Datacube Guide"** (https://developmentseed.org/datacube-guide/latest/): an
   up-to-date comparison of dynamic tilers and client renderers. It also links the NASA-IMPACT Zarr
   visualisation report (benchmarks on pyramid choices and pixels-per-tile).
9. **GridLook** (DKRZ/MPI-M, 1.0 April 2026; EGU26-9862) and **Browzarr**: browser viewers for model
   grids (ICON, HEALPix) and 3D cubes, used for WCRP hackathon / DYAMOND / EERIE data.
10. **Earthmover Flux** (launched April 2025): commercial WMS/EDR/OPeNDAP gateway on Arraylake/Icechunk.
11. **DestinE**: ECMWF/ESA/EUMETSAT digital-twin platform. Visualisation by Exprivia "Vision" (2D/3D/VR)
    and a web preview app ("miniDEA"). I could **not verify** any open-source Zarr/HEALPix web viewer
    from DestinE.
12. Searched but **not found** as distinct projects: "xarray-tiles", "zarr-tiles" (the names are used
    loosely; the real projects are the ones above).

---

## Key takeaways

1. **No mature server emits float data tiles from multiscale native-grid Zarr.** xpublish-tiles has
   the right LOD and cost logic but only outputs PNG/JPEG/MVT. TiTiler has `npy` but no automatic
   multiscale selection for xarray (only titiler-eopf does this). GeoLoupe's server design fills a
   real gap. Copy xpublish-tiles' ideas: per-tile overview selection (log-space nearest, tie → coarser),
   a byte budget → HTTP 413, and a `minzoom` computed from the coarsest level and advertised in TileJSON.
2. **Write zarr-conventions `multiscales` v0.1 + `proj` + `spatial` exactly** (declare all three in
   `zarr_conventions`; `spatial:transform` in rasterio `[a,b,c,d,e,f]` order; put `spatial:transform`/
   `spatial:shape` on the layout entries *and* on each level group). Then xpublish-tiles, xpublish-edr,
   zarr-layer, deck.gl-zarr, OpenLayers GeoZarr and titiler-eopf can all read GeoLoupe's pyramids.
   Pin the `v0.1` schema URLs: v1 is expected "before end of 2026" and may break things.
3. **Do not use ndpyramid's layout for new data.** It is Web-Mercator-only and uses an old
   `multiscales[0].datasets` shape. topozarr is the native-CRS successor, and you could reuse or extend it.
4. **Target MapLibre v6** (ESM, WebGL2). `addProtocol(name, async (params, abort) => ({data}))` can
   return an ArrayBuffer of encoded bytes or (officially since 6.11) an `ImageBitmap`. Raster sources
   are 8-bit RGBA, so float32 → colormap must happen in the handler (worker + OffscreenCanvas), in a
   WebGL2 custom layer, or through `raster-dem` custom encoding + `color-relief` (GPU colormap as a
   style expression, 24-bit precision).
5. **Before building a custom renderer, check zarr-layer and the OpenLayers WebGLTile/GeoZarr code.**
   zarr-layer already does per-tile level choice, native-CRS reprojection on the GPU, globe with poles,
   and client queries on MapLibre. GeoLoupe's extra value is server-side **multi-dataset per-pixel
   compositing**, which no client library does across datasets of different resolution.
6. **Use OGC API – EDR (position/area/cube/trajectory) for point-click values and time series**, and
   consider publishing the float tiles as OGC API – Tiles *coverage* tilesets. xpublish-edr can be
   mounted next to the tile router at almost no cost.
7. **Low zoom needs real stored overviews, not just on-the-fly coarsening.** rio-tiler loads
   full-resolution clips (1 GB cap), and xpublish-tiles coarsens after loading. Both become slow or
   return 413 without pyramids. Icechunk snapshot ids make good cache keys for immutable tiles.
8. **The ecosystem is pre-1.0 and changes monthly** (xpublish-tiles: 74 releases in 15 months;
   deck.gl v10 announced; MapLibre v6 three months old; GeoZarr conventions "Pilot"). Keep the
   metadata layer and the renderer behind thin adapters so you can swap them.

---

### Main sources
- https://github.com/earth-mover/xpublish-tiles (source: `config.py`, `multiscale.py`, `types.py`, `tiles_lib.py`, `render/raster.py`)
- https://pypi.org/project/xpublish-tiles/ · https://github.com/xpublish-community/xpublish · https://github.com/xpublish-community/xpublish-edr
- https://github.com/developmentseed/titiler (CHANGES.md, `titiler/xarray/io.py`) · https://github.com/cogeotiff/rio-tiler (`io/xarray.py`, `utils.py`, `constants.py`)
- https://github.com/developmentseed/titiler-multidim · https://github.com/developmentseed/titiler-cmr · https://github.com/EOPF-Explorer/titiler-eopf
- https://github.com/zarr-conventions/multiscales · https://github.com/zarr-conventions/spatial · https://github.com/zarr-conventions/proj · https://github.com/zarr-developers/geozarr-spec · https://geozarr.org/roadmap
- https://github.com/carbonplan/ndpyramid · https://github.com/carbonplan/topozarr · https://github.com/ome/ngff-spec
- https://github.com/carbonplan/zarr-layer · https://github.com/carderne/zarr-gl · https://github.com/carbonplan/maps · https://github.com/developmentseed/deck.gl-raster
- https://github.com/maplibre/maplibre-gl-js (CHANGELOG.md, `src/source/protocol_crud.ts`, `src/util/config.ts`)
- https://deck.gl/docs/whats-new · https://deck.gl/docs/api-reference/maplibre/overview
- https://github.com/openlayers/openlayers (changelog v10.8.0–v10.11.0, `src/ol/source/GeoZarr.js`)
- https://www.ogc.org/standards/ogcapi-edr/ · https://docs.ogc.org/is/20-058/20-058.pdf · https://ogcapi.ogc.org/
- https://developmentseed.org/datacube-guide/latest/visualization/client-side-comparison.html
- https://eox.at/2026/02/visualizing-geozarr/ · https://developmentseed.org/blog/2026-02-13-eopf-explorer-launch
- https://github.com/google/neuroglancer/blob/master/src/datasource/precomputed/volume.md
- https://meetingorganizer.copernicus.org/EGU26/EGU26-9862.html (GridLook)
- npm registry and PyPI JSON APIs for all version/date numbers (queried 2026-11-05).
