// Top-right tools: comparison, basemap, globe, share link, export.

import { useState } from "react";
import { api } from "../api/client";
import { BASEMAPS } from "../lib/basemaps";
import { getLut } from "../lib/colormap";
import { exportPng } from "../lib/exportImage";
import { unitLabel } from "../lib/format";
import { timelineOf } from "../lib/timeline";
import { useStore, validTime, type Basemap, type CompareMode } from "../state/store";
import { niceTicks } from "./Colorbar";
import { AddDataButton } from "./AddData";
import { Icon } from "./Icon";

function Menu({ label, children, active }: { label: string; children: React.ReactNode; active?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="menu" onKeyDown={(e) => e.key === "Escape" && setOpen(false)}>
      <button
        className={`btn tool ${active ? "on" : ""}`}
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        {label} <Icon name="down" size={12} />
      </button>
      {open && (
        <>
          <div className="menu-backdrop" onClick={() => setOpen(false)} />
          <div className="menu-pop panel">{children}</div>
        </>
      )}
    </div>
  );
}

function ComparePanel() {
  const catalog = useStore((s) => s.catalog);
  const compare = useStore((s) => s.compare);
  const main = useStore((s) => s.main);
  const { setCompare, setDiffStyle } = useStore.getState();
  const modes: { id: CompareMode; label: string; help: string }[] = [
    { id: "off", label: "Off", help: "" },
    { id: "split", label: "Side by side", help: "Two synchronised maps with the same colour scale." },
    { id: "diff", label: "Difference A − B", help: "One map of A minus B at display resolution." },
  ];
  const tl = timelineOf(compare.layer);
  const [bTime, setBTime] = useState<string | null>(null);
  return (
    <div className="compare-panel">
      <div className="eyebrow">Compare A (current layer) with B</div>
      <div className="seg">
        {modes.map((m) => (
          <button
            key={m.id}
            className={compare.mode === m.id ? "sel" : ""}
            onClick={async () => {
              await setCompare({ mode: m.id, layerId: compare.layerId ?? main.layerId });
              if (m.id === "diff") setDiffStyle({ colormap: "RdBu_r" });
            }}
          >
            {m.label}
          </button>
        ))}
      </div>
      {compare.mode !== "off" && (
        <>
          <p className="muted small">{modes.find((m) => m.id === compare.mode)?.help}</p>
          <label className="field">
            <span className="eyebrow">B layer</span>
            <select
              value={compare.layerId ?? ""}
              onChange={(e) => setCompare({ layerId: e.target.value, layer: null })}
            >
              {catalog?.layers.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.title} {l.kind === "dataset" ? `(${l.id})` : ""}
                </option>
              ))}
            </select>
          </label>
          {tl.count > 1 && (
            <>
              <label className="check">
                <input
                  type="checkbox"
                  checked={compare.linkTime}
                  onChange={(e) => setCompare({ linkTime: e.target.checked })}
                />
                same time as A
              </label>
              {!compare.linkTime && (
                <label className="field">
                  <span className="eyebrow">
                    B time ({tl.at(0)} … {tl.at(tl.count - 1)})
                  </span>
                  <input
                    type="text"
                    className="time-input"
                    value={bTime ?? compare.time ?? ""}
                    onChange={(e) => setBTime(e.target.value)}
                    onBlur={() => {
                      if (bTime && compare.layer)
                        setCompare({ time: validTime(compare.layer, bTime.trim()) });
                      setBTime(null);
                    }}
                    onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
                    aria-label="Time of B (ISO 8601)"
                  />
                </label>
              )}
            </>
          )}
          {compare.mode === "diff" && (
            <p className="muted small">
              Both layers are sampled on the same screen pixels, each at its own best resolution, then
              subtracted. Use the colour panel to set the symmetric range (Fit to view).
            </p>
          )}
        </>
      )}
    </div>
  );
}

export function Toolbar() {
  const basemap = useStore((s) => s.basemap);
  const globe = useStore((s) => s.globe);
  const compare = useStore((s) => s.compare);
  const main = useStore((s) => s.main);
  const style = useStore((s) => s.style);
  const diffStyle = useStore((s) => s.diffStyle);
  const colormaps = useStore((s) => s.colormaps);
  const view = useStore((s) => s.view);
  const catalog = useStore((s) => s.catalog);
  const { setBasemap, setGlobe, notify } = useStore.getState();

  const share = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      notify("info", "Link copied: it reopens this exact view (layer, time, colours, position).");
    } catch {
      notify("info", `Copy this link: ${window.location.href}`);
    }
  };

  const png = async () => {
    if (!main.layer) return;
    const st = compare.mode === "diff" ? diffStyle : style;
    const sources = main.layer.sources.map((s) => s.dataset_title.split(" (")[0]).join(", ");
    const attrib = catalog?.datasets
      .filter((d) => main.layer!.sources.some((s) => s.dataset === d.id) && d.attribution)
      .map((d) => d.attribution)
      .join("; ");
    try {
      await exportPng(
        { lut: getLut(colormaps, st.colormap), vmin: st.vmin, vmax: st.vmax, log: st.log },
        {
          title: compare.mode === "diff" ? `${main.layer.title} − ${compare.layer?.title}` : main.layer.title,
          subtitle: [
            main.time,
            ...Object.entries(main.dims).map(([k, v]) => `${k} ${v}`),
            `Sources: ${sources}`,
          ]
            .filter(Boolean)
            .join(" · "),
          units: unitLabel(main.layer.units),
          ticks: niceTicks(st.vmin, st.vmax, st.log),
          credit: `GeoLoupe${attrib ? ` · ${attrib}` : ""}`,
        },
      );
    } catch (e) {
      notify("error", `Image export failed: ${(e as Error).message}`);
    }
  };

  const finest = main.layer?.sources[0];
  const exportNc = () => {
    if (!finest || !view?.bounds) return;
    const [w, s, e, n] = view.bounds;
    window.open(
      api.exportUrl(finest.id, [Math.max(w, -180), s, Math.min(e, 180), n], main.time, main.dims),
      "_blank",
    );
  };

  return (
    <div className="toolbar">
      <AddDataButton />
      {!!catalog?.views?.length && (
        <button className="btn tool" onClick={() => useStore.getState().setGalleryOpen(true)}>
          Gallery
        </button>
      )}
      <Menu
        label={
          compare.mode === "off"
            ? "Compare"
            : compare.mode === "split"
              ? "Compare: side by side"
              : "Compare: A − B"
        }
        active={compare.mode !== "off"}
      >
        <ComparePanel />
      </Menu>
      <Menu label="Map">
        <div className="eyebrow">Basemap</div>
        {BASEMAPS.map((b) => (
          <label className="radio" key={b.id}>
            <input
              type="radio"
              name="basemap"
              checked={basemap === b.id}
              onChange={() => setBasemap(b.id as Basemap)}
            />
            {b.label}
            {b.online && <span className="muted small"> · online</span>}
          </label>
        ))}
        <div className="eyebrow">Projection</div>
        <label className="check">
          <input type="checkbox" checked={globe} onChange={(e) => setGlobe(e.target.checked)} />
          3-D globe
        </label>
      </Menu>
      <Menu label="Export">
        <button className="menu-item" onClick={png}>
          Map image (PNG with legend)
        </button>
        <button
          className="menu-item"
          onClick={exportNc}
          disabled={!finest || !view?.bounds}
          title="Native resolution, current time"
        >
          Data in view as NetCDF
          <div className="muted small">
            {finest ? `${finest.dataset_title.split(" (")[0]}, native grid` : ""}
          </div>
        </button>
        <button className="menu-item" onClick={share}>
          Copy link to this view
        </button>
        <a className="menu-item" href="api/docs" target="_blank" rel="noreferrer">
          API documentation ↗
        </a>
      </Menu>
    </div>
  );
}
