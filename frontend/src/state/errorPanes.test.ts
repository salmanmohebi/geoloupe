// Error panes in the store: created from two panes, they follow them; closing a followed pane
// keeps the layer; links keep the setup.

import { beforeEach, describe, expect, it } from "vitest";
import type { LayerMeta } from "../api/types";
import { decodeLayout } from "../lib/layout";
import { drawTitle, drawUnits, fitErrorRange, paneError } from "../lib/paneDraw";
import { combine } from "../lib/protocol";
import { buildHash, parseHash } from "../lib/urlState";
import { linkState, paneOf, snapshot, useStore, type PaneState, type Target } from "./store";

const meta = (id: string, units: string, extra: Partial<LayerMeta> = {}) =>
  ({
    id,
    title: id.toUpperCase(),
    kind: "dataset",
    units,
    bands: null,
    classes: null,
    dims: {},
    times: ["2020-01-01", "2020-01-02"],
    time_axis: null,
    time_step: "1 day",
    tile_size: 256,
    sources: [],
    datasets: [id.split(":")[0]],
    style: { vmin: null, vmax: null, colormap: null, gamma: null },
    ...extra,
  }) as unknown as LayerMeta;

const target = (layer: LayerMeta, time = "2020-01-01"): Target => ({
  layerId: layer.id,
  layer,
  time,
  dims: {},
});

const TRUTH = meta("obs:tas", "K");
const MODEL = meta("model:tas", "degC");
const OTHER = meta("model:pr", "mm/day");

const INITIAL = useStore.getState();

/** Two panes side by side: p0 = truth (active), p1 = model. */
function twoPanes() {
  useStore.setState(INITIAL, true);
  const s0 = useStore.getState();
  const base = snapshot(s0);
  const p1: PaneState = { ...base, main: target(MODEL), styleReady: true };
  useStore.setState({
    layout: decodeLayout("r50(0,1)")!,
    active: "p0",
    main: target(TRUTH),
    styleReady: true,
    panes: { p1 },
    zoomed: null,
    notices: [],
  });
}

beforeEach(twoPanes);

describe("error panes", () => {
  it("are created from two panes and follow them", () => {
    useStore.getState().addErrorPane("p1", "p0", { metric: "abs" });
    let s = useStore.getState();
    expect(s.active).toBe("p2"); // the new pane is active
    expect(s.main.layerId).toBe("model:tas"); // P
    expect(s.compare.layerId).toBe("obs:tas"); // T
    expect(s.compare.mode).toBe("error");
    expect(s.compare.follow).toEqual({ p: "p1", t: "p0" });
    expect(s.diffStyle.colormap).toBe("YlOrRd"); // sizes of errors: sequential
    expect(drawTitle(snapshot(s))).toBe("|P − T| · MODEL:TAS vs OBS:TAS");
    expect(drawUnits(snapshot(s))).toBe("K");
    expect(paneError(snapshot(s))?.conv).toMatchObject({ scale: 1, offset: 273.15 });

    // pane 1 changes layer and time: the error pane follows
    useStore.setState({
      panes: { ...s.panes, p1: { ...s.panes.p1, main: target(OTHER, "2020-01-02") } },
    });
    s = useStore.getState();
    expect(s.main.layerId).toBe("model:pr");
    expect(s.main.time).toBe("2020-01-02");
    expect(s.diffFitPending).toBe(true); // a new layer: refit the range
    expect(paneError(snapshot(s))?.conv.warning).toContain("units differ");
  });

  it("follow the active pane too", () => {
    useStore.getState().addErrorPane("p1", "p0");
    useStore.getState().activatePane("p0"); // the truth pane is active now
    useStore.getState().setTime("2020-01-02");
    const s = useStore.getState();
    expect(s.panes.p2.compare.time).toBe("2020-01-02");
    expect(s.panes.p2.main.time).toBe("2020-01-02"); // model pane is time-linked
  });

  it("keep the layer of a closed pane", () => {
    useStore.getState().addErrorPane("p1", "p0");
    useStore.getState().closePane("p1");
    const s = useStore.getState();
    const err = paneOf(s, "p2")!;
    expect(err.compare.follow).toEqual({ p: null, t: "p0" });
    expect(err.main.layerId).toBe("model:tas");
    // the new pane split pane 1, so the order is p0, p2, p1: the closed pane was number 3
    expect(s.notices.at(-1)?.text).toContain("Pane 3 was closed");
  });

  it("stop following P when its layer is chosen by hand", async () => {
    useStore.getState().addErrorPane("p1", "p0");
    // setLayer needs the server for the layer itself; only the unfollow step matters here
    const s = useStore.getState();
    useStore.setState({ compare: { ...s.compare } });
    await useStore
      .getState()
      .setLayer("obs:tas")
      .catch(() => undefined);
    expect(useStore.getState().compare.follow.p).toBeNull();
  });

  it("change definition with fitting colours", () => {
    useStore.getState().addErrorPane("p1", "p0");
    expect(useStore.getState().diffStyle.colormap).toBe("RdBu_r");
    useStore.getState().setError({ metric: "ratio" });
    let s = useStore.getState();
    expect(s.diffStyle).toMatchObject({ log: true, colormap: "RdBu_r" });
    useStore.getState().setError({ metric: "events", params: { h: 5 } });
    s = useStore.getState();
    expect(s.compare.error).toEqual({ metric: "events", params: { h: 5 } });
    expect(drawTitle(snapshot(s))).toContain("Event agreement (h = 5)");
  });

  it("round-trip through a link, with followed panes by index", () => {
    useStore.getState().addErrorPane("p1", "p0", { metric: "rel", params: { eps: 0.5 } });
    const link = linkState(useStore.getState());
    // layout order p0, p2 (the error pane), p1
    expect(link).toMatchObject({ cmp: "error", em: "rel", ep: { eps: 0.5 }, fp: 2, ft: 0 });
    const back = parseHash(buildHash(link));
    expect(back).toMatchObject({ cmp: "error", em: "rel", ep: { eps: 0.5 }, fp: 2, ft: 0 });
    expect(back.cmpLayer).toBe("obs:tas");
    const custom = parseHash(
      buildHash({ layer: "a", cmp: "error", em: "custom", ef: "abs(P - T) > 2", eu: "1" }),
    );
    expect(custom).toMatchObject({ em: "custom", ef: "abs(P - T) > 2", eu: "1" });
  });

  it("read links from 0.2 (cmp=diff) as P − T", () => {
    const u = parseHash("#layer=a&cmp=diff&cl=b");
    expect(u).toMatchObject({ cmp: "diff", cmpLayer: "b" });
  });
});

describe("error tiles", () => {
  const tile = (values: number[]) => ({ values: Float32Array.from(values) }) as never;
  it("convert P into T's units, then apply the definition", () => {
    useStore.getState().addErrorPane("p1", "p0", { metric: "bias" });
    const pe = paneError(snapshot(useStore.getState()))!;
    const out = combine(tile([20, NaN]), tile([290, 290]), {
      fn: pe.e!.fn,
      scale: pe.conv.scale,
      offset: pe.conv.offset,
      key: pe.key,
      server: null,
    });
    expect(out![0]).toBeCloseTo(3.15, 4); // 20 °C = 293.15 K
    expect(out![1]).toBeNaN();
  });

  it("fit ranges by kind", () => {
    const sample = [-3, -1, 0, 1, 2, 4];
    expect(fitErrorRange({ metric: "bias" }, sample)).toMatchObject({ vmin: -3, vmax: 3 }); // 2–98 %
    expect(fitErrorRange({ metric: "abs" }, [0, 1, 2, 5])).toMatchObject({ vmin: 0, vmax: 2 });
    expect(fitErrorRange({ metric: "ratio" }, [0.5, 1, 2])).toMatchObject({ log: true, vmin: 0.5, vmax: 2 });
    expect(fitErrorRange({ metric: "events" }, [1, 2])).toBeNull();
    expect(fitErrorRange({ metric: "custom", formula: "abs(P)" }, [0, 3])).toMatchObject({ vmin: 0 });
  });
});
