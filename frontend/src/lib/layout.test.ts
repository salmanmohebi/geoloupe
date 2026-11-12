import { describe, expect, it } from "vitest";
import {
  close,
  decodeLayout,
  dividers,
  encodeLayout,
  equalize,
  neighbour,
  paneIds,
  preset,
  rects,
  setRatio,
  split,
  type Layout,
} from "./layout";

const one: Layout = { pane: "a" };

describe("split-pane layout", () => {
  it("splits right and down like tmux", () => {
    const l1 = split(one, "a", "row", "b"); // a | b
    const l2 = split(l1, "b", "col", "c"); // a | (b over c)
    expect(paneIds(l2)).toEqual(["a", "b", "c"]);
    const r = rects(l2);
    expect(r.a).toEqual({ x: 0, y: 0, w: 0.5, h: 1 });
    expect(r.b).toEqual({ x: 0.5, y: 0, w: 0.5, h: 0.5 });
    expect(r.c).toEqual({ x: 0.5, y: 0.5, w: 0.5, h: 0.5 });
    expect(dividers(l2).map((d) => [d.path, d.dir])).toEqual([
      ["", "row"],
      ["b", "col"],
    ]);
  });

  it("closing a pane gives its space to the sibling", () => {
    const l = split(split(one, "a", "row", "b"), "b", "col", "c");
    expect(paneIds(close(l, "b")!)).toEqual(["a", "c"]);
    expect(rects(close(l, "b")!).c).toEqual({ x: 0.5, y: 0, w: 0.5, h: 1 });
    expect(close(one, "a")).toBeNull();
    expect(close(l, "nope")).toEqual(l);
  });

  it("resizes one split and clamps the ratio", () => {
    const l = split(split(one, "a", "row", "b"), "b", "col", "c");
    const r = rects(setRatio(l, "b", 0.25));
    expect(r.b.h).toBeCloseTo(0.25);
    expect(r.c.h).toBeCloseTo(0.75);
    expect(rects(setRatio(l, "", 2)).a.w).toBeCloseTo(0.95);
    expect(rects(equalize(setRatio(l, "", 0.2))).a.w).toBe(0.5);
  });

  it("finds the neighbour on each side", () => {
    const l = preset("4", ["a", "b", "c", "d"]); // a b / c d
    expect(neighbour(l, "a", "right")).toBe("b");
    expect(neighbour(l, "a", "down")).toBe("c");
    expect(neighbour(l, "d", "left")).toBe("c");
    expect(neighbour(l, "d", "up")).toBe("b");
    expect(neighbour(l, "a", "left")).toBeNull();
    const three = preset("3", ["a", "b", "c"]); // a | (b over c)
    expect(neighbour(three, "c", "left")).toBe("a");
    expect(neighbour(three, "a", "right")).toBe("b"); // nearest of the two
  });

  it("round-trips through link text", () => {
    const l = setRatio(preset("3", ["x", "y", "z"]), "", 0.333);
    const s = encodeLayout(l, ["x", "y", "z"]);
    expect(s).toBe("r33.3(0,c50(1,2))");
    const back = decodeLayout(s)!;
    expect(paneIds(back)).toEqual(["p0", "p1", "p2"]);
    expect(rects(back).p0.w).toBeCloseTo(0.333);
    expect(encodeLayout(back, ["p0", "p1", "p2"])).toBe(s);
  });

  it("rejects malformed link text", () => {
    for (const bad of [
      "",
      "r50(0,1",
      "r50(0,0)",
      "x50(0,1)",
      "r50(0,1)junk",
      "9",
      "r50(0,c50(1,c50(2,c50(3,c50(4,c50(5,6))))))",
    ])
      expect(decodeLayout(bad)).toBeNull();
    expect(decodeLayout("0")).toEqual({ pane: "p0" });
  });
});

describe("featured views with panes", async () => {
  const { viewState, viewLayout } = await import("./gallery");
  it("become a link with the layout and one entry per pane", () => {
    const v = {
      id: 0,
      title: "four",
      layer: "elevation",
      layout: "4",
      panes: [
        { layer: "s2:rgb" },
        { layer: "fc", link_time: false, time: "2020" },
        { layer: "lc", compare: "x" },
      ],
    };
    const s = viewState(v);
    expect(s.lay).toBe("c50(r50(0,1),r50(2,3))");
    expect(s.pa).toBe(0);
    expect(s.panes?.[2]).toMatchObject({ layer: "fc", t: "2020", lt: false });
    expect(s.panes?.[3]).toMatchObject({ layer: "lc", cmp: "diff", cmpLayer: "x" });
    expect(viewLayout({ id: 1, title: "one", layer: "a" })).toBeNull();
    expect(viewState({ id: 2, title: "two", layer: "a", panes: [{ layer: "b" }] }).lay).toBe("r50(0,1)");
  });
});

describe("layer names", async () => {
  const { namesDataset } = await import("./layers");
  it("add the dataset only when the title does not name it", () => {
    expect(namesDataset("Land cover (ESA WorldCover 2021)", "ESA WorldCover 2021")).toBe(true);
    expect(
      namesDataset("Sea surface temperature (GFDL-ESM4 model)", "GFDL-ESM4 climate model ocean (CMIP6)"),
    ).toBe(true);
    expect(namesDataset("2 metre temperature", "ERA5 hourly reanalysis (ARCO-ERA5)")).toBe(false);
    expect(namesDataset("Near-surface air temperature", "Alps downscaled (synthetic)")).toBe(false);
  });
});
