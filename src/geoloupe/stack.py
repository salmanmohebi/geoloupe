"""Time stacks: many GeoTIFF files, one per time step, shown as one layer with a time axis.

Most public archives store one file per date: CHELSA, MODIS and Landsat products, many model
outputs. The time of each file comes from its name, through a pattern with placeholders::

    https://os.unil.cloud.switch.ch/chelsa02/chelsa/global/daily/tas/{YYYY}/CHELSA_tas_{DD}_{MM}_{YYYY}_V.2.1.tif

Placeholders: ``{YYYY}`` year, ``{MM}`` month, ``{DD}`` day, ``{DOY}`` day of the year (001–366),
``{hh}`` hour. A placeholder may appear several times (here the year, in the folder and the file
name).

The files are found by listing the folders (local, s3://, or an S3-compatible store over HTTPS),
or, when the dataset gives both ends of ``time_range``, by building the names without listing.

Nothing but the first file is opened at start: every read opens the one file it needs (GDAL caches
the headers of remote files), and only the window it needs, at the internal overview level that
fits the zoom. Files that do not exist give missing values. All files must share the first file's
grid, overviews and data type.
"""

from __future__ import annotations

import itertools
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from xarray.backends import BackendArray
from xarray.core import indexing

from geoloupe.readers import OpenError, gdal_path, is_s3

log = logging.getLogger(__name__)

_FIELDS = {"YYYY": 4, "MM": 2, "DD": 2, "DOY": 3, "hh": 2}
_PLACEHOLDER = re.compile(r"\{(" + "|".join(_FIELDS) + r")\}")
_LIST_PAGES = 200  # at most 200 000 entries per folder

_POOL = ThreadPoolExecutor(max_workers=32, thread_name_prefix="geoloupe-stack")


def is_stack(location: str) -> bool:
    """A file pattern with time placeholders ({YYYY}, {MM}, {DD}, {DOY}, {hh})."""
    return bool(_PLACEHOLDER.search(location))


def stack_name(location: str) -> str:
    """A variable name from a pattern: CHELSA_tas_{DD}_{MM}_{YYYY}_V.2.1.tif → CHELSA_tas_V."""
    stem = Path(location.split("?")[0]).name
    stem = _PLACEHOLDER.sub("", stem)
    stem = stem.split(".")[0]
    stem = re.sub(r"[_-]{2,}", "_", stem).strip("_-")
    name = re.sub(r"\W", "_", stem) or "data"
    return f"b_{name}" if name[0].isdigit() else name


# ------------------------------------------------------------------ patterns
@dataclass
class Pattern:
    """A file name pattern with time placeholders."""

    text: str
    regex: re.Pattern[str] = field(init=False)
    step: str = field(init=False)  # "h", "D", "M" or "Y": the finest placeholder

    def __post_init__(self) -> None:
        names = _PLACEHOLDER.findall(self.text)
        have = set(names)
        if "YYYY" not in have:
            raise OpenError(f"{self.text}: a file pattern needs {{YYYY}}")
        if "DD" in have and "MM" not in have:
            raise OpenError(f"{self.text}: {{DD}} needs {{MM}} (or use {{DOY}})")
        if "DOY" in have and have & {"MM", "DD"}:
            raise OpenError(f"{self.text}: use either {{DOY}} or {{MM}} and {{DD}}")
        if "hh" in have and not have & {"DD", "DOY"}:
            raise OpenError(f"{self.text}: {{hh}} needs the day ({{DD}} or {{DOY}})")
        self.regex = re.compile(_to_regex(self.text, backrefs=True))
        self.step = (
            "h" if "hh" in have else "D" if have & {"DD", "DOY"} else "M" if "MM" in have else "Y"
        )

    def time_of(self, location: str) -> datetime | None:
        m = self.regex.fullmatch(location)
        return _time_from(m.groupdict()) if m else None

    def format(self, t: datetime) -> str:
        values = {
            "YYYY": f"{t.year:04d}",
            "MM": f"{t.month:02d}",
            "DD": f"{t.day:02d}",
            "DOY": f"{t.timetuple().tm_yday:03d}",
            "hh": f"{t.hour:02d}",
        }
        return _PLACEHOLDER.sub(lambda m: values[m.group(1)], self.text)

    def times_between(self, start: str, end: str) -> list[datetime]:
        """Every time step from start to end (inclusive), at the pattern's step."""
        a, b = pd.Timestamp(start), pd.Timestamp(end)
        if self.step == "M":
            a = a.to_period("M").start_time
        elif self.step == "Y":
            a = a.to_period("Y").start_time
        elif self.step == "D":
            a = a.normalize()
        freq = {"h": "h", "D": "D", "M": "MS", "Y": "YS"}[self.step]
        return [t.to_pydatetime() for t in pd.date_range(a, b, freq=freq)]


def _to_regex(text: str, backrefs: bool) -> str:
    """The pattern as a regular expression: placeholders become digit groups (named the first time,
    back-references later when `backrefs`), the rest is literal."""
    out, seen, pos = [], set(), 0
    for m in _PLACEHOLDER.finditer(text):
        out.append(re.escape(text[pos : m.start()]))
        name = m.group(1)
        if name in seen and backrefs:
            out.append(f"(?P={name})")
        elif name in seen:
            out.append(rf"\d{{{_FIELDS[name]}}}")
        else:
            out.append(rf"(?P<{name}>\d{{{_FIELDS[name]}}})")
            seen.add(name)
        pos = m.end()
    out.append(re.escape(text[pos:]))
    return "".join(out)


def _time_from(groups: dict[str, str | None]) -> datetime | None:
    try:
        year = int(groups["YYYY"] or 0)
        if groups.get("DOY"):
            t = datetime(year, 1, 1) + timedelta(days=int(groups["DOY"] or 1) - 1)
            if t.year != year:
                return None
        else:
            t = datetime(year, int(groups.get("MM") or 1), int(groups.get("DD") or 1))
        return t + timedelta(hours=int(groups.get("hh") or 0))
    except ValueError:  # 31 April, month 13, …: not a date
        return None


# ------------------------------------------------------------------ finding the files
def find_files(
    location: str,
    time_range: tuple[str | None, str | None] | None = None,
    storage_options: dict[str, Any] | None = None,
) -> tuple[np.ndarray, list[str]]:
    """(times, files) of a stack, sorted by time. Raises OpenError if no file is found."""
    so = storage_options or {}
    start, end = time_range or (None, None)
    pat = Pattern(location)
    if start and end:  # the names are built: no listing needed
        found = [(t, pat.format(t)) for t in pat.times_between(start, end)]
        if not found:
            raise OpenError(f"time_range {start} … {end} holds no time step of {location}")
    else:
        found = []
        for f in _list_matching(location, so):
            t = pat.time_of(f)
            if t is not None:
                found.append((t, f))
        if not found:
            raise OpenError(
                f"no files match {location}; check the pattern (placeholders {{YYYY}}, {{MM}}, "
                "{DD}, {DOY}, {hh}), or give both ends of time_range to build the names "
                "without listing"
            )
    lo = pd.Timestamp(start) if start else None
    hi = pd.Timestamp(end) if end else None
    found = [(t, f) for t, f in found if (lo is None or t >= lo) and (hi is None or t <= hi)]
    if not found:
        raise OpenError(f"time_range {start} … {end} selects none of the files of {location}")
    found.sort(key=lambda tf: tf[0])
    times = [t for t, _ in found]
    dupes = sorted({str(a) for a, b in itertools.pairwise(times) if a == b})
    if dupes:
        raise OpenError(f"several files have the same time ({', '.join(dupes[:3])}) in {location}")
    return np.array(times, dtype="datetime64[ns]"), [f for _, f in found]


def _list_matching(location: str, so: dict[str, Any]) -> list[str]:
    """Every file that matches a placeholder pattern: the folders are listed level by level from
    the last folder before the first placeholder (folders of one level in parallel)."""
    first = _PLACEHOLDER.search(location)
    assert first is not None
    head = location[: first.start()]
    base = head[: head.rfind("/") + 1]  # "" for a name in the current folder
    parts = location[len(base) :].split("/")
    current = [base]
    for k, part in enumerate(parts):
        last = k == len(parts) - 1
        rx = re.compile(_to_regex(part, backrefs=False))

        def keep(folder: str, rx: re.Pattern[str] = rx, last: bool = last) -> list[str]:
            return [
                folder + name + ("" if last else "/")
                for name, is_dir in _list_folder(folder or ".", so)
                if is_dir != last and rx.fullmatch(name)
            ]

        current = [p for found in _POOL.map(keep, current) for p in found]
        if not current:
            return []
    return current


def _list_folder(folder: str, so: dict[str, Any]) -> list[tuple[str, bool]]:
    """(name, is_folder) of the entries of one folder: local, s3:// or S3-compatible HTTPS."""
    if folder.startswith(("http://", "https://")):
        return _list_https(folder)
    if is_s3(folder):
        url = gdal_path(folder, so)
        if url != folder:  # public: plain HTTPS, no AWS setup
            return _list_https(url)
        import fsspec

        fs = fsspec.filesystem("s3", **so)
        out = []
        for e in fs.ls(folder, detail=True):
            name = str(e["name"]).rstrip("/").rsplit("/", 1)[-1]
            out.append((name, e.get("type") == "directory"))
        return out
    try:
        with os.scandir(folder) as it:
            return [(e.name, e.is_dir()) for e in it]
    except FileNotFoundError:
        return []
    except OSError as e:
        raise OpenError(f"cannot list {folder}: {e}") from e


def _list_https(folder: str) -> list[tuple[str, bool]]:
    """An S3 listing (ListObjectsV2) of a folder given by its HTTPS address: path style
    (https://host/bucket/prefix/) or AWS virtual hosts (https://bucket.s3.amazonaws.com/prefix/)."""
    u = urllib.parse.urlsplit(folder)
    path = u.path.lstrip("/")
    if ".s3" in u.netloc and u.netloc.endswith("amazonaws.com"):
        root, prefix = f"{u.scheme}://{u.netloc}/", path
    else:
        bucket, _, prefix = path.partition("/")
        root = f"{u.scheme}://{u.netloc}/{bucket}"
    out: list[tuple[str, bool]] = []
    token = None
    for _ in range(_LIST_PAGES):
        q = {"list-type": "2", "prefix": prefix, "delimiter": "/"}
        if token:
            q["continuation-token"] = token
        url = f"{root}?{urllib.parse.urlencode(q)}"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                tree = ET.fromstring(r.read())
        except (urllib.error.URLError, OSError, ET.ParseError) as e:
            raise OpenError(
                f"cannot list the files at {folder} ({e}); the store must allow S3 listing, or "
                "give both ends of time_range to build the file names without listing"
            ) from e
        if not tree.tag.endswith("ListBucketResult"):
            raise OpenError(f"{folder} is not an S3 folder; give both ends of time_range")
        ns = tree.tag[: tree.tag.index("}") + 1] if tree.tag.startswith("{") else ""
        for p in tree.iter(f"{ns}CommonPrefixes"):
            name = (p.findtext(f"{ns}Prefix") or "")[len(prefix) :].rstrip("/")
            if name:
                out.append((name, True))
        for c in tree.iter(f"{ns}Contents"):
            name = (c.findtext(f"{ns}Key") or "")[len(prefix) :]
            if name and "/" not in name:
                out.append((name, False))
        token = tree.findtext(f"{ns}NextContinuationToken")
        if tree.findtext(f"{ns}IsTruncated") != "true" or not token:
            break
    return out


# ------------------------------------------------------------------ reading
@dataclass
class _Shared:
    """What all levels of one stack share."""

    files: list[str]
    storage_options: dict[str, Any]
    scale: float  # the first file's packing (internal overviews do not carry it)
    offset: float
    missing: set[int] = field(default_factory=set)  # files that do not exist
    warned: set[int] = field(default_factory=set)


class StackArray(BackendArray):
    """(time, y, x) float32 values of one resolution level, read lazily from one file per step."""

    def __init__(self, shared: _Shared, shape: tuple[int, int], overview: int | None):
        self.shared = shared
        self.overview = overview
        self.shape = (len(shared.files), *shape)
        self.dtype = np.dtype(np.float32)

    def __getitem__(self, key: indexing.ExplicitIndexer) -> np.ndarray:
        return indexing.explicit_indexing_adapter(
            key, self.shape, indexing.IndexingSupport.OUTER, self._getitem
        )

    def _getitem(self, key: tuple[Any, ...]) -> np.ndarray:
        tk, yk, xk = key
        t_idx = np.arange(self.shape[0])[tk]
        rows, rsel = _span(yk, self.shape[1])
        cols, csel = _span(xk, self.shape[2])
        steps = np.atleast_1d(t_idx)
        h, w = rows[1] - rows[0], cols[1] - cols[0]
        if steps.size == 0 or h <= 0 or w <= 0:
            out = np.full((steps.size, max(h, 0), max(w, 0)), np.nan, np.float32)
        else:

            def one(i: Any) -> np.ndarray:
                return self._read(int(i), rows[0], rows[1], cols[0], cols[1])

            blocks = [one(steps[0])] if steps.size == 1 else list(_POOL.map(one, steps))
            out = np.stack(blocks)
        out = out[:, rsel]  # one axis at a time: outer indexing
        out = out[..., csel]
        return out[0] if np.ndim(t_idx) == 0 else out

    def _read(self, i: int, r0: int, r1: int, c0: int, c1: int) -> np.ndarray:
        import rasterio
        from rasterio.windows import Window

        sh = self.shared
        empty = np.full((r1 - r0, c1 - c0), np.nan, np.float32)
        if i in sh.missing:
            return empty
        path = gdal_path(sh.files[i], sh.storage_options)
        kw = {} if self.overview is None else {"overview_level": self.overview}
        try:
            with rasterio.open(path, **kw) as src:
                if (src.height, src.width) != self.shape[1:]:
                    if i not in sh.warned:
                        sh.warned.add(i)
                        log.warning(
                            "%s: %d × %d cells, not %d × %d as the first file; left out",
                            sh.files[i],
                            src.height,
                            src.width,
                            *self.shape[1:],
                        )
                    return empty
                a = src.read(1, window=Window(c0, r0, c1 - c0, r1 - r0), masked=True)
                scale, offset = (
                    (src.scales[0], src.offsets[0])
                    if self.overview is None
                    else (sh.scale, sh.offset)
                )
        except rasterio.errors.RasterioIOError as e:
            if _not_found(e):
                sh.missing.add(i)
                return empty
            raise
        out = np.ma.filled(a.astype(np.float32), np.nan)
        if (scale, offset) != (1.0, 0.0):
            out = (out * np.float32(scale) + np.float32(offset)).astype(np.float32)
        return out


def _span(k: Any, n: int) -> tuple[tuple[int, int], Any]:
    """The contiguous range [a, b) to read for one axis key, and the selection within it."""
    if isinstance(k, slice):
        start, stop, step = k.indices(n)
        if step == 1:
            return (start, max(start, stop)), slice(None)
    idx = np.arange(n)[k]
    if np.ndim(idx) == 0:
        return (int(idx), int(idx) + 1), 0
    if idx.size == 0:
        return (0, 0), slice(None)
    a = int(idx.min())
    return (a, int(idx.max()) + 1), idx - a


def _not_found(e: Exception) -> bool:
    text = str(e)
    return any(s in text for s in ("404", "No such file", "does not exist", "not recognized"))


def open_levels(
    location: str,
    time_range: tuple[str | None, str | None] | None = None,
    storage_options: dict[str, Any] | None = None,
) -> list[tuple[str, xr.Dataset, str]]:
    """(origin, dataset, location) of every resolution level of a stack: the full resolution and
    the internal overviews of its files, each a Dataset with one (time, y, x) variable."""
    import rasterio

    from geoloupe.readers import _gdal_env, _open_geotiff, geotiff_overviews

    so = dict(storage_options or {})
    _gdal_env(so)
    times, files = find_files(location, time_range, so)
    first, problems = None, []
    for f in files[:: max(1, len(files) // 8)][:9]:  # a few files spread over the stack
        try:
            with rasterio.open(gdal_path(f, so)) as src:
                if src.count != 1:
                    raise OpenError(
                        f"{f} has {src.count} bands; time stacks need single-band files"
                    )
                scale, offset = float(src.scales[0]), float(src.offsets[0])
            first = f
            break
        except rasterio.errors.RasterioIOError as e:
            problems.append(f"{f}: {e}")
    if first is None:
        raise OpenError(f"none of the files of {location} could be opened: {problems[0]}")
    shared = _Shared(files, so, scale, offset)
    name = stack_name(location)
    factors = geotiff_overviews(first, so)
    out: list[tuple[str, xr.Dataset, str]] = []
    for level in [None, *range(len(factors))]:
        kw = {} if level is None else {"overview_level": level}
        tmpl = _open_geotiff(first, kw, so)
        var = next(v for v in tmpl.data_vars if v != "spatial_ref")
        da0 = tmpl[var]
        ydim, xdim = (str(d) for d in da0.dims)
        attrs = {
            k: v
            for k, v in da0.attrs.items()
            if k not in ("scale_factor", "add_offset", "_FillValue", "missing_value")
        }
        arr = StackArray(shared, (da0.sizes[ydim], da0.sizes[xdim]), level)
        variable = xr.Variable(("time", ydim, xdim), indexing.LazilyIndexedArray(arr), attrs)
        ds = xr.Dataset(
            {name: variable},
            coords={"time": ("time", times), ydim: da0[ydim].values, xdim: da0[xdim].values},
        )
        for d in (ydim, xdim):
            ds[d].attrs.update(da0[d].attrs)
        if "spatial_ref" in tmpl:
            ds["spatial_ref"] = tmpl["spatial_ref"]
            ds[name].attrs["grid_mapping"] = "spatial_ref"
        where = location if level is None else f"{location} (overview ×{factors[level]})"
        out.append(("native" if level is None else "overview", ds, where))
    log.info("time stack %s: %d files from %s to %s", location, len(files), times[0], times[-1])
    return out
