import { describe, expect, it } from "vitest";
import { niceTicks } from "../components/Colorbar";
import { mergeSeries } from "../components/TimeSeriesChart";
import { colorize, getLut, niceRange, scalePosition } from "./colormap";
import { decodeValues, parseContributions, parseSkipped } from "./datatile";
import { locate, lngLatToTileFloat, tileZoom, tilesInBounds } from "./mercator";
import { buildHash, parseHash } from "./urlState";
import { histogram, percentiles } from "./view";

/** Same byte layout as terrascope.engine.encode_data_tile. */
function encode(values: Float32Array, sourceMap?: Uint8Array): ArrayBuffer {
  const n = values.length;
  const raw = new Uint8Array(values.buffer);
  const out = new Uint8Array(4 * n + (sourceMap ? n : 0));
  for (let i = 0; i < n; i++) for (let b = 0; b < 4; b++) out[b * n + i] = raw[4 * i + b];
  if (sourceMap) out.set(sourceMap, 4 * n);
  return out.buffer;
}

describe("data tiles", () => {
  it("round-trips byte-shuffled float32 with a source map", () => {
    const v = new Float32Array([1.5, -2.25, NaN, 300.125]);
    const s = new Uint8Array([0, 1, 255, 0]);
    const d = decodeValues(encode(v, s), 2);
    expect(Array.from(d.values.slice(0, 2))).toEqual([1.5, -2.25]);
    expect(Number.isNaN(d.values[2])).toBe(true);
    expect(d.values[3]).toBe(300.125);
    expect(Array.from(d.sourceMap!)).toEqual([0, 1, 255, 0]);
  });
  it("works without a source map and rejects short bodies", () => {
    expect(decodeValues(encode(new Float32Array(4)), 2).sourceMap).toBeNull();
    expect(() => decodeValues(new ArrayBuffer(3), 2)).toThrow();
  });
  it("parses provenance headers", () => {
    expect(parseContributions("0:2:100,1:0:5")).toEqual([
      { source: 0, level: 2, pixels: 100 },
      { source: 1, level: 0, pixels: 5 },
    ]);
    expect(parseSkipped("0:cost")).toEqual([{ source: 0, reason: "cost" }]);
    expect(parseContributions("")).toEqual([]);
  });
});

describe("colormaps", () => {
  const table = {
    ramp: {
      category: "sequential" as const,
      provider: "test",
      lut: Array.from({ length: 256 }, (_, i) =>
        [i, 0, 255 - i].map((c) => c.toString(16).padStart(2, "0")).join(""),
      ).join(""),
    },
  };
  it("colours linearly, clamps, and makes NaN transparent", () => {
    const lut = getLut(table, "ramp");
    const rgba = colorize(new Float32Array([0, 5, 10, 20, NaN]), { lut, vmin: 0, vmax: 10, log: false });
    expect(Array.from(rgba.slice(0, 4))).toEqual([0, 0, 255, 255]);
    expect(rgba[4]).toBe(128); // 5 → index 128 (rounded)
    expect(rgba[8]).toBe(255);
    expect(rgba[12]).toBe(255); // clamped
    expect(rgba[19]).toBe(0); // NaN alpha
  });
  it("supports reversed maps and log scales", () => {
    const rev = getLut(table, "ramp_r");
    expect(rev[0]).toBe(255);
    const rgba = colorize(new Float32Array([1, 10, 100, 0]), {
      lut: getLut(table, "ramp"),
      vmin: 1,
      vmax: 100,
      log: true,
    });
    expect(rgba[4]).toBe(128);
    expect(rgba[15]).toBe(0); // non-positive is transparent on a log scale
    expect(scalePosition(10, { vmin: 1, vmax: 100, log: true })).toBeCloseTo(0.5);
  });
  it("rounds ranges nicely", () => {
    expect(niceRange(251.3, 307.9)).toEqual([250, 310]);
  });
});

describe("mercator", () => {
  it("locates tiles like the server", () => {
    expect(lngLatToTileFloat(0, 0, 1)).toEqual([1, 1]);
    const { tile, px, py } = locate(8.5, 47.4, 10, 256);
    expect(tile).toEqual({ z: 10, x: 536, y: 358 });
    expect(px).toBeGreaterThanOrEqual(0);
    expect(py).toBeLessThan(256);
  });
  it("matches MapLibre's raster tile zoom", () => {
    expect(tileZoom(3.4, 256, 12)).toBe(4);
    expect(tileZoom(3.6, 256, 12)).toBe(5);
    expect(tileZoom(14, 256, 9)).toBe(9);
  });
  it("covers a view with tiles, also across the antimeridian", () => {
    expect(tilesInBounds(-180, -85, 180, 85, 1)).toHaveLength(4);
    const across = tilesInBounds(170, -10, -170, 10, 4);
    expect(new Set(across.map((t) => t.x))).toEqual(new Set([15, 0]));
  });
});

describe("url state", () => {
  it("round-trips a view", () => {
    const s = {
      layer: "hires:tas",
      t: "1978-07-15",
      dims: { plev: "500" },
      cmap: "RdYlBu_r",
      range: [250, 310] as [number, number],
      center: [8.5, 46.5] as [number, number],
      zoom: 6.25,
      cmp: "split" as const,
      cmpLayer: "era5:t2m_mean",
      pt: [8.1234, 46.9876] as [number, number],
    };
    const back = parseHash(buildHash(s));
    expect(back).toMatchObject({ ...s, zoom: 6.25 });
  });
  it("ignores junk", () => {
    expect(parseHash("#range=a,b&z=x&bm=nope")).toEqual({});
  });
});

describe("statistics and helpers", () => {
  it("percentiles and histograms", () => {
    const v = Float32Array.from({ length: 101 }, (_, i) => i);
    expect(percentiles(v, [0, 50, 100])).toEqual([0, 50, 100]);
    expect(histogram(v, 0, 100, 4)).toEqual([25, 25, 25, 25]); // 100 is outside [0, 100)
  });
  it("nice ticks", () => {
    expect(niceTicks(250, 310, false)).toEqual([260, 280, 300]);
    expect(niceTicks(0, 30, false)).toEqual([0, 10, 20, 30]);
    expect(niceTicks(0.1, 100, true)).toEqual([0.1, 1, 10, 100]);
  });
  it("merges series with different time axes", () => {
    const m = mergeSeries([
      {
        source: 0,
        id: "a",
        resolution: "",
        cell: { lon: 0, lat: 0, row: 0, col: 0 },
        times: ["2000-01-02", "2000-01-03"],
        values: [1, 2],
      },
      {
        source: 1,
        id: "b",
        resolution: "",
        cell: { lon: 0, lat: 0, row: 0, col: 0 },
        times: ["2000-01-01", "2000-01-02"],
        values: [5, 6],
      },
    ]);
    expect(m.labels).toEqual(["2000-01-01", "2000-01-02", "2000-01-03"]);
    expect(m.data).toEqual([
      [null, 1, 2],
      [5, 6, null],
    ]);
  });
});

describe("stations", async () => {
  const { colorExpression, toGeoJSON } = await import("./points");
  const lut = new Uint8Array(768);
  for (let i = 0; i < 256; i++) lut.set([i, 0, 255 - i], i * 3);
  it("builds a colour expression with the scale's stops", () => {
    const e = colorExpression({ lut, vmin: 250, vmax: 310, log: false }, 4) as unknown[];
    expect(e.slice(0, 3)).toEqual(["interpolate", ["linear"], ["get", "value"]]);
    expect(e[3]).toBe(250);
    expect(e[4]).toBe("rgb(0,0,255)");
    expect(e[9]).toBe(310);
    expect(e[10]).toBe("rgb(255,0,0)");
    const log = colorExpression({ lut, vmin: 1, vmax: 100, log: true }, 3) as unknown[];
    expect(log[5]).toBeCloseTo(10);
  });
  it("converts features to GeoJSON", () => {
    const g = toGeoJSON({
      source: "s",
      title: "t",
      variable: "tg",
      time: "1978-07-15",
      count: 1,
      truncated: false,
      features: [{ id: "X", name: null, lon: 8, lat: 47, elevation: 400, value: 290 }],
    });
    expect(g.features[0].geometry).toEqual({ type: "Point", coordinates: [8, 47] });
    expect(g.features[0].properties).toMatchObject({ id: "X", name: "X", value: 290 });
  });
});

describe("time pairing", async () => {
  const { validTime } = await import("../state/store");
  it("finds the containing month, the nearest day, or the same label", () => {
    const monthly = { times: ["1978-06-16", "1978-07-16", "1978-08-16"], time_step: "1 month" };
    expect(validTime(monthly, "1978-07-01")).toBe("1978-07-16");
    const daily = { times: ["2021-01-01", "2021-01-05", "2021-01-09"], time_step: "4 days" };
    expect(validTime(daily, "2021-01-04")).toBe("2021-01-05");
    expect(validTime(daily, "2021-01-02")).toBe("2021-01-01");
    expect(validTime(daily, "2021-01-05")).toBe("2021-01-05");
    expect(validTime(daily, null)).toBe("2021-01-09");
  });
});

describe("timeline", async () => {
  const { timelineOf } = await import("./timeline");
  it("computes labels of a long hourly axis without listing them", () => {
    const tl = timelineOf({
      times: null,
      time_axis: {
        start: "1940-01-01T00:00",
        end: "2090-12-30T23:00",
        count: 1323648,
        step_seconds: 3600,
        format: "datetime",
      },
    });
    expect(tl.count).toBe(1323648);
    expect(tl.at(0)).toBe("1940-01-01T00:00");
    expect(tl.at(tl.count - 1)).toBe("2090-12-30T23:00");
    expect(tl.at(25)).toBe("1940-01-02T01:00");
    expect(tl.nearest("1978-07-15T12:20")).toBe(tl.indexOf("1978-07-15T12:00"));
    expect(tl.indexOf("1978-07-15T12:30")).toBe(-1);
    expect(tl.nearest("1978-07-15")).toBe(tl.indexOf("1978-07-15T00:00"));
  });
  it("searches a listed axis", () => {
    const tl = timelineOf({ times: ["2021-01-01", "2021-01-05", "2021-01-09"] });
    expect(tl.nearest("2021-01-04")).toBe(1);
    expect(tl.nearest("2021-01-02")).toBe(0);
    expect(tl.nearest("2030-01-01")).toBe(2);
    expect(tl.indexOf("2021-01-05")).toBe(1);
  });
});

describe("rgb", async () => {
  const { colorizeRgb } = await import("./colormap");
  it("decodes three bands and stretches them with gamma", () => {
    const r = new Float32Array([0, 50, 100, NaN]);
    const g = new Float32Array([0, 50, 100, 1]);
    const b = new Float32Array([100, 50, 0, 1]);
    const buf = new Uint8Array([
      ...new Uint8Array(encode(r)),
      ...new Uint8Array(encode(g)),
      ...new Uint8Array(encode(b)),
    ]).buffer;
    const d = decodeValues(buf, 2, 3);
    expect(d.bands).toHaveLength(3);
    expect(Array.from(d.bands![2])).toEqual([100, 50, 0, 1]);
    expect(d.sourceMap).toBeNull();
    const rgba = colorizeRgb(d.bands!, 0, 100, 1);
    expect(Array.from(rgba.slice(0, 4))).toEqual([0, 0, 255, 255]);
    expect(rgba[4]).toBe(128); // 50 % at gamma 1 (127.5 rounds up)
    expect(rgba[15]).toBe(0); // NaN in one band → transparent
    expect(colorizeRgb(d.bands!, 0, 100, 2)[4]).toBeGreaterThan(170); // gamma 2 brightens mid-tones
  });
});

describe("classes", async () => {
  const { classTable, colorizeClasses, classShares } = await import("./classes");
  it("colours class codes exactly and counts shares", () => {
    const lut = new Uint8Array(768);
    const t = classTable(
      [
        { value: 10, name: "Trees", color: "#006400" },
        { value: 80, name: "Water", color: null },
      ],
      lut,
    );
    const rgba = colorizeClasses(new Float32Array([10, 80, 30, NaN]), t);
    expect(Array.from(rgba.slice(0, 4))).toEqual([0, 100, 0, 255]);
    expect(rgba[7]).toBe(255); // water: fallback colour, opaque
    expect(rgba[11]).toBe(0); // 30 is not a class
    expect(rgba[15]).toBe(0);
    const shares = classShares(new Float32Array([10, 10, 80, 30]), t);
    expect(shares.get(10)).toBeCloseTo(2 / 3);
  });
});

describe("gallery", async () => {
  const { thumbnailUrl, viewHash } = await import("./gallery");
  const { parseHash } = await import("./urlState");
  const view = {
    id: 0,
    title: "Heat",
    layer: "temperature",
    time: "2023-07-18T14:00",
    center: [14, 42] as [number, number],
    zoom: 3.6,
    vmin: 5,
    vmax: 45,
    compare: "other",
  };
  it("turns a view into the state of a shareable link", () => {
    const s = parseHash(viewHash(view));
    expect(s.layer).toBe("temperature");
    expect(s.t).toBe("2023-07-18T14:00");
    expect(s.range).toEqual([5, 45]);
    expect(s.center).toEqual([14, 42]);
    expect(s.cmp).toBe("split");
    expect(s.cmpLayer).toBe("other");
  });
  it("asks for a preview centred on the view", () => {
    const u = thumbnailUrl(view);
    expect(u.startsWith("api/layers/temperature/preview.png?")).toBe(true);
    const q = new URLSearchParams(u.split("?")[1]);
    expect(q.get("center")).toBe("14,42");
    expect(q.get("zoom")).toBe("2"); // 3.6 − 1.6
    expect(q.get("vmin")).toBe("5");
    expect(thumbnailUrl(view, "other")).not.toContain("vmin"); // B side keeps its own colours
    expect(new URLSearchParams(thumbnailUrl({ ...view, zoom: 0.5 }).split("?")[1]).get("zoom")).toBe("0");
  });
});

describe("comparison colour scales", async () => {
  const { comparable } = await import("../state/store");
  const meta = (o: Record<string, unknown>) =>
    ({
      kind: "dataset",
      units: "K",
      bands: null,
      classes: null,
      style: { vmin: null, vmax: null },
      ...o,
    }) as never;
  it("shares A's scale only for the same quantity", () => {
    expect(comparable(meta({}), meta({ kind: "composite" }))).toBe(true);
    expect(comparable(meta({}), meta({ units: "mm" }))).toBe(false);
    const tci = meta({ kind: "rgb", units: null, bands: ["1", "2", "3"] });
    const fc = meta({
      kind: "rgb",
      units: null,
      bands: ["B08", "B04", "B03"],
      style: { vmin: 0, vmax: 0.45 },
    });
    expect(comparable(tci, fc)).toBe(false);
    expect(comparable(fc, fc)).toBe(true);
  });
});
