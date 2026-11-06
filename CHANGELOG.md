# Changelog

All notable changes to GeoLoupe. The format follows [Keep a Changelog](https://keepachangelog.com/),
and versions follow [Semantic Versioning](https://semver.org/) (before 1.0, minor versions may change
the configuration format; such changes are listed here).

## [Unreleased]

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

[Unreleased]: https://github.com/salmanmohebi/geoloupe/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/salmanmohebi/geoloupe/releases/tag/v0.1.0
