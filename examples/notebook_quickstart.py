# %% [markdown]
# # TerraScope in a notebook
#
# Open this file as a notebook (VS Code and JupyterLab with jupytext understand the `# %%` cells),
# or run it as a script. It shows:
#
# 1. any xarray Dataset on an interactive map with `terrascope.show`,
# 2. a multi-resolution layer that combines a coarse and a fine dataset,
# 3. reading the same data back through the HTTP API.

# %%
import numpy as np
import pandas as pd

import terrascope
from terrascope.demo import make_global, make_regional

days = pd.date_range("2021-06-01", periods=30, freq="D")
coarse = make_global(days, res=1.0)  # synthetic 1° global field
fine = make_regional(days, res=0.02)  # synthetic 0.02° (~2 km) Alps field, downscaled
coarse

# %% [markdown]
# ## 1. One dataset
# `show` starts a small server in a background thread and returns a viewer. In a notebook the
# viewer renders as the map; on JupyterHub with jupyter-server-proxy it uses the proxy URL.

# %%
viewer = terrascope.show({"global": coarse})
viewer  # in Jupyter: the interactive map

# %% [markdown]
# ## 2. A multi-resolution layer
# The fine Alps field is drawn wherever it has data, the global field everywhere else and at low
# zoom. Open the link and zoom into the Alps.

# %%
viewer.stop()
viewer = terrascope.show(
    {"global": coarse, "alps": fine},
    layers={
        "tas": {
            "title": "Air temperature (2 km over the Alps)",
            "sources": [
                {"dataset": "alps", "variable": "tas"},
                {"dataset": "global", "variable": "tas"},
            ],
        }
    },
)
print(viewer.link(layer="tas", time="2021-06-15"))

# %% [markdown]
# ## 3. The same data through the API
# Values at a point (native cells of every source) and a data tile as a numpy array.

# %%
import httpx

from terrascope.engine import decode_data_tile

p = httpx.get(
    viewer.url + "api/layers/tas/point", params={"lon": 9.9, "lat": 46.5, "time": "2021-06-15"}
).json()
for s in p["sources"]:
    print(f"{s['id']:12s} {s['resolution']:16s} {s.get('value')}")

r = httpx.get(viewer.url + "api/layers/tas/tiles/6/33/22.bin", params={"time": "2021-06-15"})
tile = decode_data_tile(r.content, 256)
print(
    "tile:",
    tile.shape,
    "mean",
    float(np.nanmean(tile)),
    "sources:",
    r.headers["x-terrascope-sources"],
)

# %%
viewer.stop()
