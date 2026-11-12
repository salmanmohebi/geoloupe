# Changelog

All notable changes to GeoLoupe. The format follows [Keep a Changelog](https://keepachangelog.com/),
and versions follow [Semantic Versioning](https://semver.org/) (before 1.0, minor versions may change
the configuration format; such changes are listed here).

## [Unreleased]

### Added

- Split panes, like tmux: split the map right or down as often as needed (up to 6 panes), drag the
  dividers to resize, and show a different layer, time and colour scale in each pane. The side
  panel, time bar and colour bar act on the active pane; each pane's corner shows its layer, time
  and colour range. Panes share the camera and the time unless unlinked (per pane). The hover
  read-out lists the value of every pane, and the other panes mark the point. Keys: `%` or `|`
  split right, `"` split down, `x` close, `z` zoom a pane, `Alt`+arrows move between panes.
  Presets (1, 2, 3, 4 panes) in the Compare menu. Links keep the whole layout.

- Browse S3 buckets in *Add data* (with `--allow-open`): list a bucket or prefix on AWS or an
  S3-compatible store (custom endpoint), filter names, see which entries GeoLoupe opens (Zarr,
  NetCDF, GeoTIFF/COG, tables, vector files; a folder that is a Zarr store), and open one in the
  active pane or a new pane. Public access, or the server's credentials (environment or a named
  AWS profile; the browser only sees profile names). Public objects of S3-compatible stores are read
  over path-style HTTPS.
- *Add data* opens the data behind a web viewer's link (`…/preview.html?store=s3://…&endpoint=…`,
  e.g. a GeoZarr preview page), with its S3 endpoint. GeoZarr multiscale stores (`multiscales`
  layout of levels `0`, `1`, …) open with every level used as an overview.
- The side panel shows which pane it acts on; pane titles name the dataset of a variable when the
  title does not.

### Fixed

- A `?` in a web address (query string) is no longer read as a file pattern, so signed or
  parameterised URLs open with the right reader; a web page (`.html`) gets a clear message.
- An `s3://` bucket that is not found on AWS suggests giving the endpoint of its S3 service.
- *Add data* takes a pasted address with quotes, angle brackets or an upper-case scheme; an address
  without `https://` (e.g. `host.org/bucket/data.zarr`) gets a clear message instead of "opening
  local files is off".

### Changed

- "Side by side" comparison is replaced by the panes; links from 0.1 (`cmp=split`) open as two panes.

## [0.1.1] - 2026-11-12

### Fixed

- HTTPS on macOS and Windows: the `geoloupe` command now trusts the system's certificate store
  (through `truststore`, a new dependency). With Python from python.org on macOS, ERA5, Sentinel-2
  and the CMIP6 ocean model failed with `CERTIFICATE_VERIFY_FAILED`; on Linux without a system list,
  `certifi`'s list is used. `SSL_CERT_FILE` and `SSL_CERT_DIR` still take precedence.
- A remote dataset that cannot be reached (certificate, DNS, timeout) now reports the connection
  error instead of "cannot tell the format"; certificate errors also say how to fix them.

## [0.1.0] - 2026-11-06

First public version.

### Data
- Gridded data from Zarr v2/v3, NetCDF 3/4 (also many files as one), GRIB, GeoTIFF/COG and
  kerchunk references; local, HTTPS, S3 (public buckets need no setup) and GCS.
- Grids: regular and irregular lat/lon in either longitude convention, projected CRSs, rotated pole,
  and curvilinear grids with 2-D longitude/latitude (ocean models, WRF, CMIP6 native grids).
- Layers that combine datasets of different resolution per pixel; mosaics of many files; STAC
  searches and items; RGB composites (true and false colour); categorical data with class names
  and colours; fixed-time sources for static fields.
- Stations and point data (CSV, Parquet, GeoParquet, CF timeSeries NetCDF) as layers and overlays.
- Vector overlays (GeoJSON, Shapefile, GeoPackage, FlatGeobuf, KML, GML, GeoParquet).
- Overview pyramids in the native grid (`geoloupe overviews build`, GeoZarr conventions v0.1).

### Web client
- Float data tiles coloured in the browser: instant colour changes, real values on hover and click,
  a badge that names the data and resolution on screen.
- Time slider and playback, time series, side-by-side and difference comparison, PNG and NetCDF
  export, shareable links, a gallery of featured views, "Add data" (off unless allowed).

### Tools
- `geoloupe serve | gallery | info | demo-data | overviews build`; `geoloupe.show()` in notebooks.
- The gallery: ERA5, Copernicus DEM, Sentinel-2 (STAC), ESA WorldCover, a CMIP6 climate model ocean,
  NOAA stations and Natural Earth overlays, all read live from public archives.

[Unreleased]: https://github.com/salmanmohebi/geoloupe/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/salmanmohebi/geoloupe/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/salmanmohebi/geoloupe/releases/tag/v0.1.0
