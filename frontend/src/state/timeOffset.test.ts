// Time offsets between linked panes ("the same day, 30 years earlier").

import { beforeEach, describe, expect, it } from "vitest";
import type { LayerMeta } from "../api/types";
import { decodeLayout } from "../lib/layout";
import { buildHash, parseHash } from "../lib/urlState";
import { fmtOffset, linkState, parseOffset, shiftTime, snapshot, useStore, type PaneState } from "./store";

describe("shiftTime", () => {
  it("keeps the form of the label", () => {
    expect(shiftTime("2021-07-15", { n: -30, unit: "y" })).toBe("1991-07-15");
    expect(shiftTime("2021-07", { n: -30, unit: "y" })).toBe("1991-07");
    expect(shiftTime("2021", { n: 2, unit: "y" })).toBe("2023");
    expect(shiftTime("2021-07-15T12:00", { n: 6, unit: "h" })).toBe("2021-07-15T18:00");
    expect(shiftTime("2021-07-15T12:00:30", { n: -1, unit: "d" })).toBe("2021-07-14T12:00:30");
    expect(shiftTime("2014-08-16T12:00", { n: 1, unit: "m" })).toBe("2014-09-16T12:00");
  });
  it("keeps the day within the month", () => {
    expect(shiftTime("2021-01-31", { n: 1, unit: "m" })).toBe("2021-02-28");
    expect(shiftTime("2020-02-29", { n: 1, unit: "y" })).toBe("2021-02-28");
    expect(shiftTime("2021-03-31", { n: -1, unit: "m" })).toBe("2021-02-28");
    expect(shiftTime("2021-01-15", { n: -13, unit: "m" })).toBe("2019-12-15");
  });
  it("crosses days, months and years", () => {
    expect(shiftTime("2021-12-31T23:00", { n: 2, unit: "h" })).toBe("2022-01-01T01:00");
    expect(shiftTime("2021-03-01", { n: -1, unit: "d" })).toBe("2021-02-28");
    expect(shiftTime("0950-06-01", { n: -1000, unit: "y" })).toBe("-0050-06-01");
  });
  it("is undone by the opposite shift", () => {
    const o = { n: -30, unit: "y" } as const;
    expect(shiftTime(shiftTime("2021-07-15", o), o, -1)).toBe("2021-07-15");
    expect(shiftTime("2021-07-15", null)).toBe("2021-07-15");
  });
  it("reads and writes offsets", () => {
    expect(parseOffset("-30y")).toEqual({ n: -30, unit: "y" });
    expect(parseOffset("6m")).toEqual({ n: 6, unit: "m" });
    expect(parseOffset("0d")).toBeNull();
    expect(parseOffset("x")).toBeNull();
    expect(fmtOffset({ n: -30, unit: "y" })).toBe("−30 y");
    expect(fmtOffset({ n: 6, unit: "m" })).toBe("+6 mo");
  });
});

const daily = (id: string, first: number, last: number) =>
  ({
    id,
    title: id,
    kind: "dataset",
    units: "K",
    dims: {},
    times: Array.from({ length: last - first + 1 }, (_, i) => `${first + i}-07-15`),
    time_axis: null,
    time_step: "1 year",
    tile_size: 256,
    sources: [],
    style: { vmin: null, vmax: null },
  }) as unknown as LayerMeta;

const INITIAL = useStore.getState();

describe("linked panes with an offset", () => {
  beforeEach(() => {
    useStore.setState(INITIAL, true);
    const now = daily("a", 1950, 2021);
    const base = snapshot(useStore.getState());
    const p1: PaneState = {
      ...base,
      main: { layerId: "a", layer: now, time: "2021-07-15", dims: {} },
      styleReady: true,
    };
    useStore.setState({
      layout: decodeLayout("r50(0,1)")!,
      active: "p0",
      main: { layerId: "a", layer: now, time: "2021-07-15", dims: {} },
      panes: { p1 },
    });
  });

  it("show the group's time shifted, both ways", () => {
    useStore.getState().activatePane("p1");
    useStore.getState().setTimeOffset({ n: -30, unit: "y" });
    expect(useStore.getState().main.time).toBe("1991-07-15");
    useStore.getState().activatePane("p0");
    useStore.getState().setTime("2000-07-15");
    expect(useStore.getState().panes.p1.main.time).toBe("1970-07-15");
    // moving the shifted pane moves the group by the same step
    useStore.getState().activatePane("p1");
    useStore.getState().setTime("1980-07-15");
    expect(useStore.getState().panes.p0.main.time).toBe("2010-07-15");
  });

  it("snap to the pane's own time steps, and keep the offset in links", () => {
    useStore.getState().activatePane("p1");
    useStore.getState().setTimeOffset({ n: -100, unit: "y" }); // before 1950: first step
    expect(useStore.getState().main.time).toBe("1950-07-15");
    const link = linkState(useStore.getState());
    expect(link.to).toBe("-100y");
    expect(parseHash(buildHash(link)).to).toBe("-100y");
    useStore.getState().setTimeOffset(null);
    expect(useStore.getState().main.time).toBe("2021-07-15");
  });
});
