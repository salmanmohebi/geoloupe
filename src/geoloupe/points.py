"""Point observations (weather stations, buoys, sensors): datasets of their own.

A point source is a set of fixed locations (stations) with values over time.
Every observed variable becomes a layer of its own (``<source>:<variable>``,
own colour scale and time axis), and can also be drawn over gridded layers.

Two storage families are supported:

* **Tables** (CSV, Parquet — a file or a hive-partitioned directory —, and
  GeoParquet), one row per station and time, either
  - *long*: a variable column and a value column (e.g. GHCN-Daily / ECA&D archives:
    ``station_id, lat, lon, date, element, value``), or
  - *wide*: one column per variable (``id, lon, lat, date, tas, pr``).
  Column names are detected (``lat``/``latitude``, ``date``/``time``, …) or set
  in the config. Parquet is read with filter push-down, so a query loads only
  the requested day.
* **CF discrete sampling geometries** in NetCDF or Zarr with
  ``featureType = "timeSeries"`` (orthogonal layout: variables on a station and
  a time dimension, ``lat``/``lon`` per station).

Values can be converted to the units of a gridded layer with ``scale``/``offset``
per variable (station temperatures are often °C, gridded ones K).
"""

from __future__ import annotations

import logging
import os
import re
import struct
import threading
from abc import ABC, abstractmethod
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from geoloupe.config import Config, PointColumns, PointsConfig
from geoloupe.readers import public_https, s3_options, with_retries
from geoloupe.styles import default_style
from geoloupe.timeaxis import TimeAxis, TimeError, compact_axis

log = logging.getLogger(__name__)

TABLE_EXT = (".csv", ".csv.gz", ".txt", ".tsv", ".parquet", ".parq", ".geoparquet")


class PointsError(ValueError):
    """A point query cannot be answered."""


@dataclass
class PointVar:
    name: str
    title: str | None
    units: str | None


def _day_bounds(label: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    t = pd.Timestamp(label)
    start = t.normalize() if len(label) <= 10 else t
    end = start + (pd.Timedelta(days=1) if len(label) <= 10 else pd.Timedelta(seconds=1))
    return start, end


def _labels(times: pd.DatetimeIndex) -> list[str]:
    midnight = bool((times.normalize() == times).all())
    return [t.strftime("%Y-%m-%d" if midnight else "%Y-%m-%dT%H:%M") for t in times]


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def _round(v: float) -> float | None:
    return float(f"{v:.6g}") if np.isfinite(v) else None


# ---------------------------------------------------------------- columns
_CANDIDATES: dict[str, tuple[str, ...]] = {
    "lon": ("lon", "longitude", "lng", "long", "x"),
    "lat": ("lat", "latitude", "y"),
    "time": ("date", "time", "datetime", "timestamp", "valid_time", "obs_time", "date_time"),
    "id": ("station_id", "station", "stationid", "id", "site_id", "site", "stid", "wmo_id"),
    "name": ("station_name", "name", "site_name", "stn_name"),
    "elevation": ("elevation_m", "elevation", "elev", "altitude", "alt", "height", "z"),
    "variable": ("element", "variable", "parameter", "param", "var"),
    "value": ("value", "val", "observation", "obs"),
}


def detect_columns(names: list[str], given: PointColumns) -> PointColumns:
    """Fill the columns that the config does not set, by common names (case-insensitive)."""
    lower = {n.lower(): n for n in names}
    found: dict[str, str | None] = {}
    for key, cands in _CANDIDATES.items():
        explicit = getattr(given, key)
        if explicit is not None:
            found[key] = explicit
            continue
        found[key] = next((lower[c] for c in cands if c in lower), None)
    if found["variable"] and not found["value"]:
        found["variable"] = None  # a "parameter" column without a value column: wide table
    if not found["variable"]:
        found["value"] = None
    if found["id"] is None:
        found["id"] = found["name"]
    missing = [k for k in ("lon", "lat", "time") if not found[k]]
    if missing and given.geometry is None:
        raise PointsError(
            f"cannot find the {', '.join(missing)} column(s) among {names[:20]}; set them under 'columns'"
        )
    return PointColumns(**{**given.model_dump(), **found})


def _wkb_point(b: bytes) -> tuple[float, float]:
    """(x, y) of a WKB / ISO WKB point (GeoParquet geometry column)."""
    order = "<" if b[0] == 1 else ">"
    gtype = struct.unpack(order + "I", b[1:5])[0] % 1000
    if gtype != 1:
        raise PointsError("only Point geometries are supported in point tables")
    return struct.unpack(order + "dd", b[5:21])


# ----------------------------------------------------------------- source
class PointSource(ABC):
    """A set of stations with values over time."""

    def __init__(self, sid: str, cfg: PointsConfig, location: str):
        self.id = sid
        self.cfg = cfg
        self.location = location
        self._lock = threading.Lock()
        self._cache: OrderedDict[Any, Any] = OrderedDict()

    @property
    def title(self) -> str:
        return self.cfg.title or self.id

    def _cached(self, key: Any, make: Any) -> Any:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        value = make()
        with self._lock:
            self._cache[key] = value
            while len(self._cache) > 32:
                self._cache.popitem(last=False)
        return value

    def convert(self, variable: str, values: np.ndarray) -> np.ndarray:
        v = self.cfg.variables.get(variable)
        out = np.asarray(values, dtype=np.float64)
        return out * v.scale + v.offset if v else out

    def var_info(self, name: str, attrs: dict[str, Any] | None = None) -> PointVar:
        v = self.cfg.variables.get(name)
        attrs = attrs or {}
        return PointVar(
            name, attrs.get("long_name"), (v.units if v and v.units else None) or attrs.get("units")
        )

    # implemented by backends
    @abstractmethod
    def variables(self) -> list[PointVar]: ...

    @abstractmethod
    def times(self) -> list[str]: ...

    @abstractmethod
    def bounds(self) -> tuple[float, float, float, float]: ...

    @abstractmethod
    def _frame_at(self, variable: str, time: str) -> pd.DataFrame:
        """Columns id, name, lon, lat, elevation, value (unconverted) of one time."""

    @abstractmethod
    def _station_series(
        self, station: str, variable: str, start: str, end: str
    ) -> tuple[pd.DataFrame, list[str]]:
        """(one row of station metadata, labels) and the values; see `series`."""

    # queries
    def at_time(
        self,
        variable: str,
        time: str,
        bbox: tuple[float, float, float, float] | None = None,
        limit: int = 20_000,
    ) -> dict[str, Any]:
        """Observations of one time (a whole day for date labels) inside a box."""
        df = self._cached(("at", variable, time), lambda: self._frame_at(variable, time))
        if bbox is not None and len(df):
            w, s, e, n = bbox
            lon, lat = df["lon"].to_numpy(), df["lat"].to_numpy()
            m = (
                (lat >= s)
                & (lat <= n)
                & (((lon >= w) & (lon <= e)) if w <= e else ((lon >= w) | (lon <= e)))
            )
            df = df.loc[m]
        df = df[np.isfinite(df["value"].to_numpy(dtype=np.float64))]
        truncated = len(df) > limit
        if truncated:
            df = df.iloc[np.linspace(0, len(df) - 1, limit).astype(int)]
        values = self.convert(variable, df["value"].to_numpy())
        features = [
            {
                "id": str(r["id"]),
                "name": None
                if r.get("name") is None or r.get("name") != r.get("name")
                else str(r["name"]),
                "lon": float(r["lon"]),
                "lat": float(r["lat"]),
                "elevation": _num(r.get("elevation")),
                "value": _round(values[i]),
            }
            for i, r in enumerate(df.to_dict("records"))
        ]
        return {
            "source": self.id,
            "title": self.title,
            "variable": variable,
            "time": time,
            "count": len(features),
            "truncated": truncated,
            "features": features,
        }

    def series(self, station: str, variable: str, start: str, end: str) -> dict[str, Any]:
        """Observations of one station over a period."""
        df, labels = self._station_series(station, variable, start, end)
        if df.empty:
            raise PointsError(
                f"no {variable} observations for station {station} in {start} … {end}"
            )
        vals = self.convert(variable, df["value"].to_numpy())
        first = df.iloc[0]
        return {
            "source": self.id,
            "station": station,
            "name": None if first.get("name") is None else str(first["name"]),
            "lon": float(first["lon"]),
            "lat": float(first["lat"]),
            "elevation": _num(first.get("elevation")),
            "times": labels,
            "values": [_round(v) for v in vals],
        }

    def stats(self, variable: str, time: str) -> dict[str, Any]:
        """Value statistics of one time (for the colour range of a point layer)."""
        d = self.at_time(variable, time, limit=10**9)
        v = np.array(
            [f["value"] for f in d["features"] if f["value"] is not None], dtype=np.float64
        )
        if not v.size:
            return {"count": 0}
        p = np.percentile(v, [2, 25, 50, 75, 98])
        return {
            "count": int(v.size),
            "min": float(v.min()),
            "max": float(v.max()),
            "mean": float(v.mean()),
            "p2": float(p[0]),
            "p25": float(p[1]),
            "median": float(p[2]),
            "p75": float(p[3]),
            "p98": float(p[4]),
        }


# ------------------------------------------------------------------ tables
class TablePointSource(PointSource):
    """CSV, Parquet (file or hive-partitioned directory) and GeoParquet tables."""

    def __init__(
        self,
        sid: str,
        cfg: PointsConfig,
        location: str,
        fmt: str,
        locations: list[str] | None = None,
    ):
        super().__init__(sid, cfg, location)
        self.format = fmt
        self.locations = locations or [location]
        self._frame: pd.DataFrame | None = None
        self._dataset: Any = None
        names = self._schema_names()
        given = cfg.columns
        if given.geometry is None and "geometry" in names and not self._has_lonlat(names):
            given = given.model_copy(update={"geometry": "geometry"})  # GeoParquet points
        self.cols = detect_columns(names, given)
        self._geom = self.cols.geometry
        self._names = names

    def _has_lonlat(self, names: list[str]) -> bool:
        lower = {n.lower() for n in names}
        return bool(lower & set(_CANDIDATES["lon"])) and bool(lower & set(_CANDIDATES["lat"]))

    # raw access ------------------------------------------------------
    def _schema_names(self) -> list[str]:
        if self.format == "csv":
            return list(self._load_csv().columns)
        return list(self._pa_dataset().schema.names)

    def _csv_usecols(self) -> Any:
        """Columns to read from a wide CSV whose variables the config lists; None = all.

        Station files often carry many more columns (quality flags, attributes) than are shown;
        skipping them saves most of the memory and parsing time.
        """
        if not self.cfg.variables or self.cfg.columns.variable:
            return None
        keep = {c.lower() for cands in _CANDIDATES.values() for c in cands}
        keep |= {str(v).lower() for v in self.cfg.columns.model_dump().values() if v}
        keep |= {k.lower() for k in (*self.cfg.variables, *self.cfg.filters)}
        return lambda name: str(name).lower() in keep

    def _load_csv(self) -> pd.DataFrame:
        if self._frame is None:
            sep = "\t" if self.location.lower().endswith(".tsv") else ","
            usecols = self._csv_usecols()

            def read(loc: str) -> pd.DataFrame:
                url = public_https(loc, self.cfg.storage_options)  # public S3: plain HTTPS
                if url:
                    return pd.read_csv(url, sep=sep, low_memory=False, usecols=usecols)
                so = s3_options(loc, self.cfg.storage_options) if _fsspec_url(loc) else None
                return pd.read_csv(
                    loc, sep=sep, low_memory=False, usecols=usecols, storage_options=so
                )

            with ThreadPoolExecutor(max_workers=8) as pool:  # remote files: download in parallel
                frames = list(pool.map(read, self.locations))
            self._frame = frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True)
        return self._frame

    def _pa_dataset(self) -> Any:
        if self._dataset is None:
            import pyarrow.dataset as pds

            fs, paths = _filesystem(self.locations, self.cfg.storage_options)
            folder = fs.isdir(paths[0]) if fs is not None else os.path.isdir(self.location)
            part = "hive" if folder else None
            src = paths if len(paths) > 1 else paths[0]
            self._dataset = pds.dataset(src, format="parquet", partitioning=part, filesystem=fs)
        return self._dataset

    def _csv_prepared(self) -> pd.DataFrame:
        df = self._load_csv()
        tcol = self.cols.time
        if tcol and not pd.api.types.is_datetime64_any_dtype(df[tcol]):
            df[tcol] = pd.to_datetime(df[tcol])
        return df

    def _value_column(self, variable: str) -> str:
        return self.cols.value if self.cols.variable and self.cols.value else variable

    def _select(
        self,
        variable: str | None,
        start: pd.Timestamp | None,
        end: pd.Timestamp | None,
        station: str | None,
        extra_cols: tuple[str, ...] = (),
    ) -> pd.DataFrame:
        c = self.cols
        vcol = self._value_column(variable) if variable else None
        wanted = [
            x
            for x in (
                c.id,
                c.name,
                c.lon,
                c.lat,
                c.elevation,
                c.time,
                vcol,
                self._geom,
                *extra_cols,
            )
            if x
        ]
        wanted = list(dict.fromkeys(w for w in wanted if w in self._names))
        if self.format == "csv":
            df = self._csv_prepared()
            m = np.ones(len(df), dtype=bool)
            if start is not None and end is not None:
                m &= (df[c.time] >= start) & (df[c.time] < end)
            if variable and c.variable:
                m &= df[c.variable] == variable
            if station is not None:
                m &= df[c.id].astype(str) == station
            for k, v in self.cfg.filters.items():
                m &= df[k] == v
            return df.loc[m, wanted]
        import pyarrow.dataset as pds

        ds = self._pa_dataset()
        f = None

        def both(a: Any, b: Any) -> Any:
            return b if a is None else a & b

        if start is not None and end is not None:
            f = both(
                f,
                (pds.field(c.time) >= start.to_pydatetime())
                & (pds.field(c.time) < end.to_pydatetime()),
            )
            if "year" in self._names and c.time != "year":  # hive partition pruning
                years = list(range(start.year, (end - pd.Timedelta(microseconds=1)).year + 1))
                f = both(f, pds.field("year").isin(years))
        if variable and c.variable:
            f = both(f, pds.field(c.variable) == variable)
        if station is not None:
            f = both(f, pds.field(c.id) == station)
        for k, v in self.cfg.filters.items():
            f = both(f, pds.field(k) == v)
        return ds.to_table(filter=f, columns=wanted).to_pandas()

    def _standard(self, df: pd.DataFrame, variable: str | None) -> pd.DataFrame:
        """Rename to id, name, lon, lat, elevation, time, value."""
        c = self.cols
        out = pd.DataFrame(index=df.index)
        if self._geom and self._geom in df.columns:
            xy = np.array(
                [
                    _wkb_point(bytes(b)) if b is not None else (np.nan, np.nan)
                    for b in df[self._geom]
                ]
            )
            out["lon"], out["lat"] = (xy[:, 0], xy[:, 1]) if len(xy) else ([], [])
        else:
            out["lon"] = df[c.lon].astype(float)
            out["lat"] = df[c.lat].astype(float)
        out["id"] = df[c.id].astype(str) if c.id in df.columns else out.index.astype(str)
        out["name"] = df[c.name] if c.name and c.name in df.columns else None
        out["elevation"] = df[c.elevation] if c.elevation and c.elevation in df.columns else None
        if c.time in df.columns:
            out["time"] = pd.to_datetime(df[c.time])
        if variable:
            out["value"] = pd.to_numeric(df[self._value_column(variable)], errors="coerce")
        return out

    # metadata ---------------------------------------------------------
    def variables(self) -> list[PointVar]:
        def make() -> list[PointVar]:
            c = self.cols
            if self.cfg.variables:
                return [self.var_info(v) for v in self.cfg.variables]
            if c.variable:
                if self.format == "csv":
                    names = sorted(self._csv_prepared()[c.variable].dropna().astype(str).unique())
                else:
                    import pyarrow.compute as pc

                    frag = next(iter(self._pa_dataset().get_fragments()))
                    names = sorted(
                        str(x)
                        for x in pc.unique(
                            frag.to_table(columns=[c.variable])[c.variable]
                        ).to_pylist()
                    )
                return [self.var_info(v) for v in names]
            known = {c.id, c.name, c.lon, c.lat, c.elevation, c.time, self._geom, "year"}
            if self.format == "csv":
                df = self._load_csv()
                numeric = [
                    n for n in df.columns if n not in known and pd.api.types.is_numeric_dtype(df[n])
                ]
            else:
                import pyarrow as pa

                schema = self._pa_dataset().schema
                numeric = [
                    f.name
                    for f in schema
                    if f.name not in known
                    and (pa.types.is_integer(f.type) or pa.types.is_floating(f.type))
                ]
            return [
                self.var_info(v)
                for v in numeric
                if not re.search(r"(_attributes|_flag|_qc)$", v, re.I)
            ]

        return self._cached(("variables",), make)

    def times(self) -> list[str]:
        def make() -> list[str]:
            if self.format == "csv":
                t = pd.DatetimeIndex(sorted(self._csv_prepared()[self.cols.time].dropna().unique()))
                return _labels(t)
            lo, hi, rows = self._parquet_time_range()
            step = self.cfg.time_step
            if rows <= 5_000_000 and not step:
                col = self._pa_dataset().to_table(columns=[self.cols.time])[self.cols.time]
                return _labels(pd.DatetimeIndex(sorted(set(col.to_pandas()))))
            return _labels(pd.date_range(lo, hi, freq=pd.Timedelta(step or "1D")))

        return self._cached(("times",), make)

    def _parquet_time_range(self) -> tuple[pd.Timestamp, pd.Timestamp, int]:
        """Min and max time from row-group statistics (no data read), and the row count."""
        lo, hi, rows = None, None, 0
        for frag in self._pa_dataset().get_fragments():
            md = frag.metadata
            rows += md.num_rows
            idx = (
                md.schema.names.index(self.cols.time) if self.cols.time in md.schema.names else None
            )
            for g in range(md.num_row_groups):
                st = md.row_group(g).column(idx).statistics if idx is not None else None
                if st is not None and st.has_min_max:
                    lo = st.min if lo is None else min(lo, st.min)
                    hi = st.max if hi is None else max(hi, st.max)
        if lo is None:
            raise PointsError(
                "cannot determine the time range (no statistics); set 'time_step' or use CSV"
            )
        return pd.Timestamp(lo), pd.Timestamp(hi), rows

    def bounds(self) -> tuple[float, float, float, float]:
        def make() -> tuple[float, float, float, float]:
            if self.format == "csv" or self._geom:
                df = self._standard(self._select(None, None, None, None), None)
                lon, lat = df["lon"].to_numpy(), df["lat"].to_numpy()
            else:
                cols = [self.cols.lon, self.cols.lat]
                frag = next(iter(self._pa_dataset().get_fragments()))
                t = frag.to_table(columns=cols)
                lon, lat = t[cols[0]].to_numpy(), t[cols[1]].to_numpy()
            ok = np.isfinite(lon) & np.isfinite(lat)
            if not ok.any():
                return (-180.0, -90.0, 180.0, 90.0)
            return (
                float(lon[ok].min()),
                float(lat[ok].min()),
                float(lon[ok].max()),
                float(lat[ok].max()),
            )

        return self._cached(("bounds",), make)

    # queries -----------------------------------------------------------
    def _frame_at(self, variable: str, time: str) -> pd.DataFrame:
        start, end = _day_bounds(time)
        return self._standard(self._select(variable, start, end, None), variable)

    def _station_series(
        self, station: str, variable: str, start: str, end: str
    ) -> tuple[pd.DataFrame, list[str]]:
        t0, _ = _day_bounds(start)
        _, t1 = _day_bounds(end)
        df = self._standard(self._select(variable, t0, t1, station), variable)
        if df.empty:
            return df, []
        df = df.sort_values("time")
        return df, _labels(pd.DatetimeIndex(df["time"]))


# --------------------------------------------------------------- CF DSG
class DSGPointSource(PointSource):
    """CF discrete sampling geometry, featureType timeSeries, orthogonal layout (NetCDF / Zarr)."""

    def __init__(self, sid: str, cfg: PointsConfig, location: str, ds: Any):
        super().__init__(sid, cfg, location)
        self.ds = ds
        self.station_dim, self.time_dim = self._dims()
        self.lon = np.asarray(self._coord(("longitude", "lon")), dtype=np.float64)
        self.lat = np.asarray(self._coord(("latitude", "lat")), dtype=np.float64)
        self.ids = self._ids()
        self.names = self._names()
        self.elev = self._optional(("height", "altitude", "elevation", "elev", "station_elevation"))
        self.taxis = TimeAxis.from_values(ds[self.time_dim].values)

    def _find(self, wanted: tuple[str, ...], by: str = "standard_name") -> str | None:
        for name, v in self.ds.variables.items():
            if v.attrs.get(by) in wanted:
                return str(name)
        for name in self.ds.variables:
            if str(name).lower() in wanted:
                return str(name)
        return None

    def _coord(self, wanted: tuple[str, ...]) -> Any:
        name = self._find(wanted)
        if name is None:
            raise PointsError(f"no {wanted[0]} variable in this station dataset")
        return self.ds[name].values

    def _optional(self, wanted: tuple[str, ...]) -> np.ndarray | None:
        name = self._find(wanted)
        return None if name is None else np.asarray(self.ds[name].values, dtype=np.float64)

    def _dims(self) -> tuple[str, str]:
        tname = next(
            (
                str(n)
                for n, v in self.ds.variables.items()
                if np.issubdtype(v.dtype, np.datetime64)
                or v.attrs.get("axis") == "T"
                or str(n) == "time"
            ),
            None,
        )
        if tname is None:
            raise PointsError("no time variable in this station dataset")
        tdim = self.ds[tname].dims[0]
        lat = self._find(("latitude", "lat"))
        if lat is None or not self.ds[lat].dims:
            raise PointsError("no station latitude variable")
        return str(self.ds[lat].dims[0]), str(tdim)

    def _ids(self) -> list[str]:
        name = self._find(("timeseries_id",), by="cf_role") or self._find(
            ("station_id", "station", "id", "wmo_id")
        )
        n = self.ds.sizes[self.station_dim]
        if name is None:
            return [str(i) for i in range(n)]
        vals = self.ds[name].values
        return [v.decode() if isinstance(v, bytes) else str(v) for v in vals]

    def _names(self) -> list[str | None]:
        name = self._find(("station_name", "name", "site_name"))
        if name is None:
            return [None] * len(self.ids)
        vals = self.ds[name].values
        return [v.decode() if isinstance(v, bytes) else str(v) for v in vals]

    def variables(self) -> list[PointVar]:
        out = []
        for name, da in self.ds.data_vars.items():
            if set(da.dims) == {self.station_dim, self.time_dim} and np.issubdtype(
                da.dtype, np.number
            ):
                if self.cfg.variables and name not in self.cfg.variables:
                    continue
                out.append(self.var_info(str(name), dict(da.attrs)))
        return out

    def times(self) -> list[str]:
        return list(self.taxis.labels)

    def bounds(self) -> tuple[float, float, float, float]:
        ok = np.isfinite(self.lon) & np.isfinite(self.lat)
        return (
            float(self.lon[ok].min()),
            float(self.lat[ok].min()),
            float(self.lon[ok].max()),
            float(self.lat[ok].max()),
        )

    def _frame_at(self, variable: str, time: str) -> pd.DataFrame:
        try:
            i = self.taxis.index(time)
        except TimeError as e:
            raise PointsError(str(e)) from e
        if i is None:
            vals = np.full(len(self.ids), np.nan)
        else:
            vals = np.asarray(
                self.ds[variable].isel({self.time_dim: i}).transpose(self.station_dim).values,
                dtype=np.float64,
            )
        return pd.DataFrame(
            {
                "id": self.ids,
                "name": self.names,
                "lon": self.lon,
                "lat": self.lat,
                "elevation": self.elev if self.elev is not None else None,
                "value": vals,
            }
        )

    def _station_series(
        self, station: str, variable: str, start: str, end: str
    ) -> tuple[pd.DataFrame, list[str]]:
        if station not in self.ids:
            raise PointsError(f"unknown station {station!r}")
        j = self.ids.index(station)
        idx = self.taxis.range(start, end)
        if idx.size == 0:
            return pd.DataFrame(), []
        da = self.ds[variable].isel({self.station_dim: j})
        vals = np.asarray(da.values, dtype=np.float64)[idx]
        df = pd.DataFrame({"value": vals})
        df["id"], df["name"], df["lon"], df["lat"] = (
            station,
            self.names[j],
            self.lon[j],
            self.lat[j],
        )
        df["elevation"] = None if self.elev is None else self.elev[j]
        return df, [self.taxis.labels[i] for i in idx]


# ------------------------------------------------------------------ open
def is_dsg(ds: Any) -> bool:
    return str(ds.attrs.get("featureType", ds.attrs.get("featuretype", ""))).lower() == "timeseries"


def open_points(sid: str, cfg: PointsConfig, config: Config) -> PointSource:
    """Open a point source; the format follows the config or the file name."""
    paths = cfg.path if isinstance(cfg.path, list) else [cfg.path]
    resolved = [config.resolve(p) for p in paths]
    for loc in resolved:
        if "://" not in loc and not os.path.exists(loc):
            raise PointsError(f"not found: {loc}")
    location = resolved[0]
    fmt = cfg.format
    low = location.lower().rstrip("/")
    if fmt == "auto":
        if low.endswith((".csv", ".csv.gz", ".txt", ".tsv")):
            fmt = "csv"
        elif low.endswith((".nc", ".nc4", ".netcdf", ".h5", ".zarr")):
            fmt = "netcdf"
        else:
            fmt = "parquet"
    if fmt in ("netcdf", "zarr"):
        from geoloupe.readers import open_dataset

        ds = open_dataset(
            location, "zarr" if low.endswith(".zarr") else "netcdf", cfg.storage_options
        )
        if not is_dsg(ds):
            raise PointsError(f"{location} is not a CF timeSeries dataset (featureType attribute)")
        return DSGPointSource(sid, cfg, location, ds)
    return TablePointSource(sid, cfg, location, fmt, resolved)


def _fsspec_url(loc: str) -> bool:
    """Object-store URLs (s3://, gs://, …) that pandas and pyarrow read through fsspec."""
    return "://" in loc and not loc.startswith(("http://", "https://", "file://"))


def _filesystem(locations: list[str], so: dict[str, Any]) -> tuple[Any, list[str]]:
    """(fsspec filesystem or None for local files, paths without the scheme)."""
    if not _fsspec_url(locations[0]):
        return None, locations
    import fsspec

    if all(loc.lower().endswith((".parquet", ".parq")) for loc in locations):
        urls = [public_https(loc, so) for loc in locations]
        if all(urls):  # public single files: HTTPS range requests (folders need s3fs listing)
            return fsspec.filesystem("https"), [str(u) for u in urls]
    proto = locations[0].split("://", 1)[0]
    fs = fsspec.filesystem(proto, **s3_options(locations[0], so))
    return fs, [loc.split("://", 1)[1] for loc in locations]


def looks_like_points(path: str) -> bool:
    """Whether `geoloupe serve <path>` should treat the file as point data."""
    low = path.lower().rstrip("/")
    if low.endswith(TABLE_EXT):
        return True
    if low.endswith((".nc", ".nc4")):
        try:
            import xarray as xr

            with xr.open_dataset(path, engine="h5netcdf", decode_times=False) as ds:
                return is_dsg(ds)
        except Exception:  # not readable here: let the gridded reader report it
            return False
    return False


# -------------------------------------------------------------- registry
class PointRegistry:
    """All point sources of a configuration, their layers and their overlay mapping."""

    def __init__(self, config: Config, grid_layers: set[str]):
        self.sources: dict[str, PointSource] = {}
        self.errors: dict[str, str] = {}
        self.overlays: dict[str, tuple[str, str]] = {}  # grid layer → (source, variable)
        for pid, pcfg in config.points.items():
            self.add(pid, pcfg, config, grid_layers)

    def add(self, pid: str, pcfg: PointsConfig, config: Config, grid_layers: set[str]) -> bool:
        """Open one point source (also while the server runs); False if it failed (see errors)."""
        try:
            paths = pcfg.path if isinstance(pcfg.path, list) else [pcfg.path]
            remote = any("://" in p for p in paths)
            src = with_retries(lambda: open_points(pid, pcfg, config), remote, pid)
            src.variables()  # fail early on unusable tables
        except Exception as e:  # any reader error is reported, the server still starts
            self.errors[pid] = str(e)
            log.error("points %r: %s", pid, e)
            return False
        self.errors.pop(pid, None)
        self.sources[pid] = src
        for lid, var in pcfg.layers.items():
            if lid in grid_layers:
                self.overlays[lid] = (pid, var)
            else:
                log.warning("points %r: unknown layer %r", pid, lid)
        return True

    def layer_ids(self) -> dict[str, tuple[str, str]]:
        """Point layers: '<source>:<variable>' → (source, variable)."""
        out = {}
        for pid, src in self.sources.items():
            for v in src.variables():
                out[f"{pid}:{v.name}"] = (pid, v.name)
        return out

    def resolve(self, layer_id: str) -> tuple[str, str] | None:
        """The point data shown on a layer: itself for point layers, the overlay for grid layers."""
        return self.layer_ids().get(layer_id) or self.overlays.get(layer_id)

    def meta(self, pid: str, var: str) -> dict[str, Any]:
        src = self.sources[pid]
        info = next((v for v in src.variables() if v.name == var), PointVar(var, None, None))
        return {
            "source": pid,
            "variable": var,
            "title": src.title,
            "units": info.units,
            "layer": f"{pid}:{var}",
        }

    def layer_json(self, layer_id: str) -> dict[str, Any]:
        pid, var = self.layer_ids()[layer_id]
        src = self.sources[pid]
        info = next(v for v in src.variables() if v.name == var)
        style = default_style(var, {"long_name": info.title} if info.title else {})
        times = src.times()
        step = None
        if len(times) > 1:
            sample = np.array(times[: min(len(times), 1000)], dtype="datetime64[ns]")
            step = TimeAxis.from_values(sample).step_label()
        return {
            "id": layer_id,
            "title": f"{info.title or var} ({src.title})",
            "description": src.cfg.attribution,
            "kind": "points",
            "units": info.units,
            "style": {"colormap": style.colormap, "vmin": None, "vmax": None, "resampling": None},
            "bounds": list(src.bounds()),
            "times": None if (compact := compact_axis(times)) else times,
            "time_axis": compact,
            "time_step": step,
            "dims": {},
            "tile_size": 256,
            "minzoom": 0,
            "maxzoom": 22,
            "sources": [],
            "points": self.meta(pid, var),
        }
