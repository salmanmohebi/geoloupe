// Error definitions of error panes: P (compared data) against T (reference).
//
// The table is src/geoloupe/errors.json, shared with the server (geoloupe/errors.py), so the
// browser preview and the exact server maps use the same formulas.

import table from "../../../src/geoloupe/errors.json";
import { check, compile, parse, type PixelFn } from "./formula";

export type ErrorScale = "diverging" | "sequential" | "ratio" | "classes";

export interface ErrorParam {
  label: string;
  default: number;
  units: string; // "same": units of T
  auto?: string; // "std_T": can be set from the view's standard deviation of T
}

export interface ErrorClass {
  value: number;
  name: string;
  color: string;
}

export interface ErrorMetric {
  id: string;
  group: string;
  label: string;
  symbol: string;
  formula: string;
  units: string; // same | squared | % | "" | class
  scale: ErrorScale;
  params?: Record<string, ErrorParam>;
  classes?: ErrorClass[];
}

/** What a pane asks for: a metric id (or "custom" with a formula) and parameter values. */
export interface ErrorChoice {
  metric: string;
  formula?: string;
  params?: Record<string, number>;
  units?: string; // custom formulas: units of the result
}

export interface ResolvedError {
  metric: string;
  label: string;
  symbol: string;
  formula: string;
  params: Record<string, number>;
  units: string;
  scale: ErrorScale;
  classes: ErrorClass[];
  fn: PixelFn;
}

export const CUSTOM = "custom";
export const ERROR_METRICS = (table as { metrics: ErrorMetric[] }).metrics;
const BY_ID = new Map(ERROR_METRICS.map((m) => [m.id, m]));

export function errorMetric(id: string): ErrorMetric | undefined {
  return BY_ID.get(id);
}

/**
 * Resolve a choice into a ready-to-use definition. Missing parameters take their defaults;
 * unknown metrics or parameters and unreadable formulas throw (FormulaError for formulas).
 */
export function resolveError(choice: ErrorChoice): ResolvedError {
  const given = choice.params ?? {};
  for (const [k, v] of Object.entries(given))
    if (typeof v !== "number" || !Number.isFinite(v))
      throw new Error(`parameter '${k}' must be a finite number`);
  if (choice.metric === CUSTOM) {
    if (!choice.formula) throw new Error("a custom error needs a formula");
    if (Object.keys(given).length)
      throw new Error("a custom formula takes no parameters; write the numbers in it");
    const node = parse(choice.formula);
    check(node, ["P", "T"]);
    return {
      metric: CUSTOM,
      label: "Custom",
      symbol: choice.formula,
      formula: choice.formula,
      params: {},
      units: choice.units ?? "",
      scale: "diverging",
      classes: [],
      fn: compile(node),
    };
  }
  const m = BY_ID.get(choice.metric);
  if (!m) throw new Error(`unknown error '${choice.metric}'`);
  const declared = m.params ?? {};
  const unknown = Object.keys(given).filter((k) => !(k in declared));
  if (unknown.length) throw new Error(`error '${m.id}' has no parameter '${unknown[0]}'`);
  const params: Record<string, number> = {};
  for (const [k, p] of Object.entries(declared)) params[k] = given[k] ?? p.default;
  const node = parse(m.formula);
  check(node, ["P", "T", ...Object.keys(params)]);
  return {
    metric: m.id,
    label: m.label,
    symbol: m.symbol,
    formula: m.formula,
    params,
    units: m.units,
    scale: m.scale,
    classes: m.classes ?? [],
    fn: compile(node, params),
  };
}

/** Units of the error map, given the reference's units. */
export function errorUnits(e: Pick<ResolvedError, "units">, tUnits: string | null | undefined) {
  const u = tUnits ?? "";
  if (e.units === "same") return u;
  if (e.units === "squared") return /[ /]/.test(u) ? `(${u})²` : u ? `${u}²` : "";
  if (e.units === "class") return "";
  return e.units;
}

/** Error per pixel; NaN wherever P or T has no value. */
export function computeError(e: Pick<ResolvedError, "fn">, p: Float32Array, t: Float32Array) {
  const out = new Float32Array(p.length);
  const f = e.fn;
  for (let i = 0; i < out.length; i++) out[i] = f(p[i], t[i]);
  return out;
}
