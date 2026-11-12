// HTTP client. All URLs are relative to the page, so the app works under any
// path prefix (for example behind jupyter-server-proxy).

import { track } from "../lib/activity";
import type {
  AddDataResult,
  BrowseResult,
  S3Access,
  Catalog,
  Colormap,
  ErrorStats,
  LayerMeta,
  PointResult,
  Stats,
  TimeSeriesResult,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

export function apiUrl(
  path: string,
  params: Record<string, string | number | undefined | null> = {},
): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params))
    if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  const qs = q.toString();
  return `api/${path}${qs ? `?${qs}` : ""}`;
}

function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  return track(getJsonUntracked<T>(url, signal));
}

async function getJsonUntracked<T>(url: string, signal?: AbortSignal, init?: RequestInit): Promise<T> {
  let r: Response;
  try {
    r = await fetch(url, { signal, ...init });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    throw new ApiError("The GeoLoupe server cannot be reached. Is it still running?", 0);
  }
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const body = await r.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail || `HTTP ${r.status}`, r.status);
  }
  return (await r.json()) as T;
}

export const dimsParam = (dims: Record<string, string>): string | undefined => {
  const parts = Object.entries(dims).map(([k, v]) => `${k}:${v}`);
  return parts.length ? parts.join(",") : undefined;
};

const enc = encodeURIComponent;

export const api = {
  catalog: () => getJson<Catalog>(apiUrl("catalog")),
  colormaps: () => getJson<Record<string, Colormap>>(apiUrl("colormaps")),
  layer: (id: string) => getJson<LayerMeta>(apiUrl(`layers/${enc(id)}`)),
  stats: (id: string, time: string | null, dims: Record<string, string>, bbox?: number[]) =>
    getJson<Stats>(
      apiUrl(`layers/${enc(id)}/stats`, {
        time: time ?? undefined,
        dims: dimsParam(dims),
        bbox: bbox?.map((v) => v.toFixed(4)).join(","),
      }),
    ),
  point: (
    id: string,
    lon: number,
    lat: number,
    time: string | null,
    dims: Record<string, string>,
    signal?: AbortSignal,
  ) =>
    getJson<PointResult>(
      apiUrl(`layers/${enc(id)}/point`, { lon, lat, time: time ?? undefined, dims: dimsParam(dims) }),
      signal,
    ),
  timeseries: (
    id: string,
    lon: number,
    lat: number,
    start: string | undefined,
    end: string | undefined,
    dims: Record<string, string>,
    signal?: AbortSignal,
  ) =>
    getJson<TimeSeriesResult>(
      apiUrl(`layers/${enc(id)}/timeseries`, { lon, lat, start, end, dims: dimsParam(dims) }),
      signal,
    ),
  /** Exact error tile (server, from native cells) of an error pane; `q` from paneDraw.errorQuery. */
  errorTileUrl: (q: Record<string, string>, z: number, x: number, y: number) =>
    apiUrl(`error/tiles/${z}/${x}/${y}.bin`, q),
  errorStats: (q: Record<string, string>, bbox: number[], z: number, signal?: AbortSignal) =>
    getJson<ErrorStats>(
      apiUrl("error/stats", { ...q, bbox: bbox.map((v) => v.toFixed(4)).join(","), z: String(z) }),
      signal,
    ),
  tileUrl: (id: string, z: number, x: number, y: number, time: string | null, dims: Record<string, string>) =>
    apiUrl(`layers/${enc(id)}/tiles/${z}/${x}/${y}.bin`, { time: time ?? undefined, dims: dimsParam(dims) }),
  exportUrl: (sourceId: string, bbox: number[], time: string | null, dims: Record<string, string>) =>
    apiUrl(`sources/${enc(sourceId)}/export.nc`, {
      bbox: bbox.map((v) => v.toFixed(4)).join(","),
      time: time ?? undefined,
      dims: dimsParam(dims),
    }),
  /** Open a file or URL on the server ("Add data"; only when the server allows it). */
  addData: (path: string, name?: string, access: S3Access = {}) =>
    track(
      getJsonUntracked<AddDataResult>(apiUrl("datasets"), undefined, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, name: name || undefined, ...access }),
      }),
    ),
  browse: (url: string, access: S3Access, match?: string, signal?: AbortSignal) =>
    getJson<BrowseResult>(
      apiUrl("browse", {
        url,
        endpoint: access.endpoint,
        profile: access.profile,
        anon: access.anon === undefined ? undefined : String(access.anon),
        match,
      }),
      signal,
    ),
  s3Profiles: () => getJsonUntracked<{ profiles: string[] }>(apiUrl("s3/profiles")),
};
