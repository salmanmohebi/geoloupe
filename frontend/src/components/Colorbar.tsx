// Legend: colour bar with ticks and units, plus a histogram of the values on
// screen so the user sees how the range fits the data.

import { useMemo } from "react";
import { cssGradient, getLut, scalePosition } from "../lib/colormap";
import { fmtTick, unitLabel } from "../lib/format";
import { classShares, classTable } from "../lib/classes";
import { pointsSample } from "../lib/points";
import { drawTitle, drawUnits, exactness, paneError, statsLine } from "../lib/paneDraw";
import { histogram } from "../lib/view";
import { useStore } from "../state/store";

const BINS = 48;

export function niceTicks(lo: number, hi: number, log: boolean, n = 5): number[] {
  if (!(hi > lo)) return [lo];
  if (log && lo > 0) {
    const out: number[] = [];
    for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++) {
      const v = 10 ** e;
      if (v >= lo * 0.999 && v <= hi * 1.001) out.push(v);
    }
    return out.length >= 2 ? out : [lo, hi];
  }
  const raw = (hi - lo) / (n - 1);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step)
    out.push(Number(v.toPrecision(12)));
  return out;
}

export function Colorbar() {
  const main = useStore((s) => s.main);
  const compare = useStore((s) => s.compare);
  const styleMain = useStore((s) => s.style);
  const diffStyle = useStore((s) => s.diffStyle);
  const colormaps = useStore((s) => s.colormaps);
  const summary = useStore((s) => s.summary);
  const points = useStore((s) => s.points);
  const isPoints = useStore((s) => s.main.layer?.kind === "points");
  const pane = { main, compare, style: styleMain, diffStyle };
  const diff = compare.mode === "error";
  const st = diff ? diffStyle : styleMain; // = drawStyle(pane)
  const errClasses = paneError(pane)?.e?.classes ?? [];
  const errStats = useStore((s) => (diff ? (s.errorStats[s.active] ?? null) : null));
  const statsBlock = errStats?.count ? (
    <div className="cb-stats small">
      <b>{statsLine(errStats, compare.error, unitLabel(drawUnits(pane)))}</b>
      <div className="muted">{exactness(errStats)}</div>
    </div>
  ) : null;

  const { vmin, vmax, log } = st;
  const hist = useMemo(() => {
    const s = isPoints ? pointsSample(points) : summary?.sample;
    if (!s || !s.length) return null;
    let below = 0;
    let above = 0;
    for (let i = 0; i < s.length; i++) {
      if (s[i] < vmin) below++;
      else if (s[i] > vmax) above++;
    }
    let counts: number[];
    if (log && vmin > 0) {
      const logs = new Float32Array(s.length);
      for (let i = 0; i < s.length; i++) logs[i] = s[i] > 0 ? Math.log(s[i]) : NaN;
      counts = histogram(logs, Math.log(vmin), Math.log(vmax), BINS);
    } else counts = histogram(s, vmin, vmax, BINS);
    return { counts, max: Math.max(1, ...counts), below: below / s.length, above: above / s.length };
  }, [summary, points, isPoints, vmin, vmax, log]);

  if (!main.layer) return null;
  const classes = diff ? (errClasses.length ? errClasses : null) : main.layer.classes;
  if (classes) {
    const table = classTable(classes, getLut(colormaps, st.colormap));
    const shares = summary?.sample.length ? classShares(summary.sample, table) : null;
    const name = drawTitle(pane);
    return (
      <div className="colorbar panel class-legend" aria-label="Legend">
        <div className="cb-title" title={name}>
          {name}
        </div>
        <ul>
          {classes.map((c) => {
            const share = shares?.get(c.value) ?? 0;
            return (
              <li key={c.value} className={shares && !share ? "absent" : ""}>
                <span className="swatch" style={{ background: table.byValue.get(c.value)?.color }} />
                <span className="class-name">{c.name}</span>
                {shares && share > 0 && (
                  <span className="muted small">{(share * 100).toFixed(share < 0.01 ? 1 : 0)}%</span>
                )}
              </li>
            );
          })}
        </ul>
        {shares && <div className="muted small">share of the view</div>}
        {statsBlock}
      </div>
    );
  }
  if (main.layer.kind === "rgb" && !diff) {
    const b = main.layer.bands ?? [];
    return (
      <div className="colorbar panel" aria-label="Legend">
        <div className="cb-title">
          {main.layer.title}
          {main.layer.units && <span className="muted"> [{unitLabel(main.layer.units)}]</span>}
        </div>
        <div className="rgb-legend">
          {["red", "green", "blue"].map((c, k) => (
            <span key={c} className="rgb-chan">
              <span className="swatch" style={{ background: c }} />
              {b[k] ?? main.layer?.sources[k]?.variable ?? c}
            </span>
          ))}
        </div>
        <div className="cb-bar" style={{ background: "linear-gradient(to right, #000, #fff)" }} />
        <div className="row between muted small">
          <span>{fmtTick(st.vmin)}</span>
          <span>gamma {styleMain.gamma.toFixed(2)}</span>
          <span>{fmtTick(st.vmax)}</span>
        </div>
      </div>
    );
  }
  const lut = getLut(colormaps, st.colormap);
  const ticks = niceTicks(st.vmin, st.vmax, st.log);
  const units = unitLabel(drawUnits(pane));
  const title = diff ? drawTitle(pane) : main.layer.title;

  return (
    <div className="colorbar panel" aria-label="Legend">
      <div className="cb-title" title={title}>
        {title}
        {units && <span className="muted"> [{units}]</span>}
      </div>
      {hist && (
        <svg className="cb-hist" viewBox={`0 0 ${BINS} 24`} preserveAspectRatio="none" aria-hidden>
          {hist.counts.map((c, i) => {
            const h = (c / hist.max) * 22;
            return <rect key={i} x={i + 0.1} y={24 - h} width={0.8} height={h} rx={0.2} />;
          })}
        </svg>
      )}
      <div className="cb-bar" style={{ background: cssGradient(lut) }} />
      <div className="cb-ticks">
        {ticks.map((t) => {
          const p = scalePosition(t, st);
          if (!(p >= -0.001 && p <= 1.001)) return null;
          return (
            <span key={t} style={{ left: `${p * 100}%` }}>
              {fmtTick(t)}
            </span>
          );
        })}
      </div>
      {hist && (hist.below > 0.005 || hist.above > 0.005) && (
        <div className="muted small cb-out">
          {hist.below > 0.005 && <span>{Math.round(hist.below * 100)}% below min</span>}
          {hist.above > 0.005 && <span>{Math.round(hist.above * 100)}% above max</span>}
        </div>
      )}
      {statsBlock}
    </div>
  );
}
