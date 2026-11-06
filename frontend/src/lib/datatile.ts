// Decoding of TerraScope data tiles (docs/data-tiles.md).
//
// Body (after HTTP gzip, which the browser removes): size² float32 values,
// byte-shuffled, then optionally size² uint8 source indices (255 = none).
// Headers: X-TerraScope-Sources "src:level:pixels,..." and
// X-TerraScope-Skipped "src:reason,...".

export interface Contribution {
  source: number;
  level: number;
  pixels: number;
}

export interface DataTile {
  values: Float32Array | null; // null = empty tile (no source has data here); RGB: the red band
  sourceMap: Uint8Array | null;
  bands?: Float32Array[] | null; // RGB layers: red, green, blue
  contributions: Contribution[];
  skipped: { source: number; reason: string }[];
  ms: number; // server time
}

export const EMPTY_SOURCE = 255;

export function decodeValues(
  buf: ArrayBuffer,
  size: number,
  nBands = 1,
): { values: Float32Array; sourceMap: Uint8Array | null; bands: Float32Array[] | null } {
  const n = size * size;
  const bytes = new Uint8Array(buf);
  if (bytes.length < 4 * n * nBands)
    throw new Error(`data tile too short: ${bytes.length} bytes for ${size}²`);
  const planes: Float32Array[] = [];
  for (let p = 0; p < nBands; p++) {
    const base = p * 4 * n;
    const out = new Uint8Array(4 * n);
    // un-shuffle: out[4i + b] = bytes[base + b·n + i]
    for (let b = 0; b < 4; b++) {
      const off = base + b * n;
      for (let i = 0; i < n; i++) out[4 * i + b] = bytes[off + i];
    }
    planes.push(new Float32Array(out.buffer));
  }
  const sourceMap = nBands === 1 && bytes.length >= 5 * n ? bytes.slice(4 * n, 5 * n) : null;
  return { values: planes[0], sourceMap, bands: nBands > 1 ? planes : null };
}

export function parseContributions(h: string | null): Contribution[] {
  if (!h) return [];
  return h
    .split(",")
    .filter(Boolean)
    .map((p) => {
      const [s, l, n] = p.split(":").map(Number);
      return { source: s, level: l, pixels: n };
    });
}

export function parseSkipped(h: string | null): { source: number; reason: string }[] {
  if (!h) return [];
  return h
    .split(",")
    .filter(Boolean)
    .map((p) => {
      const [s, reason] = p.split(":");
      return { source: Number(s), reason };
    });
}
