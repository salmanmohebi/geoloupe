"""Web Mercator (EPSG:3857) XYZ tile geometry.

Tile (z, x, y) follows the usual XYZ scheme: x grows eastwards, y southwards,
and zoom z has 2^z × 2^z tiles. Each tile has `size` × `size` pixels.

Web Mercator is separable: the longitude of a pixel depends only on its
column and the latitude only on its row. For lat/lon source grids this makes
the per-tile resampling a pair of 1-D lookups (fast and exact).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

MAX_LAT = 85.0511287798066  # latitude where Web Mercator becomes square
EARTH_CIRCUMFERENCE_M = 2 * math.pi * 6_378_137.0


class TileError(ValueError):
    """Invalid tile coordinates."""


def _merc_y_to_lat(t: np.ndarray) -> np.ndarray:
    """t in [0, 1] from the top (north) edge of the world to latitude in degrees."""
    return np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * t))))


def lat_to_merc_t(lat: np.ndarray | float) -> np.ndarray:
    """Inverse of _merc_y_to_lat: latitude to [0, 1] from the north edge."""
    lat_r = np.radians(np.clip(lat, -MAX_LAT, MAX_LAT))
    return (1.0 - np.log(np.tan(lat_r) + 1.0 / np.cos(lat_r)) / np.pi) / 2.0


@dataclass(frozen=True)
class Tile:
    z: int
    x: int
    y: int
    size: int = 256

    def __post_init__(self) -> None:
        n = 1 << self.z if 0 <= self.z <= 30 else -1
        if n < 0 or not (0 <= self.x < n and 0 <= self.y < n):
            raise TileError(f"invalid tile {self.z}/{self.x}/{self.y}")

    @property
    def n(self) -> int:
        return 1 << self.z

    def _edges(self, idx: int) -> np.ndarray:
        return (idx + np.arange(self.size + 1) / self.size) / self.n

    def lon_edges(self) -> np.ndarray:
        return self._edges(self.x) * 360.0 - 180.0

    def lat_edges(self) -> np.ndarray:
        """Latitude of the row edges, north to south (size + 1 values)."""
        return _merc_y_to_lat(self._edges(self.y))

    def lon_centers(self) -> np.ndarray:
        e = self.lon_edges()
        return (e[1:] + e[:-1]) / 2

    def lat_centers(self) -> np.ndarray:
        return _merc_y_to_lat((self.y + (np.arange(self.size) + 0.5) / self.size) / self.n)

    def bounds(self) -> tuple[float, float, float, float]:
        """(west, south, east, north) in degrees."""
        lon = self.lon_edges()
        lat = self.lat_edges()
        return float(lon[0]), float(lat[-1]), float(lon[-1]), float(lat[0])

    def pixel_size_deg(self) -> tuple[float, float]:
        """Typical pixel size in degrees (lon, lat at the tile centre)."""
        dlon = 360.0 / self.n / self.size
        lat = self.lat_edges()
        dlat = abs(float(lat[0] - lat[-1])) / self.size
        return dlon, dlat

    def pixel_size_m(self) -> float:
        """Ground size of a pixel at the tile's central latitude, in metres."""
        _, s, _, n = self.bounds()
        lat = math.radians((s + n) / 2)
        return EARTH_CIRCUMFERENCE_M * math.cos(lat) / self.n / self.size

    def lonlat_grid(self) -> tuple[np.ndarray, np.ndarray]:
        """2-D (size, size) arrays of pixel-centre lon and lat."""
        lon, lat = np.meshgrid(self.lon_centers(), self.lat_centers())
        return lon, lat


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 1 << z
    x = int(np.floor((lon + 180.0) / 360.0 * n)) % n
    y = int(np.clip(np.floor(float(lat_to_merc_t(lat)) * n), 0, n - 1))
    return x, y


def zoom_for_resolution(cell_size_m: float, tile_size: int = 256) -> float:
    """Zoom level at which one pixel at the equator equals `cell_size_m`."""
    return math.log2(EARTH_CIRCUMFERENCE_M / (tile_size * cell_size_m))
