# Roadmap

Features are ranked by value for researchers relative to their cost. "Done" lists what 0.1 contains,
so the gaps are visible.

## Done in 0.1

* Engine: rectilinear grids in any CRS, both longitude conventions, antimeridian, Gaussian grids;
  per-tile level choice with a read budget; exact block means (integral image); nearest/min/max;
  per-pixel compositing of several datasets; float data tiles with a per-pixel source map; PNG tiles
  and TileJSON for other tools.
* Data: Zarr v2/v3 (incl. sharding), NetCDF 3/4, multi-file NetCDF, GRIB, GeoTIFF/COG, kerchunk
  references, remote stores through fsspec; numpy and cftime calendars; extra dimensions.
* Overviews: external native-grid pyramids with GeoZarr `multiscales`/`proj`/`spatial` v0.1 metadata,
  packing preserved, parallel over time, time subsets.
* Queries: native point values of all sources, time series (parallel block reads), statistics,
  NetCDF export of the view at native resolution.
* Web client: layer and dimension selection, 41 colormaps, range/log/opacity, histogram of the view,
  time slider and playback, provenance badge, hover values with source, inspector with time-series chart
  and CSV, side-by-side and difference comparison, offline and online basemaps, globe, PNG export,
  shareable links, light/dark themes.
* Point observations: long or wide tables (Parquet with filter push-down, CSV), unit conversion,
  drawn in the layer's colour scale, station series in the inspector.
* Python: `terrascope serve | gallery | info | demo-data | overviews build`; `terrascope.show()` for
  notebooks.
* CI (GitHub Actions): pytest on Python 3.11–3.13, ruff, mypy, vitest, eslint, build, browser smoke
  test on demo data, and the gallery test on live public data (reported, not blocking).

## Done: any kind of data, not only gridded reanalyses (2026-11-06)

TerraScope is a general explorer: anyone should be able to open, visit and play with the main kinds of
geoscience data — reanalyses and model output, satellite imagery, stations, elevation, land cover —
from public sources or their own files.
Each item is done when it has tests, docs, an example with real public data where possible, and
passes the browser smoke test.

- [x] **1. Stations and point data as first-class datasets.** Point layers of their own (own colour
      scale, time slider, click → station series), not only overlays. Readers: CSV, Parquet,
      GeoParquet, NetCDF CF discrete sampling geometry (`featureType: timeSeries`). Long and wide
      tables; columns detected automatically (`terrascope serve stations.csv` just works). Any point
      layer can be overlaid on any gridded layer from the UI.
- [x] **2. Satellite rasters (GeoTIFF / COG) done right.** (Also: remote Zarr over HTTPS, and
      compact long time axes — ARCO-ERA5's 1.3 million hourly steps.) Internal COG overviews used as resolution
      levels; scale/offset and nodata; band names; remote COGs over HTTPS/S3. Tested on a real
      Sentinel-2 scene (public AWS open data).
- [x] **3. RGB composites.** True/false colour from three bands or variables, with per-band stretch
      and gamma in the browser; automatic for 3-band images (e.g. Sentinel-2 TCI).
- [x] **4. Categorical data.** Classes with names and colours (CF `flag_values`/`flag_meanings`,
      GeoTIFF palette, config); class legend with shares of the view; hover shows the class name;
      never averaged (nearest in tiles, majority `mode` in overviews); class colours in PNG tiles.
      Tested on ESA WorldCover (remote COG).
- [x] **4b. A gallery of open public data** (`terrascope gallery`). ERA5 (ARCO, hourly since 1940),
      Copernicus DEM 30 m mosaic over ERA5 orography, Sentinel-2 true/false colour, ESA WorldCover,
      NOAA GHCN-Daily stations — all read live from public buckets. Featured `views:` as a start
      screen with centred preview images (`/preview.png`); every view is a shareable link. On the way:
      parallel GeoTIFF reads (6.6× faster cold tiles), 16× faster opening of long time axes,
      `time_range`, mosaics in composites, retries of remote opens, side-by-side with different units.
- [x] **5. Curvilinear grids.** 2-D latitude/longitude coordinates (NEMO, ROMS, MOM, POP, WRF, CMIP6
      native grids, swaths): nearest cell through a KD-tree on unit vectors (date line and poles
      work), empty beyond ¾ cell, subsampled levels for low zooms, point queries, time series and
      export. Nominal 1-D axes lose to the 2-D coordinates unless a CRS is declared. Gallery: the
      GFDL-ESM4 ocean (CMIP6). On the way: a whole-field cache for small grids (CMIP6 pan/zoom
      3.9 s → 0.15 s), Zarr detection for URLs without `.zarr`, time labels rounded to the minute,
      shared links that keep the comparison time. Later: overviews (block means) for curvilinear
      grids.
- [x] **6. Open data without a config.** `terrascope serve a.zarr b.tif stations.csv regions.geojson`;
      an "Add data" dialog in the browser (gridded data, stations or vectors; path or URL), only
      when the server is started with `--allow-open` (URLs) or `--open-root DIR` (local files below
      DIR). New datasets open through their own small catalog; failures are reported, not kept.
- [x] **7. Vector overlays.** GeoJSON, Shapefile (also zipped), GeoPackage, FlatGeobuf, KML, GML and
      GeoParquet; local or remote; reprojection, `where` filters, simplification, size limit; drawn
      between the data and the stations; hover shows label and attributes; overlays in links and
      views. Gallery: Natural Earth provinces, rivers and lakes. Later: colour by attribute, vector
      tiles for very large files.
- [x] **8. STAC.** A STAC search (collections, bbox, datetime, query, limit, sortby) or one item
      becomes one mosaic per asset, best item first; scale/offset from `raster:bands`; RGB layers
      whose channels are mosaics (per-channel compositing in the engine). Gallery: Sentinel-2 over
      the Western Alps from Earth Search. Later: time series of items (one time step per date),
      cloud masks (SCL), signed URLs (Planetary Computer).

Later in this series: unstructured meshes (UGRID, ICON, HEALPix), vector fields (wind arrows),
Icechunk. General analysis features (model error layers, station validation statistics, time-chunked
copies for long series) follow in the next section.

## Next (short term: weeks)

| Item | Why | Notes |
|---|---|---|
| **Station statistics** | Validation at a glance | Bias/RMSE of grid − stations in view, per day and over a period; optional alignment of local station days with UTC grid days |
| **Time-optimised copies for series** | Point series over 38 years are slow with map-chunked data | `alternates:` in the dataset config, e.g. a rechunked (time-contiguous) copy used only by `/timeseries` |
| **Overview builds on clusters** | Building overviews of multi-terabyte stores | Partition by time (template in `examples/hpc/`); resume; `--append` for new times |
| **Prefetch next time step** during playback | Smoother animation | Fetch the next frame's visible tiles into the cache before switching |
| **Area-weighted means** on lat/lon grids | Accuracy towards the poles | cos(lat) weights in overviews and in the integral image |
| **Colour-scale presets per variable** and diverging ranges centred on 0 | Fewer manual steps | from config and CF metadata |
| **Server-side difference endpoint** | Make A − B available to API clients and PNG export | Reuses the engine; same semantics as the browser |

## Medium term (months)

* **Curvilinear and unstructured grids** (2-D lat/lon, rotated grids with 2-D coordinates, UGRID,
  HEALPix, ICON): a KD-tree / per-cell rasteriser planner next to the separable and projected ones;
  follow xpublish-tiles' approach.
* **OGC API compliance:** EDR (`position`, `area`, `cube`) for point and area queries; OGC API – Tiles
  coverage tilesets for the float tiles; STAC import/export of the catalog (datacube + render
  extensions).
* **Other tile grids:** EPSG:4326 tiles and polar stereographic views (MapLibre v6.15 custom
  projections, or the OpenLayers renderer).
* **Transects and area statistics:** draw a line → profile; draw a polygon → time series of the area
  mean; download both.
* **GPU colouring** (WebGL2 custom layer or `color-relief`) if profiling on large screens shows the CPU
  path limits frame rate.
* **Compact tile encodings** for slow links (float16 where precision allows, per-tile quantisation with
  an error bound).
* **Icechunk** stores (versioned): snapshot ids in tile URLs for safe long-lived HTTP caching.
* **Jupyter widget** (anywidget) with two-way state: select in the notebook, see on the map, and back.
* **Model evaluation helpers**: derived layers for model − reference errors, skill relative to a
  baseline, and run comparison, driven by configuration.

## Long term (ideas)

* Vertical cross-sections through 3-D fields; 3-D volume rendering of atmospheric columns.
* Vector fields (wind particles, streamlines) from u/v components.
* Server-side plugins: user-defined derived variables (anomalies against a climatology, indices) as
  lazy xarray expressions.
* Multi-user deployment: authentication, per-user catalogs, shared annotations.
* Publishing: static export of a layer's pyramid to object storage for serverless viewing with
  client-side Zarr readers (zarr-layer, OpenLayers GeoZarr).

## Known issues (from the 2026-11-06 review; the others were fixed with regression tests)

* While a new time step or colour scale is being swapped in, hover reads the new (not yet shown) tiles
  for up to a fraction of a second.
* In difference mode the provenance badge lists only A's sources.
* The last row/column of an overview level is placed as if its block were full (see ARCHITECTURE §5).

## Open challenges and risks

| Risk | Impact | Mitigation |
|---|---|---|
| GeoZarr conventions are v0.1 "pilot"; v1 expected end of 2026 may break them | Overview metadata may need migration | Metadata writing is in one function; readers accept old and new forms; pin schema URLs |
| MapLibre v6 is new (ESM-only, WebGL2) | Bundler and browser compatibility issues | Worker URL handled explicitly; OpenLayers documented as fallback renderer |
| Map-chunked archives make time series slow | Poor experience for long series | Time-optimised copies (above); warn in the UI for long ranges |
| Float tiles are 3–5× larger than PNG | Slow over the internet | Compression options (above); local and HPC use is the main target now |
| Unweighted means and centre-based pixel assignment | Small biases near the poles and at data edges | Documented in ARCHITECTURE.md §5; area weighting planned |
| No authentication | Accidental exposure of data | Binds to 127.0.0.1 by default; documentation recommends a proxy for shared deployments |
