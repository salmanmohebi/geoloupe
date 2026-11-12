// Top-right tools: comparison, basemap, globe, share link, export.

import { useState } from "react";
import { api } from "../api/client";
import { BASEMAPS } from "../lib/basemaps";
import { getLut } from "../lib/colormap";
import { exportLayoutPng, exportPng, type PaneFigure } from "../lib/exportImage";
import { drawStyle, drawTitle, drawUnits, isErrorPane, paneError } from "../lib/paneDraw";
import { getPaneMaps } from "../lib/mapRegistry";
import { MAX_PANES, paneIds, PRESET_PANES, preset, rects, type Preset } from "../lib/layout";
import { unitLabel } from "../lib/format";
import { paneOf, useStore, type Basemap } from "../state/store";
import { niceTicks } from "./Colorbar";
import { AddDataButton } from "./AddData";
import { NewErrorPane } from "./ErrorPanel";
import { MenuClose } from "./menuContext";
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
          <div className="menu-pop panel">
            <MenuClose.Provider value={() => setOpen(false)}>{children}</MenuClose.Provider>
          </div>
        </>
      )}
    </div>
  );
}

const PRESETS: { id: Preset; label: string }[] = [
  { id: "1", label: "One map" },
  { id: "2h", label: "Two, side by side" },
  { id: "2v", label: "Two, one above the other" },
  { id: "3", label: "Three: one large, two small" },
  { id: "4", label: "Four (2 × 2)" },
];

/** A small picture of a preset layout. */
function PresetIcon({ p }: { p: Preset }) {
  const ids = Array.from({ length: PRESET_PANES[p] }, (_, i) => String(i));
  return (
    <svg width="30" height="20" viewBox="0 0 30 20" aria-hidden>
      {Object.values(rects(preset(p, ids))).map((r, i) => (
        <rect
          key={i}
          x={1 + r.x * 28 + 0.75}
          y={1 + r.y * 18 + 0.75}
          width={r.w * 28 - 1.5}
          height={r.h * 18 - 1.5}
          rx="1.5"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.3"
        />
      ))}
    </svg>
  );
}

function PanesSection() {
  const count = useStore((s) => paneIds(s.layout).length);
  const { applyPreset, equalizePanes } = useStore.getState();
  return (
    <>
      <div className="eyebrow">
        Panes ({count} of at most {MAX_PANES})
      </div>
      <div className="preset-row">
        {PRESETS.map((p) => (
          <button key={p.id} className="btn preset" title={p.label} onClick={() => applyPreset(p.id)}>
            <PresetIcon p={p.id} />
          </button>
        ))}
        {count > 1 && (
          <button className="btn" onClick={equalizePanes} title="All dividers back to the middle">
            Equal sizes
          </button>
        )}
      </div>
      <p className="muted small">
        Each pane shows its own layer, time and colours; the side panel acts on the active pane (blue frame).
        Use the buttons in a pane's corner, or the keys <kbd>%</kbd> or <kbd>|</kbd> split right, <kbd>"</kbd>{" "}
        split down, <kbd>x</kbd> close, <kbd>z</kbd> zoom a pane, <kbd>Alt</kbd>+arrows move between panes.
        Drag a divider to resize it.
      </p>
    </>
  );
}

function ComparePanel() {
  return (
    <div className="compare-panel">
      <PanesSection />
      <div className="eyebrow section-gap">
        <Icon name="error" /> Error map: compare data with a reference
      </div>
      <NewErrorPane />
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
  const panes = useStore((s) => paneIds(s.layout).length);
  const zoomed = useStore((s) => s.zoomed);
  const picking = useStore((s) => s.picking);
  const { setBasemap, setGlobe, notify } = useStore.getState();

  const share = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      notify("info", "Link copied: it reopens this exact view (layer, time, colours, position).");
    } catch {
      notify("info", `Copy this link: ${window.location.href}`);
    }
  };

  const layoutPng = async () => {
    const s = useStore.getState();
    const boxes = rects(s.layout);
    const maps = getPaneMaps();
    const area = document.querySelector(".map-view");
    if (!area) return;
    const figures: PaneFigure[] = [];
    const times = new Set<string>();
    const used = new Set<string>();
    paneIds(s.layout).forEach((id, i) => {
      const p = paneOf(s, id);
      const map = maps[id];
      const l = p?.main.layer;
      if (!p || !l || !map) return;
      const diff = isErrorPane(p);
      const st = drawStyle(p);
      const bar = diff
        ? !paneError(p)?.e?.classes.length
        : l.kind !== "rgb" && !l.bands && !l.classes && l.kind !== "points";
      const name = drawTitle(p);
      figures.push({
        map,
        rect: boxes[id],
        label: `${i + 1} · ${name}${p.main.time ? ` · ${p.main.time}` : ""}`,
        scale: bar
          ? { lut: getLut(colormaps, st.colormap), vmin: st.vmin, vmax: st.vmax, log: st.log }
          : null,
        units: unitLabel(drawUnits(p)),
        ticks: niceTicks(st.vmin, st.vmax, st.log),
      });
      if (p.main.time) times.add(p.main.time);
      for (const src of l.sources) used.add(src.dataset);
    });
    if (!figures.length) return;
    const attrib = catalog?.datasets
      .filter((d) => used.has(d.id) && d.attribution)
      .map((d) => d.attribution)
      .join("; ");
    try {
      await exportLayoutPng(
        figures,
        { width: area.clientWidth, height: area.clientHeight },
        {
          title: figures.map((f) => f.label.split(" · ")[1]).join(" | "),
          subtitle: [times.size === 1 ? [...times][0] : "", `${figures.length} panes`]
            .filter(Boolean)
            .join(" · "),
          credit: `GeoLoupe${attrib ? ` · ${attrib}` : ""}`,
        },
      );
    } catch (e) {
      notify("error", `Image export failed: ${(e as Error).message}`);
    }
  };

  const png = async () => {
    if (!main.layer) return;
    const pane = { main, compare, style, diffStyle };
    const st = drawStyle(pane);
    const sources = main.layer.sources.map((s) => s.dataset_title.split(" (")[0]).join(", ");
    const attrib = catalog?.datasets
      .filter((d) => main.layer!.sources.some((s) => s.dataset === d.id) && d.attribution)
      .map((d) => d.attribution)
      .join("; ");
    try {
      await exportPng(
        { lut: getLut(colormaps, st.colormap), vmin: st.vmin, vmax: st.vmax, log: st.log },
        {
          title: isErrorPane(pane) ? drawTitle(pane) : main.layer.title,
          subtitle: [
            main.time,
            ...Object.entries(main.dims).map(([k, v]) => `${k} ${v}`),
            `Sources: ${sources}`,
          ]
            .filter(Boolean)
            .join(" · "),
          units: unitLabel(drawUnits(pane)),
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
      <button
        className={`btn tool${picking ? " on" : ""}`}
        aria-pressed={picking}
        onClick={() => useStore.getState().setPicking(!picking)}
        title={
          picking
            ? "Click the map to pick a point (its values and time series); click here or press Esc to stop"
            : "Pick a point: then a click on the map shows its values and time series (key i)"
        }
      >
        <Icon name="target" /> Point
      </button>
      <AddDataButton />
      {!!catalog?.views?.length && (
        <button className="btn tool" onClick={() => useStore.getState().setGalleryOpen(true)}>
          Gallery
        </button>
      )}
      <Menu
        label={
          panes > 1 ? `Compare: ${panes} panes` : isErrorPane({ compare }) ? "Compare: error" : "Compare"
        }
        active={isErrorPane({ compare }) || panes > 1}
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
          {panes > 1 ? "Active pane as an image (PNG)" : "Map image (PNG with legend)"}
        </button>
        {panes > 1 && (
          <button
            className="menu-item"
            onClick={layoutPng}
            disabled={!!zoomed}
            title={
              zoomed ? "Show all panes first (z)" : "Every pane as on screen, with its label and colour bar"
            }
          >
            All {panes} panes as one image (PNG)
          </button>
        )}
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
