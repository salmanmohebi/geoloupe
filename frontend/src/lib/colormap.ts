// Colormaps: 256-entry RGB lookup tables shared with the server (colormaps.json).

import type { Colormap } from "../api/types";

export type Lut = Uint8Array; // 256 × 3

const lutCache = new Map<string, Lut>();

export function parseLut(hex: string): Lut {
  const out = new Uint8Array(768);
  for (let i = 0; i < 768; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}

/** LUT by name; a "_r" suffix reverses any colormap. Unknown names fall back to viridis. */
export function getLut(table: Record<string, Colormap>, name: string): Lut {
  const hit = lutCache.get(name);
  if (hit) return hit;
  let base = name;
  let reverse = false;
  if (!(name in table) && name.endsWith("_r") && name.slice(0, -2) in table) {
    base = name.slice(0, -2);
    reverse = true;
  }
  const entry = table[base] ?? table["viridis"];
  if (!entry) return new Uint8Array(768);
  let lut = parseLut(entry.lut);
  if (reverse) {
    const r = new Uint8Array(768);
    for (let i = 0; i < 256; i++) r.set(lut.subarray((255 - i) * 3, (255 - i) * 3 + 3), i * 3);
    lut = r;
  }
  lutCache.set(name, lut);
  return lut;
}

export interface ColorScale {
  lut: Lut;
  vmin: number;
  vmax: number;
  log: boolean;
}

/** Normalised position in [0, 1] of a value on the scale (unclamped; NaN for invalid). */
export function scalePosition(v: number, s: Pick<ColorScale, "vmin" | "vmax" | "log">): number {
  if (s.log) {
    if (v <= 0 || s.vmin <= 0) return NaN;
    return (Math.log(v) - Math.log(s.vmin)) / (Math.log(s.vmax) - Math.log(s.vmin));
  }
  return (v - s.vmin) / (s.vmax - s.vmin);
}

/** Map float values to RGBA; NaN (and a hidden mask) become transparent. */
export function colorize(values: Float32Array, s: ColorScale, out?: Uint8ClampedArray): Uint8ClampedArray {
  const n = values.length;
  const rgba = out ?? new Uint8ClampedArray(n * 4);
  const { lut } = s;
  if (s.log) {
    const l0 = Math.log(Math.max(s.vmin, 1e-30));
    const k = 255 / (Math.log(Math.max(s.vmax, 1e-30)) - l0);
    for (let i = 0; i < n; i++) {
      const v = values[i];
      const j = i * 4;
      if (!(v > 0)) {
        rgba[j + 3] = 0;
        continue;
      }
      let t = (Math.log(v) - l0) * k + 0.5;
      t = t < 0 ? 0 : t > 255 ? 255 : t;
      const c = (t | 0) * 3;
      rgba[j] = lut[c];
      rgba[j + 1] = lut[c + 1];
      rgba[j + 2] = lut[c + 2];
      rgba[j + 3] = 255;
    }
    return rgba;
  }
  const k = 255 / (s.vmax - s.vmin);
  for (let i = 0; i < n; i++) {
    const v = values[i];
    const j = i * 4;
    if (v !== v) {
      rgba[j + 3] = 0; // NaN
      continue;
    }
    let t = (v - s.vmin) * k + 0.5;
    t = t < 0 ? 0 : t > 255 ? 255 : t;
    const c = (t | 0) * 3;
    rgba[j] = lut[c];
    rgba[j + 1] = lut[c + 1];
    rgba[j + 2] = lut[c + 2];
    rgba[j + 3] = 255;
  }
  return rgba;
}

export function lutColor(lut: Lut, t: number): string {
  const i = Math.max(0, Math.min(255, Math.round(t * 255))) * 3;
  return `rgb(${lut[i]},${lut[i + 1]},${lut[i + 2]})`;
}

/** CSS linear-gradient for a colorbar. */
export function cssGradient(lut: Lut, direction = "to right", stops = 24): string {
  const parts: string[] = [];
  for (let i = 0; i < stops; i++) {
    const t = i / (stops - 1);
    parts.push(`${lutColor(lut, t)} ${(t * 100).toFixed(1)}%`);
  }
  return `linear-gradient(${direction}, ${parts.join(", ")})`;
}

/** "Nice" rounded bounds for a range, e.g. (251.3, 307.9) → (250, 310). */
export function niceRange(lo: number, hi: number): [number, number] {
  if (!isFinite(lo) || !isFinite(hi)) return [0, 1];
  if (hi <= lo) return [lo - 0.5, lo + 0.5];
  const span = hi - lo;
  const step = Math.pow(10, Math.floor(Math.log10(span))) / 2;
  return [Math.floor(lo / step) * step, Math.ceil(hi / step) * step];
}

/** RGB composite: every band stretched from vmin..vmax to 0..255 with a display gamma. */
export function colorizeRgb(
  bands: Float32Array[],
  vmin: number,
  vmax: number,
  gamma = 1,
  out?: Uint8ClampedArray,
): Uint8ClampedArray {
  const n = bands[0].length;
  const rgba = out ?? new Uint8ClampedArray(n * 4);
  const k = 1 / (vmax - vmin);
  const g = 1 / gamma;
  // lookup of the gamma curve, 1024 steps
  const curve = new Float32Array(1025);
  for (let i = 0; i <= 1024; i++) curve[i] = Math.pow(i / 1024, g) * 255;
  for (let i = 0; i < n; i++) {
    const j = i * 4;
    let ok = true;
    for (let c = 0; c < 3; c++) {
      const v = bands[c][i];
      if (v !== v) {
        ok = false;
        break;
      }
      let t = (v - vmin) * k;
      t = t < 0 ? 0 : t > 1 ? 1 : t;
      rgba[j + c] = curve[(t * 1024) | 0];
    }
    rgba[j + 3] = ok ? 255 : 0;
  }
  return rgba;
}
