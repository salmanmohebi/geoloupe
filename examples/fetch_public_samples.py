"""Download three small public datasets and write a GeoLoupe config for them.

    python examples/fetch_public_samples.py samples/
    geoloupe serve samples/public.yaml

The files come from the xarray tutorial data (https://github.com/pydata/xarray-data):

* era5-2mt-2019-03-uk.grib   ERA5 2 m temperature, UK, 0.25°, hourly, March 2019  (GRIB)
* air_temperature.nc          NCEP reanalysis air temperature, North America, 2.5°, 6-hourly, 2013-2014
* ersstv5.nc                  NOAA ERSST v5 sea surface temperature, global, 2°, monthly, 1970-2021

GRIB needs `pip install geoloupe[grib]`.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

BASE = "https://github.com/pydata/xarray-data/raw/master/"
FILES = ["era5-2mt-2019-03-uk.grib", "air_temperature.nc", "ersstv5.nc"]

CONFIG = """\
title: Public sample data
datasets:
  era5-uk:
    title: ERA5 2 m temperature, UK (GRIB, 0.25°, hourly)
    path: era5-2mt-2019-03-uk.grib
    attribution: Contains modified Copernicus Climate Change Service information
  ncep:
    title: NCEP reanalysis air temperature (NetCDF, 2.5°, 6-hourly)
    path: air_temperature.nc
    attribution: NOAA PSL
  ersst:
    title: NOAA ERSST v5 (NetCDF, 2°, monthly)
    path: ersstv5.nc
    attribution: NOAA NCEI
    styles:
      sst: {colormap: thermal, vmin: -2, vmax: 32}
"""


def main(out: str) -> None:
    d = Path(out)
    d.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        target = d / f
        if not target.exists():
            print(f"downloading {f} …")
            urllib.request.urlretrieve(BASE + f, target)
    (d / "public.yaml").write_text(CONFIG)
    print(f"done. Start with:  geoloupe serve {d / 'public.yaml'}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "samples")
