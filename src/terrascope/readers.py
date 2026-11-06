"""Open datasets lazily with xarray, whatever the storage format.

Supported: Zarr v2/v3 (local or remote, sharded or not, consolidated or not),
NetCDF 3/4 and HDF5 (single files or multi-file collections), GRIB (cfgrib),
GeoTIFF / Cloud-Optimized GeoTIFF (rioxarray) and kerchunk / VirtualiZarr
reference files. Everything is opened *without dask* where possible: the tile
engine reads small windows, and dask's graph overhead would dominate.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

import xarray as xr

log = logging.getLogger(__name__)

T = TypeVar("T")

_PERMANENT = (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError)


def _transient(exc: BaseException) -> bool:
    """A network failure worth retrying (DNS, timeout, reset, HTTP 429/5xx), not a missing file."""
    seen: set[int] = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        code = getattr(e, "code", None) or getattr(e, "status", None)
        text = str(e)
        if isinstance(e, _PERMANENT) or code in (401, 403, 404, 410) or " 404" in text:
            return False
        if isinstance(code, int) and (code == 429 or code >= 500):
            return True
        name = type(e).__name__
        if isinstance(e, TimeoutError | ConnectionError) or "Timeout" in name or "Connect" in name:
            return True
        if isinstance(e, OSError) and ("name resolution" in text or "timed out" in text):
            return True
        e = e.__cause__ or e.__context__
    return False


def with_retries(fn: Callable[[], T], remote: bool, what: str, attempts: int = 3) -> T:
    """Call `fn`; for remote data, retry network failures with backoff (1 s, 2 s, …).

    One DNS hiccup or reset connection at start-up should not remove a dataset until the next
    restart. Permanent errors (not found, no access) are raised at once.
    """
    for k in range(attempts):
        try:
            return fn()
        except Exception as e:
            if not remote or k == attempts - 1 or not _transient(e):
                raise
            log.warning("%s: %s; retrying in %d s", what, e, 2**k)
            time.sleep(2**k)
    raise AssertionError("unreachable")


_NETCDF_EXT = (".nc", ".nc4", ".netcdf", ".cdf", ".h5", ".hdf5", ".he5")
_GRIB_EXT = (".grib", ".grib2", ".grb", ".grb2")
_TIFF_EXT = (".tif", ".tiff", ".geotiff")
_REF_EXT = (".json", ".parq", ".parquet")


class OpenError(RuntimeError):
    """A dataset could not be opened. The message says why and how to fix it."""


def _is_remote(loc: str) -> bool:
    return "://" in loc and not loc.startswith("file://")


def _looks_like_zarr(loc: str) -> bool:
    last = loc.rstrip("/").split("/")[-1].lower()
    if last.endswith(".zarr") or ".zarr-" in last or last.endswith(".zarr.zip"):
        return True
    if not _is_remote(loc) and os.path.isdir(loc):
        return any(
            os.path.exists(os.path.join(loc, m))
            for m in ("zarr.json", ".zgroup", ".zarray", ".zmetadata")
        )
    return False


def detect_engine(location: str | list[str]) -> str:
    """Guess the engine from the path. Lists and globs are NetCDF collections."""
    if isinstance(location, list):
        return "netcdf"
    loc = location.lower().rstrip("/")
    if _looks_like_zarr(location):
        return "zarr"
    if any(ch in location for ch in "*?["):
        return "netcdf"
    if loc.endswith(_NETCDF_EXT):
        return "netcdf"
    if loc.endswith(_GRIB_EXT):
        return "grib"
    if loc.endswith(_TIFF_EXT):
        return "geotiff"
    if loc.endswith(_REF_EXT):
        return "kerchunk"
    raise OpenError(f"cannot tell the format of {location!r}; set 'engine' in the dataset config")


def open_dataset(
    location: str | list[str],
    engine: str = "auto",
    storage_options: dict[str, Any] | None = None,
    open_kwargs: dict[str, Any] | None = None,
    group: str | None = None,
) -> xr.Dataset:
    """Open one dataset lazily. Raises OpenError with a readable message."""
    storage_options = storage_options or {}
    kw = dict(open_kwargs or {})
    if engine == "auto":
        engine = detect_engine(location)
    try:
        if engine == "zarr":
            assert isinstance(location, str)
            return _open_zarr(location, storage_options, kw, group)
        if engine == "netcdf":
            return _open_netcdf(location, storage_options, kw, group)
        if engine == "grib":
            assert isinstance(location, str)
            kw.setdefault("decode_timedelta", False)
            return xr.open_dataset(location, engine="cfgrib", chunks=None, **kw)
        if engine == "geotiff":
            assert isinstance(location, str)
            return _open_geotiff(location, kw, storage_options)
        if engine == "kerchunk":
            assert isinstance(location, str)
            return xr.open_dataset(
                "reference://",
                engine="zarr",
                chunks=None,
                backend_kwargs={
                    "consolidated": False,
                    "storage_options": {"fo": location, **storage_options},
                },
                **kw,
            )
    except OpenError:
        raise
    except ImportError as e:
        extra = {"grib": "grib", "geotiff": "geotiff", "kerchunk": "remote"}.get(engine, "all")
        raise OpenError(
            f"{engine} support is not installed ({e}); install terrascope[{extra}]"
        ) from e
    except FileNotFoundError as e:
        raise OpenError(f"not found: {location}") from e
    except Exception as e:  # any backend failure becomes one readable message
        raise OpenError(
            f"cannot open {location} with engine {engine!r}: {type(e).__name__}: {e}"
        ) from e
    raise OpenError(f"unknown engine {engine!r}")


def _open_zarr(loc: str, so: dict[str, Any], kw: dict[str, Any], group: str | None) -> xr.Dataset:
    if not _is_remote(loc) and not os.path.exists(loc):
        raise OpenError(f"not found: {loc}")
    kw.setdefault("decode_timedelta", False)
    return xr.open_zarr(loc, group=group, chunks=None, storage_options=so or None, **kw)


def _expand(location: str | list[str]) -> list[str]:
    items = location if isinstance(location, list) else [location]
    out: list[str] = []
    for item in items:
        if any(ch in item for ch in "*?[") and not _is_remote(item):
            matches = sorted(glob.glob(item))
            if not matches:
                raise OpenError(f"no files match {item!r}")
            out.extend(matches)
        else:
            out.append(item)
    return out


def _open_netcdf(
    location: str | list[str], so: dict[str, Any], kw: dict[str, Any], group: str | None
) -> xr.Dataset:
    files = _expand(location)
    kw.setdefault("decode_timedelta", False)
    if group:
        kw["group"] = group
    if len(files) == 1:
        f = files[0]
        if _is_remote(f):
            import fsspec

            return xr.open_dataset(
                fsspec.open(f, **so).open(), engine="h5netcdf", chunks=None, **kw
            )
        if not os.path.exists(f):
            raise OpenError(f"not found: {f}")
        return _open_one_netcdf(f, kw)
    try:
        import dask  # noqa: F401
    except ImportError as e:
        raise OpenError("multi-file NetCDF needs dask; install terrascope[dask]") from e
    kw.setdefault("combine", "by_coords")
    kw.setdefault("data_vars", "minimal")
    kw.setdefault("coords", "minimal")
    kw.setdefault("compat", "override")
    log.info("opening %d NetCDF files as one dataset", len(files))
    return xr.open_mfdataset(files, engine=_netcdf_engine(files[0]), **kw)


def _netcdf_engine(path: str) -> str:
    """h5netcdf for NetCDF-4/HDF5 files, scipy for classic NetCDF-3."""
    with open(path, "rb") as fh:
        magic = fh.read(4)
    return "scipy" if magic[:3] == b"CDF" else "h5netcdf"


def _open_one_netcdf(path: str, kw: dict[str, Any]) -> xr.Dataset:
    return xr.open_dataset(path, engine=_netcdf_engine(path), chunks=None, **kw)


# GDAL settings for cloud-optimized reads over HTTP(S)/S3: no directory listing, merged range
# requests, an in-memory block cache. Applied once, before the first GeoTIFF is opened.
_GDAL_CLOUD_DEFAULTS = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_VERSION": "2",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": str(256 * 2**20),
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff,.TIF,.TIFF,.vrt,.jp2",
}


def _gdal_env(storage_options: dict[str, Any] | None) -> None:
    for k, v in _GDAL_CLOUD_DEFAULTS.items():
        os.environ.setdefault(k, v)
    if (storage_options or {}).get("anon"):
        os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")  # public S3 buckets


def geotiff_overviews(path: str, storage_options: dict[str, Any] | None = None) -> list[int]:
    """Internal overview factors of a (Cloud-Optimized) GeoTIFF, e.g. [2, 4, 8, 16]."""
    import rasterio

    _gdal_env(storage_options)
    with rasterio.open(path) as src:
        return list(src.overviews(1))


def _open_geotiff(
    path: str, kw: dict[str, Any], storage_options: dict[str, Any] | None = None
) -> xr.Dataset:
    """A GeoTIFF / COG as a Dataset: one variable, with a `band` dimension if it has several bands.

    `overview_level` in `kw` opens an internal overview instead of the full resolution.
    Nodata becomes NaN and the TIFF scale/offset tags are applied (mask_and_scale).
    """
    import rioxarray

    _gdal_env(storage_options)
    kw.pop("decode_timedelta", None)
    # lock=False: rioxarray's default is one global lock for all files, which serialises every read
    # in the server (each remote read waits for HTTP round trips). GDAL reads are thread-safe with
    # one handle per read; GDAL caches the headers of remote files, so reopening is cheap.
    kw.setdefault("lock", False)
    da = rioxarray.open_rasterio(path, mask_and_scale=True, **kw)
    if not isinstance(da, xr.DataArray):
        raise OpenError(f"{path} holds several subdatasets; open one of them")
    desc = da.attrs.get("long_name")
    names = list(desc) if isinstance(desc, tuple | list) else [desc] if desc else []
    if "band" in da.dims and da.sizes["band"] == 1:
        da = da.squeeze("band", drop=True)
        names = names[:1]
    elif "band" in da.dims and names and all(names) and len(set(names)) == len(names):
        da = da.assign_coords(band=[str(n) for n in names])  # band names instead of 1, 2, 3
    stem = Path(path.split("?")[0]).name.split(".")[0]
    name = names[0] if len(names) == 1 and names[0] else stem
    name = re.sub(r"\W", "_", str(name)) or "band_data"
    if name[0].isdigit():
        name = f"b_{name}"
    crs = da.rio.crs
    da.attrs.pop("long_name", None)
    if "overview_level" not in kw and "band" not in da.dims:
        da.attrs.update(_palette_flags(path))  # categorical rasters carry a colour table
    ds = da.to_dataset(name=name)
    if crs is not None:
        ds[name].attrs["grid_mapping"] = "spatial_ref"
        ds["spatial_ref"] = xr.DataArray(0, attrs={"crs_wkt": crs.to_wkt()})
    return ds


def _palette_flags(path: str) -> dict[str, Any]:
    """CF flag attributes from a GeoTIFF colour table (palette), or nothing."""
    import rasterio

    try:
        with rasterio.open(path) as src:
            if src.count != 1 or src.colorinterp[0].name != "palette":
                return {}
            cmap = src.colormap(1)
            nodata = src.nodata
    except (rasterio.errors.RasterioError, ValueError):
        return {}
    used = {
        v: c
        for v, c in cmap.items()
        if v != nodata and tuple(c) not in ((0, 0, 0, 0), (0, 0, 0, 255))
    }
    if not used or len(used) > 255:
        return {}
    values = sorted(used)
    return {
        "flag_values": values,
        "flag_colors": " ".join("#{:02x}{:02x}{:02x}".format(*used[v][:3]) for v in values),
    }


def multiscale_groups(
    location: str, storage_options: dict[str, Any] | None = None
) -> list[str] | None:
    """Group paths of a multiscale Zarr store, finest first, or None if it is not one.

    Understands the Zarr ``multiscales`` conventions (zarr-conventions
    ``layout`` list, and the OME-NGFF / ndpyramid ``datasets`` list) and falls
    back to integer-named groups (``0``, ``1``, ...).
    """
    import zarr

    try:
        root = zarr.open_group(location, mode="r", storage_options=storage_options or None)
    except Exception:  # not a group (e.g. a plain array store)
        return None
    ms = root.attrs.get("multiscales")
    paths: list[str] = []
    if isinstance(ms, dict) and isinstance(ms.get("layout"), list):
        paths = [str(e["asset"]) for e in ms["layout"] if isinstance(e, dict) and "asset" in e]
    elif isinstance(ms, list) and ms and isinstance(ms[0], dict):
        paths = [str(d["path"]) for d in ms[0].get("datasets", []) if "path" in d]
    if not paths:
        names = sorted((k for k, _ in root.groups() if k.isdigit()), key=int)
        paths = names
    return paths or None


def iter_data_vars(ds: xr.Dataset) -> Iterator[str]:
    for name in ds.data_vars:
        yield str(name)
