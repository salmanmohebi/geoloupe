"""Datasets from STAC (SpatioTemporal Asset Catalog): a search of a STAC API, or one item.

The catalog turns the result into mosaics: every asset key (``red``, ``nir``, ``visual`` …) becomes
a variable whose files are that asset of every item found, the best item first. Only metadata is
read here (one or a few HTTP requests); the images themselves are read later, block by block, like
any other Cloud-Optimized GeoTIFF.

Works with open STAC APIs whose assets are public (e.g. Element84 Earth Search on AWS). APIs that
require signed asset URLs (Microsoft Planetary Computer) are not supported yet.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from terrascope.config import StacConfig
from terrascope.readers import is_s3, s3_https

log = logging.getLogger(__name__)

_COG_TYPES = ("image/tiff", "image/vnd.stac.geotiff")
_MAX_PAGES = 20


class StacError(ValueError):
    """The STAC catalogue cannot be read or returned nothing usable."""


@dataclass
class StacAsset:
    """One file of a STAC dataset: an asset of an item."""

    item: str
    key: str
    href: str
    datetime: str | None
    cloud_cover: float | None
    scale: float = 1.0
    offset: float = 0.0


def _fetch_json(url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET (or POST a JSON body) and parse the answer."""
    data = None if body is None else json.dumps(body).encode()
    headers = {"Accept": "application/geo+json, application/json", "User-Agent": "terrascope"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        out: dict[str, Any] = json.load(r)
    return out


def _search(cfg: StacConfig) -> list[dict[str, Any]]:
    assert cfg.url is not None
    body: dict[str, Any] = {"limit": min(cfg.limit * 2, 100)}
    if cfg.collections:
        body["collections"] = cfg.collections
    if cfg.bbox:
        body["bbox"] = list(cfg.bbox)
    if cfg.datetime:
        body["datetime"] = _rfc3339_interval(cfg.datetime)
    if cfg.query:
        body["query"] = cfg.query
    url: str | None = cfg.url.rstrip("/") + "/search"
    items: list[dict[str, Any]] = []
    page_body: dict[str, Any] | None = body
    for _ in range(_MAX_PAGES):
        if url is None:
            break
        page = _fetch_json(url, page_body)
        items += page.get("features", [])
        if len(items) >= cfg.limit * 4:  # enough candidates to sort and pick from
            break
        nxt = next((ln for ln in page.get("links", []) if ln.get("rel") == "next"), None)
        if nxt is None:
            break
        url = nxt["href"]
        if nxt.get("method", "GET").upper() == "POST":
            page_body = {**body, **nxt.get("body", {})} if nxt.get("merge") else nxt.get("body")
        else:
            page_body = None
    return items


def _rfc3339_interval(dt: str) -> str:
    """'2023-08-01/2023-08-31' → full RFC 3339 instants (some APIs reject plain dates)."""

    def one(s: str, end: bool) -> str:
        s = s.strip()
        if s in ("", ".."):
            return ".."
        if len(s) == 10:  # a date
            return s + ("T23:59:59Z" if end else "T00:00:00Z")
        return s

    if "/" in dt:
        a, b = dt.split("/", 1)
        return f"{one(a, False)}/{one(b, True)}"
    return f"{one(dt, False)}/{one(dt, True)}"


def _band_scale(asset: dict[str, Any]) -> tuple[float, float]:
    """Scale and offset of the first band (STAC raster extension v1 or v2 / STAC 1.1 bands)."""
    bands = asset.get("raster:bands") or asset.get("bands") or []
    if not bands:
        return float(asset.get("raster:scale", 1.0)), float(asset.get("raster:offset", 0.0))
    b = bands[0]
    return (
        float(b.get("scale", b.get("raster:scale", 1.0))),
        float(b.get("offset", b.get("raster:offset", 0.0))),
    )


def _is_cog_data(asset: dict[str, Any]) -> bool:
    kind = str(asset.get("type", ""))
    roles = asset.get("roles") or ["data"]
    return kind.startswith(_COG_TYPES) and ("data" in roles or "visual" in roles)


def _s3_to_https(href: str) -> str:
    """s3://bucket/key → https://bucket.s3.amazonaws.com/key (public buckets, no credentials)."""
    return s3_https(href) if is_s3(href) else href


def resolve(cfg: StacConfig) -> list[StacAsset]:
    """The files of a STAC dataset, best item first. Raises StacError."""
    try:
        items = [_fetch_json(cfg.item)] if cfg.item else _search(cfg)
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        raise StacError(f"cannot read STAC {cfg.item or cfg.url}: {e}") from e
    if not items:
        raise StacError(
            f"the STAC search of {cfg.url} found no items; widen bbox, datetime or query"
        )

    def cloud(it: dict[str, Any]) -> float:
        v = it.get("properties", {}).get("eo:cloud_cover")
        return float(v) if v is not None else 100.0

    def when(it: dict[str, Any]) -> str:
        return str(it.get("properties", {}).get("datetime") or "")

    if cfg.sortby == "cloud":
        items.sort(key=lambda it: (cloud(it), _neg(when(it))))
    else:
        items.sort(key=when, reverse=cfg.sortby == "newest")
    items = items[: cfg.limit]

    out: list[StacAsset] = []
    for it in items:
        props = it.get("properties", {})
        assets = it.get("assets", {})
        keys = cfg.assets or [k for k, a in assets.items() if _is_cog_data(a)]
        for key in keys:
            a = assets.get(key)
            if a is None:
                continue
            scale, offset = _band_scale(a)
            if props.get("earthsearch:boa_offset_applied"):
                # Element84 already removed the BOA offset from the pixels, but raster:bands
                # still lists it: applying it again would shift reflectance by -0.1
                offset = 0.0
            out.append(
                StacAsset(
                    str(it.get("id")),
                    key,
                    _s3_to_https(str(a["href"])),
                    props.get("datetime"),
                    props.get("eo:cloud_cover"),
                    scale,
                    offset,
                )
            )
    if not out:
        wanted = ", ".join(cfg.assets or ["COG data assets"])
        raise StacError(f"the STAC items found have no {wanted}")
    log.info("STAC %s: %d items, %d files", cfg.item or cfg.url, len(items), len(out))
    return out


def _neg(s: str) -> str:
    """Sort key that orders ISO strings newest first within an ascending sort."""
    return "".join(chr(0x10FFFF - ord(c)) for c in s)
