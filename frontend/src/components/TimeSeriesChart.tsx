// Time series of every source of a layer at one location (uPlot).
//
// The x axis is the index of the time labels (not epoch seconds), so model
// calendars such as 360_day or noleap plot correctly. Colours follow the
// source (categorical slots 1–3), never its rank in the data.

import { useEffect, useMemo, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import type { Series } from "../api/types";
import { fmt } from "../lib/format";
import { useLatest } from "../lib/useLatest";

export const SERIES_COLORS_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"];
export const SERIES_COLORS_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500"];

export function seriesColor(i: number): string {
  const dark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  const c = dark ? SERIES_COLORS_DARK : SERIES_COLORS_LIGHT;
  return c[i % c.length];
}

interface Props {
  series: Series[];
  names: string[];
  units: string;
  current: string | null;
  onPick?: (time: string) => void;
  height?: number;
}

export function mergeSeries(series: Series[]): { labels: string[]; data: (number | null)[][] } {
  const set = new Set<string>();
  for (const s of series) for (const t of s.times) set.add(t);
  const labels = [...set].sort();
  const index = new Map(labels.map((t, i) => [t, i]));
  const data = series.map((s) => {
    const arr: (number | null)[] = new Array(labels.length).fill(null);
    s.times.forEach((t, i) => (arr[index.get(t)!] = s.values[i]));
    return arr;
  });
  return { labels, data };
}

export function TimeSeriesChart({ series, names, units, current, onPick, height = 220 }: Props) {
  const el = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  const merged = useMemo(() => mergeSeries(series), [series]);
  const currentRef = useLatest(current);
  const pickRef = useLatest(onPick);

  useEffect(() => {
    if (!el.current) return;
    const { labels, data } = merged;
    const xs = labels.map((_, i) => i);
    const css = getComputedStyle(document.documentElement);
    const ink = css.getPropertyValue("--text-secondary").trim() || "#555";
    const grid = css.getPropertyValue("--grid").trim() || "rgba(128,128,128,0.15)";
    const opts: uPlot.Options = {
      width: el.current.clientWidth,
      height,
      padding: [8, 8, 0, 0],
      cursor: { drag: { x: true, y: false }, points: { size: 7 } },
      legend: { show: true, live: true },
      scales: { x: { time: false } },
      axes: [
        {
          stroke: ink,
          grid: { stroke: grid, width: 1 },
          ticks: { stroke: grid },
          values: (_u, splits) => splits.map((v) => labels[Math.round(v)] ?? ""),
          space: 90,
          font: "11px system-ui, sans-serif",
        },
        {
          stroke: ink,
          grid: { stroke: grid, width: 1 },
          ticks: { stroke: grid },
          values: (_u, splits) => splits.map((v) => fmt(v, 4)),
          size: 54,
          font: "11px system-ui, sans-serif",
        },
      ],
      series: [
        { label: "time", value: (_u, v) => (v === null ? "–" : (labels[Math.round(v)] ?? "")) },
        ...names.map((n, i) => ({
          label: n,
          stroke: seriesColor(i),
          width: 2,
          spanGaps: false,
          points: { show: false },
          value: (_u: uPlot, v: number | null) => (v === null ? "–" : `${fmt(v)} ${units}`),
        })),
      ],
      hooks: {
        draw: [
          (u) => {
            const t = currentRef.current;
            const i = t ? labels.indexOf(t) : -1;
            if (i < 0) return;
            const x = Math.round(u.valToPos(i, "x", true));
            const ctx = u.ctx;
            ctx.save();
            ctx.strokeStyle = ink;
            ctx.setLineDash([4, 3]);
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(x, u.bbox.top);
            ctx.lineTo(x, u.bbox.top + u.bbox.height);
            ctx.stroke();
            ctx.restore();
          },
        ],
      },
    };
    plot.current?.destroy();
    plot.current = new uPlot(opts, [xs, ...data] as uPlot.AlignedData, el.current);
    const over = plot.current.over;
    const onClick = () => {
      const i = plot.current?.cursor.idx;
      if (i !== null && i !== undefined && labels[i]) pickRef.current?.(labels[i]);
    };
    over.addEventListener("click", onClick);
    const ro = new ResizeObserver(() => {
      if (el.current && plot.current) plot.current.setSize({ width: el.current.clientWidth, height });
    });
    ro.observe(el.current);
    return () => {
      over.removeEventListener("click", onClick);
      ro.disconnect();
      plot.current?.destroy();
      plot.current = null;
    };
  }, [merged, names, units, height, currentRef, pickRef]);

  useEffect(() => {
    plot.current?.redraw(false);
  }, [current]);

  return <div ref={el} className="ts-chart" />;
}
