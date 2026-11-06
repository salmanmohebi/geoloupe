"""Download daily station records from NOAA NCEI (GHCN-Daily) and write a GeoLoupe config.

    python examples/fetch_ncei_stations.py samples/ncei SZ000001940 SZ000002220 SZ000003700
    geoloupe serve samples/ncei/stations.yaml

Without station ids, eight Swiss stations are used. The files are NCEI's public "daily summaries"
(https://www.ncei.noaa.gov/data/global-historical-climatology-network-daily/access/), one CSV per
station with columns STATION, DATE, LATITUDE, LONGITUDE, ELEVATION, NAME, PRCP, TMAX, TMIN, …
GeoLoupe detects these columns itself; the config only adds units (NCEI stores temperatures in
tenths of °C and precipitation in tenths of mm).
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

import pandas as pd

BASE = "https://www.ncei.noaa.gov/data/global-historical-climatology-network-daily/access/"
SWISS = [
    "SZ000001940",
    "SZ000002220",
    "SZ000003700",
    "SZ000006717",
    "SZ000008440",
    "SZ000009480",
    "SZE00105086",
    "SZE00116090",
]

CONFIG = """\
title: GHCN-Daily stations (NOAA NCEI)
points:
  ghcn:
    title: GHCN-Daily
    attribution: NOAA NCEI, GHCN-Daily (Menne et al. 2012)
    path: stations.csv
    variables:                 # NCEI units → °C and mm
      TMAX: {scale: 0.1, units: degC}
      TMIN: {scale: 0.1, units: degC}
      TAVG: {scale: 0.1, units: degC}
      PRCP: {scale: 0.1, units: mm day-1}
      SNWD: {units: mm}
"""


def main(out: str, ids: list[str]) -> None:
    d = Path(out)
    (d / "raw").mkdir(parents=True, exist_ok=True)
    frames = []
    for sid in ids or SWISS:
        f = d / "raw" / f"{sid}.csv"
        if not f.exists():
            print(f"downloading {sid} …")
            urllib.request.urlretrieve(BASE + f"{sid}.csv", f)
        frames.append(pd.read_csv(f, low_memory=False))
    pd.concat(frames, ignore_index=True).to_csv(d / "stations.csv", index=False)
    (d / "stations.yaml").write_text(CONFIG)
    print(f"done. Start with:  geoloupe serve {d / 'stations.yaml'}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "samples/ncei", sys.argv[2:])
