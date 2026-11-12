// Featured views (the gallery): a view is the state of a shareable link, so opening one is the same
// as opening that link. Thumbnails are preview images from the server, centred on the view.

import { apiUrl } from "../api/client";
import type { FeaturedView, ViewPane } from "../api/types";
import type { UrlState } from "../state/store";
import { decodeLayout, encodeLayout, preset, type Layout, type Preset } from "./layout";
import { buildHash } from "./urlState";

const PRESETS = ["1", "2h", "2v", "3", "4"];
const DEFAULT_PRESET: Record<number, Preset> = { 1: "1", 2: "2h", 3: "3", 4: "4" };

/** The layout of a view with `n` panes (pane ids "p0", "p1", …), or null when it has one pane. */
export function viewLayout(v: FeaturedView): Layout | null {
  const n = 1 + (v.panes?.length ?? 0);
  if (n < 2) return null;
  const ids = Array.from({ length: n }, (_, i) => `p${i}`);
  const want = v.layout ?? DEFAULT_PRESET[n];
  if (want && PRESETS.includes(want)) return preset(want as Preset, ids);
  return want ? decodeLayout(want) : null;
}

const dimsOf = (d?: Record<string, string | number>) =>
  d ? Object.fromEntries(Object.entries(d).map(([k, x]) => [k, String(x)])) : undefined;

function paneState(p: ViewPane): Partial<UrlState> {
  const e = p.error;
  if (e)
    return {
      layer: p.layer,
      t: p.time,
      dims: dimsOf(p.dims),
      cmap: p.colormap,
      range: p.vmin !== undefined && p.vmax !== undefined ? [p.vmin, p.vmax] : undefined,
      log: p.log,
      cmp: "error",
      cmpLayer: e.reference,
      cmpT: p.compare_time,
      em: e.metric ?? "bias",
      ef: e.formula,
      ep: e.params,
      eu: e.units,
      fp: e.p_pane !== undefined ? e.p_pane - 1 : undefined, // link indices start at 0
      ft: e.t_pane !== undefined ? e.t_pane - 1 : undefined,
      lv: p.link_view === false ? false : undefined,
      lt: p.link_time === false ? false : undefined,
      to: p.time_offset,
    };
  return {
    layer: p.layer,
    t: p.time,
    dims: dimsOf(p.dims),
    cmap: p.colormap,
    range: p.vmin !== undefined && p.vmax !== undefined ? [p.vmin, p.vmax] : undefined,
    log: p.log,
    cmp: p.compare ? "diff" : undefined,
    cmpLayer: p.compare,
    cmpT: p.compare_time,
    lv: p.link_view === false ? false : undefined,
    lt: p.link_time === false ? false : undefined,
    to: p.time_offset,
  };
}

export function viewState(v: FeaturedView): Partial<UrlState> {
  const lay = viewLayout(v);
  const panes = lay
    ? {
        lay: encodeLayout(
          lay,
          Array.from({ length: 1 + v.panes!.length }, (_, i) => `p${i}`),
        ),
        pa: 0,
        panes: Object.fromEntries(
          v.panes!.map((p, i) => [i + 1, paneState({ ...p, time: p.time ?? v.time })]),
        ),
      }
    : {};
  return {
    ...panes,
    layer: v.layer,
    t: v.time,
    dims: dimsOf(v.dims),
    cmap: v.colormap,
    range: v.vmin !== undefined && v.vmax !== undefined ? [v.vmin, v.vmax] : undefined,
    log: v.log,
    center: v.center,
    zoom: v.zoom,
    basemap: v.basemap,
    globe: v.globe,
    cmp: v.compare ? (v.compare_mode ?? "split") : undefined,
    cmpLayer: v.compare,
    cmpT: v.compare_time,
    ov: v.overlay,
    vec: v.vectors,
  };
}

export function viewHash(v: FeaturedView): string {
  return buildHash(viewState(v));
}

/**
 * Preview image of the view, centred on its centre. Rendered about 1.6 zoom levels out, so the
 * 480 px image covers roughly what the view shows on a laptop screen (~1500 px).
 * `layer` overrides the view's layer (the B side of a comparison); `pane` draws one more pane.
 */
export function thumbnailUrl(v: FeaturedView, layer = v.layer, time = v.time, pane?: ViewPane): string {
  const e = pane?.error;
  if (pane && e?.reference) {
    // an error pane: the server's preview of the error map
    const [lng, lat] = v.center ?? [0, 20];
    const t = pane.time ?? v.time;
    return apiUrl("error/preview.png", {
      p: pane.layer,
      t: e.reference,
      pt: t,
      tt: pane.compare_time ?? t,
      metric: e.metric ?? "bias",
      formula: e.formula,
      params: e.params
        ? Object.entries(e.params)
            .map(([k, x]) => `${k}:${x}`)
            .join(",")
        : undefined,
      units: e.units,
      center: `${lng},${lat}`,
      zoom: Math.max(0, Math.min(22, Math.round((v.zoom ?? 1) - 1.6))),
      width: 480,
      height: 240,
      colormap: pane.colormap,
      vmin: pane.vmin,
      vmax: pane.vmax,
    });
  }
  if (pane)
    return thumbnailUrl({
      ...v,
      layer: pane.layer,
      time: pane.time ?? v.time,
      dims: pane.dims ?? v.dims,
      colormap: pane.colormap,
      vmin: pane.vmin,
      vmax: pane.vmax,
      log: pane.log,
    });
  const [lng, lat] = v.center ?? [0, 20];
  const own = layer === v.layer;
  return apiUrl(`layers/${encodeURIComponent(layer)}/preview.png`, {
    center: `${lng},${lat}`,
    zoom: Math.max(0, Math.min(22, Math.round((v.zoom ?? 1) - 1.6))),
    width: 480,
    height: 240,
    time,
    dims: v.dims
      ? Object.entries(v.dims)
          .map(([k, d]) => `${k}:${d}`)
          .join(",")
      : undefined,
    colormap: own ? v.colormap : undefined,
    vmin: own ? v.vmin : undefined,
    vmax: own ? v.vmax : undefined,
    scale: own && v.log ? "log" : undefined,
  });
}
