# Data tile format (version 1)

TerraScope serves **data tiles**: the values themselves, not pictures. The browser colours them, so
colormap and range changes need no new requests, and any client (Python, JavaScript, Julia) can read
the numbers.

## Request

```
GET /api/layers/{layer}/tiles/{z}/{x}/{y}.bin?time={ISO 8601}&dims={name:value,...}
```

* `z/x/y`: Web Mercator (EPSG:3857) XYZ tile, y counted from the north.
* `time`: optional; default is the last time of the layer. Matched to the nearest time of each source
  within the source's tolerance (default: half a step for monthly or coarser data, else at most 12 h).
* `dims`: values of extra dimensions such as `plev:500`. Values are matched exactly or, for numbers, to
  the nearest value.

## Response

| Status | Meaning |
|---|---|
| 200 | Tile with data (body below) |
| 204 | No source has data in this tile (headers below still present) |
| 400 | Bad time, dimension value or tile coordinates (`{"detail": "..."}`) |
| 404 | Unknown layer |

**Headers**

| Header | Content |
|---|---|
| `Content-Encoding: gzip` | when the request accepts gzip (browsers decompress transparently) |
| `X-TerraScope-Sources` | `source:level:pixels,...`, the sources that filled pixels (index into the layer's `sources`), the level used (index into that source's `levels`) and how many pixels each filled |
| `X-TerraScope-Skipped` | `source:reason,...`, sources not used in this tile: `cost` (too many cells to read at this zoom), `time` (no data at this time), `zoom` (outside the configured zoom window) |
| `X-TerraScope-Time-Ms` | server time for the tile |

**Body** (after gzip decompression), for tile size `n` (default 256):

1. `n·n·4` bytes: float32 little-endian values in row-major order (row 0 = north), **byte-shuffled**:
   first byte 0 of every value, then byte 1 of every value, then bytes 2 and 3. NaN = no data.
2. `n·n` bytes (optional): uint8 index of the source that filled each pixel; 255 = none.

Byte shuffling (as in Blosc and HDF5) groups the slowly varying sign/exponent bytes, which roughly
doubles the gzip compression ratio of smooth geophysical fields.

## Decoding

Python:

```python
import gzip, numpy as np, requests

r = requests.get(f"{base}/api/layers/tas/tiles/6/33/22.bin", params={"time": "2021-07-15"})
raw, n = r.content, 256                      # requests removes the gzip encoding
values = np.frombuffer(raw[: 4 * n * n], np.uint8).reshape(4, -1).T.copy().view("<f4").reshape(n, n)
source = np.frombuffer(raw[4 * n * n :], np.uint8).reshape(n, n) if len(raw) > 4 * n * n else None
```

(`terrascope.engine.decode_data_tile` does the same.)

JavaScript: see [frontend/src/lib/datatile.ts](../frontend/src/lib/datatile.ts).

## Rendered tiles

For tools that expect images (QGIS, Leaflet, a slide), the same tiles are available as PNG:

```
GET /api/layers/{layer}/tiles/{z}/{x}/{y}.png?time=&colormap=viridis&vmin=250&vmax=310&scale=linear
GET /api/layers/{layer}/tilejson.json?colormap=viridis&vmin=250&vmax=310      (TileJSON 3.0)
```

If `vmin`/`vmax` are not given, the layer's configured range or the 2nd–98th percentile of the layer at
that time is used.
