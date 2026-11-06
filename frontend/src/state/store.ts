// Application state (zustand). Components read slices; actions keep the
// state consistent (e.g. a new layer resets the time and dims to valid values).

import { create } from "zustand";
import { api } from "../api/client";
import type { Catalog, Colormap, LayerMeta } from "../api/types";
import type { PointsResult } from "../lib/points";
import { timelineOf } from "../lib/timeline";
import type { ViewSummary } from "../lib/view";

export type CompareMode = "off" | "split" | "diff";
export type Basemap = "minimal" | "light" | "dark";

export interface Target {
  layerId: string | null;
  layer: LayerMeta | null;
  time: string | null;
  dims: Record<string, string>;
}

export interface Style {
  colormap: string;
  vmin: number;
  vmax: number;
  log: boolean;
  opacity: number;
  gamma: number; // RGB layers
}

export interface StationRef {
  source: string;
  id: string;
  name: string | null;
  variable: string;
  elevation: number | null;
}

/** The inspected location; `station` when the user clicked a station. */
export interface Inspect {
  lng: number;
  lat: number;
  station?: StationRef;
}

export interface Notice {
  kind: "error" | "info";
  text: string;
  id: number;
}

export interface AppState {
  catalog: Catalog | null;
  colormaps: Record<string, Colormap>;
  main: Target;
  style: Style;
  styleReady: boolean; // range known (from config, URL or stats)
  // ownStyle: B's colour scale when A and B are different quantities (else B uses A's)
  compare: Target & { mode: CompareMode; linkTime: boolean; ownStyle: Style | null };
  diffStyle: Style;
  basemap: Basemap;
  globe: boolean;
  inspect: Inspect | null;
  view: { center: [number, number]; zoom: number; bounds?: [number, number, number, number] } | null;
  notices: Notice[];
  panelOpen: boolean;
  galleryOpen: boolean; // the start screen with featured views (only shown when there are views)
  summary: ViewSummary | null;
  loading: number;
  diffFitPending: boolean; // fit the A − B range once the first difference tiles are on screen
  showPoints: boolean;
  overlay: string | null; // point layer drawn over gridded layers; null = the configured one
  points: PointsResult | null;

  init: (initial?: Partial<UrlState>) => Promise<void>;
  setLayer: (id: string, keep?: Partial<UrlState>) => Promise<void>;
  setTime: (t: string | null) => void;
  stepTime: (k: number) => void;
  setDim: (name: string, value: string) => void;
  setStyle: (s: Partial<Style>) => void;
  autoRange: (bbox?: number[]) => Promise<void>;
  setCompare: (c: Partial<AppState["compare"]>) => Promise<void>;
  setDiffStyle: (s: Partial<Style>) => void;
  setBasemap: (b: Basemap) => void;
  setGlobe: (g: boolean) => void;
  setInspect: (p: Inspect | null) => void;
  setShowPoints: (s: boolean) => void;
  setOverlay: (id: string | null) => void;
  setPoints: (p: PointsResult | null) => void;
  setView: (v: { center: [number, number]; zoom: number; bounds?: [number, number, number, number] }) => void;
  notify: (kind: Notice["kind"], text: string) => void;
  dismiss: (id: number) => void;
  setPanelOpen: (o: boolean) => void;
  setGalleryOpen: (o: boolean) => void;
  setSummary: (s: ViewSummary | null) => void;
  setLoading: (n: number) => void;
}

/** Everything that goes into a shareable link. */
export interface UrlState {
  layer: string;
  t: string;
  dims: Record<string, string>;
  cmap: string;
  range: [number, number];
  log: boolean;
  opacity: number;
  center: [number, number];
  zoom: number;
  basemap: Basemap;
  globe: boolean;
  cmp: CompareMode;
  cmpLayer: string;
  cmpT: string;
  pt: [number, number];
  ov: string; // point overlay: a point layer id, or "none"
}

const emptyTarget: Target = { layerId: null, layer: null, time: null, dims: {} };
let noticeId = 0;

function validDims(layer: LayerMeta, wanted: Record<string, string> = {}): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [d, values] of Object.entries(layer.dims)) {
    const w = wanted[d];
    out[d] = w !== undefined && values.map(String).includes(w) ? w : String(values[0]);
  }
  return out;
}

/** The layer's own label for a wanted time: the same label, the containing month or year for
 * monthly and yearly data, otherwise the nearest label in time. */
export function validTime(
  layer: Pick<LayerMeta, "times" | "time_axis" | "time_step">,
  wanted?: string | null,
): string | null {
  const tl = timelineOf(layer);
  if (!tl.count) return null;
  if (!wanted) return tl.at(tl.count - 1);
  if (tl.indexOf(wanted) >= 0) return wanted;
  const step = layer.time_step ?? "";
  const key = step.includes("month") ? wanted.slice(0, 7) : step.includes("year") ? wanted.slice(0, 4) : null;
  if (key && layer.times) {
    const hit = layer.times.find((t) => t.startsWith(key));
    if (hit) return hit;
  }
  return tl.at(tl.nearest(wanted));
}

/** The point layer drawn on the map: the main layer itself, or the overlay of a gridded layer. */
export function pointsLayerOf(
  s: Pick<AppState, "main" | "showPoints" | "overlay" | "compare">,
): string | null {
  const layer = s.main.layer;
  if (!layer) return null;
  if (layer.kind === "points") return s.main.layerId;
  if (!s.showPoints || s.compare.mode === "diff") return null;
  return s.overlay ?? layer.points?.layer ?? null;
}

/** Can B be drawn with A's colour scale? Same units and kind (and the same range for RGB). */
export function comparable(a: LayerMeta, b: LayerMeta): boolean {
  const rgb = (l: LayerMeta) => l.kind === "rgb" || !!l.bands;
  if (rgb(a) !== rgb(b) || (a.units ?? "") !== (b.units ?? "")) return false;
  if (rgb(a)) return a.style.vmin === b.style.vmin && a.style.vmax === b.style.vmax;
  return !a.classes === !b.classes;
}

/** B's own colour scale: its configured range, else its 2–98 % range. */
async function ownStyleOf(
  layer: LayerMeta,
  time: string | null,
  dims: Record<string, string>,
  base: Style,
): Promise<Style> {
  const s = { ...base, colormap: layer.style.colormap ?? base.colormap, gamma: layer.style.gamma ?? 1 };
  if (layer.style.vmin !== null && layer.style.vmax !== null)
    return { ...s, vmin: layer.style.vmin, vmax: layer.style.vmax };
  const st = await api.stats(layer.id, time, dims);
  if (!st.count || st.p2 === undefined || st.p98 === undefined) return s;
  return { ...s, vmin: Number(st.p2.toPrecision(3)), vmax: Number(st.p98.toPrecision(3)) };
}

export const useStore = create<AppState>((set, get) => ({
  catalog: null,
  colormaps: {},
  main: emptyTarget,
  style: { colormap: "viridis", vmin: 0, vmax: 1, log: false, opacity: 1, gamma: 1 },
  styleReady: false,
  compare: { ...emptyTarget, mode: "off", linkTime: true, ownStyle: null },
  diffStyle: { colormap: "RdBu_r", vmin: -1, vmax: 1, log: false, opacity: 1, gamma: 1 },
  basemap: "minimal",
  globe: false,
  inspect: null,
  view: null,
  notices: [],
  panelOpen: true,
  galleryOpen: false,
  summary: null,
  loading: 0,
  diffFitPending: false,
  showPoints: true,
  overlay: null,
  points: null,

  init: async (initial = {}) => {
    try {
      const [catalog, colormaps] = await Promise.all([api.catalog(), api.colormaps()]);
      if (initial.ov)
        set({ overlay: initial.ov === "none" ? null : initial.ov, showPoints: initial.ov !== "none" });
      // view, basemap and globe were set from the URL before the first render (main.tsx)
      set({ catalog, colormaps, inspect: initial.pt ? { lng: initial.pt[0], lat: initial.pt[1] } : null });
      for (const d of catalog.datasets)
        if (d.status === "error")
          get().notify("error", `Dataset “${d.title}” could not be opened: ${d.error}`);
      if (!catalog.layers.length) {
        get().notify("error", "No layers available. Check the server configuration and log.");
        return;
      }
      const first =
        catalog.layers.find((l) => l.id === initial.layer) ??
        catalog.layers.find((l) => l.kind === "composite") ??
        catalog.layers[0];
      await get().setLayer(first.id, initial);
      if (initial.cmp && initial.cmp !== "off")
        await get().setCompare({
          mode: initial.cmp,
          layerId: initial.cmpLayer ?? first.id,
          time: initial.cmpT ?? null,
        });
    } catch (e) {
      get().notify("error", (e as Error).message);
    }
  },

  setLayer: async (id, keep = {}) => {
    try {
      const layer = await api.layer(id);
      const prev = get().main;
      const time = validTime(layer, keep.t ?? prev.time);
      const dims = validDims(layer, keep.dims ?? prev.dims);
      const cmap = keep.cmap ?? layer.style.colormap ?? "viridis";
      const fixed =
        keep.range ??
        (layer.style.vmin !== null && layer.style.vmax !== null
          ? [layer.style.vmin, layer.style.vmax]
          : null);
      set({
        main: { layerId: id, layer, time, dims },
        style: {
          ...get().style,
          colormap: cmap,
          log: keep.log ?? false,
          opacity: keep.opacity ?? get().style.opacity,
          gamma: layer.style.gamma ?? 1,
          ...(fixed ? { vmin: fixed[0], vmax: fixed[1] } : {}),
        },
        styleReady: !!fixed,
      });
      const c = get().compare;
      if (c.mode !== "off" && c.linkTime) await get().setCompare({});
      if (!fixed) await get().autoRange();
    } catch (e) {
      get().notify("error", `Cannot load layer ${id}: ${(e as Error).message}`);
    }
  },

  setTime: (t) => {
    const { main, compare } = get();
    set({ main: { ...main, time: t } });
    // linked B gets the same time; the server matches it per source (month, day, …)
    if (compare.mode !== "off" && compare.linkTime && compare.layer)
      set({ compare: { ...get().compare, time: timelineOf(compare.layer).count ? t : null } });
  },

  stepTime: (k) => {
    const { main } = get();
    const tl = timelineOf(main.layer);
    if (!tl.count) return;
    const i = main.time ? tl.nearest(main.time) : tl.count - 1;
    const j = Math.max(0, Math.min(tl.count - 1, i + k));
    if (j !== i) get().setTime(tl.at(j));
  },

  setDim: (name, value) => {
    const { main } = get();
    set({ main: { ...main, dims: { ...main.dims, [name]: value } } });
  },

  setStyle: (s) => set({ style: { ...get().style, ...s }, styleReady: true }),

  autoRange: async (bbox) => {
    const { main } = get();
    if (!main.layerId) return;
    try {
      const st = await api.stats(main.layerId, main.time, main.dims, bbox);
      // the user may have switched layer or time while this was computed
      const now = get().main;
      if (now.layerId !== main.layerId || now.time !== main.time) return;
      if (!st.count || st.p2 === undefined || st.p98 === undefined) {
        set({ styleReady: true });
        if (!bbox)
          get().notify(
            "info",
            "No data visible at this zoom to set the colour range; zoom in and press Fit.",
          );
        return;
      }
      let lo = st.p2;
      let hi = st.p98;
      if (hi <= lo) [lo, hi] = [lo - 1, hi + 1];
      if ((st.min ?? 0) >= 0 && lo < (hi - lo) * 0.05) lo = 0; // non-negative fields start at 0
      set({
        style: { ...get().style, vmin: Number(lo.toPrecision(3)), vmax: Number(hi.toPrecision(3)) },
        styleReady: true,
      });
    } catch (e) {
      set({ styleReady: true });
      get().notify("error", `Statistics failed: ${(e as Error).message}`);
    }
  },

  setCompare: async (c) => {
    const cur = { ...get().compare, ...c };
    if (cur.mode === "diff" && get().compare.mode !== "diff") set({ diffFitPending: true });
    if (cur.mode === "off") {
      set({ compare: { ...cur } });
      return;
    }
    const main = get().main;
    const id = cur.layerId ?? main.layerId;
    if (!id) return;
    try {
      const layer = cur.layer && cur.layer.id === id ? cur.layer : await api.layer(id);
      const time = cur.linkTime ? (timelineOf(layer).count ? main.time : null) : validTime(layer, cur.time);
      const dims = validDims(layer, cur.dims && Object.keys(cur.dims).length ? cur.dims : main.dims);
      const ownStyle =
        main.layer && !comparable(main.layer, layer)
          ? await ownStyleOf(layer, time, dims, get().style)
          : null;
      set({ compare: { ...cur, layerId: id, layer, time, dims, ownStyle } });
    } catch (e) {
      get().notify("error", `Cannot load comparison layer: ${(e as Error).message}`);
    }
  },

  setDiffStyle: (s) => set({ diffStyle: { ...get().diffStyle, ...s } }),
  setBasemap: (b) => set({ basemap: b }),
  setGlobe: (g) => set({ globe: g }),
  setInspect: (p) => set({ inspect: p }),
  setShowPoints: (s) => set({ showPoints: s }),
  setOverlay: (id) => set({ overlay: id }),
  setPoints: (p) => set({ points: p }),
  setView: (v) => set({ view: v }),
  notify: (kind, text) => {
    const id = ++noticeId;
    set({ notices: [...get().notices.filter((n) => n.text !== text), { kind, text, id }].slice(-4) });
    if (kind === "info") setTimeout(() => get().dismiss(id), 6000);
  },
  dismiss: (id) => set({ notices: get().notices.filter((n) => n.id !== id) }),
  setPanelOpen: (o) => set({ panelOpen: o }),
  setGalleryOpen: (o) => set({ galleryOpen: o }),
  setSummary: (s) => {
    set({ summary: s });
    const { diffFitPending, compare } = get();
    // A new difference map starts with a symmetric range around 0 fitted to the screen.
    if (
      diffFitPending &&
      compare.mode === "diff" &&
      s &&
      s.tilesLoaded === s.tilesTotal &&
      s.sample.length > 200
    ) {
      const abs = Float32Array.from(s.sample, Math.abs).sort();
      const m = abs[Math.floor(0.98 * (abs.length - 1))] || 1;
      const r = Number(m.toPrecision(2));
      set({ diffStyle: { ...get().diffStyle, vmin: -r, vmax: r }, diffFitPending: false });
    }
  },
  setLoading: (n) => set({ loading: n }),
}));
