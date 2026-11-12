// With several panes, the point details also show every pane at the clicked point: its value now,
// and its time series around the current time. Panes with the same units and time step share a
// chart; others get their own (hourly and monthly data on one axis would mislead).

import { useMemo } from "react";
import { useShallow } from "zustand/react/shallow";
import { api } from "../api/client";
import type { LayerMeta, PointResult, Series, TimeSeriesResult } from "../api/types";
import { fmt, unitLabel } from "../lib/format";
import { layerName } from "../lib/layers";
import { drawTitle, drawUnits, paneError } from "../lib/paneDraw";
import { paneIds } from "../lib/layout";
import { timelineOf, type Timeline } from "../lib/timeline";
import { useFetch } from "../lib/useFetch";
import { useStore, type AppState, type Inspect } from "../state/store";
import { seriesColor, TimeSeriesChart } from "./TimeSeriesChart";

const WINDOW = 730; // time steps around the current time, as in the active pane's chart

interface PaneAt {
  n: number; // pane number (1, 2, …)
  layer: LayerMeta;
  time: string | null;
  dims: Record<string, string>;
  error: AppState["compare"] | null; // error panes: T and the definition (the layer is P)
}

function windowAround(tl: Timeline, current: string | null): [string, string] {
  const i = current ? tl.nearest(current) : tl.count - 1;
  const lo = Math.max(0, Math.min(i - Math.floor(WINDOW / 2), tl.count - WINDOW));
  return [tl.at(lo), tl.at(Math.min(tl.count - 1, lo + WINDOW - 1))];
}

/** The series the map shows at the point: the first source (finest first) with any value. */
function shownSeries(ts: TimeSeriesResult | undefined): Series | null {
  return ts?.series.find((s) => s.values.some((v) => v !== null)) ?? null;
}

export function PaneValues({ inspect }: { inspect: Inspect }) {
  // flat list of stable references (layer, time, dims per pane), so the selection compares cheaply
  const flat = useStore(
    useShallow((s) =>
      paneIds(s.layout).flatMap((id) => {
        const p = id === s.active ? s.main : s.panes[id]?.main;
        const c = id === s.active ? s.compare : s.panes[id]?.compare;
        return [p?.layer ?? null, p?.time ?? null, p?.dims ?? null, c?.mode === "error" ? c : null];
      }),
    ),
  );
  const { setTime } = useStore.getState();
  const panes: PaneAt[] = useMemo(() => {
    const out: PaneAt[] = [];
    for (let i = 0; i < flat.length; i += 4) {
      const layer = flat[i] as LayerMeta | null;
      if (layer && layer.kind !== "points")
        out.push({
          n: i / 4 + 1,
          layer,
          time: flat[i + 1] as string | null,
          dims: (flat[i + 2] ?? {}) as Record<string, string>,
          error: flat[i + 3] as AppState["compare"] | null,
        });
    }
    return out;
  }, [flat]);

  // values now
  const pointKey = JSON.stringify([
    inspect.lng,
    inspect.lat,
    panes.map((p) => [p.layer.id, p.time, p.dims, p.error?.layerId, p.error?.time, p.error?.dims]),
  ]);
  const { data: points } = useFetch<(PointResult | null)[]>(pointKey, (signal) =>
    Promise.all(
      panes.map((p) =>
        api.point(p.layer.id, inspect.lng, inspect.lat, p.time, p.dims, signal).catch(() => null),
      ),
    ),
  );
  // error panes also need T's value
  const { data: refPoints } = useFetch<(PointResult | null)[]>(pointKey, (signal) =>
    Promise.all(
      panes.map((p) =>
        p.error?.layerId
          ? api
              .point(p.error.layerId, inspect.lng, inspect.lat, p.error.time, p.error.dims, signal)
              .catch(() => null)
          : Promise.resolve(null),
      ),
    ),
  );

  // series around each pane's time (chosen when the point or the panes' layers change, not on
  // every time step)
  const layerIds = panes.map((p) => p.layer.id).join();
  const windows = useMemo(
    () =>
      panes.map((p) => (timelineOf(p.layer).count > 1 ? windowAround(timelineOf(p.layer), p.time) : null)),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- layerIds stands for the panes
    [inspect, layerIds],
  );
  const tsKey = JSON.stringify([
    inspect.lng,
    inspect.lat,
    panes.map((p, i) => [p.layer.id, p.dims, windows[i]]),
  ]);
  const { data: series, loading } = useFetch<(TimeSeriesResult | null)[]>(tsKey, (signal) =>
    Promise.all(
      panes.map((p, i) =>
        windows[i] && !p.error
          ? api
              .timeseries(
                p.layer.id,
                inspect.lng,
                inspect.lat,
                windows[i]![0],
                windows[i]![1],
                p.dims,
                signal,
              )
              .catch(() => null)
          : Promise.resolve(null),
      ),
    ),
  );

  // one chart per (units, time step)
  const charts = useMemo(() => {
    const groups = new Map<string, { units: string; series: Series[]; names: string[]; colors: string[] }>();
    panes.forEach((p, i) => {
      const s = shownSeries(series?.[i] ?? undefined);
      if (!s) return;
      const units = unitLabel(p.layer.units);
      const key = `${units}|${p.layer.time_step ?? ""}`;
      if (!groups.has(key)) groups.set(key, { units, series: [], names: [], colors: [] });
      const g = groups.get(key)!;
      g.series.push(s);
      g.names.push(`${p.n} · ${layerName(p.layer)}`);
      g.colors.push(seriesColor(p.n - 1));
    });
    return [...groups.values()];
  }, [panes, series]);

  if (panes.length < 2) return null;
  const current = panes[0]?.time ?? null;
  return (
    <section className="pane-values">
      <div className="section-head">
        <span>All panes at this point</span>
      </div>
      <table className="value-table">
        <tbody>
          {panes.map((p, i) => {
            let pt = points?.[i];
            let classes = p.layer.classes;
            let name = layerName(p.layer);
            let units = unitLabel(p.layer.units);
            if (p.error) {
              // the error of P against T at this point, from their native values
              const draw = {
                main: { layerId: p.layer.id, layer: p.layer, time: p.time, dims: p.dims },
                compare: p.error,
              };
              const pe = paneError(draw);
              const t = refPoints?.[i];
              const v =
                pe?.e && pt?.value != null && t?.value != null
                  ? pe.e.fn(pt.value * pe.conv.scale + pe.conv.offset, t.value)
                  : null;
              pt =
                pt === undefined || t === undefined
                  ? undefined
                  : pt && { ...pt, value: v !== null && Number.isFinite(v) ? v : null };
              classes = pe?.e?.classes.length ? pe.e.classes.map((c) => ({ ...c, color: c.color })) : null;
              name = drawTitle(draw);
              units = unitLabel(drawUnits(draw));
            }
            const cls =
              classes && pt?.value != null ? classes.find((c) => c.value === Math.round(pt!.value!)) : null;
            return (
              <tr key={p.n}>
                <td>
                  <span className="pane-tag" style={{ background: seriesColor(p.n - 1) }}>
                    {p.n}
                  </span>
                  {name}
                  {p.time && <div className="muted small">{p.time}</div>}
                </td>
                <td className="num">
                  {pt === undefined ? (
                    "…"
                  ) : pt === null || pt.value === null ? (
                    "–"
                  ) : (
                    <b>{cls ? cls.name : fmt(pt.value)}</b>
                  )}{" "}
                  {pt?.value != null && !cls && <span className="muted small">{units}</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {loading && <div className="skeleton chart-skeleton">Reading the time series…</div>}
      {charts.map((c) => (
        <TimeSeriesChart
          key={c.names.join()}
          series={c.series}
          names={c.names}
          colors={c.colors}
          units={c.units}
          current={current}
          onPick={setTime}
          height={charts.length > 1 ? 150 : 200}
        />
      ))}
      {charts.length > 1 && (
        <p className="muted small">
          One chart per unit and time step. Click a chart to move the active pane in time.
        </p>
      )}
    </section>
  );
}
