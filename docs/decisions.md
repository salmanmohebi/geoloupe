# Decision log

Each entry: the problem, the decision, the alternatives, and the consequences. Newest last. Revisit an
entry when its "revisit when" condition happens.

---

### D1 · Send float data tiles; colour them in the browser (2026-11-05)

**Problem.** Scientific exploration changes colormaps and ranges all the time and needs the values
under the cursor. Image tiles (PNG) bake the colour scale in and lose the values.

**Decision.** The server sends float32 tiles (byte-shuffled, gzip, plus a per-pixel source index). The
browser keeps them in a cache and colours them. PNG tiles exist as a secondary endpoint for other tools.

**Alternatives.** Server-rendered PNG (TiTiler, xpublish-tiles, many project viewers): every
colour change refetches every tile; hover needs a request. `.npy` tiles (TiTiler): not designed for
browsers. Packing floats into RGB for MapLibre `raster-dem` + `color-relief`: GPU colouring, but a fixed
quantisation per layer and no transparency for missing data.

**Consequences.** Instant restyling, hover values without requests, difference maps in the browser.
Measured on a 1 km daily store, gzip data tiles are 10–100 kB (35 kB for a native tile at zoom 8, ~100 kB
for low-zoom tiles of block means) against ~30 kB for a PNG of the same tile: comparable at high zoom,
up to 3× larger at low zoom. Quantised encodings would close the gap for slow links. *Revisit when*
serving over the public internet.

### D2 · Composite several datasets per pixel on the server (2026-11-05)

**Problem.** "Google Maps for climate data": a 1 km regional product and a 30 km global product should
be one seamless map.

**Decision.** A layer lists sources. Per tile and per pixel, the first source (by default the finest)
with a finite value wins; coarser sources fill the rest. The tile records which source filled each pixel.

**Alternatives.** Client-side stacking of two map layers (the fine layer drawn over the coarse one):
simple, but hover/inspection would have to query both, the level logic would be duplicated, and other
API clients would not get composites. Blending at the boundary: smoother, but changes values.

**Consequences.** One request per tile regardless of the number of sources; the fine source hides
coarse data completely where it has data (no blending). Units must match (per-source `scale`/`offset`).

### D3 · Overviews in the native grid, stored outside the source (2026-11-05)

**Problem.** Low zoom levels of a 20 880 × 43 200 grid cannot be computed on request.

**Decision.** `terrascope overviews build` writes block means at factors 2^k into a separate Zarr v3
store, in the source grid and CRS, with the source's integer packing, and GeoZarr `multiscales` +
`proj` + `spatial` v0.1 metadata. Each level is computed from the native cells (exact means). The
catalog also accepts multiscale stores and explicit lists of copies at other resolutions.

**Alternatives.** Web Mercator pyramids (ndpyramid, carbonplan/maps): tied to one display projection,
resampling baked in, values no longer cell means of the source. Overviews inside the source store: not
possible for read-only archives and mixes provenance. On-the-fly coarsening only (xpublish-tiles,
rio-tiler): bounded by read cost; cannot serve zoom 0 of a billion-cell grid.

**Consequences.** Values at every zoom are block means of source cells. Storage ≈ 1/3 of the source
(1/12 with `--min-factor 4`, which on-the-fly aggregation covers). Overviews can cover a subset of
times. *Revisit when* GeoZarr conventions reach v1 (expected end of 2026; may break v0.1).

### D4 · Per-tile level choice with a read budget (2026-11-05)

**Problem.** Bound the cost of every request without hand-tuned zoom limits per dataset.

**Decision.** Pick the coarsest level whose cells are no larger than a screen pixel (both axes). If the
read window exceeds `max_read_cells`, try coarser levels; else skip the source with reason `cost`. The
layer metadata advertises, per source, the zoom from which it fits the budget.

**Alternatives.** Fixed zoom ranges per level (common in project viewers); the nearest level in log space
(xpublish-tiles default; slightly blurrier); a byte limit with HTTP 413 (xpublish-tiles, rio-tiler) — a
whole-tile error is worse than a missing source in a composite.

**Consequences.** A composite stays usable at every zoom: the coarse source shows where the fine one
is too expensive, and the UI says "<dataset> appears when you zoom in".

### D5 · Exact block means through an integral image (2026-11-05)

**Problem.** Nearest-neighbour decimation of fine data at intermediate zooms produces aliasing
(speckled precipitation fields) and is not a meaningful aggregate.

**Decision.** For lat/lon grids, each pixel is the mean of the cells whose centres fall inside it,
computed for all pixels at once from a summed-area table (float64). `nearest`, `min` and `max` are
configurable; categorical variables default to `nearest`.

**Alternatives.** GDAL warp per tile (rio-tiler): heavier dependency, per-tile overhead. Datashader
quadmesh (xpublish-tiles): proven, adds numba/datashader. Area-weighted conservative remapping: exact
but more costly; on the roadmap for overview building.

**Consequences.** Smooth, honest maps at every zoom; values verified against brute force in the tests.
Projected grids use nearest neighbour for now.

### D6 · Own tile engine instead of TiTiler or xpublish-tiles (2026-11-05)

**Problem.** Reuse versus fit.

**Decision.** A small engine (numpy + pyproj + xarray) with the features above, structured so that the
planning step per grid type can be extended.

**Alternatives.** TiTiler (`titiler.xarray`) — no automatic multiscale for xarray, GDAL warp per tile,
PNG/npy output. xpublish-tiles — the best grid coverage (curvilinear, UGRID, HEALPix) and cost model,
but server-side colouring only, and no cross-dataset compositing.

**Consequences.** ~500 lines to own and test (brute-force tests included). Curvilinear and unstructured
grids are not supported yet; xpublish-tiles is the reference for adding them. *Revisit when* one of these
projects supports float tiles and cross-dataset compositing.

### D7 · MapLibre GL JS v6 with a custom protocol (2026-11-05)

**Decision.** MapLibre requests `terrascope://` tiles; a protocol handler fetches float tiles, colours
them on the CPU and returns an `ImageBitmap`. Changes are double-buffered (new source on top, old one
removed when loaded).

**Alternatives.** deck.gl `TileLayer` with a colormap shader (more code, a second rendering stack);
OpenLayers WebGLTile/DataTile (GPU colormaps and any CRS — the strongest alternative and the fallback
if we need non-Mercator tile grids before MapLibre's custom projections mature); CesiumJS (3D, heavy);
Leaflet (no WebGL).

**Consequences.** Vector basemaps with labels above the data, globe projection, terrain later; small
amount of rendering code. MapLibre v6 is ESM-only: the worker file must be emitted by the bundler and
set with `setWorkerUrl` (done in `lib/protocol.ts`).

### D8 · Two kinds of values: displayed (hover) and stored (click) (2026-11-05)

**Decision.** Hover reads the displayed pixel from the cached tile (instant; names source and level).
Click asks the server for the native cell of every source and their time series.

**Consequences.** No ambiguity about what a number means; the inspector shows the native cell centre
of each source.

### D9 · Configuration: YAML, every variable a layer, composites explicit (2026-11-05)

**Decision.** A small YAML file with `datasets`, optional `layers` (composites) and `settings`; env
variables in paths; relative paths resolved against the file. `terrascope serve <dataset path>` works
without any config.

**Alternatives.** STAC catalogs as the primary registry (richer, heavier; planned as import/export);
Python-only configuration (less shareable).

### D10 · Shareable state in the URL hash (2026-11-05)

**Decision.** Layer, time, dims, colour scale, view, comparison and inspected point are written to the
URL hash (replaceState). Opening the link restores the view exactly.

**Consequences.** Reproducible views without a database; links work across users of the same server.

### D11 · Featured views are link states (2026-11-06)

**Problem.** People who open TerraScope for the first time need a starting point: interesting data,
places and times, not an empty map and a layer list.

**Decision.** A configuration lists `views:` (layer, time, place, colour scale, comparison). The client
shows them as a gallery start screen when the URL has no state. Opening a view sets the URL hash and
goes through the normal start-up that restores shared links. Previews come from a server endpoint that
stitches the map's own tiles into a centred image of any size.

**Alternatives.** A separate "story" or "tour" state machine (more code, a second way to set the map);
static thumbnails in the repository (go stale when data or styles change); one plain PNG tile per
view (cheap, but rarely centred on the place of interest).

**Consequences.** A view can never do anything a link cannot; every view is reproducible and shareable.
Opening a view reloads the page (simple and robust; costs ~1 s).

### D12 · GeoTIFF reads without a global lock (2026-11-06)

**Problem.** `rioxarray.open_rasterio` uses one process-wide lock by default. Every read from every
GeoTIFF in the server ran one at a time; over HTTPS each read waits for network round trips, so tiles
of remote COGs queued behind each other (16 cold Sentinel-2 tiles: 13.3 s).

**Decision.** Open GeoTIFFs with `lock=False`: each read uses its own GDAL handle (GDAL is thread-safe
with one handle per thread), and GDAL's caches keep re-opening cheap.

**Consequences.** The same 16 tiles take 2.0 s. More file handles are opened (one per read); with
local files this costs little, with remote files the header is cached by GDAL. *Revisit when* reading
many local GeoTIFFs on a file system with slow `open()`.

### D13 · Curvilinear grids through the nearest cell (2026-11-06)

**Problem.** Ocean and regional models, CMIP6 native grids and satellite swaths store 2-D longitude
and latitude per cell; they were skipped.

**Decision.** Find the nearest cell centre of every pixel with a KD-tree on unit vectors, leave pixels
beyond ¾ of a cell empty, and reuse the projected-grid plan (per-pixel row/col). Coarser levels are
strided views (every 2nd, 4th, … cell), labelled "subsampled". The 2-D coordinates win over 1-D axes
unless the variable declares a CRS.

**Alternatives.** Regridding to lat/lon first (xESMF): changes values and needs a target grid;
datashader quadmesh rasterisation (xpublish-tiles): exact cell polygons, heavier dependency; per-cell
polygons in the browser: does not scale.

**Consequences.** Values are the stored cells (nearest, no averaging) at every zoom; the date line,
poles and tripolar folds need no special code. Building the tree and the cell sizes costs ~0.5 s per million
cells, once per level; locating the 65 536 pixels of a tile takes ~30 ms. *Revisit when* block-mean overviews for curvilinear grids are needed (very large grids).

### D14 · Whole fields of small grids are read once per time step (2026-11-06)

**Problem.** Many stores chunk small grids as whole fields or many time steps per chunk (ERA5, CMIP6).
Every tile then decompresses the same chunk again: 3.9 s per batch of tiles when panning a CMIP6 map.

**Decision.** Grids up to `field_cache_cells` (4 M) are read whole once per time step into a
byte-bounded LRU cache with one lock per field; tiles cut their windows from it.

**Consequences.** Panning and zooming at one time step is instant after the first read (0.15 s for 8
tiles). The first tile of a large window reads the whole field even if it needs part of it: bounded by
the size limit.

