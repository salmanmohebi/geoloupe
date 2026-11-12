// Display names of layers.

import type { LayerMeta } from "../api/types";

/** Whether a layer title already names its dataset, e.g. "Land cover (ESA WorldCover 2021)" and
 * "ESA WorldCover 2021", or "(GFDL-ESM4 model)" and "GFDL-ESM4 climate model ocean". */
export function namesDataset(title: string, dataset: string): boolean {
  const a = title.toLowerCase();
  const b = dataset.split(" (")[0].toLowerCase();
  if (a.includes(b) || b.includes(a)) return true;
  const inner = /\(([^)]+)\)/.exec(a)?.[1] ?? "";
  const words = (s: string) =>
    new Set(s.split(/[^a-z0-9-]+/).filter((w) => w.length >= 4 && /[a-z]/.test(w)));
  const bw = words(b);
  return [...words(inner)].some((w) => bw.has(w));
}

/** A layer's name for panes and charts: its title, plus the dataset for one variable of one
 * dataset (the same variable may come from several datasets), unless the title names it. */
export function layerName(l: LayerMeta | null | undefined): string {
  if (!l) return "";
  const ds = l.kind === "dataset" ? l.sources[0]?.dataset_title : undefined;
  return ds && !namesDataset(l.title, ds) ? `${l.title} · ${ds.split(" (")[0]}` : l.title;
}
