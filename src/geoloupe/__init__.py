"""GeoLoupe: interactive, multi-resolution exploration of large gridded datasets.

Quick start from Python (e.g. in Jupyter)::

    import xarray as xr
    import geoloupe

    ds = xr.open_zarr("era5.zarr")
    viewer = geoloupe.show({"era5": ds})     # opens the map in the notebook

From the command line::

    geoloupe serve config.yaml
"""

from __future__ import annotations

__version__ = "0.3.2"

from geoloupe.catalog import Catalog
from geoloupe.config import Config, load_config
from geoloupe.server import Viewer, serve, show

__all__ = ["Catalog", "Config", "Viewer", "__version__", "load_config", "serve", "show"]
