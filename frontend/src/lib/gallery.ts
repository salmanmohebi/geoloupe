// Featured views (the gallery): a view is the state of a shareable link, so opening one is the same
// as opening that link. Thumbnails are preview images from the server, centred on the view.

import { apiUrl } from "../api/client";
import type { FeaturedView } from "../api/types";
import type { UrlState } from "../state/store";
import { buildHash } from "./urlState";

export function viewState(v: FeaturedView): Partial<UrlState> {
  return {
    layer: v.layer,
    t: v.time,
    dims: v.dims ? Object.fromEntries(Object.entries(v.dims).map(([k, x]) => [k, String(x)])) : undefined,
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
  };
}

export function viewHash(v: FeaturedView): string {
  return buildHash(viewState(v));
}

/**
 * Preview image of the view, centred on its centre. Rendered about 1.6 zoom levels out, so the
 * 480 px image covers roughly what the view shows on a laptop screen (~1500 px).
 * `layer` overrides the view's layer (the B side of a comparison).
 */
export function thumbnailUrl(v: FeaturedView, layer = v.layer, time = v.time): string {
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
