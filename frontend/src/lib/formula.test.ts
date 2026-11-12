// The formula language and the error definitions, on the cases shared with the server
// (tests/formula_cases.json, also run by tests/test_formula.py).

import { describe, expect, it } from "vitest";
import cases from "../../../tests/formula_cases.json";
import { CUSTOM, ERROR_METRICS, computeError, errorUnits, resolveError } from "./errors";
import { check, compile, evaluateRaw, FormulaError, parse } from "./formula";

type Num = number | null;
const nan = (xs: Num[]) => xs.map((v) => (v === null ? NaN : v));

function same(got: ArrayLike<number>, want: Num[]) {
  expect(got.length).toBe(want.length);
  want.forEach((w, i) => {
    if (w === null) expect(got[i], `index ${i}`).toBeNaN();
    else expect(got[i], `index ${i}`).toBeCloseTo(w, 4);
  });
}

describe("formula values (shared cases)", () => {
  for (const c of cases.values as { formula: string; P: Num[]; T: Num[]; expect: Num[] }[]) {
    it(JSON.stringify(c.formula), () => {
      const node = parse(c.formula);
      check(node, ["P", "T"]);
      const P = nan(c.P);
      const T = nan(c.T);
      same(
        P.map((p, i) => evaluateRaw(node, p, T[i])),
        c.expect,
      );
    });
  }
});

describe("formula errors (shared cases)", () => {
  for (const c of cases.errors as { formula: string; message: string; pos: number | null }[]) {
    it(JSON.stringify(c.formula), () => {
      let err: unknown = null;
      try {
        check(parse(c.formula), ["P", "T"]);
      } catch (e) {
        err = e;
      }
      expect(err).toBeInstanceOf(FormulaError);
      expect((err as FormulaError).reason).toBe(c.message);
      expect((err as FormulaError).pos).toBe(c.pos);
    });
  }
});

describe("error definitions (shared cases)", () => {
  type Case = {
    metric: string;
    formula?: string;
    params?: Record<string, number>;
    P: Num[];
    T: Num[];
    expect: Num[];
  };
  for (const c of cases.metrics as Case[]) {
    it(`${c.metric} ${JSON.stringify(c.params ?? {})}`, () => {
      const e = resolveError({ metric: c.metric, formula: c.formula, params: c.params });
      same(computeError(e, new Float32Array(nan(c.P)), new Float32Array(nan(c.T))), c.expect);
    });
  }

  it("covers every metric", () => {
    const tested = new Set((cases.metrics as { metric: string }[]).map((c) => c.metric));
    expect([...ERROR_METRICS.map((m) => m.id), CUSTOM].sort()).toEqual([...tested].sort());
  });
});

describe("error definitions", () => {
  it("fills parameters with defaults", () => {
    expect(resolveError({ metric: "events" }).params).toEqual({ h: 1 });
    expect(resolveError({ metric: "rel", params: { eps: 0.1 } }).params).toEqual({ eps: 0.1 });
  });

  it("rejects what the server rejects", () => {
    expect(() => resolveError({ metric: "nope" })).toThrow("unknown error 'nope'");
    expect(() => resolveError({ metric: "rel", params: { h: 1 } })).toThrow("no parameter 'h'");
    expect(() => resolveError({ metric: "rel", params: { eps: NaN } })).toThrow("finite");
    expect(() => resolveError({ metric: CUSTOM })).toThrow("needs a formula");
    expect(() => resolveError({ metric: CUSTOM, formula: "P", params: { a: 1 } })).toThrow("no parameters");
    expect(() => resolveError({ metric: CUSTOM, formula: "P - eps" })).toThrow("unknown name 'eps'");
  });

  it("gives units", () => {
    const u = (metric: string, t: string) => errorUnits(resolveError({ metric }), t);
    expect(u("bias", "K")).toBe("K");
    expect(u("sq", "K")).toBe("K²");
    expect(u("sq", "mm/day")).toBe("(mm/day)²");
    expect(u("rel", "K")).toBe("%");
    expect(u("ratio", "K")).toBe("");
    expect(u("events", "mm/day")).toBe("");
    expect(errorUnits(resolveError({ metric: CUSTOM, formula: "P", units: "K" }), "mm")).toBe("K");
  });

  it("keeps missing inputs missing in compiled formulas", () => {
    const f = compile(parse("isnan(P)"));
    expect(f(NaN, 1)).toBeNaN();
    expect(f(1, 1)).toBe(0);
  });

  it("limits length and nesting", () => {
    expect(() => parse("P+".repeat(300) + "T")).toThrow("too long");
    expect(() => parse("(".repeat(60) + "P" + ")".repeat(60))).toThrow("nested too deeply");
    expect(() => parse("-".repeat(60) + "P")).toThrow("nested too deeply");
    expect(() => parse("(".repeat(30) + "P" + ")".repeat(30))).not.toThrow();
  });

  it("is fast enough for tiles", () => {
    const e = resolveError({ metric: "events", params: { h: 0.5 } });
    const n = 256 * 256;
    const p = new Float32Array(n).map(() => Math.random());
    const t = new Float32Array(n).map(() => Math.random());
    const t0 = performance.now();
    for (let k = 0; k < 10; k++) computeError(e, p, t);
    expect((performance.now() - t0) / 10).toBeLessThan(50); // ms per tile, generous for CI
  });
});
