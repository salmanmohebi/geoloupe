"""TerraScope: interactive, multi-resolution exploration of large gridded datasets.

Quick start from Python (e.g. in Jupyter)::

    import xarray as xr
    import terrascope

    ds = xr.open_zarr("era5.zarr")
    viewer = terrascope.show({"era5": ds})     # opens the map in the notebook

From the command line::

    terrascope serve config.yaml
"""

from __future__ import annotations

__version__ = "0.1.0"

from terrascope.catalog import Catalog
from terrascope.config import Config, load_config
from terrascope.server import Viewer, serve, show

__all__ = ["Catalog", "Config", "Viewer", "__version__", "load_config", "serve", "show"]
