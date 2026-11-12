// MapLibre custom protocol "geoloupe://<spec>/<z>/<x>/<y>".
//
// MapLibre handles what it is good at — which tiles are visible, level of
// detail, caching, fading, globe projection — and asks this handler for each
// tile image. The handler fetches the float data tile (or two, for error
// maps), colours it with the spec's colour scale and returns an ImageBitmap.
// A "spec" is an immutable description of what to draw; any change (time,
// colormap, range) creates a new spec id, which makes MapLibre request fresh
// images while the float data comes from the in-memory cache.

import * as maplibregl from "maplibre-gl";
// MapLibre v6 is ESM-only and loads its worker from next to its own module;
// once bundled, the worker file must be emitted and its URL set explicitly.
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import { api } from "../api/client";
import { colorizeClasses, type ClassTable } from "./classes";
import { colorize, colorizeRgb, type ColorScale } from "./colormap";
import type { DataTile } from "./datatile";
import type { PixelFn } from "./formula";
import { cachedTile, fetchTile } from "./tiles";

export interface DataRef {
  layerId: string;
  time: string | null;
  dims: Record<string, string>;
}

/** An error map: fn(P, T) per pixel, P = the data (in T's units: × scale + offset), T = ref. */
export interface ErrorRender {
  fn: PixelFn;
  scale: number;
  offset: number;
  key: string; // identity of fn + conversion (for render keys)
}

export interface RenderSpec {
  data: DataRef;
  ref: DataRef | null; // error maps: the reference T
  error: ErrorRender | null;
  scale: ColorScale;
  tileSize: number;
  rgb?: { gamma: number } | null; // RGB layer: three bands, stretched, no colormap
  classes?: ClassTable | null; // categorical data: exact colour per class value
}

const specs = new Map<string, RenderSpec>();
let counter = 0;
let registered = false;

export function registerSpec(spec: RenderSpec): string {
  const id = `s${++counter}`;
  specs.set(id, spec);
  while (specs.size > 32) specs.delete(specs.keys().next().value as string);
  return id;
}

export function getSpec(id: string): RenderSpec | undefined {
  return specs.get(id);
}

export const dataUrl = (ref: DataRef, z: number, x: number, y: number) =>
  api.tileUrl(ref.layerId, z, x, y, ref.time, ref.dims);

/** Values shown on one tile: the data, or the error of the data (P) against the reference (T). */
export function combine(
  a: DataTile,
  b: DataTile | null | undefined,
  error: ErrorRender | null = null,
): Float32Array | null {
  if (!b || !error) return a.values;
  if (!a.values || !b.values) return null;
  const { fn, scale, offset } = error;
  const p = a.values;
  const t = b.values;
  const out = new Float32Array(p.length);
  for (let i = 0; i < out.length; i++) out[i] = fn(p[i] * scale + offset, t[i]);
  return out;
}

/** Shown values of a tile if both parts are cached (for hover and statistics). */
export function cachedValues(
  spec: RenderSpec,
  z: number,
  x: number,
  y: number,
): { tile: DataTile; values: Float32Array | null } | null {
  const a = cachedTile(dataUrl(spec.data, z, x, y));
  if (!a) return null;
  if (!spec.ref) return { tile: a, values: a.values };
  const b = cachedTile(dataUrl(spec.ref, z, x, y));
  if (!b) return null;
  return { tile: a, values: combine(a, b, spec.error) };
}

let blank: Promise<ImageBitmap> | null = null;
function blankBitmap(): Promise<ImageBitmap> {
  blank ??= createImageBitmap(new ImageData(1, 1));
  return blank;
}

export function registerProtocol(): void {
  if (registered) return;
  registered = true;
  maplibregl.setWorkerUrl(workerUrl);
  maplibregl.addProtocol("geoloupe", async (params, abort) => {
    const m = /^geoloupe:\/\/([^/]+)\/(\d+)\/(\d+)\/(\d+)/.exec(params.url);
    const spec = m ? specs.get(m[1]) : undefined;
    if (!m || !spec) return { data: await blankBitmap() };
    const [z, x, y] = [Number(m[2]), Number(m[3]), Number(m[4])];
    const size = spec.tileSize;
    const [a, b] = await Promise.all([
      fetchTile(dataUrl(spec.data, z, x, y), size, abort.signal),
      spec.ref ? fetchTile(dataUrl(spec.ref, z, x, y), size, abort.signal) : Promise.resolve(null),
    ]);
    if (spec.rgb && a.bands) {
      const rgba = colorizeRgb(a.bands, spec.scale.vmin, spec.scale.vmax, spec.rgb.gamma);
      return {
        data: await createImageBitmap(new ImageData(rgba as Uint8ClampedArray<ArrayBuffer>, size, size)),
      };
    }
    const values = combine(a, b, spec.error);
    if (!values) return { data: await blankBitmap() };
    const rgba = spec.classes ? colorizeClasses(values, spec.classes) : colorize(values, spec.scale);
    return {
      data: await createImageBitmap(new ImageData(rgba as Uint8ClampedArray<ArrayBuffer>, size, size)),
    };
  });
}
