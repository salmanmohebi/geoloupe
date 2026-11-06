# Configuration reference

A TerraScope configuration is a YAML file. Only `datasets` is required. Check a configuration with
`terrascope info config.yaml`; it lists every dataset, variable, grid, resolution level and time range,
and every error.

```yaml
title: My project                       # shown in the browser
description: …                          # optional: shown on the gallery start screen

datasets:                               # gridded data, opened with xarray
  <dataset-id>:                         # letters, digits, -, _ (no ":")
    title: ERA5 reanalysis (0.25°)
    description: …
    attribution: ERA5 © ECMWF / Copernicus   # shown in exports
    path: data/era5.zarr                # file, directory, glob ("data/*.nc"), list, or URL
    # levels: [a_30s.zarr, a_0p025.zarr, a_0p25.zarr]   # instead of `path`: the same data at several resolutions
    # mosaic: [N45E006.tif, N45E007.tif]                # instead of `path`: files that tile an area
    # stac:                             # instead of `path`: the files of a STAC search or item
    #   url: https://earth-search.aws.element84.com/v1   # a STAC API (or `item: <item URL>`)
    #   collections: [sentinel-2-l2a]
    #   bbox: [6.8, 45.6, 8.8, 46.6]
    #   datetime: 2023-08-01/2023-08-31
    #   query: {"eo:cloud_cover": {"lt": 5}}
    #   limit: 10                       # at most this many items
    #   assets: [visual, red, nir]      # default: every COG asset with role data/visual
    #   sortby: cloud                   # cloud (least cloudy wins) | newest | oldest
    # variable_name: elevation          # name of the single variable of a GeoTIFF or mosaic
    overviews: data/era5.overviews.zarr # optional, written by `terrascope overviews build`
    engine: auto                        # auto | zarr | netcdf | grib | geotiff | kerchunk
    variables: [t2m, tp]                # default: all gridded variables
    crs: EPSG:3035                      # only if the metadata does not say it
    x: x                                # dimension names, only if not detected
    y: y
    time: time
    time_range: ["1940-01-01", "2026-10-30"]   # keep part of the time axis (either end may be null)
    time_tolerance: 12h                 # default: half a step (monthly+), else at most 12 h
    storage_options: {anon: true}       # fsspec options for remote data
    open_kwargs: {}                     # passed to xarray.open_* (e.g. {decode_timedelta: true})
    decode:                             # files without CF scale/offset (e.g. Sentinel-2 COGs,
      B04: {scale: 0.0001, offset: 0}   # whose scale is only in STAC): value × scale + offset
    styles:                             # display defaults per variable
      t2m: {title: 2 m temperature, colormap: RdYlBu_r, vmin: 250, vmax: 310, units: K}
      lc:                               # categorical data: class value → name (and colour)
        classes: {10: {name: Tree cover, color: "#006400"}, 80: {name: Water}}

layers:                                 # composites of several sources (optional)
  <layer-id>:                           # no ":"
    title: Air temperature (1 km where available)
    description: …
    colormap: RdYlBu_r                  # style defaults as above
    vmin: 250
    vmax: 310
    priority: resolution                # resolution (finest first, default) | listed (config order)
    # rgb: true                         # RGB composite: exactly 3 sources = red, green, blue
    # gamma: 1.3                        #   display gamma; vmin/vmax are the stretch
    sources:
      - {dataset: regional, variable: tas}
      - {dataset: era5, variable: t2m, scale: 1.0, offset: 0.0, min_zoom: 0, max_zoom: 22}
      # - {dataset: img, variable: rgbn, dims: {band: nir}}   # a fixed band of a multi-band image
      # - {dataset: era5, variable: orography, time: "1940-01-01T00:00"}   # one fixed time (static field)

points:                                 # point observations: layers of their own, and overlays
  <points-id>:                          # no ":"; must differ from the dataset ids
    title: Weather stations
    attribution: …
    path: stations.parquet              # CSV/TSV, Parquet (file or hive-partitioned folder),
                                        # GeoParquet, or CF timeSeries NetCDF/Zarr; a list of files
                                        # or URLs (e.g. one CSV per station) is read as one table
    format: auto                        # auto | csv | parquet | netcdf | zarr
    columns:                            # only what detection gets wrong (case-insensitive):
      id: station_id                    #   station_id, station, id, site …
      name: station_name                #   station_name, name …
      lon: lon                          #   lon, longitude, lng, x
      lat: lat                          #   lat, latitude, y
      elevation: elevation_m            #   elevation_m, elevation, altitude …
      time: date                        #   date, time, datetime, timestamp …
      variable: element                 # long tables: element, variable, parameter …
      value: value                      #   …and value, val, obs
      geometry: geometry                # GeoParquet: WKB point column instead of lon/lat
    filters: {duplicate: false, qc_flag: 0}   # equality filters on every query
    time_step: 1D                       # very large Parquet tables: list times by this step
    variables:                          # optional: which variables, and unit conversion
      tg: {offset: 273.15, units: K}    #   value × scale + offset; for a wide CSV, only these
                                        #   columns (and id, name, position, time) are read
    layers: {tas: tg, "regional:tas": tg} # default overlay of gridded layers

vectors:                                # overlays: regions, rivers, tracks, sites (optional)
  <vectors-id>:                         # letters, digits, -, _
    title: Catchments
    attribution: …
    path: catchments.gpkg               # GeoJSON, Shapefile (also .zip), GeoPackage, FlatGeobuf,
                                        # KML, GML, GeoParquet; path or URL
    layer: level6                       # layer of a multi-layer file
    where: "area_km2 > 100"             # attribute filter (OGR SQL; not for GeoParquet)
    properties: [name, area_km2]        # attributes sent to the browser (default: all)
    label: name                         # shown on hover (default: a name-like attribute)
    simplify: 0.001                     # tolerance in degrees (≈ 100 m); default: none
    color: "#1d4f91"                    # lines, outlines, points
    width: 1.2
    fill: "#1d4f91"                     # polygon fill (default: outlines only)
    fill_opacity: 0.15
    visible: false                      # shown when the page opens

views:                                  # featured views: the gallery start screen (optional)
  - title: Europe's heatwave, July 2023
    description: Afternoon temperatures above 40 °C …
    layer: temperature                  # any layer id: composite, <dataset>:<variable>, rgb, points
    time: "2023-07-18T14:00"
    center: [14, 42]                    # lon, lat
    zoom: 3.6
    colormap: RdYlBu_r                  # optional colour scale: colormap, vmin, vmax, log
    vmin: 5
    vmax: 45
    # globe: true                       # optional: globe projection, basemap: minimal | light | dark
    # compare: other-layer              # optional: a second layer, compare_mode: split | diff,
    #                                   #   compare_time; overlay: <points-id> or none
    # vectors: [regions]                # optional: the overlays shown in this view

settings:
  max_read_cells: 6000000               # per tile and source; above → next coarser level or skip
  tile_size: 256
  tile_cache_mb: 256                    # server tile cache
  field_cache_cells: 4000000            # grids up to this size are read whole, once per time step
  field_cache_mb: 512                   #   and shared by all tiles (0 cells: off)
  max_timeseries_steps: 20000
  max_export_mb: 512
  max_vector_mb: 60                     # GeoJSON size limit of one overlay
  cors_origins: ["*"]
  allow_open: false                     # "Add data" in the browser may open remote URLs
  open_roots: []                        #   … and local files below these folders
```

## Notes

* **Paths** may use environment variables, `${NAME}` or `${NAME:-default}`. An unset variable without a
  default is an error (a silently empty path usually points at the wrong data). Relative paths are
  relative to the configuration file.
* **Every gridded variable is a layer** with the id `<dataset-id>:<variable>`. `layers:` is only needed
  for composites.
* **Units in composites.** All sources of a layer must end up in the same units. Use `scale` and
  `offset` per source (`value × scale + offset`), e.g. `scale: 1000` for m → mm or `offset: -273.15` for
  K → °C. TerraScope does not convert units automatically.
* **Resampling.** `mean` (default) suits continuous fields. `min` and `max` are also available.
  Categorical data (classes) is never averaged: tiles use `nearest`, and overviews built with
  `--method mode` keep the most frequent class of each block.
* **Categorical data** (land cover, masks): class codes with names and colours come from CF
  `flag_values` / `flag_meanings` / `flag_colors`, from a GeoTIFF palette (colours), and from
  `styles: {<var>: {classes: …}}` in the config (names; colours if the file has none). The browser
  colours each class exactly, the hover shows the class name, and the legend lists the share of each
  class in the view.
* **Mosaics.** `mosaic:` lists files that tile an area (DEM tiles, satellite granules); they become one
  layer `<dataset>:<variable_name>`, opened in parallel. A composite layer can use a mosaic like any
  dataset (`{dataset: dem, variable: elevation}`): it stands for all its files.
* **Curvilinear grids** (2-D longitude/latitude per cell: ocean models such as NEMO, ROMS, MOM and
  POP, WRF, CMIP6 native grids, satellite swaths) are detected from CF `standard_name`/`units` or
  common names (`nav_lon`, `XLONG`, `lon_rho`, …); `x` and `y` may name the 2-D longitude and
  latitude coordinates. Each pixel shows the nearest cell centre (KD-tree; empty beyond ¾ of a cell,
  so the grid's outline stays sharp). When a variable has both 1-D axes and 2-D longitude/latitude,
  the 2-D coordinates win unless the variable declares a CRS (`grid_mapping` or `crs:`): model
  grids often carry 1-D *nominal* axes. Coarser levels for low zooms are made on the fly by taking
  every 2nd, 4th, … cell (shown as "subsampled").
* **The read budget** (`max_read_cells`) bounds the work per tile and source. With overviews, every
  zoom has a cheap level. Without them, a large source appears only from the zoom at which its native
  cells fit the budget; the UI shows that zoom and says why the source is hidden.
* **Point observations** are datasets of their own: every variable is a layer `<points-id>:<variable>`
  with its own colour scale and time axis; clicking a station shows its series. Any point layer can
  also be drawn over a gridded layer (choose it in the layer panel; `layers:` sets the default), in
  the grid's colour scale, so a station that disagrees with the grid stands out. Wide tables (one
  column per variable) and long tables (variable + value columns) both work; columns are detected,
  so `terrascope serve stations.csv` needs no configuration. Parquet tables are read with filter
  push-down (only the requested day; hive `year=` partitions are pruned); CSV tables are read once into memory. Station values keep their own
  time convention (often the local day): compare with gridded data on other day windows with care.
* **Satellite rasters (GeoTIFF / COG).** Internal overviews of a Cloud-Optimized GeoTIFF are used
  as resolution levels automatically (no `terrascope overviews build` needed). Nodata becomes missing,
  and the TIFF's own scale/offset tags are applied; use `decode` when the scale is known only from
  elsewhere (STAC). Several bands become a `band` dimension (named after the band descriptions).
  Over HTTP(S) and S3, only the needed blocks are read; for public S3 buckets set
  `storage_options: {anon: true}`.
* **RGB composites.** A layer with `rgb: true` shows three sources as red, green and blue, stretched
  from `vmin` to `vmax` with a display `gamma` (both adjustable in the browser). A source can pick one
  band of a multi-band image with `dims: {band: …}`. Every image with three or four bands (e.g. a
  Sentinel-2 TCI or an aerial photo), or with bands named red/green/blue, also gets an automatic
  layer `<dataset>:<variable>:rgb`.
* **Long time axes.** Layers with more than 20 000 regular time steps (e.g. decades of hourly data)
  are described to the browser by start, step and count instead of a list of labels.
* **Vector overlays** (`vectors:`) are drawn above the data and below the stations and the map's
  labels; each has a checkbox in the layer panel, and hovering a feature shows its label and a few
  attributes next to the data value. The server reads a file once, reprojects it to lon/lat and sends
  GeoJSON (gzip); the browser simplifies per zoom. They are meant for thousands to a few hundred
  thousand features: above `max_vector_mb` the server asks for `simplify`, `where` or fewer
  `properties`. `terrascope serve regions.geojson grid.zarr` shows a vector file as an overlay.
  Needs `terrascope[vector]` (pyogrio, shapely).
* **STAC.** `stac:` runs one search (or reads one item) when the server starts. Every asset key becomes
  a variable, shown as a mosaic over the items found: `<dataset>:red`, `<dataset>:nir`, … The best
  item (by `sortby`) wins where items overlap. Scale and offset come from the STAC `raster:bands`
  metadata (also STAC 1.1 `bands`); `decode` overrides them. Items with
  `earthsearch:boa_offset_applied: true` (Element84) already have the offset in their pixels, so it
  is not applied again. 3-band assets (e.g. `visual`) also get a true-colour layer
  `<dataset>:visual:rgb`, and an RGB layer can use three STAC assets as channels. `s3://` asset
  links are read through public HTTPS. APIs that require signed URLs (Planetary Computer) are not
  supported yet.
* **Featured views** (`views:`) are listed on a start screen (the gallery) with a preview image each.
  Opening a view is the same as opening a shared link with that state. A view whose layer is missing
  (for example, its dataset failed to open) is left out, with a warning in the server log.
* **Remote Zarr without `.zarr` in the URL** (e.g. the CMIP6 archive) is recognised by its metadata
  files (`zarr.json`, `.zmetadata`, `.zgroup`); otherwise set `engine`.
* **Remote data**: `s3://`, `gs://`, `https://` paths work for Zarr (through fsspec), NetCDF, GeoTIFF/COG
  (through GDAL) and CSV/Parquet. Install `terrascope[remote]`. Pass credentials through
  `storage_options` or the usual environment variables. Network failures at start-up (DNS, timeouts,
  HTTP 5xx) are retried twice; a missing file is reported at once.

## Overviews

```
terrascope overviews build SOURCE OUTPUT.zarr [--variables a,b] [--start 1978-01-01 --end 1978-12-31]
                                             [--min-factor 4] [--method mean] [--workers 16]
```

* Writes block aggregates at factors `min-factor, 2·min-factor, …` until the grid is about 512 cells
  wide, each **computed from the native data** (exact means, also where blocks are partly missing).
* Keeps integer packing (`scale_factor`, `_FillValue`) of the source, so overviews are small:
  about 1/12 of the source with `--min-factor 4`.
* Time steps are processed in parallel (`--workers`). The time range can be a subset; times without
  overviews fall back to finer levels.
* Metadata: Zarr conventions `multiscales`, `proj`, `spatial` v0.1 (GeoZarr), readable by other tools.

## Environment variables

None are required. `terrascope serve` options: `--host` (default `127.0.0.1`), `--port` (default
`8000`), `--log-level`, `--allow-open` (the browser's "Add data" may open `https://`, `s3://` and
`gs://` URLs) and `--open-root DIR` (it may open local files below `DIR`; repeatable; relative
paths start in the first root). Both are off by default: anyone who can reach the server can then
make it read those, so keep such a server on localhost or behind a login. `terrascope gallery` takes the same options and serves the configuration of
open public data that ships with TerraScope (`--show-config` prints it).
