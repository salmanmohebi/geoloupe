// Application state (zustand). Components read slices; actions keep the
// state consistent (e.g. a new layer resets the time and dims to valid values).

import { create } from "zustand";
import { api } from "../api/client";
import type { Catalog, Colormap, LayerMeta } from "../api/types";
import {
  MAX_PANES,
  PRESET_PANES,
  close as closeLayout,
  decodeLayout,
  encodeLayout,
  equalize,
  neighbour,
  paneIds,
  preset as presetLayout,
  setRatio,
  split as splitLayout,
  type Dir,
  type Layout,
  type Preset,
} from "../lib/layout";
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

export interface View {
  center: [number, number];
  zoom: number;
  bounds?: [number, number, number, number];
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
  view: View | null; // the active pane's camera
  // Split panes (like tmux). The active pane's state is in the fields above (main, style, compare,
  // view, …: see PANE_KEYS), so the side panel, time bar and colour bar act on it; the other panes'
  // states are in `panes`. Linked panes share the camera (linkView) and the time (linkTime).
  linkView: boolean;
  linkTime: boolean;
  layout: Layout;
  active: string;
  panes: Record<string, PaneState>;
  zoomed: string | null; // a pane shown alone (tmux "zoom"); the layout is kept
  notices: Notice[];
  panelOpen: boolean;
  galleryOpen: boolean; // the start screen with featured views (only shown when there are views)
  vectorsOn: string[] | null; // vector overlays shown; null = those visible by default
  summary: ViewSummary | null;
  loading: number;
  diffFitPending: boolean; // fit the A − B range once the first difference tiles are on screen
  showPoints: boolean;
  overlay: string | null; // point layer drawn over gridded layers; null = the configured one
  points: PointsResult | null;

  init: (initial?: Partial<UrlState>) => Promise<void>;
  setupPane: (u: Partial<UrlState>, fallbackLayer: string) => Promise<void>;
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
  setView: (v: View) => void;
  splitPane: (dir: Dir) => void;
  closePane: (id?: string) => void;
  activatePane: (id: string) => void;
  setPaneRatio: (path: string, ratio: number) => void;
  equalizePanes: () => void;
  applyPreset: (p: Preset) => void;
  toggleZoom: (id?: string) => void;
  setLinks: (l: { view?: boolean; time?: boolean }) => Promise<void>;
  setPaneView: (id: string, v: View) => void;
  notify: (kind: Notice["kind"], text: string) => void;
  dismiss: (id: number) => void;
  setPanelOpen: (o: boolean) => void;
  setGalleryOpen: (o: boolean) => void;
  setVectorsOn: (ids: string[]) => void;
  refreshCatalog: () => Promise<void>;
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
  vec: string[]; // vector overlays shown (empty: none)
  lay: string; // split layout (lib/layout encodeLayout); the top-level fields are pane `pa`
  pa: number;
  lv: boolean; // false: the pane's camera is not linked
  lt: boolean; // false: the pane's time is not linked
  panes: Record<number, Partial<UrlState>>; // the other panes, by index
}

/** The fields each pane has of its own. The active pane's are the top-level fields. */
export const PANE_KEYS = [
  "main",
  "style",
  "styleReady",
  "compare",
  "diffStyle",
  "diffFitPending",
  "linkView",
  "linkTime",
  "view",
] as const;
export type PaneState = Pick<AppState, (typeof PANE_KEYS)[number]>;

export function snapshot(s: PaneState): PaneState {
  return {
    main: s.main,
    style: s.style,
    styleReady: s.styleReady,
    compare: s.compare,
    diffStyle: s.diffStyle,
    diffFitPending: s.diffFitPending,
    linkView: s.linkView,
    linkTime: s.linkTime,
    view: s.view,
  };
}

/** The state of pane `id`: the live top-level fields for the active pane. */
export function paneOf(s: AppState, id: string): PaneState | undefined {
  return id === s.active ? snapshot(s) : s.panes[id];
}

/** A pane's part of a shareable link. The camera only for a pane that is not linked. */
export function paneUrl(p: PaneState, withView = !p.linkView): Partial<UrlState> {
  const diff = p.compare.mode === "diff";
  return {
    layer: p.main.layerId ?? undefined,
    t: p.main.time ?? undefined,
    dims: p.main.dims,
    cmap: p.style.colormap,
    range: [p.style.vmin, p.style.vmax],
    log: p.style.log,
    opacity: p.style.opacity,
    center: withView ? p.view?.center : undefined,
    zoom: withView ? p.view?.zoom : undefined,
    cmp: diff ? "diff" : undefined,
    cmpLayer: diff ? (p.compare.layerId ?? undefined) : undefined,
    cmpT: diff && !p.compare.linkTime ? (p.compare.time ?? undefined) : undefined,
    lv: p.linkView ? undefined : false,
    lt: p.linkTime ? undefined : false,
  };
}

/** The whole shareable state of the app (all panes). */
export function linkState(s: AppState): Partial<UrlState> {
  const order = paneIds(s.layout);
  const panes: Record<number, Partial<UrlState>> = {};
  for (const [i, id] of order.entries())
    if (id !== s.active && s.panes[id]?.main.layerId) panes[i] = paneUrl(s.panes[id]);
  return {
    ...paneUrl(snapshot(s), true),
    basemap: s.basemap,
    globe: s.globe,
    pt: s.inspect ? [s.inspect.lng, s.inspect.lat] : undefined,
    ov: !s.showPoints ? "none" : (s.overlay ?? undefined),
    vec: s.vectorsOn ?? undefined,
    lay: order.length > 1 ? encodeLayout(s.layout, order) : undefined,
    pa: order.length > 1 ? order.indexOf(s.active) : undefined,
    panes,
  };
}

/** The lowest free pane id ("p0", "p1", …). */
function freePaneId(taken: string[]): string {
  let i = 0;
  while (taken.includes(`p${i}`)) i++;
  return `p${i}`;
}

/** A pane's state with the time of the linked group (each pane snaps to its own nearest step). */
function withTime(p: PaneState, t: string | null): PaneState {
  if (!p.main.layer) return p;
  const time = t && timelineOf(p.main.layer).count ? validTime(p.main.layer, t) : p.main.time;
  const c = p.compare;
  const compare =
    c.mode !== "off" && c.linkTime && c.layer && t && timelineOf(c.layer).count
      ? { ...c, time: validTime(c.layer, t) }
      : c;
  return { ...p, main: { ...p.main, time }, compare };
}

const emptyTarget: Target = { layerId: null, layer: null, time: null, dims: {} };
const defaultStyle: Style = { colormap: "viridis", vmin: 0, vmax: 1, log: false, opacity: 1, gamma: 1 };
const defaultDiffStyle: Style = { colormap: "RdBu_r", vmin: -1, vmax: 1, log: false, opacity: 1, gamma: 1 };
const freshPane = (view: View | null): PaneState => ({
  main: emptyTarget,
  style: defaultStyle,
  styleReady: false,
  compare: { ...emptyTarget, mode: "off", linkTime: true, ownStyle: null },
  diffStyle: defaultDiffStyle,
  diffFitPending: false,
  linkView: true,
  linkTime: true,
  view,
});
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
  ...freshPane(null),
  layout: { pane: "p0" },
  active: "p0",
  panes: {},
  zoomed: null,
  basemap: "minimal",
  globe: false,
  inspect: null,
  notices: [],
  panelOpen: true,
  galleryOpen: false,
  vectorsOn: null,
  summary: null,
  loading: 0,
  showPoints: true,
  overlay: null,
  points: null,

  init: async (initial = {}) => {
    try {
      const [catalog, colormaps] = await Promise.all([api.catalog(), api.colormaps()]);
      if (initial.ov)
        set({ overlay: initial.ov === "none" ? null : initial.ov, showPoints: initial.ov !== "none" });
      if (initial.vec) set({ vectorsOn: initial.vec });
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
      // Links from 0.1 compared side by side with cmp=split: now two panes.
      let lay = initial.lay ? decodeLayout(initial.lay) : null;
      let others = initial.panes ?? {};
      let top = initial.pa ?? 0;
      if (!lay && initial.cmp === "split") {
        lay = decodeLayout("r50(0,1)");
        top = 0;
        others = { 1: { layer: initial.cmpLayer ?? first.id, t: initial.cmpT, lt: !initial.cmpT } };
        initial = { ...initial, cmp: undefined };
      }
      const ids = lay ? paneIds(lay) : [];
      if (lay && ids.includes(`p${top}`)) {
        // Set up each pane in turn as the active one; the link's top-level pane last.
        const order = [...ids.filter((id) => id !== `p${top}`), `p${top}`];
        set({ layout: lay, active: order[0], panes: {} });
        for (const [k, id] of order.entries()) {
          if (k > 0) {
            const s = get();
            set({ ...freshPane(s.view), panes: { ...s.panes, [s.active]: snapshot(s) }, active: id });
          }
          const u = id === `p${top}` ? initial : (others[Number(id.slice(1))] ?? {});
          await get().setupPane(u, first.id);
        }
        // the linked panes follow the time of the link's own pane (set up last)
        const { linkTime, main } = get();
        if (linkTime && main.time) get().setTime(main.time);
      } else await get().setupPane(initial, first.id);
    } catch (e) {
      get().notify("error", (e as Error).message);
    }
  },

  setupPane: async (u, fallback) => {
    const known = get().catalog?.layers.some((l) => l.id === u.layer);
    await get().setLayer(known ? u.layer! : fallback, u);
    set({ linkView: u.lv ?? true, linkTime: u.lt ?? true });
    if (u.center && u.zoom !== undefined) set({ view: { center: u.center, zoom: u.zoom } });
    if (u.cmp === "diff")
      await get().setCompare({
        mode: "diff",
        layerId: u.cmpLayer ?? get().main.layerId,
        time: u.cmpT ?? null,
        linkTime: !u.cmpT, // a link with its own B time (ct=) must keep it
      });
  },

  setLayer: async (id, keep = {}) => {
    const pane = get().active;
    try {
      const layer = await api.layer(id);
      if (get().active !== pane) return; // the user moved to another pane meanwhile
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
    const { main, compare, linkTime, panes } = get();
    set({ main: { ...main, time: t } });
    // linked B gets the same time; the server matches it per source (month, day, …)
    if (compare.mode !== "off" && compare.linkTime && compare.layer)
      set({ compare: { ...get().compare, time: timelineOf(compare.layer).count ? t : null } });
    // the other linked panes follow, each to its nearest time step
    if (linkTime && t)
      set({
        panes: Object.fromEntries(
          Object.entries(panes).map(([id, p]) => [id, p.linkTime ? withTime(p, t) : p]),
        ),
      });
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
    const { main, active } = get();
    if (!main.layerId) return;
    try {
      const st = await api.stats(main.layerId, main.time, main.dims, bbox);
      // the user may have switched pane, layer or time while this was computed
      const now = get().main;
      if (get().active !== active || now.layerId !== main.layerId || now.time !== main.time) return;
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
    const { main, active } = get();
    const id = cur.layerId ?? main.layerId;
    if (!id) return;
    try {
      const layer = cur.layer && cur.layer.id === id ? cur.layer : await api.layer(id);
      if (get().active !== active) return;
      const time = cur.linkTime ? (timelineOf(layer).count ? main.time : null) : validTime(layer, cur.time);
      const dims = validDims(layer, cur.dims && Object.keys(cur.dims).length ? cur.dims : main.dims);
      const ownStyle =
        main.layer && !comparable(main.layer, layer)
          ? await ownStyleOf(layer, time, dims, get().style)
          : null;
      if (get().active !== active) return;
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

  splitPane: (dir) => {
    const s = get();
    const ids = paneIds(s.layout);
    if (ids.length >= MAX_PANES) {
      get().notify("info", `At most ${MAX_PANES} panes: close one first (×).`);
      return;
    }
    const id = freePaneId(ids);
    // the new pane starts as a copy of the split one, and becomes the active pane (as in tmux)
    set({
      layout: splitLayout(s.layout, s.active, dir, id),
      panes: { ...s.panes, [id]: { ...snapshot(s), diffFitPending: false } },
      zoomed: null,
    });
    get().activatePane(id);
  },

  closePane: (id) => {
    const s = get();
    const gone = id ?? s.active;
    const layout = closeLayout(s.layout, gone);
    if (!layout) return; // the last pane stays
    if (gone === s.active) {
      const next =
        (["left", "up", "right", "down"] as const)
          .map((side) => neighbour(s.layout, gone, side))
          .find((n) => n !== null) ?? paneIds(layout)[0];
      get().activatePane(next);
    }
    const panes = { ...get().panes };
    delete panes[gone];
    set({ layout, panes, zoomed: get().zoomed === gone ? null : get().zoomed });
  },

  activatePane: (id) => {
    const s = get();
    if (id === s.active || !s.panes[id]) return;
    const panes: Record<string, PaneState> = { ...s.panes, [s.active]: snapshot(s) };
    const next = panes[id];
    delete panes[id];
    set({ ...next, panes, active: id, summary: null });
  },

  setPaneRatio: (path, ratio) => set({ layout: setRatio(get().layout, path, ratio) }),
  equalizePanes: () => set({ layout: equalize(get().layout) }),

  applyPreset: (p) => {
    const s = get();
    const ids = paneIds(s.layout);
    const n = PRESET_PANES[p];
    // keep the first n panes, and always the active one
    const keep = (
      ids.slice(0, n).includes(s.active) ? ids : [s.active, ...ids.filter((i) => i !== s.active)]
    ).slice(0, n);
    const panes = { ...s.panes };
    while (keep.length < n) {
      const id = freePaneId([...ids, ...keep]);
      keep.push(id);
      panes[id] = { ...snapshot(s), diffFitPending: false };
    }
    for (const id of ids) if (!keep.includes(id)) delete panes[id];
    set({ layout: presetLayout(p, keep), panes, zoomed: null });
  },

  toggleZoom: (id) => {
    const s = get();
    if (s.zoomed) return set({ zoomed: null });
    if (paneIds(s.layout).length < 2) return;
    const target = id ?? s.active;
    get().activatePane(target);
    set({ zoomed: target });
  },

  setLinks: async (l) => {
    if (l.view !== undefined) set({ linkView: l.view });
    if (l.time !== undefined) {
      set({ linkTime: l.time });
      // a pane that joins the group takes the group's time
      const group = Object.values(get().panes).find((p) => p.linkTime && p.main.time);
      const layer = get().main.layer;
      if (l.time && group && layer && timelineOf(layer).count)
        get().setTime(validTime(layer, group.main.time));
    }
  },

  setPaneView: (id, v) => {
    const s = get();
    if (id === s.active) set({ view: v });
    else if (s.panes[id]) set({ panes: { ...s.panes, [id]: { ...s.panes[id], view: v } } });
  },
  notify: (kind, text) => {
    const id = ++noticeId;
    set({ notices: [...get().notices.filter((n) => n.text !== text), { kind, text, id }].slice(-4) });
    if (kind === "info") setTimeout(() => get().dismiss(id), 6000);
  },
  dismiss: (id) => set({ notices: get().notices.filter((n) => n.id !== id) }),
  setPanelOpen: (o) => set({ panelOpen: o }),
  setGalleryOpen: (o) => set({ galleryOpen: o }),
  setVectorsOn: (ids) => set({ vectorsOn: ids }),
  refreshCatalog: async () => set({ catalog: await api.catalog() }),
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
