"""Browse S3 buckets for "Add data": list a bucket or prefix and tell which entries GeoLoupe opens.

Works with AWS S3 and S3-compatible stores (MinIO, Ceph, SWITCH, Cloudflare R2, …) through a custom
endpoint. Access is public (anonymous) or with the credentials of the server's environment: the
default AWS credential chain, or a named profile from ~/.aws/config. Keys are never sent by the
browser; the browser only learns the profile names.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

LIMIT = 1000  # entries per listing

_KINDS = {
    "zarr": (".zarr", ".zarr.zip"),
    "netcdf": (".nc", ".nc4", ".netcdf", ".h5", ".hdf5", ".he5"),
    "grib": (".grib", ".grib2", ".grb", ".grb2"),
    "geotiff": (".tif", ".tiff", ".geotiff"),
    "table": (".csv", ".parquet", ".pq", ".csv.gz"),
    "vector": (".geojson", ".gpkg", ".fgb", ".shp", ".kml", ".gml", ".geoparquet", ".shp.zip"),
    "reference": (".json", ".parq"),
}
_ZARR_MARKERS = {"zarr.json", ".zgroup", ".zarray", ".zmetadata"}


class BrowseError(RuntimeError):
    """A listing failed; the message says why and what to try."""


def access_options(
    endpoint: str | None = None,
    profile: str | None = None,
    anon: bool | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    """fsspec/s3fs options for one access choice (also stored with the datasets opened)."""
    so: dict[str, Any] = {}
    kw: dict[str, Any] = {}
    if endpoint:
        if not endpoint.startswith(("http://", "https://")):
            endpoint = "https://" + endpoint
        kw["endpoint_url"] = endpoint.rstrip("/")
    if region:
        kw["region_name"] = region
    if kw:
        so["client_kwargs"] = kw
    if profile:
        so["profile"] = profile
        so["anon"] = False
    elif anon is not None:
        so["anon"] = anon
    return so


def kind_of(name: str, is_dir: bool) -> str | None:
    """What GeoLoupe can open: zarr, netcdf, grib, geotiff, table, vector, reference; else None."""
    low = name.lower().rstrip("/")
    for kind, ends in _KINDS.items():
        if low.endswith(ends):
            if kind == "reference" and is_dir:
                return None
            if is_dir and kind != "zarr":
                return None
            return kind
    return None


def profiles() -> list[str]:
    """Names of the AWS profiles of the server's environment (no secrets)."""
    try:
        import botocore.session

        return sorted(botocore.session.Session().available_profiles)
    except Exception:  # botocore missing or a broken config: no profiles to offer
        return []


def _filesystem(so: dict[str, Any]) -> Any:
    try:
        import s3fs
    except ImportError as e:
        raise BrowseError("S3 support is not installed: pip install 'geoloupe[remote]'") from e
    return s3fs.S3FileSystem(**so)


def normalise(url: str) -> str:
    """s3://bucket/prefix/ from what the user typed (s3://…, bucket/prefix, or an S3 console link)."""
    u = url.strip()
    for head in ("https://s3.console.aws.amazon.com/s3/buckets/", "s3a://", "s3n://"):
        if u.startswith(head):
            u = "s3://" + u[len(head) :].split("?")[0]
    if not u.startswith("s3://"):
        u = "s3://" + u.lstrip("/")
    return u


def list_location(
    url: str, storage_options: dict[str, Any] | None = None, match: str | None = None
) -> dict[str, Any]:
    """The folders and files at an s3:// location, with what GeoLoupe can open.

    Returns {url, parent, zarr (the location itself is a Zarr store), entries: [{name, url, dir,
    size, kind}], total, truncated}. Folders first, then files, by name. `match` keeps the names
    that contain it (any case), before the limit, to find files in large folders.
    """
    url = normalise(url)
    path = url[len("s3://") :].strip("/")
    if not path:
        raise BrowseError(
            "give a bucket, e.g. s3://my-bucket/ (listing all buckets is not supported)"
        )
    so = dict(storage_options or {})
    so.setdefault("anon", True)
    fs = _filesystem(so)
    try:
        raw = fs.ls(path, detail=True, refresh=True)
    except FileNotFoundError:
        raise BrowseError(f"not found: {url}") from None
    except PermissionError:
        raise BrowseError(_denied(url, so)) from None
    except Exception as e:  # botocore errors: no credentials, endpoint unreachable, access denied
        raise BrowseError(_explain(e, url, so)) from e
    entries = []
    names = set()
    for item in raw:
        full = str(item.get("name", "")).strip("/")
        name = full.rsplit("/", 1)[-1]
        if not name or full == path:
            continue
        is_dir = item.get("type") == "directory"
        names.add(name)
        if match and match.lower() not in name.lower():
            continue
        entries.append(
            {
                "name": name,
                "url": f"s3://{full}{'/' if is_dir else ''}",
                "dir": is_dir,
                "size": None if is_dir else item.get("size"),
                "kind": kind_of(name, is_dir),
            }
        )
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    parent = path.rsplit("/", 1)[0] if "/" in path else None
    return {
        "url": f"s3://{path}/",
        "parent": f"s3://{parent}/" if parent else None,
        "zarr": bool(names & _ZARR_MARKERS) or path.lower().endswith(".zarr"),
        "entries": entries[:LIMIT],
        "total": len(entries),
        "truncated": len(entries) > LIMIT,
    }


def _denied(url: str, so: dict[str, Any]) -> str:
    if so.get("anon"):
        return f"access denied to {url} without credentials: the bucket is not public. Choose 'With the server's credentials'"
    return (
        f"access denied to {url} with these credentials (profile {so.get('profile') or 'default'})"
    )


def _explain(e: Exception, url: str, so: dict[str, Any]) -> str:
    name = type(e).__name__
    text = str(e)
    if "NoCredentials" in name or "Unable to locate credentials" in text:
        return (
            "no AWS credentials on the server: set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or a "
            "profile in ~/.aws, or choose 'Public'"
        )
    if "AccessDenied" in text or "Forbidden" in text or "403" in text:
        return _denied(url, so)
    if "NoSuchBucket" in text:
        return f"no such bucket: {url}"
    if "EndpointConnection" in name or "Could not connect" in text:
        endpoint = (so.get("client_kwargs") or {}).get("endpoint_url", "AWS")
        return f"cannot reach the S3 endpoint {endpoint}"
    if "ProfileNotFound" in name:
        return f"no AWS profile {so.get('profile')!r} on the server"
    return f"cannot list {url}: {name}: {text}"
