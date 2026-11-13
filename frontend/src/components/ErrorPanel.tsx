// Error maps: the side-panel section of an error pane (definition, parameters, custom formula,
// which panes P and T follow) and the Compare-menu part that creates error panes.

import { useMemo, useState } from "react";
import { unitLabel } from "../lib/format";
import { CUSTOM, ERROR_METRICS, errorMetric, type ErrorChoice } from "../lib/errors";
import { check, FormulaError, parse } from "../lib/formula";
import { layerName } from "../lib/layers";
import { getPaneMaps } from "../lib/mapRegistry";
import { paneIds } from "../lib/layout";
import { drawUnits, isErrorPane, paneError } from "../lib/paneDraw";
import { summarize } from "../lib/view";
import { paneOf, useStore, validTime, type AppState } from "../state/store";
import { timelineOf } from "../lib/timeline";
import { Icon } from "./Icon";
import { useMenuClose } from "./menuContext";

/** Panes that can be followed (they show a gridded layer), with their numbers. */
function usePaneChoices(exclude: string | null) {
  const layout = useStore((s) => s.layout);
  const panes = useStore((s) => s.panes);
  const main = useStore((s) => s.main);
  const active = useStore((s) => s.active);
  return useMemo(() => {
    const s = useStore.getState();
    return paneIds(layout)
      .map((id, i) => ({ id, n: i + 1, p: paneOf(s, id) }))
      .filter(
        (x) => x.id !== exclude && x.p?.main.layer && x.p.main.layer.kind !== "points" && !isErrorPane(x.p),
      )
      .map((x) => ({ id: x.id, n: x.n, label: `Pane ${x.n} · ${layerName(x.p!.main.layer)}` }));
    // panes/main/active: recompute when any pane changes
  }, [layout, panes, main, active, exclude]); // eslint-disable-line react-hooks/exhaustive-deps
}

function MetricSelect({ value, onChange }: { value: string; onChange: (id: string) => void }) {
  const groups = useMemo(() => {
    const out: Record<string, typeof ERROR_METRICS> = {};
    for (const m of ERROR_METRICS) (out[m.group] ??= []).push(m);
    return out;
  }, []);
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} aria-label="Error definition">
      {Object.entries(groups).map(([g, ms]) => (
        <optgroup key={g} label={g}>
          {ms.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label}: {m.symbol}
            </option>
          ))}
        </optgroup>
      ))}
      <optgroup label="Your own">
        <option value={CUSTOM}>Custom formula of P and T</option>
      </optgroup>
    </select>
  );
}

function ParamInput({
  label,
  units,
  value,
  onCommit,
  extra,
}: {
  label: string;
  units: string;
  value: number;
  onCommit: (v: number) => void;
  extra?: React.ReactNode;
}) {
  const [text, setText] = useState<string | null>(null);
  const commit = () => {
    if (text !== null && text.trim() !== "" && Number.isFinite(Number(text))) onCommit(Number(text));
    setText(null);
  };
  return (
    <label className="field param">
      <span className="eyebrow">
        {label} {units && <span className="muted">[{units}]</span>}
      </span>
      <span className="row gap">
        <input
          type="text"
          inputMode="decimal"
          value={text ?? String(value)}
          onChange={(e) => setText(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
        {extra}
      </span>
    </label>
  );
}

function FormulaInput({ choice }: { choice: ErrorChoice }) {
  const { setError } = useStore.getState();
  const [text, setText] = useState(choice.formula ?? "P - T");
  const [units, setUnits] = useState(choice.units ?? "");
  let problem: string | null = null;
  try {
    check(parse(text), ["P", "T"]);
  } catch (e) {
    problem = e instanceof FormulaError ? e.message : String(e);
  }
  const apply = () => {
    if (!problem && (text !== choice.formula || units !== (choice.units ?? "")))
      setError({ formula: text, units: units || undefined });
  };
  return (
    <>
      <label className="field">
        <span className="eyebrow">Formula (P, T, + − * / ^, abs, sqrt, log, exp, min, max, where)</span>
        <input
          type="text"
          className={`formula-input${problem ? " invalid" : ""}`}
          value={text}
          spellCheck={false}
          onChange={(e) => setText(e.target.value)}
          onBlur={apply}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
          aria-invalid={!!problem}
        />
      </label>
      {problem ? (
        <div className="error-text small">{problem}</div>
      ) : (
        <div className="muted small">
          e.g. <code>abs(P - T) / (abs(T) + 1)</code>, <code>where(T &gt; 0, P / T, nan)</code>,{" "}
          <code>abs(P - T) &gt; 2</code>
        </div>
      )}
      <label className="field">
        <span className="eyebrow">Units of the result (optional)</span>
        <input
          type="text"
          value={units}
          onChange={(e) => setUnits(e.target.value)}
          onBlur={apply}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
      </label>
    </>
  );
}

/** Standard deviation of T over the visible tiles of the active pane (for "normalised error"). */
function viewStdOfT(s: AppState): number | null {
  const map = getPaneMaps()[s.active];
  const c = s.compare;
  if (!map || !c.layer || !c.layerId) return null;
  const spec = {
    data: { layerId: c.layerId, time: c.time, dims: c.dims },
    ref: null,
    error: null,
    scale: { lut: new Uint8Array(768), vmin: 0, vmax: 1, log: false },
    tileSize: c.layer.tile_size,
  };
  const v = summarize(map, spec, c.layer).sample.filter((x) => Number.isFinite(x));
  if (v.length < 10) return null;
  const mean = v.reduce((a, b) => a + b, 0) / v.length;
  const sd = Math.sqrt(v.reduce((a, b) => a + (b - mean) ** 2, 0) / (v.length - 1));
  return sd > 0 ? Number(sd.toPrecision(3)) : null;
}

function SourceRow({
  role,
  follow,
  choices,
  fixedLabel,
  onFollow,
  children,
}: {
  role: "P" | "T";
  follow: string | null;
  choices: { id: string; label: string }[];
  fixedLabel: string;
  onFollow: (id: string | null) => void;
  children?: React.ReactNode;
}) {
  return (
    <div className="error-source">
      <label className="field">
        <span className="eyebrow">{role === "P" ? "P · compared data" : "T · reference"}</span>
        <select value={follow ?? ""} onChange={(e) => onFollow(e.target.value || null)}>
          {choices.map((c) => (
            <option key={c.id} value={c.id}>
              follows {c.label}
            </option>
          ))}
          <option value="">fixed layer: {fixedLabel}</option>
        </select>
      </label>
      {!follow && children}
    </div>
  );
}

/** T's time: the same as P's, or its own (e.g. August 1850 against August 2014). */
function RefTime() {
  const compare = useStore((s) => s.compare);
  const { setCompare } = useStore.getState();
  const [text, setText] = useState<string | null>(null);
  const tl = timelineOf(compare.layer);
  if (tl.count < 2) return null;
  return (
    <>
      <label className="check">
        <input
          type="checkbox"
          checked={compare.linkTime}
          onChange={(e) => void setCompare({ linkTime: e.target.checked })}
        />
        same time as P
      </label>
      {!compare.linkTime && (
        <label className="field">
          <span className="eyebrow">
            T's time ({tl.at(0)} … {tl.at(tl.count - 1)})
          </span>
          <input
            type="text"
            className="time-input"
            value={text ?? compare.time ?? ""}
            onChange={(e) => setText(e.target.value)}
            onBlur={() => {
              if (text && compare.layer) void setCompare({ time: validTime(compare.layer, text.trim()) });
              setText(null);
            }}
            onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
            aria-label="Time of T (ISO 8601)"
          />
        </label>
      )}
    </>
  );
}

/** Side panel section of the active pane when it is an error map. */
export function ErrorPanel() {
  const main = useStore((s) => s.main);
  const compare = useStore((s) => s.compare);
  const catalog = useStore((s) => s.catalog);
  const active = useStore((s) => s.active);
  const { setError, setFollow, setCompare, setLayer, notify } = useStore.getState();
  const choices = usePaneChoices(active);
  const pe = paneError({ main, compare });
  const choice = compare.error;
  const metric = errorMetric(choice.metric);
  const tUnits = unitLabel(compare.layer?.units ?? main.layer?.units);
  const grids = catalog?.layers.filter((l) => l.kind !== "points") ?? [];

  const swap = () => {
    const { follow } = compare;
    const P = main;
    const T = compare;
    // P takes T's layer and the other way round; followed panes swap too
    useStore.setState({
      main: { layerId: T.layerId, layer: T.layer, time: T.time, dims: T.dims },
      compare: { ...compare, ...P, follow: { p: follow.t, t: follow.p } },
      diffFitPending: true,
    });
  };

  return (
    <section className="error-panel">
      <div className="eyebrow">Error map</div>
      <MetricSelect value={choice.metric} onChange={(id) => setError({ metric: id })} />
      {metric && (
        <div className="muted small">
          {metric.label}: <b>{metric.symbol}</b>
          {metric.units === "%" ? " in %" : ""}
        </div>
      )}
      {choice.metric === CUSTOM ? (
        <FormulaInput key={choice.formula ?? ""} choice={choice} />
      ) : (
        Object.entries(metric?.params ?? {}).map(([k, p]) => (
          <ParamInput
            key={`${choice.metric}:${k}`}
            label={p.label}
            units={p.units === "same" ? tUnits : p.units}
            value={choice.params?.[k] ?? p.default}
            onCommit={(v) => setError({ params: { ...choice.params, [k]: v } })}
            extra={
              p.auto === "std_T" ? (
                <button
                  className="btn small"
                  title="Standard deviation of T over the map on screen"
                  onClick={() => {
                    const sd = viewStdOfT(useStore.getState());
                    if (sd === null) notify("info", "No T values on screen yet.");
                    else setError({ params: { ...choice.params, [k]: sd } });
                  }}
                >
                  σ of T in view
                </button>
              ) : undefined
            }
          />
        ))
      )}
      {pe?.problem && <div className="error-text small">{pe.problem}</div>}

      <SourceRow
        role="P"
        follow={compare.follow.p}
        choices={choices}
        fixedLabel={layerName(main.layer)}
        onFollow={(id) => void setFollow({ p: id })}
      >
        <div className="muted small">P's layer is chosen at the top of this panel.</div>
      </SourceRow>
      <SourceRow
        role="T"
        follow={compare.follow.t}
        choices={choices}
        fixedLabel={layerName(compare.layer)}
        onFollow={(id) => void setFollow({ t: id })}
      >
        <select
          value={compare.layerId ?? ""}
          onChange={(e) => void setCompare({ layerId: e.target.value, layer: null })}
          aria-label="Reference layer T"
        >
          {grids.map((l) => (
            <option key={l.id} value={l.id}>
              {l.title} {l.kind === "dataset" ? `(${l.id})` : ""}
            </option>
          ))}
        </select>
        <RefTime />
      </SourceRow>
      {compare.follow.t && compare.time && (
        <div className="muted small">T's time: {compare.time} (from the pane it follows)</div>
      )}
      {pe && pe.conv.warning && <div className="warn-text small">{pe.conv.warning}</div>}
      {pe && !pe.conv.warning && (pe.conv.scale !== 1 || pe.conv.offset !== 0) && (
        <div className="muted small">
          P is converted from {unitLabel(main.layer?.units)} to {tUnits} first.
        </div>
      )}
      <div className="row gap wrap">
        <button className="btn" onClick={swap} title="P becomes T and T becomes P">
          <Icon name="swap" /> Swap P and T
        </button>
        <button
          className="btn"
          onClick={() => {
            void setCompare({ mode: "off" });
            if (main.layerId) void setLayer(main.layerId);
          }}
          title="Show P's layer instead of the error map"
        >
          Show P only
        </button>
      </div>
      <p className="muted small">
        Computed per screen pixel from the two layers as drawn: units{" "}
        {unitLabel(drawUnits({ main, compare })) || "–"}.
      </p>
    </section>
  );
}

/** Compare menu: make an error pane from two panes, or turn this pane into an error map. */
export function NewErrorPane() {
  const active = useStore((s) => s.active);
  const compare = useStore((s) => s.compare);
  const main = useStore((s) => s.main);
  const catalog = useStore((s) => s.catalog);
  const choices = usePaneChoices(null);
  const { addErrorPane, setCompare, setError } = useStore.getState();
  const closeMenu = useMenuClose();
  const pDefault = choices.find((c) => c.id === active)?.id ?? choices[0]?.id ?? "";
  const [pId, setP] = useState<string | null>(null);
  const [tId, setT] = useState<string | null>(null);
  const [metric, setMetric] = useState("bias");
  const P = pId && choices.some((c) => c.id === pId) ? pId : pDefault;
  const T =
    tId && choices.some((c) => c.id === tId) && tId !== P ? tId : (choices.find((c) => c.id !== P)?.id ?? "");
  const [tLayer, setTLayer] = useState<string | null>(null);
  const grids = catalog?.layers.filter((l) => l.kind !== "points") ?? [];

  if (isErrorPane({ compare }))
    return (
      <p className="muted small">
        The active pane is an error map: set it up in the side panel (definition, P and T).
      </p>
    );
  return (
    <div className="new-error">
      {choices.length >= 2 ? (
        <>
          <div className="row gap wrap error-pick">
            <label className="field">
              <span className="eyebrow">P · compared</span>
              <select value={P} onChange={(e) => setP(e.target.value)}>
                {choices.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="eyebrow">T · reference</span>
              <select value={T} onChange={(e) => setT(e.target.value)}>
                {choices
                  .filter((c) => c.id !== P)
                  .map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.label}
                    </option>
                  ))}
              </select>
            </label>
          </div>
          <label className="field">
            <span className="eyebrow">Error</span>
            <MetricSelect value={metric} onChange={setMetric} />
          </label>
          <button
            className="btn primary"
            disabled={!P || !T}
            onClick={() => {
              addErrorPane(P, T, metric === CUSTOM ? { metric, formula: "P - T" } : { metric });
              closeMenu();
            }}
          >
            Add error pane
          </button>
          <p className="muted small">
            A new pane shows the error of P against T. It follows both panes: change their layer or time and
            the error map follows.
          </p>
        </>
      ) : (
        <>
          <label className="field">
            <span className="eyebrow">Error of {layerName(main.layer) || "this layer"} (P) against T</span>
            <select value={tLayer ?? main.layerId ?? ""} onChange={(e) => setTLayer(e.target.value)}>
              {grids.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.title} {l.kind === "dataset" ? `(${l.id})` : ""}
                </option>
              ))}
            </select>
          </label>
          <button
            className="btn"
            onClick={async () => {
              await setCompare({
                mode: "error",
                layerId: tLayer ?? main.layerId,
                layer: null,
                follow: { p: null, t: null },
              });
              setError({ metric });
              closeMenu();
            }}
          >
            Show the error map here
          </button>
          <p className="muted small">
            Or split the map first (key %) and show T in the new pane: then “Add error pane” makes a third
            pane that follows both.
          </p>
        </>
      )}
    </div>
  );
}
