// Application state (zustand). Components read slices; actions keep the
// state consistent (e.g. a new layer resets the time and dims to valid values).

import { create } from "zustand";
import { api } from "../api/client";
import type { Catalog, Colormap, ErrorStats, LayerMeta } from "../api/types";
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
import { CUSTOM, errorMetric, type ErrorChoice } from "../lib/errors";
import { errorStyleFor, fitErrorRange, isErrorPane } from "../lib/paneDraw";
import type { PointsResult } from "../lib/points";
import { timelineOf } from "../lib/timeline";
import type { ViewSummary } from "../lib/view";

// "error": the pane shows an error map of its layer (P) against a reference (T, `compare`);
// "split" only appears in links from 0.1 (now two panes).
export type CompareMode = "off" | "split" | "error";

/** Panes an error pane follows: its P and T take the layer, time and dims of these panes. */
export interface Follow {
  p: string | null;
  t: string | null;
}
export type Basemap = "minimal" | "light" | "dark";

/** A range for data shown with a colormap: centred on 0 when the colormap is diverging and the
 * data crosses 0 (wind components, anomalies, °C), so the neutral colour means zero. */
export function centredRange(
  lo: number,
  hi: number,
  colormap: string,
  colormaps: Record<string, Colormap>,
): [number, number] {
  // a name, its reverse ("_r"), or the reverse of a reversed name: the same colours
  const entry = colormaps[colormap] ?? colormaps[colormap.replace(/_r$/, "")] ?? colormaps[`${colormap}_r`];
  if (entry?.category === "diverging" && lo < 0 && hi > 0) {
    const m = Math.max(-lo, hi);
    return [-m, m];
  }
  return [lo, hi];
}

/** A pane's time offset from its linked group, e.g. { n: -30, unit: "y" }: 30 years earlier. */
export interface TimeOffset {
  n: number;
  unit: "y" | "m" | "d" | "h";
}

const pad = (v: number, w = 2) => String(Math.abs(v)).padStart(w, "0");
const daysIn = (y: number, m: number) => new Date(Date.UTC(y, m, 0)).getUTCDate(); // m: 1–12

/** A time label shifted by an offset (`sign` -1: back), in the same form as the label
 * ("2021", "2021-07", "2021-07-15", "2021-07-15T12:00"). Month and year shifts keep the day
 * within the month (Jan 31 + 1 month = Feb 28). */
export function shiftTime(label: string, off: TimeOffset | null, sign = 1): string {
  if (!off || !off.n) return label;
  const m = /^(-?\d{4,})(?:-(\d{2})(?:-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?)?)?/.exec(label);
  if (!m) return label;
  const k = off.n * sign;
  let y = Number(m[1]);
  let mo = Number(m[2] ?? 1);
  let d = Number(m[3] ?? 1);
  let h = Number(m[4] ?? 0);
  const mi = Number(m[5] ?? 0);
  const sec = Number(m[6] ?? 0);
  if (off.unit === "y" || off.unit === "m") {
    const total = y * 12 + (mo - 1) + (off.unit === "y" ? 12 * k : k);
    y = Math.floor(total / 12);
    mo = total - y * 12 + 1;
    d = Math.min(d, daysIn(y, mo));
  } else {
    const t = new Date(Date.UTC(2000, 0, 1, h, mi, sec));
    t.setUTCFullYear(y, mo - 1, d);
    if (off.unit === "d") t.setUTCDate(t.getUTCDate() + k);
    else t.setUTCHours(t.getUTCHours() + k);
    [y, mo, d, h] = [t.getUTCFullYear(), t.getUTCMonth() + 1, t.getUTCDate(), t.getUTCHours()];
  }
  let out = `${y < 0 ? "-" : ""}${pad(y, 4)}`;
  if (m[2] !== undefined) out += `-${pad(mo)}`;
  if (m[3] !== undefined) out += `-${pad(d)}`;
  if (m[4] !== undefined) out += `T${pad(h)}:${pad(mi)}`;
  if (m[6] !== undefined) out += `:${pad(sec)}`;
  return out + label.slice(m[0].length); // e.g. a time zone suffix stays
}

/** "−30 y", "+6 m", … */
export function fmtOffset(o: TimeOffset | null): string {
  if (!o || !o.n) return "";
  const name = { y: "y", m: "mo", d: "d", h: "h" }[o.unit];
  return `${o.n > 0 ? "+" : "−"}${Math.abs(o.n)} ${name}`;
}

export function parseOffset(s: string | null | undefined): TimeOffset | null {
  const m = /^([+-]?\d+)([ymdh])$/.exec((s ?? "").trim());
  return m && Number(m[1]) ? { n: Number(m[1]), unit: m[2] as TimeOffset["unit"] } : null;
}

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
  // In an error pane, `compare` is the reference T and `diffStyle` the error map's colour scale.
  compare: Target & {
    mode: CompareMode;
    linkTime: boolean;
    ownStyle: Style | null;
    error: ErrorChoice;
    follow: Follow;
  };
  diffStyle: Style;
  basemap: Basemap;
  globe: boolean;
  inspect: Inspect | null;
  picking: boolean; // "Point" mode: a click on the map picks a point (else clicks only pan)
  view: View | null; // the active pane's camera
  // Split panes (like tmux). The active pane's state is in the fields above (main, style, compare,
  // view, …: see PANE_KEYS), so the side panel, time bar and colour bar act on it; the other panes'
  // states are in `panes`. Linked panes share the camera (linkView) and the time (linkTime).
  linkView: boolean;
  linkTime: boolean;
  timeOffset: TimeOffset | null; // a linked pane shows the group's time shifted by this
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
  diffFitPending: boolean; // fit the error map's range once its first tiles are on screen
  showPoints: boolean;
  overlay: string | null; // point layer drawn over gridded layers; null = the configured one
  points: PointsResult | null;
  errorStats: Record<string, ErrorStats | null>; // per error pane: statistics of its view

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
  setError: (c: Partial<ErrorChoice>) => void;
  setFollow: (f: Partial<Follow>) => Promise<void>;
  addErrorPane: (p: string, t: string, choice?: ErrorChoice) => void;
  setBasemap: (b: Basemap) => void;
  setGlobe: (g: boolean) => void;
  setInspect: (p: Inspect | null) => void;
  setPicking: (on: boolean) => void;
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
  setTimeOffset: (o: TimeOffset | null) => void;
  setPaneView: (id: string, v: View) => void;
  notify: (kind: Notice["kind"], text: string) => void;
  dismiss: (id: number) => void;
  setPanelOpen: (o: boolean) => void;
  setGalleryOpen: (o: boolean) => void;
  setVectorsOn: (ids: string[]) => void;
  refreshCatalog: () => Promise<void>;
  setSummary: (s: ViewSummary | null) => void;
  setLoading: (n: number) => void;
  setErrorStats: (pane: string, s: ErrorStats | null) => void;
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
  cmp: CompareMode | "diff"; // "diff": links from 0.2 (an error pane showing P − T)
  cmpLayer: string;
  cmpT: string;
  em: string; // error definition (metric id or "custom")
  ef: string; // custom formula
  ep: Record<string, number>; // parameters of the error definition
  eu: string; // units of a custom formula
  fp: number; // pane (index) P follows
  ft: number; // pane (index) T follows
  pt: [number, number];
  ov: string; // point overlay: a point layer id, or "none"
  vec: string[]; // vector overlays shown (empty: none)
  lay: string; // split layout (lib/layout encodeLayout); the top-level fields are pane `pa`
  pa: number;
  lv: boolean; // false: the pane's camera is not linked
  lt: boolean; // false: the pane's time is not linked
  to: string; // the pane's time offset, e.g. "-30y"
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
  "timeOffset",
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
    timeOffset: s.timeOffset,
    view: s.view,
  };
}

/** The state of pane `id`: the live top-level fields for the active pane. */
export function paneOf(s: AppState, id: string): PaneState | undefined {
  return id === s.active ? snapshot(s) : s.panes[id];
}

/** A pane's part of a shareable link. The camera only for a pane that is not linked. `order`:
 * the panes in layout order (followed panes are stored by index). */
export function paneUrl(p: PaneState, withView = !p.linkView, order: string[] = []): Partial<UrlState> {
  const diff = isErrorPane(p);
  const e = p.compare.error;
  const idx = (id: string | null) => (id && order.includes(id) ? order.indexOf(id) : undefined);
  return {
    layer: p.main.layerId ?? undefined,
    t: p.main.time ?? undefined,
    dims: p.main.dims,
    // an error pane's link keeps the error map's colours
    cmap: (diff ? p.diffStyle : p.style).colormap,
    range: diff ? [p.diffStyle.vmin, p.diffStyle.vmax] : [p.style.vmin, p.style.vmax],
    log: (diff ? p.diffStyle : p.style).log,
    opacity: p.style.opacity,
    center: withView ? p.view?.center : undefined,
    zoom: withView ? p.view?.zoom : undefined,
    cmp: diff ? "error" : undefined,
    cmpLayer: diff ? (p.compare.layerId ?? undefined) : undefined,
    cmpT: diff && !p.compare.linkTime ? (p.compare.time ?? undefined) : undefined,
    em: diff ? e.metric : undefined,
    ef: diff ? e.formula : undefined,
    ep: diff && e.params && Object.keys(e.params).length ? e.params : undefined,
    eu: diff ? e.units : undefined,
    fp: diff ? idx(p.compare.follow.p) : undefined,
    ft: diff ? idx(p.compare.follow.t) : undefined,
    lv: p.linkView ? undefined : false,
    lt: p.linkTime ? undefined : false,
    to: p.timeOffset?.n ? `${p.timeOffset.n}${p.timeOffset.unit}` : undefined,
  };
}

/** The whole shareable state of the app (all panes). */
export function linkState(s: AppState): Partial<UrlState> {
  const order = paneIds(s.layout);
  const panes: Record<number, Partial<UrlState>> = {};
  for (const [i, id] of order.entries())
    if (id !== s.active && s.panes[id]?.main.layerId) panes[i] = paneUrl(s.panes[id], undefined, order);
  return {
    ...paneUrl(snapshot(s), true, order),
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

/** A pane's state with the time of the linked group, shifted by the pane's offset (each pane
 * snaps to its own nearest step). */
function withTime(p: PaneState, base: string | null): PaneState {
  if (!p.main.layer) return p;
  const t = base ? shiftTime(base, p.timeOffset) : base;
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
  compare: {
    ...emptyTarget,
    mode: "off",
    linkTime: true,
    ownStyle: null,
    error: { metric: "bias" },
    follow: { p: null, t: null },
  },
  diffStyle: defaultDiffStyle,
  diffFitPending: false,
  linkView: true,
  linkTime: true,
  timeOffset: null,
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
  if (!s.showPoints || s.compare.mode === "error") return null;
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
  picking: false,
  notices: [],
  panelOpen: true,
  galleryOpen: false,
  vectorsOn: null,
  summary: null,
  loading: 0,
  showPoints: true,
  overlay: null,
  points: null,
  errorStats: {},

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
    set({ linkView: u.lv ?? true, linkTime: u.lt ?? true, timeOffset: parseOffset(u.to) });
    if (u.center && u.zoom !== undefined) set({ view: { center: u.center, zoom: u.zoom } });
    if (u.cmp === "diff" || u.cmp === "error") {
      const error: ErrorChoice = {
        metric: u.em ?? "bias", // links from 0.2 (cmp=diff) showed P − T
        ...(u.ef ? { formula: u.ef } : {}),
        ...(u.ep ? { params: u.ep } : {}),
        ...(u.eu ? { units: u.eu } : {}),
      };
      const follow = {
        p: u.fp !== undefined ? `p${u.fp}` : null,
        t: u.ft !== undefined ? `p${u.ft}` : null,
      };
      set({ styleReady: true, diffStyle: errorStyleFor(error, get().diffStyle) });
      await get().setCompare({
        mode: "error",
        layerId: u.cmpLayer ?? get().main.layerId,
        time: u.cmpT ?? null,
        linkTime: !u.cmpT, // a link with its own T time (ct=) must keep it
        error,
        follow,
      });
      // colours given by the link (or view) are the error map's, and are kept as they are
      if (u.cmap || u.range || u.log) {
        const d = get().diffStyle;
        set({
          diffStyle: {
            ...d,
            colormap: u.cmap ?? d.colormap,
            log: u.log ?? d.log,
            ...(u.range ? { vmin: u.range[0], vmax: u.range[1] } : {}),
          },
          diffFitPending: !u.range,
        });
      }
    }
  },

  setLayer: async (id, keep = {}) => {
    const pane = get().active;
    const c0 = get().compare;
    // choosing P's layer by hand in an error pane stops following a pane
    if (c0.mode === "error" && c0.follow.p && id !== get().main.layerId)
      set({ compare: { ...c0, follow: { ...c0.follow, p: null } } });
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
    const { main, compare, linkTime, panes, timeOffset } = get();
    set({ main: { ...main, time: t } });
    // linked B gets the same time; the server matches it per source (month, day, …)
    if (compare.mode !== "off" && compare.linkTime && compare.layer)
      set({ compare: { ...get().compare, time: timelineOf(compare.layer).count ? t : null } });
    // the other linked panes follow, each with its own offset, to its nearest time step
    if (linkTime && t) {
      const base = shiftTime(t, timeOffset, -1);
      set({
        panes: Object.fromEntries(
          Object.entries(panes).map(([id, p]) => [id, p.linkTime ? withTime(p, base) : p]),
        ),
      });
    }
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
      [lo, hi] = centredRange(lo, hi, get().style.colormap, get().colormaps);
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
    if (cur.mode === "error" && get().compare.mode !== "error")
      set({ diffFitPending: true, styleReady: true });
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

  setError: (c) => {
    const prev = get().compare.error;
    let next: ErrorChoice;
    if (c.metric && c.metric !== prev.metric) {
      // a new definition starts with its own default parameters (unless given); a custom
      // formula starts from the formula shown so far
      const m = errorMetric(prev.metric);
      const start = prev.formula ?? (m && !m.params ? m.formula : "P - T");
      next =
        c.metric === CUSTOM
          ? { metric: CUSTOM, formula: c.formula ?? start, ...(c.units ? { units: c.units } : {}) }
          : { metric: c.metric, ...(c.params ? { params: c.params } : {}) };
    } else next = { ...prev, ...c };
    const restyle = errorStyleFor(next, get().diffStyle);
    const sameScale = errorStyleFor(prev, get().diffStyle).colormap === restyle.colormap;
    set({
      compare: { ...get().compare, error: next },
      diffStyle: sameScale && next.metric === prev.metric ? get().diffStyle : restyle,
      diffFitPending: true,
    });
  },

  setFollow: async (f) => {
    const follow = { ...get().compare.follow, ...f };
    set({ compare: { ...get().compare, follow } });
    syncFollowers();
  },

  addErrorPane: (pId, tId, choice = { metric: "bias" }) => {
    const s = get();
    const P = paneOf(s, pId);
    const T = paneOf(s, tId);
    if (!P?.main.layer || !T?.main.layer) return;
    const ids = paneIds(s.layout);
    if (ids.length >= MAX_PANES) {
      get().notify("info", `At most ${MAX_PANES} panes: close one first (×).`);
      return;
    }
    const id = freePaneId(ids);
    const base = snapshot(s);
    const pane: PaneState = {
      ...base,
      main: P.main,
      style: P.style,
      styleReady: true,
      compare: {
        ...T.main,
        mode: "error",
        linkTime: true,
        ownStyle: null,
        error: choice,
        follow: { p: pId, t: tId },
      },
      diffStyle: errorStyleFor(choice, base.diffStyle),
      diffFitPending: true,
      linkTime: P.linkTime && T.linkTime,
    };
    // the new pane splits the active one along its longer side (in screen terms, roughly)
    set({
      layout: splitLayout(s.layout, s.active, ids.length % 2 ? "col" : "row", id),
      panes: { ...s.panes, [id]: pane },
      zoomed: null,
    });
    get().activatePane(id);
  },
  setBasemap: (b) => set({ basemap: b }),
  setGlobe: (g) => set({ globe: g }),
  setInspect: (p) => set({ inspect: p }),
  setPicking: (on) => set({ picking: on }),
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
    unfollow(gone, paneIds(s.layout).indexOf(gone) + 1);
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
      // a pane that joins the group takes the group's time (with its own offset)
      const group = Object.values(get().panes).find((p) => p.linkTime && p.main.time);
      const layer = get().main.layer;
      if (l.time && group && layer && timelineOf(layer).count) {
        const base = shiftTime(group.main.time!, group.timeOffset, -1);
        set({ main: { ...get().main, time: validTime(layer, shiftTime(base, get().timeOffset)) } });
      }
    }
  },

  setTimeOffset: (o) => {
    const off = o && o.n ? o : null;
    const prev = get().timeOffset;
    set({ timeOffset: off });
    const { main, linkTime } = get();
    if (!main.time || !main.layer || !timelineOf(main.layer).count) return;
    // the group's time stays; this pane moves to it plus the new offset
    const group = Object.values(get().panes).find((p) => p.linkTime && p.main.time);
    const base =
      linkTime && group ? shiftTime(group.main.time!, group.timeOffset, -1) : shiftTime(main.time, prev, -1);
    set({ main: { ...main, time: validTime(main.layer, shiftTime(base, off)) } });
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
    // A new error map starts with a range fitted to the screen (by the kind of error).
    if (diffFitPending && compare.mode === "error" && s && s.tilesLoaded === s.tilesTotal) {
      const fit = s.sample.length > 200 ? fitErrorRange(compare.error, s.sample) : null;
      if (fit) set({ diffStyle: { ...get().diffStyle, ...fit }, diffFitPending: false });
      else if (s.tilesTotal > 0 && s.sample.length > 200) set({ diffFitPending: false });
    }
  },
  setLoading: (n) => set({ loading: n }),
  setErrorStats: (pane, s) => {
    if (get().errorStats[pane] !== s) set({ errorStats: { ...get().errorStats, [pane]: s } });
  },
}));

const sameDims = (a: Record<string, string>, b: Record<string, string>) => {
  const ka = Object.keys(a);
  return ka.length === Object.keys(b).length && ka.every((k) => a[k] === b[k]);
};

/** `src` as the new target of a follower, or null when nothing changed (or nothing to follow). */
function followed(cur: Target, src: Target): Target | null {
  if (!src.layer || !src.layerId || src.layer.kind === "points") return null;
  if (cur.layerId === src.layerId && cur.time === src.time && sameDims(cur.dims, src.dims)) return null;
  return { layerId: src.layerId, layer: src.layer, time: src.time, dims: src.dims };
}

/** Error panes take the layer, time and dims of the panes they follow (P and T). */
export function syncFollowers(): void {
  const s = useStore.getState();
  const updates: Record<string, PaneState> = {};
  for (const id of paneIds(s.layout)) {
    const p = paneOf(s, id);
    if (!p || p.compare.mode !== "error") continue;
    const { p: fp, t: ft } = p.compare.follow;
    const P = fp && fp !== id ? paneOf(s, fp) : undefined;
    const T = ft && ft !== id ? paneOf(s, ft) : undefined;
    const main = P ? followed(p.main, P.main) : null;
    const ref = T ? followed(p.compare, T.main) : null;
    if (!main && !ref) continue;
    const newLayer =
      (main !== null && main.layerId !== p.main.layerId) ||
      (ref !== null && ref.layerId !== p.compare.layerId);
    updates[id] = {
      ...p,
      main: main ?? p.main,
      compare: ref ? { ...p.compare, ...ref } : p.compare,
      diffFitPending: p.diffFitPending || newLayer,
    };
  }
  if (!Object.keys(updates).length) return;
  const top = updates[s.active];
  const panes = { ...s.panes };
  for (const [id, p] of Object.entries(updates)) if (id !== s.active) panes[id] = p;
  useStore.setState({
    panes,
    ...(top ? { main: top.main, compare: top.compare, diffFitPending: top.diffFitPending } : {}),
  });
}

useStore.subscribe((s, prev) => {
  if (
    s.panes !== prev.panes ||
    s.main !== prev.main ||
    s.compare !== prev.compare ||
    s.layout !== prev.layout
  )
    syncFollowers();
});

/** A followed pane was closed: its followers keep the layer they show, now fixed. */
function unfollow(gone: string, number: number): void {
  const s = useStore.getState();
  let hit = false;
  const fix = (p: PaneState): PaneState => {
    const f = p.compare.follow;
    if (p.compare.mode !== "error" || (f.p !== gone && f.t !== gone)) return p;
    hit = true;
    const follow = { p: f.p === gone ? null : f.p, t: f.t === gone ? null : f.t };
    return { ...p, compare: { ...p.compare, follow } };
  };
  const panes = Object.fromEntries(Object.entries(s.panes).map(([id, p]) => [id, fix(p)]));
  const top = fix(snapshot(s));
  if (!hit) return;
  useStore.setState({ panes, compare: top.compare });
  s.notify("info", `Pane ${number} was closed; error maps that followed it keep its layer.`);
}
