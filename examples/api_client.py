"""Use a running GeoLoupe server from Python (no GeoLoupe install needed: numpy + httpx).

    python examples/api_client.py http://127.0.0.1:8000 tas 10.3 46.45

Prints the layers, the value at a point from every source, the start of the time series, and the
statistics of one data tile.
"""

from __future__ import annotations

import sys

import httpx
import numpy as np


def decode_tile(raw: bytes, n: int = 256) -> tuple[np.ndarray, np.ndarray | None]:
    """Data tile body → (float32 values, uint8 source index or None); see docs/data-tiles.md."""
    values = (
        np.frombuffer(raw[: 4 * n * n], np.uint8).reshape(4, -1).T.copy().view("<f4").reshape(n, n)
    )
    source = (
        np.frombuffer(raw[4 * n * n : 5 * n * n], np.uint8).reshape(n, n)
        if len(raw) >= 5 * n * n
        else None
    )
    return values, source


def main(base: str, layer: str, lon: float, lat: float) -> None:
    api = base.rstrip("/") + "/api"
    cat = httpx.get(f"{api}/catalog").json()
    print("layers:", ", ".join(lyr["id"] for lyr in cat["layers"]))

    meta = httpx.get(f"{api}/layers/{layer}").json()
    t = meta["times"][len(meta["times"]) // 2] if meta["times"] else None
    print(
        f"\n{meta['title']} [{meta['units']}] at {t}; sources:",
        ", ".join(f"{s['dataset']} ({s['resolution']})" for s in meta["sources"]),
    )

    p = httpx.get(f"{api}/layers/{layer}/point", params={"lon": lon, "lat": lat, "time": t}).json()
    print(f"\nvalue on the map: {p['value']}")
    for s in p["sources"]:
        print(f"  {s['id']:24s} {s['resolution']:20s} {s.get('value')}")

    if meta["times"]:
        ts = httpx.get(
            f"{api}/layers/{layer}/timeseries",
            params={"lon": lon, "lat": lat, "start": meta["times"][0], "end": meta["times"][9]},
            timeout=60,
        ).json()
        for s in ts["series"]:
            print(f"\n{s['id']} at cell ({s['cell']['lon']:.4f}, {s['cell']['lat']:.4f}):")
            for time, v in list(zip(s["times"], s["values"], strict=True))[:5]:
                print(f"  {time}  {v}")

    # the tile containing the point at zoom 7
    z = 7
    x = int((lon + 180) / 360 * 2**z)
    y = int((1 - np.log(np.tan(np.radians(lat)) + 1 / np.cos(np.radians(lat))) / np.pi) / 2 * 2**z)
    r = httpx.get(f"{api}/layers/{layer}/tiles/{z}/{x}/{y}.bin", params={"time": t})
    if r.status_code == 204:
        print("\nno data in that tile")
        return
    values, source = decode_tile(r.content)
    if source is not None:
        idx, count = np.unique(source[source != 255], return_counts=True)
        print("\npixels per source index:", dict(zip(idx.tolist(), count.tolist(), strict=True)))
    print(
        f"\ntile {z}/{x}/{y}: min {np.nanmin(values):.2f}, max {np.nanmax(values):.2f}, "
        f"sources (index:level:pixels) {r.headers['x-geoloupe-sources']}"
    )


if __name__ == "__main__":
    a = sys.argv[1:] + [None] * 4
    main(a[0] or "http://127.0.0.1:8000", a[1] or "tas", float(a[2] or 10.3), float(a[3] or 46.45))
