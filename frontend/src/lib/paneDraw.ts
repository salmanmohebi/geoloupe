// What a pane draws: its own layer, or an error map of P (the pane's layer) against T (the
// reference). One place for the colour scale, title, units and range fit of either kind, used by
// the map, the colour bar, the hover read-out, the side panel and the image export.

import type { PaneState, Style } from "../state/store";
import {
  conversion,
  CUSTOM,
  errorMetric,
  errorUnits,
  resolveError,
  type Conversion,
  type ErrorChoice,
  type ErrorScale,
  type ResolvedError,
} from "./errors";
import { layerName } from "./layers";

type Pane = Pick<PaneState, "main" | "compare" | "style" | "diffStyle">;

export const isErrorPane = (p: Pick<PaneState, "compare">) => p.compare.mode === "error";

/** The colour scale the pane draws with. */
export const drawStyle = (p: Pane): Style => (isErrorPane(p) ? p.diffStyle : p.style);

export interface PaneError {
  e: ResolvedError | null; // null: the definition cannot be used (see problem)
  conv: Conversion; // P's values into T's units
  problem: string | null;
  key: string; // stable identity of definition + conversion (for render keys)
}

const cache = new Map<string, PaneError>();

/** The resolved error definition of an error pane (cached), or null for other panes. */
export function paneError(p: Pick<PaneState, "main" | "compare">): PaneError | null {
  if (!isErrorPane(p)) return null;
  const choice = p.compare.error;
  const pu = p.main.layer?.units ?? "";
  const tu = p.compare.layer?.units ?? "";
  const key = JSON.stringify([
    choice.metric,
    choice.formula ?? "",
    choice.params ?? {},
    choice.units ?? "",
    pu,
    tu,
  ]);
  const hit = cache.get(key);
  if (hit) return hit;
  let e: ResolvedError | null = null;
  let problem: string | null = null;
  try {
    e = resolveError(choice);
  } catch (err) {
    problem = (err as Error).message;
  }
  const out: PaneError = { e, conv: conversion(pu, tu), problem, key };
  cache.set(key, out);
  if (cache.size > 64) cache.delete(cache.keys().next().value as string);
  return out;
}

/** Short name of an error definition, e.g. "|P − T|" or "Event agreement (P ≥ 1 vs T ≥ 1)". */
export function errorName(choice: ErrorChoice): string {
  if (choice.metric === CUSTOM) return choice.formula || "custom formula";
  const m = errorMetric(choice.metric);
  if (!m) return choice.metric;
  if (m.scale === "classes") {
    const [k, v] = Object.entries({ ...defaults(m.id), ...choice.params })[0] ?? [];
    return k ? `${m.label} (${k} = ${v})` : m.label;
  }
  return m.symbol;
}

function defaults(id: string): Record<string, number> {
  const m = errorMetric(id);
  return Object.fromEntries(Object.entries(m?.params ?? {}).map(([k, p]) => [k, p.default]));
}

/** The pane's title: its layer, or "<error> · P-layer vs T-layer". */
export function drawTitle(p: Pick<PaneState, "main" | "compare">): string {
  if (!isErrorPane(p)) return layerName(p.main.layer);
  return `${errorName(p.compare.error)} · ${layerName(p.main.layer)} vs ${layerName(p.compare.layer)}`;
}

/** Units of what the pane draws. */
export function drawUnits(p: Pick<PaneState, "main" | "compare">): string {
  const pe = paneError(p);
  if (!pe) return p.main.layer?.units ?? "";
  if (!pe.e) return "";
  return errorUnits(pe.e, p.compare.layer?.units ?? p.main.layer?.units);
}

export function errorScaleOf(choice: ErrorChoice): ErrorScale {
  if (choice.metric === CUSTOM) return "diverging";
  return errorMetric(choice.metric)?.scale ?? "diverging";
}

/** The default colour scale of an error definition (the range is fitted to the view later). */
export function errorStyleFor(choice: ErrorChoice, base: Style): Style {
  switch (errorScaleOf(choice)) {
    case "sequential":
      return { ...base, colormap: "YlOrRd", vmin: 0, vmax: 1, log: false };
    case "ratio":
      return { ...base, colormap: "RdBu_r", vmin: 0.5, vmax: 2, log: true };
    case "classes":
      return { ...base, log: false };
    default:
      return { ...base, colormap: "RdBu_r", vmin: -1, vmax: 1, log: false };
  }
}

function quantile(sorted: Float32Array, q: number): number {
  return sorted[Math.floor(q * (sorted.length - 1))];
}

const round = (v: number) => Number(v.toPrecision(2));

/**
 * A range for the error values on screen: symmetric around 0 for signed errors, from 0 for
 * sizes of errors, symmetric around 1 on a log scale for ratios. Null when nothing to fit.
 */
export function fitErrorRange(
  choice: ErrorChoice,
  sample: ArrayLike<number>,
): Pick<Style, "vmin" | "vmax" | "log"> | null {
  const scale = errorScaleOf(choice);
  const v = Float32Array.from(sample).filter((x) => Number.isFinite(x));
  if (!v.length || scale === "classes") return null;
  if (scale === "ratio") {
    const logs = v.filter((x) => x > 0).map((x) => Math.abs(Math.log(x)));
    if (!logs.length) return null;
    const r = quantile(logs.sort(), 0.98) || Math.log(2);
    return { vmin: round(Math.exp(-r)), vmax: round(Math.exp(r)), log: true };
  }
  const sorted = v.slice().sort();
  const lo = quantile(sorted, 0.02);
  const hi = quantile(sorted, 0.98);
  if (scale === "sequential" || (choice.metric === CUSTOM && sorted[0] >= 0)) {
    // sizes of errors (or a custom formula that is never negative) start at 0
    const top = round(hi > 0 ? hi : 1);
    return { vmin: 0, vmax: top, log: false };
  }
  const m = round(Math.max(Math.abs(lo), Math.abs(hi)) || 1);
  return { vmin: -m, vmax: m, log: false };
}
