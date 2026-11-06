// Categorical data (land cover, masks): an exact value → colour table, not a colour scale.

import { lutColor, type Lut } from "./colormap";

export interface ClassDef {
  value: number;
  name: string;
  color: string | null;
}

export interface ClassTable {
  rgba: Uint8Array; // 4 bytes per value 0..max (alpha 0 = not a class)
  max: number;
  byValue: Map<number, { name: string; color: string }>;
}

function hexToRgb(hex: string): [number, number, number] {
  const h = hex.replace("#", "");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

/** Colour table of the classes; classes without a colour take evenly spaced colours of `fallback`. */
export function classTable(classes: ClassDef[], fallback: Lut): ClassTable {
  const max = Math.max(0, ...classes.map((c) => c.value));
  const rgba = new Uint8Array((max + 1) * 4);
  const byValue = new Map<number, { name: string; color: string }>();
  classes.forEach((c, i) => {
    const color = c.color ?? lutColor(fallback, classes.length > 1 ? i / (classes.length - 1) : 0.5);
    const [r, g, b] = c.color
      ? hexToRgb(c.color)
      : (color.match(/\d+/g)!.map(Number) as [number, number, number]);
    if (c.value >= 0) rgba.set([r, g, b, 255], c.value * 4);
    byValue.set(c.value, { name: c.name, color: c.color ?? color });
  });
  return { rgba, max, byValue };
}

/** Colour class codes; values that are not classes (or NaN) are transparent. */
export function colorizeClasses(
  values: Float32Array,
  t: ClassTable,
  out?: Uint8ClampedArray,
): Uint8ClampedArray {
  const rgba = out ?? new Uint8ClampedArray(values.length * 4);
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    const j = i * 4;
    if (!(v >= 0 && v <= t.max)) {
      rgba[j + 3] = 0;
      continue;
    }
    const k = Math.round(v) * 4;
    rgba[j] = t.rgba[k];
    rgba[j + 1] = t.rgba[k + 1];
    rgba[j + 2] = t.rgba[k + 2];
    rgba[j + 3] = t.rgba[k + 3];
  }
  return rgba;
}

/** Share of each class among the values (e.g. those on screen). */
export function classShares(sample: Float32Array, t: ClassTable): Map<number, number> {
  const counts = new Map<number, number>();
  let n = 0;
  for (let i = 0; i < sample.length; i++) {
    const v = Math.round(sample[i]);
    if (t.byValue.has(v)) {
      counts.set(v, (counts.get(v) ?? 0) + 1);
      n++;
    }
  }
  for (const [k, c] of counts) counts.set(k, c / Math.max(1, n));
  return counts;
}
