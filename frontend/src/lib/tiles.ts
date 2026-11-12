// Data tile fetching with an LRU cache and request de-duplication.
//
// Tiles are cached as decoded floats, so a colormap or range change recolours
// instantly without any network request, and hover values are read from the
// same arrays the map shows.

import { track } from "./activity";
import { decodeValues, parseContributions, parseSkipped, type DataTile } from "./datatile";

const MAX_TILES = 400; // ≈ 400 × 320 KB ≈ 130 MB at most

const cache = new Map<string, DataTile>();
const inflight = new Map<string, Promise<DataTile>>();
export { onActivity } from "./activity";

export class TileFetchError extends Error {}

export function cachedTile(url: string): DataTile | undefined {
  const t = cache.get(url);
  if (t !== undefined) {
    cache.delete(url); // refresh LRU position
    cache.set(url, t);
  }
  return t;
}

export async function fetchTile(url: string, size: number, signal?: AbortSignal): Promise<DataTile> {
  const hit = cachedTile(url);
  if (hit !== undefined) return hit;
  let p = inflight.get(url);
  if (!p) {
    p = track(
      (async () => {
        const r = await fetch(url);
        const meta = {
          contributions: parseContributions(r.headers.get("X-GeoLoupe-Sources")),
          skipped: parseSkipped(r.headers.get("X-GeoLoupe-Skipped")),
          ms: Number(r.headers.get("X-GeoLoupe-Time-Ms") ?? 0),
          ...(r.headers.has("X-GeoLoupe-Exact")
            ? {
                exact: r.headers.get("X-GeoLoupe-Exact") === "1",
                cellM: Number(r.headers.get("X-GeoLoupe-Cell-M") ?? NaN),
              }
            : {}),
        };
        if (r.status === 204) return { values: null, sourceMap: null, ...meta };
        if (!r.ok) {
          let detail = r.statusText;
          try {
            detail = (await r.json()).detail ?? detail;
          } catch {
            /* ignore */
          }
          throw new TileFetchError(`tile ${r.status}: ${detail}`);
        }
        const buf = await r.arrayBuffer();
        const nBands = Number(r.headers.get("X-GeoLoupe-Bands") ?? 1);
        const { values, sourceMap, bands } = decodeValues(buf, size, nBands);
        return { values, sourceMap, bands, ...meta };
      })(),
    );
    inflight.set(url, p);
    p.then(
      (t) => {
        cache.set(url, t);
        while (cache.size > MAX_TILES) cache.delete(cache.keys().next().value as string);
      },
      () => undefined,
    ).finally(() => inflight.delete(url));
  }
  if (!signal) return p;
  // Abort only stops waiting; the shared request finishes and fills the cache.
  return new Promise((resolve, reject) => {
    const onAbort = () => reject(new DOMException("aborted", "AbortError"));
    signal.addEventListener("abort", onAbort, { once: true });
    p!.then(resolve, reject).finally(() => signal.removeEventListener("abort", onAbort));
  });
}

export function clearTileCache(): void {
  cache.clear();
}
