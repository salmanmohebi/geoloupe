// Smoother playback: once a frame is on screen, load the visible tiles of the next frame of every
// pane (each at its own next time, with its time offset) into the tile cache, without counting
// them as loading, so the animation does not wait for them.

import { paneIds } from "./layout";
import { getPaneMaps } from "./mapRegistry";
import { tileZoom, tilesInBounds } from "./mercator";
import { dataUrl, type DataRef } from "./protocol";
import { timelineOf } from "./timeline";
import { fetchTile } from "./tiles";
import { paneOf, shiftTime, useStore, validTime, type Target } from "../state/store";

const MAX_TILES = 64; // per pane and frame

/** A target's own time step nearest to `time` (static layers keep their time). */
function stepOf(t: Target, time: string): string | null {
  return t.layer && timelineOf(t.layer).count ? validTime(t.layer, time) : t.time;
}

/** Prefetch the next frame: the active pane at `next`, linked panes at the same group time. */
export function prefetchFrame(next: string): void {
  const s = useStore.getState();
  const maps = getPaneMaps();
  const base = shiftTime(next, s.timeOffset, -1);
  for (const id of paneIds(s.layout)) {
    const p = paneOf(s, id);
    const map = maps[id];
    if (!p || !map || !p.main.layer || p.main.layer.kind === "points") continue;
    if (id !== s.active && !p.linkTime) continue; // unlinked panes do not move
    const time = id === s.active ? next : shiftTime(base, p.timeOffset);
    const refs: DataRef[] = [];
    if (p.main.layerId) refs.push({ layerId: p.main.layerId, time: stepOf(p.main, time), dims: p.main.dims });
    const c = p.compare;
    if (c.mode === "error" && c.layerId && c.layer)
      refs.push({ layerId: c.layerId, time: stepOf(c, time), dims: c.dims });
    const layer = p.main.layer;
    const b = map.getBounds();
    const z = tileZoom(map.getZoom(), layer.tile_size, layer.maxzoom);
    const tiles = tilesInBounds(
      b.getWest(),
      Math.max(b.getSouth(), -85),
      b.getEast(),
      Math.min(b.getNorth(), 85),
      z,
    );
    for (const ref of refs)
      for (const tile of tiles.slice(0, MAX_TILES))
        void fetchTile(dataUrl(ref, tile.z, tile.x, tile.y), layer.tile_size, undefined, false).catch(
          () => undefined, // a failed prefetch is retried when the frame is shown
        );
  }
}
