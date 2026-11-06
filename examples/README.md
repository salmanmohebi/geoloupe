# Examples

| File | What it shows |
|---|---|
| [`../src/geoloupe/gallery.yaml`](../src/geoloupe/gallery.yaml) via `geoloupe gallery` | The gallery: ERA5, Copernicus DEM, Sentinel-2, ESA WorldCover and NOAA stations, all read live from public buckets, with featured views. The best place to start, and a template for your own gallery (`geoloupe gallery --show-config`) |
| [`../src/geoloupe/demo.py`](../src/geoloupe/demo.py) via `geoloupe demo-data DIR` | Synthetic data (1° global + 0.01° Alps "downscaled", pressure levels, a LAEA grid) and a config: the quickest way to see every feature |
| [`fetch_public_samples.py`](fetch_public_samples.py) | Three small public datasets (ERA5 GRIB, NCEP NetCDF, ERSST NetCDF) and a config for them |
| [`sentinel2.yaml`](sentinel2.yaml) | A Sentinel-2 L2A scene read over HTTPS from the public AWS bucket: COG overviews as resolution levels, reflectance scale, true- and false-colour images |
| [`public-s3.yaml`](public-s3.yaml) | Data from public AWS S3 buckets with `s3://` paths and no setup: a CMIP6 climate model ocean (Zarr) and a Copernicus DEM tile (COG) |
| [`arco-era5.yaml`](arco-era5.yaml) | Hourly ERA5 since 1940 from Google's public ARCO-ERA5 Zarr store, read over HTTPS (761 160 hourly steps with data) |
| [`fetch_ncei_stations.py`](fetch_ncei_stations.py) | Real station records (NOAA GHCN-Daily) as a point dataset: columns detected, units set in the config |
| [`notebook_quickstart.py`](notebook_quickstart.py) | `geoloupe.show()` in a notebook: one dataset, a multi-resolution layer, and the API from Python (open as a notebook in VS Code/JupyterLab) |
| [`api_client.py`](api_client.py) | Using a running server from any Python with numpy + httpx: point values, time series, decoding a data tile |
| [`hpc/build_overviews.sbatch`](hpc/build_overviews.sbatch) | Building overviews of a large store on an HPC cluster with a Slurm array (one year per task) |

Run any configuration with `geoloupe serve <config.yaml>` and check it first with
`geoloupe info <config.yaml>`.
