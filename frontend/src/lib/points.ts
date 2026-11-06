// Point observations (stations): fetching, and MapLibre styling in the layer's colour scale.

import type { FeatureCollection } from "geojson";
import type { ExpressionSpecification } from "maplibre-gl";
import { apiUrl } from "../api/client";
import { track } from "./activity";
import { lutColor, type ColorScale } from "./colormap";

export interface PointFeature {
  id: string;
  name: string | null;
  lon: number;
  lat: number;
  elevation: number | null;
  value: number;
}

export interface PointsResult {
  source: string;
  title: string;
  variable: string;
  time: string;
  count: number;
  truncated: boolean;
  features: PointFeature[];
}

export interface StationSeries {
  source: string;
  station: string;
  name: string | null;
  lon: number;
  lat: number;
  elevation: number | null;
  times: string[];
  values: (number | null)[];
}

const enc = encodeURIComponent;

async function get<T>(url: string, signal?: AbortSignal): Promise<T> {
  const r = await track(fetch(url, { signal }));
  if (!r.ok) {
    let detail = r.statusText;
    try {
      detail = (await r.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return (await r.json()) as T;
}

export function fetchPoints(layerId: string, time: string, signal?: AbortSignal): Promise<PointsResult> {
  return get<PointsResult>(apiUrl(`layers/${enc(layerId)}/points`, { time }), signal);
}

export function fetchStationSeries(
  source: string,
  station: string,
  variable: string,
  start: string,
  end: string,
  signal?: AbortSignal,
): Promise<StationSeries> {
  return get<StationSeries>(
    apiUrl(`points/${enc(source)}/series`, { station, variable, start, end }),
    signal,
  );
}

/** GeoJSON for a MapLibre source. */
export function toGeoJSON(p: PointsResult | null): FeatureCollection {
  return {
    type: "FeatureCollection",
    features: (p?.features ?? []).map((f) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [f.lon, f.lat] },
      properties: { id: f.id, name: f.name ?? f.id, value: f.value, elevation: f.elevation },
    })),
  };
}

/** circle-color expression that reproduces the colour scale (32 stops; log scales included). */
export function colorExpression(scale: ColorScale, stops = 32): ExpressionSpecification {
  const { vmin, vmax, log } = scale;
  const pairs: (number | string)[] = [];
  for (let i = 0; i < stops; i++) {
    const t = i / (stops - 1);
    const v = log && vmin > 0 ? vmin * Math.pow(vmax / vmin, t) : vmin + t * (vmax - vmin);
    pairs.push(v, lutColor(scale.lut, t));
  }
  return ["interpolate", ["linear"], ["get", "value"], ...pairs] as unknown as ExpressionSpecification;
}

/** Values of the stations on screen, for histograms and "fit to view". */
export function pointsSample(p: PointsResult | null): Float32Array {
  return Float32Array.from((p?.features ?? []).map((f) => f.value).filter((v) => Number.isFinite(v)));
}

/** Circle radius by zoom: sparse networks get bigger dots, so a few stations stay visible. */
export function stationRadius(n: number): (string | number | string[])[] {
  const k = n < 100 ? 2.2 : n < 1000 ? 1.5 : 1;
  return ["interpolate", ["linear"], ["zoom"], 2, 2.5 * k, 6, 4.5 * k, 10, 7 * Math.min(k, 1.5)];
}
