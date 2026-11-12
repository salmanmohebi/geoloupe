// Point inspection: the stored (native-resolution) value of every source at
// the clicked location and their time series.

import { useMemo, useState } from "react";
import { api } from "../api/client";
import type { PointResult, Series, TimeSeriesResult } from "../api/types";
import { fetchStationSeries, type StationSeries } from "../lib/points";
import { fmt, fmtLonLat, unitLabel } from "../lib/format";
import { paneIds } from "../lib/layout";
import { drawUnits, errorName, paneError } from "../lib/paneDraw";
import { useStore } from "../state/store";
import { timelineOf, type Timeline } from "../lib/timeline";
import { useFetch } from "../lib/useFetch";
import { PaneValues } from "./PaneValues";
import { mergeSeries, seriesColor, TimeSeriesChart } from "./TimeSeriesChart";

const WINDOW = 730; // time steps loaded around the current time

function windowAround(tl: Timeline, current: string | null, n: number): [string, string] | null {
  if (!tl.count) return null;
  const i = current ? tl.nearest(current) : tl.count - 1;
  const lo = Math.max(0, Math.min(i - Math.floor(n / 2), tl.count - n));
  const hi = Math.min(tl.count - 1, lo + n - 1);
  return [tl.at(lo), tl.at(hi)];
}

/** Per time step, the value the map shows: the first source (in priority order) with a value. */
function shownValues(r: TimeSeriesResult): Map<string, number> {
  const out = new Map<string, number>();
  for (const s of r.series)
    s.times.forEach((t, i) => {
      const v = s.values[i];
      if (v !== null && Number.isFinite(v) && !out.has(t)) out.set(t, v);
    });
  return out;
}

/** P's and T's shown values paired by time label, and the error of each pair. */
function errorSeries(
  pe: ReturnType<typeof paneError>,
  ts: TimeSeriesResult | null | undefined,
  tsT: TimeSeriesResult | null | undefined,
): { times: string[]; values: (number | null)[] } | null {
  if (!pe?.e || !ts || !tsT) return null;
  const P = shownValues(ts);
  const T = shownValues(tsT);
  const times = [...P.keys()].filter((t) => T.has(t)).sort();
  const values = times.map((t) => {
    const v = pe.e!.fn(P.get(t)! * pe.conv.scale + pe.conv.offset, T.get(t)!);
    return Number.isFinite(v) ? v : null;
  });
  return { times, values };
}

/** The error of P against T at the inspected point over time, with its summary. */
function ErrorSeries({
  series,
  name,
  units,
  classes,
  current,
  onPick,
}: {
  series: { times: string[]; values: (number | null)[] };
  name: string;
  units: string;
  classes: { value: number; name: string }[];
  current: string | null;
  onPick: (t: string) => void;
}) {
  const v = series.values.filter((x): x is number => x !== null);
  if (!series.times.length)
    return (
      <section>
        <div className="section-head">
          <span>Error over time</span>
        </div>
        <p className="muted small">P and T have no common time steps in this window.</p>
      </section>
    );
  const n = v.length;
  const mean = v.reduce((a, b) => a + b, 0) / Math.max(n, 1);
  const mae = v.reduce((a, b) => a + Math.abs(b), 0) / Math.max(n, 1);
  const rmse = Math.sqrt(v.reduce((a, b) => a + b * b, 0) / Math.max(n, 1));
  const u = units ? ` ${units}` : "";
  const counts = classes.map((c) => ({ ...c, n: v.filter((x) => x === c.value).length }));
  const s: Series = {
    source: 0,
    id: "error",
    resolution: "native cells",
    cell: { lon: 0, lat: 0, row: -1, col: -1 },
    times: series.times,
    values: series.values,
  };
  return (
    <section className="error-series">
      <div className="section-head">
        <span>Error over time: {name}</span>
        <span className="muted small">{n} steps</span>
      </div>
      <div className="small">
        {classes.length ? (
          counts.map((c) => (
            <span key={c.value} className="stat-chip">
              {c.name} {c.n}
            </span>
          ))
        ) : (
          <>
            <span className="stat-chip">
              mean {fmt(mean)}
              {u}
            </span>
            <span className="stat-chip">
              mean |·| {fmt(mae)}
              {u}
            </span>
            <span className="stat-chip">
              RMS {fmt(rmse)}
              {u}
            </span>
          </>
        )}
      </div>
      <TimeSeriesChart series={[s]} names={[name]} units={units} current={current} onPick={onPick} />
      <p className="muted small">From the native values of P and T at this point, step by step.</p>
    </section>
  );
}

export function Inspector() {
  const inspect = useStore((s) => s.inspect);
  const main = useStore((s) => s.main);
  const compare = useStore((s) => s.compare);
  const { setInspect, setTime } = useStore.getState();
  const [full, setFull] = useState(false);
  const paneNo = useStore((s) => paneIds(s.layout).indexOf(s.active) + 1);
  const multi = useStore((s) => paneIds(s.layout).length > 1);
  const layer = main.layer;
  const gridded = layer?.kind !== "points"; // a point layer has no grid to query

  // values at the current time (main layer and, when comparing, layer B)
  const pointKey =
    inspect && main.layerId && gridded ? JSON.stringify([main.layerId, inspect, main.time, main.dims]) : null;
  const { data: point, error: pointError } = useFetch<PointResult>(pointKey, (signal) =>
    api.point(main.layerId!, inspect!.lng, inspect!.lat, main.time, main.dims, signal),
  );
  const pointBKey =
    inspect && compare.mode !== "off" && compare.layerId
      ? JSON.stringify([compare.layerId, inspect, compare.time, compare.dims])
      : null;
  const { data: pointB } = useFetch<PointResult>(pointBKey, (signal) =>
    api.point(compare.layerId!, inspect!.lng, inspect!.lat, compare.time, compare.dims, signal),
  );

  // error at the point, from the native values of P and T
  const pe = paneError({ main, compare });
  const errorNow =
    pe?.e && point?.value != null && pointB?.value != null
      ? pe.e.fn(point.value * pe.conv.scale + pe.conv.offset, pointB.value)
      : null;

  // time series: a window around the time at which the point was picked, or the full period
  const range = useMemo(
    () =>
      layer
        ? full
          ? [timelineOf(layer).at(0), timelineOf(layer).at(timelineOf(layer).count - 1)]
          : windowAround(timelineOf(layer), main.time, WINDOW)
        : null,
    // the window is chosen when the point (or layer) changes, not on every time step
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [layer, full, inspect],
  );
  const tsKey =
    inspect && main.layerId && range && layer && timelineOf(layer).count > 1 && gridded
      ? JSON.stringify([main.layerId, inspect, range, main.dims])
      : null;
  const {
    data: ts,
    error: tsError,
    loading: loadingTs,
  } = useFetch<TimeSeriesResult>(tsKey, (signal) =>
    api.timeseries(main.layerId!, inspect!.lng, inspect!.lat, range![0], range![1], main.dims, signal),
  );

  // error panes: T's series over the same window, and the error at the point over time
  const tsTKey =
    tsKey && pe?.e && compare.layerId && compare.layer && timelineOf(compare.layer).count > 1
      ? JSON.stringify([compare.layerId, inspect, range, compare.dims])
      : null;
  const { data: tsT } = useFetch<TimeSeriesResult>(tsTKey, (signal) =>
    api.timeseries(compare.layerId!, inspect!.lng, inspect!.lat, range![0], range![1], compare.dims, signal),
  );
  const errSeries = errorSeries(tsTKey ? pe : null, ts, tsT);

  // a clicked station: its observations over the same window
  const station = inspect?.station;
  const stKey =
    station && range ? JSON.stringify([station.source, station.id, station.variable, range]) : null;
  const { data: stSeries, error: stError } = useFetch<StationSeries>(stKey, (signal) =>
    fetchStationSeries(station!.source, station!.id, station!.variable, range![0], range![1], signal),
  );

  // grid series plus the station series as one more line (its colour follows the grid sources)
  const allSeries: Series[] = useMemo(() => {
    const out = [...(ts?.series ?? [])];
    if (stSeries && layer)
      out.push({
        source: layer.sources.length,
        id: `station:${stSeries.station}`,
        resolution: "point",
        cell: { lon: stSeries.lon, lat: stSeries.lat, row: -1, col: -1 },
        times: stSeries.times,
        values: stSeries.values,
      });
    return out;
  }, [ts, stSeries, layer]);

  if (!inspect || !layer) return null;
  const units = unitLabel(layer.units);
  const names = allSeries.map((s) =>
    s.id.startsWith("station:")
      ? `${station?.name ?? s.id.slice(8)} (station)`
      : (layer.sources[s.source]?.dataset_title.split(" (")[0] ?? s.id),
  );
  const obsNow = stSeries && main.time ? (stSeries.values[stSeries.times.indexOf(main.time)] ?? null) : null;

  const downloadCsv = () => {
    if (!allSeries.length) return;
    const { labels, data } = mergeSeries(allSeries);
    const head = ["time", ...allSeries.map((s) => `${s.id} [${layer.units ?? ""}]`)].join(",");
    const rows = labels.map((t, i) => [t, ...data.map((d) => (d[i] === null ? "" : String(d[i])))].join(","));
    const meta = `# GeoLoupe time series at lon=${inspect.lng.toFixed(5)} lat=${inspect.lat.toFixed(5)}; layer ${layer.id}; native grid cells: ${allSeries
      .map((s) => `${s.id} (${s.cell.lon.toFixed(4)}, ${s.cell.lat.toFixed(4)})`)
      .join("; ")}`;
    const blob = new Blob([[meta, head, ...rows].join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `geoloupe_${layer.id.replace(/[:/]/g, "_")}_${inspect.lat.toFixed(3)}_${inspect.lng.toFixed(3)}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <aside className="inspector panel" aria-label="Point details">
      <header className="panel-head">
        <div>
          <div className="eyebrow">{station ? "Station" : "Point"}</div>
          <div className="title">
            {station ? `${station.name ?? station.id} · ` : ""}
            {fmtLonLat(inspect.lng, inspect.lat, 4)}
          </div>
        </div>
        <button className="icon-btn" onClick={() => setInspect(null)} aria-label="Close point details">
          ×
        </button>
      </header>
      <section>
        {pointError && <div className="error-text">Point query failed: {pointError}</div>}
        <div className="big-value">
          {gridded
            ? point
              ? point.value === null
                ? "no data"
                : fmt(point.value)
              : "…"
            : obsNow !== null
              ? fmt(obsNow)
              : stSeries
                ? "no data"
                : "…"}
          {(gridded ? point?.value != null : obsNow !== null) && !layer.classes && (
            <span className="unit"> {units}</span>
          )}
          {layer.classes && point?.value != null && (
            <span className="unit">
              {" "}
              {layer.classes.find((c) => c.value === Math.round(point.value!))?.name ?? ""}
            </span>
          )}
        </div>
        <div className="muted small">
          {layer.title}
          {main.time ? ` · ${main.time}` : ""}
          {Object.entries(main.dims).map(([k, v]) => ` · ${k} ${v}`)}
        </div>
        {gridded && (
          <table className="value-table">
            <thead>
              <tr>
                <th>Source</th>
                <th className="num">Value</th>
                <th>Native cell</th>
              </tr>
            </thead>
            <tbody>
              {(point?.sources ?? []).map((s) => (
                <tr key={s.source} className={point?.source === s.source ? "winner" : ""}>
                  <td>
                    <span className="swatch" style={{ background: seriesColor(s.source) }} />
                    {layer.sources[s.source]?.dataset_title.split(" (")[0] ?? s.id}
                    <div className="muted small">{s.resolution}</div>
                  </td>
                  <td className="num">
                    {s.status === "ok" ? fmt(s.value) : <span className="muted small">{s.status}</span>}
                  </td>
                  <td className="muted small">
                    {s.cell ? `${s.cell.lat.toFixed(4)}, ${s.cell.lon.toFixed(4)}` : "–"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {station && (
          <div className="compare-values">
            <span className="swatch" style={{ background: seriesColor(layer.sources.length) }} />
            Observed ({station.id}
            {station.elevation !== null ? `, ${Math.round(station.elevation)} m` : ""}):{" "}
            <b>{obsNow !== null ? fmt(obsNow) : stError ? "–" : "…"}</b> {units}
            {obsNow !== null && point?.value != null && (
              <span className="muted">
                {" "}
                (observed − map = {obsNow - point.value >= 0 ? "+" : ""}
                {fmt(obsNow - point.value, 3)})
              </span>
            )}
            {stError && <div className="error-text small">{stError}</div>}
          </div>
        )}
        {pointB && (
          <div className="compare-values">
            <span className="tip-tag">T</span> {compare.layer?.title}{" "}
            {compare.time ? `· ${compare.time}` : ""}: <b>{fmt(pointB.value)}</b>{" "}
            {unitLabel(compare.layer?.units)}
            {errorNow !== null && (
              <div>
                <span className="tip-tag">E</span> {errorName(compare.error)} = <b>{fmt(errorNow)}</b>{" "}
                {unitLabel(drawUnits({ main, compare }))}
                <span className="muted small"> (native cells, not the map's pixels)</span>
              </div>
            )}
          </div>
        )}
        {gridded && (
          <p className="muted small">
            Values are the stored values of each source’s native grid cell; the highlighted row is what the
            map shows.
          </p>
        )}
      </section>
      <PaneValues inspect={inspect} />
      {errSeries && (
        <ErrorSeries
          series={errSeries}
          name={errorName(compare.error)}
          units={unitLabel(drawUnits({ main, compare }))}
          classes={pe?.e?.classes ?? []}
          current={main.time}
          onPick={setTime}
        />
      )}
      {timelineOf(layer).count > 1 && (
        <section>
          <div className="section-head">
            <span>{multi ? `Pane ${paneNo}: all sources` : "Time series"}</span>
            <span className="muted small">{range ? `${range[0]} … ${range[1]}` : ""}</span>
          </div>
          {loadingTs && <div className="skeleton chart-skeleton">Reading the time series…</div>}
          {tsError && <div className="error-text">{tsError}</div>}
          {allSeries.length > 0 && (
            <TimeSeriesChart
              series={allSeries}
              names={names}
              units={units}
              current={main.time}
              onPick={setTime}
            />
          )}
          <div className="row gap">
            {!full && timelineOf(layer).count > WINDOW && (
              <button
                className="btn"
                onClick={() => setFull(true)}
                title="May take a while for long daily records"
              >
                Full period ({timelineOf(layer).count.toLocaleString()} steps)
              </button>
            )}
            {full && (
              <button className="btn" onClick={() => setFull(false)}>
                Around current time
              </button>
            )}
            <button className="btn" onClick={downloadCsv} disabled={!allSeries.length}>
              Download CSV
            </button>
          </div>
          <p className="muted small">
            Click the chart to jump to that time. Drag to zoom, double-click to reset.
          </p>
        </section>
      )}
    </aside>
  );
}
