// The split-pane layout, like tmux: a binary tree whose leaves are panes. A "row" split puts its
// two children side by side (split right), a "col" split stacks them (split down); `ratio` is the
// share of the first child. Pure functions: the store keeps the tree, the map view draws it.

export type Dir = "row" | "col";
export type Layout = { pane: string } | { dir: Dir; ratio: number; a: Layout; b: Layout };
export type Side = "left" | "right" | "up" | "down";
export type Preset = "1" | "2h" | "2v" | "3" | "4";

/** Position and size as fractions of the map area. */
export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** A draggable divider: `path` addresses its split ("" is the root, then "a"/"b" per level). */
export interface Divider {
  path: string;
  dir: Dir;
  ratio: number;
  parent: Rect; // the split's rectangle (the divider moves within it)
}

export const MAX_PANES = 6;
const MIN_RATIO = 0.05;

const isLeaf = (l: Layout): l is { pane: string } => "pane" in l;
const clampRatio = (r: number) => Math.min(1 - MIN_RATIO, Math.max(MIN_RATIO, r));

/** Pane ids, left to right and top to bottom. */
export function paneIds(l: Layout): string[] {
  return isLeaf(l) ? [l.pane] : [...paneIds(l.a), ...paneIds(l.b)];
}

export function rects(l: Layout, r: Rect = { x: 0, y: 0, w: 1, h: 1 }): Record<string, Rect> {
  if (isLeaf(l)) return { [l.pane]: r };
  const [ra, rb] = halves(l.dir, l.ratio, r);
  return { ...rects(l.a, ra), ...rects(l.b, rb) };
}

function halves(dir: Dir, ratio: number, r: Rect): [Rect, Rect] {
  return dir === "row"
    ? [
        { ...r, w: r.w * ratio },
        { ...r, x: r.x + r.w * ratio, w: r.w * (1 - ratio) },
      ]
    : [
        { ...r, h: r.h * ratio },
        { ...r, y: r.y + r.h * ratio, h: r.h * (1 - ratio) },
      ];
}

export function dividers(l: Layout, r: Rect = { x: 0, y: 0, w: 1, h: 1 }, path = ""): Divider[] {
  if (isLeaf(l)) return [];
  const [ra, rb] = halves(l.dir, l.ratio, r);
  return [
    { path, dir: l.dir, ratio: l.ratio, parent: r },
    ...dividers(l.a, ra, path + "a"),
    ...dividers(l.b, rb, path + "b"),
  ];
}

/** Split pane `id`: it keeps the first half, `newId` gets the second (right or below). */
export function split(l: Layout, id: string, dir: Dir, newId: string): Layout {
  if (isLeaf(l)) return l.pane === id ? { dir, ratio: 0.5, a: l, b: { pane: newId } } : l;
  return { ...l, a: split(l.a, id, dir, newId), b: split(l.b, id, dir, newId) };
}

/** Remove pane `id`; its sibling takes the parent's place. Null when it was the only pane. */
export function close(l: Layout, id: string): Layout | null {
  if (isLeaf(l)) return l.pane === id ? null : l;
  const a = close(l.a, id);
  const b = close(l.b, id);
  if (!a) return b;
  if (!b) return a;
  return { ...l, a, b };
}

export function setRatio(l: Layout, path: string, ratio: number): Layout {
  if (isLeaf(l)) return l;
  if (!path) return { ...l, ratio: clampRatio(ratio) };
  return path[0] === "a"
    ? { ...l, a: setRatio(l.a, path.slice(1), ratio) }
    : { ...l, b: setRatio(l.b, path.slice(1), ratio) };
}

/** Every split back to equal halves. */
export function equalize(l: Layout): Layout {
  return isLeaf(l) ? l : { ...l, ratio: 0.5, a: equalize(l.a), b: equalize(l.b) };
}

/** The pane next to `id` on one side (the nearest one that overlaps it), or null. */
export function neighbour(l: Layout, id: string, side: Side): string | null {
  const all = rects(l);
  const me = all[id];
  if (!me) return null;
  const eps = 1e-6;
  const cx = me.x + me.w / 2;
  const cy = me.y + me.h / 2;
  let best: string | null = null;
  let bestD = Infinity;
  for (const [other, r] of Object.entries(all)) {
    if (other === id) continue;
    const overlapsY = r.y < me.y + me.h - eps && r.y + r.h > me.y + eps;
    const overlapsX = r.x < me.x + me.w - eps && r.x + r.w > me.x + eps;
    const ok =
      side === "left"
        ? overlapsY && r.x + r.w <= me.x + eps
        : side === "right"
          ? overlapsY && r.x >= me.x + me.w - eps
          : side === "up"
            ? overlapsX && r.y + r.h <= me.y + eps
            : overlapsX && r.y >= me.y + me.h - eps;
    if (!ok) continue;
    const d = Math.hypot(r.x + r.w / 2 - cx, r.y + r.h / 2 - cy);
    if (d < bestD) [best, bestD] = [other, d];
  }
  return best;
}

/** Pane counts of the presets. */
export const PRESET_PANES: Record<Preset, number> = { "1": 1, "2h": 2, "2v": 2, "3": 3, "4": 4 };

/** A preset layout over the given pane ids (as many as the preset has; see PRESET_PANES). */
export function preset(p: Preset, ids: string[]): Layout {
  const [a, b, c, d] = ids.map((pane) => ({ pane }));
  switch (p) {
    case "1":
      return a;
    case "2h":
      return { dir: "row", ratio: 0.5, a, b };
    case "2v":
      return { dir: "col", ratio: 0.5, a, b };
    case "3": // one large pane on the left, two stacked on the right
      return { dir: "row", ratio: 0.5, a, b: { dir: "col", ratio: 0.5, a: b, b: c } };
    case "4":
      return {
        dir: "col",
        ratio: 0.5,
        a: { dir: "row", ratio: 0.5, a, b },
        b: { dir: "row", ratio: 0.5, a: c, b: d },
      };
  }
}

/** Compact text for links: "0" a pane (its index in `order`), "r50(…,…)" / "c50(…,…)" a split. */
export function encodeLayout(l: Layout, order: string[]): string {
  if (isLeaf(l)) return String(order.indexOf(l.pane));
  const pct = Math.round(l.ratio * 1000) / 10;
  return `${l.dir === "row" ? "r" : "c"}${pct}(${encodeLayout(l.a, order)},${encodeLayout(l.b, order)})`;
}

/** The layout of a link, with pane ids "p0", "p1", … from the indices; null if malformed. */
export function decodeLayout(s: string): Layout | null {
  let i = 0;
  const node = (depth: number): Layout | null => {
    if (depth > MAX_PANES) return null;
    const m = /^(\d+)/.exec(s.slice(i));
    if (m) {
      i += m[1].length;
      return Number(m[1]) < MAX_PANES ? { pane: `p${m[1]}` } : null;
    }
    const sm = /^([rc])(\d+(?:\.\d+)?)\(/.exec(s.slice(i));
    if (!sm) return null;
    i += sm[0].length;
    const a = node(depth + 1);
    if (!a || s[i++] !== ",") return null;
    const b = node(depth + 1);
    if (!b || s[i++] !== ")") return null;
    return { dir: sm[1] === "r" ? "row" : "col", ratio: clampRatio(Number(sm[2]) / 100), a, b };
  };
  const out = node(0);
  if (!out || i !== s.length) return null;
  const ids = paneIds(out);
  return new Set(ids).size === ids.length && ids.length <= MAX_PANES ? out : null;
}
