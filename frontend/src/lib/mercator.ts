// Web Mercator tile math (XYZ scheme), matching src/terrascope/tiling.py.

export const MAX_LAT = 85.0511287798066;

export interface TileCoord {
  z: number;
  x: number;
  y: number;
}

/** Fractional tile coordinates of a location at zoom z. */
export function lngLatToTileFloat(lng: number, lat: number, z: number): [number, number] {
  const n = 2 ** z;
  const x = ((((lng + 180) / 360) % 1) + 1) % 1;
  const la = Math.max(-MAX_LAT, Math.min(MAX_LAT, lat));
  const r = (la * Math.PI) / 180;
  const y = (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2;
  return [x * n, y * n];
}

/** Tile and pixel (column, row) under a location. */
export function locate(
  lng: number,
  lat: number,
  z: number,
  size: number,
): { tile: TileCoord; px: number; py: number } {
  const [fx, fy] = lngLatToTileFloat(lng, lat, z);
  const n = 2 ** z;
  const x = Math.min(n - 1, Math.floor(fx));
  const y = Math.min(n - 1, Math.max(0, Math.floor(fy)));
  return {
    tile: { z, x, y },
    px: Math.min(size - 1, Math.floor((fx - x) * size)),
    py: Math.min(size - 1, Math.max(0, Math.floor((fy - y) * size))),
  };
}

/** Tile zoom MapLibre requests for a 256-px raster source at map zoom `mapZoom`. */
export function tileZoom(mapZoom: number, tileSize: number, maxzoom: number): number {
  return Math.max(0, Math.min(maxzoom, Math.round(mapZoom + Math.log2(512 / tileSize))));
}

/** All tiles at zoom z covering a lon/lat box (west may exceed east across the antimeridian). */
export function tilesInBounds(
  west: number,
  south: number,
  east: number,
  north: number,
  z: number,
): TileCoord[] {
  const n = 2 ** z;
  const [x0f, y0f] = lngLatToTileFloat(west, north, z);
  const [x1f, y1f] = lngLatToTileFloat(east, south, z);
  const y0 = Math.max(0, Math.floor(y0f));
  const y1 = Math.min(n - 1, Math.floor(y1f - 1e-9));
  const span = east - west >= 360 ? n : (Math.floor(x1f - 1e-9) - Math.floor(x0f) + n) % n;
  const out: TileCoord[] = [];
  for (let k = 0; k <= Math.min(span, n - 1); k++)
    for (let y = y0; y <= y1; y++) out.push({ z, x: (Math.floor(x0f) + k) % n, y });
  return out;
}
