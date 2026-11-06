// Left panel: choose a layer, see where its data comes from, set dimensions
// and the colour scale.

import { useMemo, useState } from "react";
import type { LayerMeta } from "../api/types";
import { cssGradient, getLut } from "../lib/colormap";
import { fmt, unitLabel } from "../lib/format";
import { tileZoom } from "../lib/mercator";
import { pointsSample } from "../lib/points";
import { timelineOf } from "../lib/timeline";
import { percentiles } from "../lib/view";
import { useStore } from "../state/store";
import { Icon } from "./Icon";

function zoomText(tileZ: number): string {
  return Math.max(0, tileZ - 1).toFixed(0); // tile zoom (256 px) → map zoom
}

function SourceList({ layer }: { layer: LayerMeta }) {
  const summary = useStore((s) => s.summary);
  // consecutive sources of one dataset (the files of a mosaic, the bands of an RGB image) as one entry
  const groups: number[][] = [];
  layer.sources.forEach((s, i) => {
    const prev = groups.at(-1);
    const p = prev && layer.sources[prev[0]];
    if (p && p.dataset_title === s.dataset_title && p.resolution === s.resolution) prev.push(i);
    else groups.push([i]);
  });
  return (
    <ul className="source-list">
      {groups.map((idx, g) => {
        const s = layer.sources[idx[0]];
        const shown = summary?.parts.filter((p) => idx.includes(p.source)) ?? [];
        const frac = Math.min(
          1,
          shown.reduce((a, p) => a + p.fraction, 0),
        );
        const hidden = summary?.skipped.find((k) => idx.includes(k.source));
        const overviews = s.levels.filter((l) => l.origin !== "native").length;
        const parts = layer.kind === "rgb" ? "bands" : "files";
        return (
          <li key={s.id}>
            <div className="src-head">
              <span className="src-rank">{g + 1}</span>
              <span className="src-name" title={s.dataset_title}>
                {s.dataset_title}
              </span>
              {frac > 0 && <span className="pill">{Math.round(frac * 100)}% of view</span>}
            </div>
            <div className="muted small">
              {idx.length > 1 ? `${idx.length} ${parts}` : s.variable} · {s.resolution} · {s.crs}
              {s.fixed_time
                ? ` · fixed time ${s.fixed_time}`
                : s.time_range
                  ? ` · ${s.time_range[0]} … ${s.time_range[1]}`
                  : " · static"}
            </div>
            <div className="muted small">
              {overviews ? `${overviews} overview level${overviews > 1 ? "s" : ""}` : "no overviews"}
              {s.min_zoom > 0 ? ` · visible from zoom ${zoomText(Math.ceil(s.min_zoom))}` : ""}
            </div>
            {hidden && (
              <div className="hint">
                {hidden.reason === "cost"
                  ? `Not shown at this zoom: too much data to read on the fly. Zoom in (≥ ${zoomText(Math.ceil(s.min_zoom))}) or build overviews.`
                  : hidden.reason === "time"
                    ? "No data for the selected time."
                    : hidden.reason === "zoom"
                      ? "Outside this source's zoom window."
                      : `Not shown: ${hidden.reason}`}
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** Point data over a gridded layer: on/off and which point layer (default: the configured one). */
function PointsOverlay() {
  const catalog = useStore((s) => s.catalog);
  const layer = useStore((s) => s.main.layer);
  const show = useStore((s) => s.showPoints);
  const overlay = useStore((s) => s.overlay);
  const points = useStore((s) => s.points);
  const diff = useStore((s) => s.compare.mode === "diff");
  const { setShowPoints, setOverlay } = useStore.getState();
  const options = catalog?.layers.filter((l) => l.kind === "points") ?? [];
  if (!layer || layer.kind === "points" || !options.length) return null;
  const current = overlay ?? layer.points?.layer ?? "";
  return (
    <div className="stations-toggle">
      <label className="check" title="Observations drawn in the same colour scale as the map">
        <input
          type="checkbox"
          checked={show && !!current}
          disabled={!current}
          onChange={(e) => setShowPoints(e.target.checked)}
        />
        <span>Points on the map</span>
      </label>
      <select
        value={current}
        onChange={(e) => {
          setOverlay(e.target.value || null);
          setShowPoints(!!e.target.value);
        }}
        aria-label="Point layer drawn over the map"
      >
        <option value="">none</option>
        {options.map((o) => (
          <option key={o.id} value={o.id}>
            {o.title}
            {o.units ? ` [${o.units}]` : ""}
          </option>
        ))}
      </select>
      <div className="muted small">
        {diff
          ? "hidden in difference mode"
          : show && points
            ? `${points.count.toLocaleString()} points on this day${points.truncated ? " (sampled)" : ""}; same colour scale as the map`
            : current
              ? ""
              : "choose point data to compare with the map"}
      </div>
    </div>
  );
}

/** Summary of a point layer shown as the main layer. */
function PointsInfo() {
  const points = useStore((s) => s.points);
  const layer = useStore((s) => s.main.layer);
  if (!layer || layer.kind !== "points") return null;
  const [w, s, e, n] = layer.bounds;
  return (
    <div className="stations-toggle muted small">
      {points ? `${points.count.toLocaleString()} stations with data on this day` : "loading…"}
      {points?.truncated ? " (sampled)" : ""} · extent {w.toFixed(1)}…{e.toFixed(1)}° E, {s.toFixed(1)}…
      {n.toFixed(1)}° N · click a station for its time series
    </div>
  );
}

function ColormapPicker({ value, onChange }: { value: string; onChange: (c: string) => void }) {
  const colormaps = useStore((s) => s.colormaps);
  const [open, setOpen] = useState(false);
  const reversed = value.endsWith("_r") && !(value in colormaps);
  const base = reversed ? value.slice(0, -2) : value;
  const groups = useMemo(() => {
    const g: Record<string, string[]> = {};
    for (const [name, c] of Object.entries(colormaps)) (g[c.category] ??= []).push(name);
    return g;
  }, [colormaps]);
  return (
    <div className="cmap-picker">
      <button className="cmap-current" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="cmap-bar" style={{ background: cssGradient(getLut(colormaps, value)) }} />
        <span>{value}</span>
        <span className="chev">
          <Icon name={open ? "up" : "down"} />
        </span>
      </button>
      <label className="check">
        <input
          type="checkbox"
          checked={reversed}
          onChange={(e) => onChange(e.target.checked ? `${base}_r` : base)}
        />
        reversed
      </label>
      {open && (
        <div className="cmap-grid" role="listbox">
          {Object.entries(groups).map(([cat, names]) => (
            <div key={cat}>
              <div className="eyebrow">{cat}</div>
              {names.map((n) => (
                <button
                  key={n}
                  role="option"
                  aria-selected={n === base}
                  className={`cmap-opt ${n === base ? "sel" : ""}`}
                  onClick={() => {
                    onChange(reversed ? `${n}_r` : n);
                    setOpen(false);
                  }}
                  title={`${n} (${colormaps[n].provider})`}
                >
                  <span className="cmap-bar" style={{ background: cssGradient(getLut(colormaps, n)) }} />
                  <span>{n}</span>
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function NumberInput({
  value,
  onCommit,
  label,
}: {
  value: number;
  onCommit: (v: number) => void;
  label: string;
}) {
  const [text, setText] = useState<string | null>(null);
  return (
    <label className="num-input">
      <span>{label}</span>
      <input
        type="text"
        inputMode="decimal"
        value={text ?? fmt(value, 6).replace(/,/g, "")}
        onChange={(e) => setText(e.target.value)}
        onBlur={() => {
          const v = Number(text);
          if (text !== null && isFinite(v)) onCommit(v);
          setText(null);
        }}
        onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      />
    </label>
  );
}

export function StyleControls() {
  const style = useStore((s) => s.style);
  const diffStyle = useStore((s) => s.diffStyle);
  const compare = useStore((s) => s.compare);
  const summary = useStore((s) => s.summary);
  const points = useStore((s) => s.points);
  const isPoints = useStore((s) => s.main.layer?.kind === "points");
  const { setStyle, setDiffStyle, autoRange, notify } = useStore.getState();
  const diff = compare.mode === "diff";
  const st = diff ? diffStyle : style;
  const set = diff ? setDiffStyle : setStyle;

  const fitView = () => {
    const sample = isPoints ? pointsSample(points) : summary?.sample;
    if (!sample || !sample.length) return notify("info", "Nothing visible yet to fit the range to.");
    if (diff) {
      const [p2, p98] = percentiles(sample, [2, 98]);
      const m = Math.max(Math.abs(p2), Math.abs(p98)) || 1;
      set({ vmin: -Number(m.toPrecision(2)), vmax: Number(m.toPrecision(2)) });
    } else {
      const [p2, p98] = percentiles(sample, [2, 98]);
      set({ vmin: Number(p2.toPrecision(3)), vmax: Number((p98 > p2 ? p98 : p2 + 1).toPrecision(3)) });
    }
  };

  const isRgb = useStore((s) => s.main.layer?.kind === "rgb") && !diff;
  const nClasses = useStore((s) => s.main.layer?.classes?.length ?? 0);
  if (nClasses && !diff)
    return (
      <div className="style-controls">
        <div className="muted small">{nClasses} classes, each with its own colour (see the legend).</div>
        <label className="slider-row">
          <span>Opacity</span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={style.opacity}
            onChange={(e) => setStyle({ opacity: Number(e.target.value) })}
          />
        </label>
      </div>
    );
  return (
    <div className="style-controls">
      {isRgb ? (
        <div className="muted small">RGB: red, green and blue are stretched from min to max.</div>
      ) : (
        <ColormapPicker value={st.colormap} onChange={(c) => set({ colormap: c })} />
      )}
      <div className="row gap">
        <NumberInput label="min" value={st.vmin} onCommit={(v) => set({ vmin: v })} />
        <NumberInput label="max" value={st.vmax} onCommit={(v) => set({ vmax: v })} />
      </div>
      <div className="row gap wrap">
        <button className="btn" onClick={fitView} title="2–98 % of the values on screen">
          Fit to view
        </button>
        {!diff && (
          <button className="btn" onClick={() => autoRange()} title="2–98 % of the whole layer at this time">
            Fit to layer
          </button>
        )}
        {!isRgb && (
          <label className="check" title="Logarithmic colour scale (values > 0)">
            <input
              type="checkbox"
              checked={st.log}
              onChange={(e) => {
                if (e.target.checked && st.vmin <= 0)
                  set({ log: true, vmin: Math.max(st.vmax / 1000, 1e-3) });
                else set({ log: e.target.checked });
              }}
            />
            log
          </label>
        )}
      </div>
      {isRgb && (
        <label className="slider-row">
          <span>Gamma {style.gamma.toFixed(2)}</span>
          <input
            type="range"
            min={0.3}
            max={3}
            step={0.05}
            value={style.gamma}
            onChange={(e) => setStyle({ gamma: Number(e.target.value) })}
          />
        </label>
      )}
      <label className="slider-row">
        <span>Opacity</span>
        <input
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={style.opacity}
          onChange={(e) => setStyle({ opacity: Number(e.target.value) })}
        />
      </label>
    </div>
  );
}

export function LayerPanel() {
  const catalog = useStore((s) => s.catalog);
  const main = useStore((s) => s.main);
  const panelOpen = useStore((s) => s.panelOpen);
  const view = useStore((s) => s.view);
  const { setLayer, setDim, setPanelOpen } = useStore.getState();
  const layer = main.layer;
  const [showDatasets, setShowDatasets] = useState(false);

  // config layers (composites, RGB) have ids without ":"; dataset layers are "<dataset>:<variable>[:rgb]"
  const composites = catalog?.layers.filter((l) => l.kind !== "points" && !l.id.includes(":")) ?? [];
  const pointLayers = catalog?.layers.filter((l) => l.kind === "points") ?? [];
  const byDataset = useMemo(() => {
    const out: Record<string, typeof composites> = {};
    for (const l of catalog?.layers ?? [])
      if (l.kind !== "points" && l.id.includes(":")) (out[l.datasets[0]] ??= []).push(l);
    return out;
  }, [catalog]);
  const dsTitle = (id: string) => catalog?.datasets.find((d) => d.id === id)?.title ?? id;
  const failed = [
    ...(catalog?.datasets.filter((d) => d.status === "error") ?? []),
    ...(catalog?.points?.filter((p) => p.status === "error") ?? []),
  ];

  if (!panelOpen)
    return (
      <button className="panel-toggle panel" onClick={() => setPanelOpen(true)} aria-label="Show layer panel">
        <Icon name="menu" /> Layers
      </button>
    );

  return (
    <aside className="layer-panel panel" aria-label="Layer and style">
      <header className="brand">
        <img src="./favicon.svg" alt="" width={22} height={22} />
        <div>
          <div className="brand-name">TerraScope</div>
          <div className="muted small">
            {catalog?.title !== "TerraScope" ? catalog?.title : "scientific data explorer"}
          </div>
        </div>
        <button className="icon-btn" onClick={() => setPanelOpen(false)} aria-label="Hide panel">
          ‹
        </button>
      </header>

      <section>
        <label className="field">
          <span className="eyebrow">Layer</span>
          <select value={main.layerId ?? ""} onChange={(e) => setLayer(e.target.value)}>
            {composites.length > 0 && (
              <optgroup label="Combined layers">
                {composites.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.title}
                  </option>
                ))}
              </optgroup>
            )}
            {Object.entries(byDataset).map(([ds, layers]) => (
              <optgroup key={ds} label={dsTitle(ds)}>
                {layers.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.title} ({l.id.split(":").slice(1).join(":")})
                  </option>
                ))}
              </optgroup>
            ))}
            {pointLayers.length > 0 && (
              <optgroup label="Stations and points">
                {pointLayers.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.title}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
        </label>
        {layer && (
          <>
            {layer.description && <p className="muted small">{layer.description}</p>}
            <div className="muted small">
              Units: {unitLabel(layer.units) || "–"}
              {layer.time_step
                ? ` · ${timelineOf(layer).count.toLocaleString()} × ${layer.time_step}`
                : " · static"}
              {view
                ? ` · zoom ${view.zoom.toFixed(1)} (tiles z${tileZoom(view.zoom, layer.tile_size, layer.maxzoom)})`
                : ""}
            </div>
            <SourceList layer={layer} />
            <PointsInfo />
            <PointsOverlay />
          </>
        )}
      </section>

      {layer && Object.keys(layer.dims).length > 0 && (
        <section>
          {Object.entries(layer.dims).map(([d, values]) => (
            <label className="field" key={d}>
              <span className="eyebrow">{d}</span>
              <select value={main.dims[d] ?? String(values[0])} onChange={(e) => setDim(d, e.target.value)}>
                {values.map((v) => (
                  <option key={String(v)} value={String(v)}>
                    {String(v)}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </section>
      )}

      <section>
        <div className="eyebrow">Colour scale</div>
        <StyleControls />
      </section>

      <section>
        <button className="link-btn" onClick={() => setShowDatasets(!showDatasets)}>
          <Icon name={showDatasets ? "down" : "right"} size={12} /> Datasets (
          {(catalog?.datasets.length ?? 0) + (catalog?.points?.length ?? 0)})
          {failed.length > 0 && <span className="pill danger">{failed.length} failed</span>}
        </button>
        {showDatasets && (
          <ul className="dataset-list">
            {catalog?.datasets.map((d) => (
              <li key={d.id}>
                <b>{d.title}</b> <span className="muted small">({d.id})</span>
                {d.status === "error" ? (
                  <div className="error-text small">{d.error}</div>
                ) : (
                  <div className="muted small">
                    {d.variables.join(", ")}
                    {d.attribution ? ` · ${d.attribution}` : ""}
                  </div>
                )}
              </li>
            ))}
            {catalog?.points?.map((p) => (
              <li key={`points:${p.id}`}>
                <b>{p.title}</b> <span className="muted small">({p.id}, points)</span>
                {p.status === "error" ? (
                  <div className="error-text small">{p.error}</div>
                ) : (
                  <div className="muted small">
                    {(p.variables ?? []).join(", ")}
                    {p.attribution ? ` · ${p.attribution}` : ""}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </aside>
  );
}
