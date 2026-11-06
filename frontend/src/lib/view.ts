// What is on screen: values under the cursor and the provenance of the view,
// both computed from the cached data tiles (no extra requests).

import type { Map as MlMap } from "maplibre-gl";
import type { LayerMeta } from "../api/types";
import { EMPTY_SOURCE } from "./datatile";
import { locate, tileZoom, tilesInBounds } from "./mercator";
import { cachedValues, type RenderSpec } from "./protocol";

export interface HoverValue {
  value: number | null;
  bands?: (number | null)[]; // RGB layers
  source: number | null; // index into layer.sources
  level: number | null;
  approximate: boolean; // taken from a coarser (parent) tile
}

/** Value displayed at a location (at the tile zoom MapLibre uses, or a cached parent). */
export function valueAt(
  spec: RenderSpec,
  layer: LayerMeta,
  mapZoom: number,
  lng: number,
  lat: number,
): HoverValue | null {
  const z0 = tileZoom(mapZoom, spec.tileSize, layer.maxzoom);
  for (let z = z0; z >= Math.max(0, z0 - 4); z--) {
    const { tile, px, py } = locate(lng, lat, z, spec.tileSize);
    const hit = cachedValues(spec, z, tile.x, tile.y);
    if (!hit) continue;
    const i = py * spec.tileSize + px;
    const v = hit.values ? hit.values[i] : NaN;
    const s = hit.tile.sourceMap ? hit.tile.sourceMap[i] : null;
    const source = s === null || s === EMPTY_SOURCE || !isFinite(v) ? null : s;
    const level =
      source === null ? null : (hit.tile.contributions.find((c) => c.source === source)?.level ?? null);
    const bands = hit.tile.bands?.map((b) => (Number.isFinite(b[i]) ? b[i] : null));
    return { value: isFinite(v) ? v : null, source, level, approximate: z !== z0, bands };
  }
  return null;
}

export interface ViewPart {
  source: number;
  level: number;
  fraction: number;
}

export interface ViewSummary {
  mapZoom: number;
  tileZoom: number;
  parts: ViewPart[];
  skipped: { source: number; reason: string }[];
  tilesLoaded: number;
  tilesTotal: number;
  sample: Float32Array; // values on screen (subsampled), for histograms and Fit
}

export function summarize(map: MlMap, spec: RenderSpec, layer: LayerMeta, maxSample = 60_000): ViewSummary {
  const mapZoom = map.getZoom();
  const z = tileZoom(mapZoom, spec.tileSize, layer.maxzoom);
  const b = map.getBounds();
  let west = b.getWest();
  let east = b.getEast();
  if (east - west >= 360) [west, east] = [-180, 180];
  const tiles = tilesInBounds(west, Math.max(b.getSouth(), -85), east, Math.min(b.getNorth(), 85), z).slice(
    0,
    96,
  );
  const pix = new Map<string, number>();
  const skipped = new Map<number, string>();
  let total = 0;
  let loaded = 0;
  const chunks: Float32Array[] = [];
  const n = spec.tileSize * spec.tileSize;
  const stride = Math.max(1, Math.floor((tiles.length * n) / maxSample));
  for (const t of tiles) {
    const hit = cachedValues(spec, t.z, t.x, t.y);
    if (!hit) continue;
    loaded++;
    for (const c of hit.tile.contributions) {
      const key = `${c.source}:${c.level}`;
      pix.set(key, (pix.get(key) ?? 0) + c.pixels);
      total += c.pixels;
    }
    for (const s of hit.tile.skipped) skipped.set(s.source, s.reason);
    if (hit.values) {
      const out = new Float32Array(Math.ceil(n / stride));
      let k = 0;
      for (let i = 0; i < n; i += stride) {
        const v = hit.values[i];
        if (v === v) out[k++] = v;
      }
      chunks.push(out.subarray(0, k));
    }
  }
  const len = chunks.reduce((a, c) => a + c.length, 0);
  const sample = new Float32Array(len);
  let o = 0;
  for (const c of chunks) {
    sample.set(c, o);
    o += c.length;
  }
  const parts = [...pix.entries()]
    .map(([k, v]) => {
      const [source, level] = k.split(":").map(Number);
      return { source, level, fraction: total ? v / total : 0 };
    })
    .sort((a, b) => a.source - b.source || a.level - b.level);
  // A skipped source that still contributes elsewhere on screen is not "hidden".
  const used = new Set(parts.map((p) => p.source));
  return {
    mapZoom,
    tileZoom: z,
    parts,
    skipped: [...skipped.entries()]
      .filter(([s]) => !used.has(s))
      .map(([source, reason]) => ({ source, reason })),
    tilesLoaded: loaded,
    tilesTotal: tiles.length,
    sample,
  };
}

/** Percentile of an unsorted sample (copies and sorts). */
export function percentiles(sample: Float32Array, ps: number[]): number[] {
  if (!sample.length) return ps.map(() => NaN);
  const s = Float32Array.from(sample).sort();
  return ps.map((p) => s[Math.min(s.length - 1, Math.max(0, Math.round((p / 100) * (s.length - 1))))]);
}

export function histogram(sample: Float32Array, lo: number, hi: number, bins: number): number[] {
  const out = new Array(bins).fill(0);
  if (!(hi > lo)) return out;
  const k = bins / (hi - lo);
  for (let i = 0; i < sample.length; i++) {
    const b = Math.floor((sample[i] - lo) * k);
    if (b >= 0 && b < bins) out[b]++;
  }
  return out;
}
