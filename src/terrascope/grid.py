"""Rectilinear grids: which dimensions are space, in which CRS, and cell lookup.

A grid is described by 1-D cell-centre coordinates along x and y and a CRS.
That covers regular lat/lon grids (reanalyses, downscaled climate), Gaussian grids (irregular
latitude spacing), and regular grids in projected or rotated-pole CRSs.
Curvilinear grids (2-D coordinates) and unstructured meshes are not supported
yet (see ROADMAP.md).

All lookups return *fractional cell positions*: position p means cell
``floor(p)``; cell i spans ``[i, i + 1)``. Positions outside ``[0, n)`` are
outside the grid. Fractional positions let the tile engine aggregate exactly
over the cells a screen pixel covers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import cached_property

import numpy as np
import pyproj
import xarray as xr

log = logging.getLogger(__name__)

WGS84 = pyproj.CRS.from_epsg(4326)

_X_NAMES = ("lon", "longitude", "x", "rlon", "nav_lon", "xc", "west_east")
_Y_NAMES = ("lat", "latitude", "y", "rlat", "nav_lat", "yc", "south_north")
_X_STD = ("longitude", "projection_x_coordinate", "grid_longitude")
_Y_STD = ("latitude", "projection_y_coordinate", "grid_latitude")


class GridError(ValueError):
    """The data cannot be interpreted as a supported grid."""


def _is_lon(coord: xr.DataArray) -> bool:
    units = str(coord.attrs.get("units", "")).lower()
    return (
        coord.attrs.get("standard_name") == "longitude"
        or units.startswith("degrees_e")
        or (str(coord.name).lower() in ("lon", "longitude") and units in ("", "degrees", "degree"))
    )


def _is_lat(coord: xr.DataArray) -> bool:
    units = str(coord.attrs.get("units", "")).lower()
    return (
        coord.attrs.get("standard_name") == "latitude"
        or units.startswith("degrees_n")
        or (str(coord.name).lower() in ("lat", "latitude") and units in ("", "degrees", "degree"))
    )


def find_spatial_dims(
    da: xr.DataArray, x: str | None = None, y: str | None = None
) -> tuple[str, str] | None:
    """Return the (x, y) dimension names of a variable, or None if it is not gridded."""
    dims = list(da.dims)
    if x and y:
        return (x, y) if x in dims and y in dims else None

    def pick(names: tuple[str, ...], std: tuple[str, ...], axis: str) -> str | None:
        for d in dims:
            c = da.coords.get(d)
            if c is not None and (
                c.attrs.get("axis") == axis or c.attrs.get("standard_name") in std
            ):
                return str(d)
        for d in dims:
            if str(d).lower() in names:
                return str(d)
        return None

    xd = x or pick(_X_NAMES, _X_STD, "X")
    yd = y or pick(_Y_NAMES, _Y_STD, "Y")
    if xd is None or yd is None or xd == yd:
        return None
    return xd, yd


def detect_crs(
    ds: xr.Dataset, da: xr.DataArray, x: str, y: str, override: str | None
) -> pyproj.CRS:
    """CRS from the config override, the CF grid_mapping, or lat/lon coordinates."""
    if override:
        return pyproj.CRS.from_user_input(override)
    gm_name = da.attrs.get("grid_mapping") or da.encoding.get("grid_mapping")
    candidates = [gm_name] if gm_name else []
    candidates += ["spatial_ref", "crs"]  # rioxarray / common conventions
    for name in candidates:
        if name and name in ds.variables:
            attrs = ds[name].attrs
            try:
                if "crs_wkt" in attrs or "spatial_ref" in attrs:
                    return pyproj.CRS.from_wkt(attrs.get("crs_wkt") or attrs["spatial_ref"])
                if "grid_mapping_name" in attrs:
                    return pyproj.CRS.from_cf(dict(attrs))
            except pyproj.exceptions.CRSError as e:
                log.warning("cannot parse grid mapping %r: %s", name, e)
    xc, yc = da.coords.get(x), da.coords.get(y)
    if xc is not None and yc is not None and (_is_lon(xc) or _is_lat(yc)):
        return WGS84
    if str(x).lower() in ("lon", "longitude") and str(y).lower() in ("lat", "latitude"):
        return WGS84
    raise GridError(
        f"cannot determine the CRS of {da.name!r} (dims {x!r}, {y!r}); set 'crs' in the dataset config"
    )


@dataclass
class Axis:
    """One grid axis: cell centres, and lookup of fractional cell positions."""

    centers: np.ndarray
    periodic: float | None = None  # period (e.g. 360°) if the axis wraps around
    single_width: float = 1.0  # cell width when the axis has only one cell

    def __post_init__(self) -> None:
        c = np.asarray(self.centers, dtype=np.float64)
        if c.ndim != 1 or c.size == 0:
            raise GridError("grid coordinates must be 1-D and non-empty")
        if c.size > 1:
            d = np.diff(c)
            if not (np.all(d > 0) or np.all(d < 0)):
                raise GridError("grid coordinates must be strictly monotonic")
        self.centers = c

    @property
    def n(self) -> int:
        return int(self.centers.size)

    @property
    def descending(self) -> bool:
        return self.n > 1 and bool(self.centers[1] < self.centers[0])

    @cached_property
    def edges(self) -> np.ndarray:
        """Cell edges in ascending coordinate order (n + 1 values)."""
        c = self.centers[::-1] if self.descending else self.centers
        if c.size == 1:
            return np.array([c[0] - self.single_width / 2, c[0] + self.single_width / 2])
        mid = (c[1:] + c[:-1]) / 2
        return np.concatenate([[c[0] - (mid[0] - c[0])], mid, [c[-1] + (c[-1] - mid[-1])]])

    @cached_property
    def resolution(self) -> float:
        """Median cell size (absolute)."""
        return float(np.median(np.diff(self.edges)))

    @cached_property
    def regular(self) -> bool:
        w = np.diff(self.edges)
        return bool(np.allclose(w, w[0], rtol=1e-4, atol=0))

    @property
    def lo(self) -> float:
        return float(self.edges[0])

    @property
    def hi(self) -> float:
        return float(self.edges[-1])

    def wrap(self, q: np.ndarray) -> np.ndarray:
        """Bring periodic coordinates (longitudes) into the axis' 360° window.

        A global axis uses [lo, lo + period), so every longitude lands on a
        cell. A regional axis uses the window centred on its own middle, so
        the seam lies opposite the data and positions are continuous near it.
        """
        if self.periodic is None:
            return q
        origin = self.lo if self.covers_full_period() else (self.lo + self.hi - self.periodic) / 2
        return origin + np.mod(q - origin, self.periodic)

    def position(self, q: np.ndarray) -> np.ndarray:
        """Fractional cell positions of coordinates `q` (in storage index order).

        Inside cell i the position runs linearly from i to i + 1. Values
        outside ``[0, n]`` mean outside the grid.
        """
        q = self.wrap(np.asarray(q, dtype=np.float64))
        e = self.edges
        if self.regular:
            pos = (q - e[0]) / (e[-1] - e[0]) * self.n
        else:
            i = np.clip(np.searchsorted(e, q, side="right") - 1, 0, self.n - 1)
            pos = i + (q - e[i]) / (e[i + 1] - e[i])
        if self.descending:
            pos = self.n - pos
        return pos

    def covers_full_period(self) -> bool:
        return (
            self.periodic is not None
            and (self.hi - self.lo) >= self.periodic - 1.5 * self.resolution
        )


@dataclass
class Grid:
    """A rectilinear grid: 1-D x and y axes in a CRS."""

    x_dim: str
    y_dim: str
    x: Axis
    y: Axis
    crs: pyproj.CRS
    _to_grid: pyproj.Transformer | None = field(default=None, repr=False)

    @classmethod
    def from_dataarray(
        cls, ds: xr.Dataset, da: xr.DataArray, x: str, y: str, crs_override: str | None = None
    ) -> Grid:
        if x not in da.coords or y not in da.coords:
            raise GridError(f"{da.name!r} has no 1-D coordinates for {x!r}/{y!r}")
        xc = np.asarray(da.coords[x].values, dtype=np.float64)
        yc = np.asarray(da.coords[y].values, dtype=np.float64)
        crs = detect_crs(ds, da, x, y, crs_override)
        plain = bool(crs.is_geographic) and not crs.is_derived
        periodic = 360.0 if plain else None
        # A single row or column takes the cell size of the other axis.
        width = 1.0
        for this, other in ((xc, yc), (yc, xc)):
            if this.size == 1 and other.size > 1:
                width = float(np.median(np.abs(np.diff(other))))
        return cls(
            x,
            y,
            Axis(xc, periodic=periodic, single_width=width),
            Axis(yc, single_width=width),
            crs,
        )

    @property
    def geographic(self) -> bool:
        """Plain longitude/latitude axes (fast separable path).

        Rotated-pole and other derived geographic CRSs are angular too, but
        their axes are not longitude and latitude: they go through pyproj.
        """
        return bool(self.crs.is_geographic) and not self.crs.is_derived

    @property
    def angular(self) -> bool:
        """Axes in degrees (plain or rotated lon/lat)."""
        return bool(self.crs.is_geographic)

    @property
    def shape(self) -> tuple[int, int]:
        return self.y.n, self.x.n

    @property
    def resolution(self) -> tuple[float, float]:
        """(x, y) median cell size in CRS units."""
        return self.x.resolution, self.y.resolution

    @cached_property
    def transformer_from_lonlat(self) -> pyproj.Transformer:
        return pyproj.Transformer.from_crs(WGS84, self.crs, always_xy=True)

    @cached_property
    def transformer_to_lonlat(self) -> pyproj.Transformer:
        return pyproj.Transformer.from_crs(self.crs, WGS84, always_xy=True)

    def from_lonlat(self, lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Lon/lat (degrees) to grid CRS coordinates."""
        if self.geographic:
            return np.asarray(lon, dtype=np.float64), np.asarray(lat, dtype=np.float64)
        with np.errstate(invalid="ignore"):
            gx, gy = self.transformer_from_lonlat.transform(lon, lat)
        return np.asarray(gx), np.asarray(gy)

    @cached_property
    def bounds_lonlat(self) -> tuple[float, float, float, float]:
        """(west, south, east, north) of the grid in degrees, conservatively large."""
        if self.geographic:
            if self.x.covers_full_period():
                west, east = -180.0, 180.0
            else:
                west = ((self.x.lo + 180.0) % 360.0) - 180.0
                east = west + (self.x.hi - self.x.lo)
            return west, max(self.y.lo, -90.0), east, min(self.y.hi, 90.0)
        # Densify the boundary so curved edges are bounded correctly.
        t = np.linspace(0.0, 1.0, 101)
        xs = np.concatenate(
            [
                self.x.lo + t * (self.x.hi - self.x.lo),
                np.full_like(t, self.x.hi),
                self.x.hi - t * (self.x.hi - self.x.lo),
                np.full_like(t, self.x.lo),
            ]
        )
        ys = np.concatenate(
            [
                np.full_like(t, self.y.lo),
                self.y.lo + t * (self.y.hi - self.y.lo),
                np.full_like(t, self.y.hi),
                self.y.hi - t * (self.y.hi - self.y.lo),
            ]
        )
        lon, lat = self.transformer_to_lonlat.transform(xs, ys)
        ok = np.isfinite(lon) & np.isfinite(lat)
        west, south = float(lon[ok].min()), float(lat[ok].min())
        east, north = float(lon[ok].max()), float(lat[ok].max())
        # A pole inside the grid (polar stereographic, rotated grids) is not on the boundary.
        with np.errstate(invalid="ignore"):
            px, py = self.transformer_from_lonlat.transform([0.0, 0.0], [90.0, -90.0])
        for k, pole in ((0, 90.0), (1, -90.0)):
            if (
                np.isfinite(px[k])
                and self.x.lo <= px[k] <= self.x.hi
                and self.y.lo <= py[k] <= self.y.hi
            ):
                west, east = -180.0, 180.0
                north, south = (pole, south) if pole > 0 else (north, pole)
        return west, south, east, north

    def cell_size_m(self) -> float:
        """Approximate cell size in metres (x direction, at the equator for lat/lon grids)."""
        if self.angular:
            return self.x.resolution * 111_320.0
        unit = self.crs.axis_info[0].unit_conversion_factor if self.crs.axis_info else 1.0
        return self.x.resolution * unit

    def resolution_label(self) -> str:
        """Human label, e.g. '0.25° (~28 km)', '30″ (~0.9 km)' or '5 km'."""
        m = self.cell_size_m()
        approx = (
            f"{m / 1000:.0f} km"
            if m >= 9500
            else f"{m / 1000:.1f} km"
            if m >= 950
            else f"{m:.0f} m"
        )
        if not self.angular:
            return approx
        deg = self.x.resolution
        decimal = any(abs(deg * 10**k - round(deg * 10**k)) < 1e-6 * 10**k for k in range(0, 4))
        arcsec, arcmin = deg * 3600, deg * 60
        if not decimal and arcsec < 59.5 and abs(arcsec - round(arcsec)) < 0.05:
            return f"{round(arcsec)}″ (~{approx})"
        if not decimal and deg < 1 and abs(arcmin * 2 - round(arcmin * 2)) < 1e-2:
            return f"{arcmin:g}′ (~{approx})"
        return f"{deg:.4g}° (~{approx})"
