# Changelog

All notable changes to GeoLoupe. The format follows [Keep a Changelog](https://keepachangelog.com/),
and versions follow [Semantic Versioning](https://semver.org/) (before 1.0, minor versions may change
the configuration format; such changes are listed here).

## [Unreleased]

### Added

- **Error panes**: the error of one layer (P, e.g. a model) against another (T, e.g. observations)
  at every point: bias, absolute, squared and relative errors, symmetric relative error, ratio, log
  ratio, normalised error, event agreement (hit, miss, false alarm, correct no event) and sign
  agreement, or a custom formula of P and T (a small, safe formula language, the same in the browser
  and on the server). *Compare → Add error pane* makes one from two panes; it follows them (layer,
  time, level), and closing one keeps its layer. The side panel sets the definition, parameters,
  formula and sources, and swaps P and T. P is converted into T's units (K, °C, °F; mm, m;
  precipitation rates; speeds; pressures; %).
- Error maps are **exact from the native cells**: the browser shows a preview at once, then the
  server's tiles, computed on sub-pixels no larger than the finest native cell (errors that cancel
  inside a screen pixel still count). The pane box and the colour bar give the statistics of the view
  (bias, MAE, RMSE; POD, FAR, CSI and frequency bias for events); the status badge names P's and T's
  data and whether the map is exact. Point details show P, T and the error at the point, the error
  over time with mean, mean |·| and RMS, and error panes in "All panes at this point".
- `geoloupe errors build`: the error of P against T per native cell and time step, as Zarr with
  overviews; error panes asking for it are drawn from it, exact at every zoom.
- Error panes in featured views (`error:` with `metric`, `params`, `formula`, `reference`, `p_pane`,
  `t_pane`) and links (`cmp=error`, `em`, `ep`, `ef`, `eu`, `fp`, `ft`), with error thumbnails in the
  gallery. New gallery view: the GFDL-ESM4 model against ERA5, with bias and absolute error.
- API: `/api/error/tiles/{z}/{x}/{y}.bin`, `/api/error/stats`, `/api/error/preview.png`
  ([docs/errors.md](docs/errors.md)).
- **Time offsets** between linked panes: a pane shows the linked time shifted by years, months,
  days or hours ("the same day, 30 years earlier"); in the side panel, links (`to=-30y`) and views
  (`time_offset`).
- *Add data* lists the data opened before in this browser. `geoloupe serve` takes `--endpoint`,
  `--profile` and `--anon` for `s3://` paths.
- Playback loads the next frame's tiles in the background.

### Changed

- *Add data* opens remote URLs by default when the server listens on localhost only; a server on
  other interfaces needs `--allow-open`, and `--no-open` turns it off. The button is always shown;
  when opening is off, it explains how to start the server.
- The difference A − B of 0.2 is now an error pane (P − T); links with `cmp=diff` still open.
- Diverging colour scales are centred on 0 when the data crosses 0 (automatic range, Fit to view).

### Fixed

- The hover read-out and the view summary read the tiles on screen while a new time step or colour
  scale loads (they read the new tiles before they were shown).
- Overview levels no longer draw beyond the edge of the data when a grid size is not a multiple of
  the overview factor.
- The status badge of a difference (error) map names both layers.

## [0.2.0] - 2026-11-12

### Added

- Split panes, like tmux: split the map right or down as often as needed (up to 6 panes), drag the
  dividers to resize, and show a different layer, time and colour scale in each pane. The side
  panel, time bar and colour bar act on the active pane; each pane's corner shows its layer, time
  and colour range. Panes share the camera and the time unless unlinked (per pane). The hover
  read-out lists the value of every pane, and the other panes mark the point. Keys: `%` or `|`
  split right, `"` split down, `x` close, `z` zoom a pane, `Alt`+arrows move between panes.
  Presets (1, 2, 3, 4 panes) in the Compare menu. Links keep the whole layout.
- With several panes, the point details show every pane at the clicked point: its value now and its
  time series around the current time, one chart per unit and time step (hourly and monthly data
  are not mixed on one axis), each pane in its own colour.
- Export → *All panes as one image*: every pane as on screen, each with its label (layer, time) and
  colour bar, a title, and the credits of all datasets shown.
- Featured views with several panes: `panes:` (layer, time, colours, difference, link flags per
  pane) and `layout:` (`2h`, `2v`, `3`, `4` or a layout string). The gallery has three: Storm Ciarán
  in three panes, the Western Alps in four, and a climate model next to the reanalysis; "True vs
  false colour" now uses `panes:`. Gallery cards show one picture per pane.
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

- Open menus (Export, Compare, …) are drawn above the point details panel.
- The *Add data* dialog opens in the middle of the page over a dimmed map; it was squeezed into the
  toolbar's corner.
- Panes of a link or view that share the time start at the link's time, not at the latest one.

- A `?` in a web address (query string) is no longer read as a file pattern, so signed or
  parameterised URLs open with the right reader; a web page (`.html`) gets a clear message.
- An `s3://` bucket that is not found on AWS suggests giving the endpoint of its S3 service.
- A dataset without a map says what it holds instead: its variables with their dimensions (e.g.
  "a time series of one place, not a map: t2m(time)"), or the groups below an empty top level.
- A path to a group inside a Zarr store (`data.zarr/1980`) opens as Zarr; a public store that shows
  no variables over HTTPS (no consolidated metadata, so nothing to list) is read again with s3fs.
- *Add data* takes a pasted address with quotes, angle brackets or an upper-case scheme; an address
  without `https://` (e.g. `host.org/bucket/data.zarr`) gets a clear message instead of "opening
  local files is off".

### Changed

- A click on the map no longer opens the point details: switch on *Point* (toolbar, key `i`) to pick
  points with a click; `Esc` or the button stops it. Hovering still shows the values, and links with a
  point still open it. Panning and zooming no longer open the panel by accident.
- Each pane's corner box folds to its number (› at its end) and unfolds with a click on the number,
  to keep the map free.

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

[Unreleased]: https://github.com/salmanmohebi/geoloupe/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/salmanmohebi/geoloupe/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/salmanmohebi/geoloupe/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/salmanmohebi/geoloupe/releases/tag/v0.1.0
