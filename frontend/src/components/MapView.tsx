// The map area: one or more panes, split like tmux (lib/layout). Each pane is a MapLibre map with
// its own layer, time and colour scale; linked panes share the camera. Panes are placed
// absolutely from the layout tree, so splitting or resizing never re-creates a map. The hover
// read-out lists the value of every pane; the other panes mark the hovered point with a cross.

import type { Map as MlMap } from "maplibre-gl";
import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { useShallow } from "zustand/react/shallow";
import type { LayerMeta, VectorInfo } from "../api/types";
import { classTable } from "../lib/classes";
import { cssGradient, getLut } from "../lib/colormap";
import { dividers, paneIds, rects, type Divider, type Rect } from "../lib/layout";
import { setMainMap, setPaneMaps } from "../lib/mapRegistry";
import { useLatest } from "../lib/useLatest";
import { fmt, fmtLonLat, unitLabel } from "../lib/format";
import type { RenderSpec } from "../lib/protocol";
import { summarize, valueAt, type HoverValue } from "../lib/view";
import { paneOf, pointsLayerOf, useStore, type PaneState, type Style, type Target } from "../state/store";
import { colorExpression, fetchPoints, toGeoJSON, type PointsResult } from "../lib/points";
import { useFetch } from "../lib/useFetch";
import { featureDetails, featureLabel, visibleVectors } from "../lib/vectors";
import { Icon } from "./Icon";
import { MapCanvas, type FeatureHit, type MapEventInfo, type StationHit } from "./MapCanvas";

interface HoverRow {
  tag: string;
  value: HoverValue | null;
  layer: LayerMeta | null;
  units: string;
  diff: boolean;
}

interface Hover {
  pane: string;
  x: number;
  y: number;
  lng: number;
  lat: number;
  rows: HoverRow[];
  units: string; // of the hovered pane (stations)
  station?: StationHit;
  stationGrid?: HoverValue | null; // grid value at the station's location
  feature?: FeatureHit; // vector-overlay feature under the cursor
}

/** What a pane draws, for the hover read-out of all panes. */
interface PaneDraw {
  spec: RenderSpec | null;
  layer: LayerMeta | null;
  diff: boolean;
  units: string;
}

const FULL: Rect = { x: 0, y: 0, w: 1, h: 1 };
const MIN_PANE_PX = 140;

function makeSpec(
  t: Target,
  style: Style,
  colormaps: ReturnType<typeof useStore.getState>["colormaps"],
  minus: Target | null,
): RenderSpec | null {
  if (!t.layer || !t.layerId || t.layer.kind === "points") return null; // points are not tiles
  if (minus && (!minus.layer || !minus.layerId)) return null;
  return {
    data: { layerId: t.layerId, time: t.time, dims: t.dims },
    minus: minus ? { layerId: minus.layerId!, time: minus.time, dims: minus.dims } : null,
    scale: { lut: getLut(colormaps, style.colormap), vmin: style.vmin, vmax: style.vmax, log: style.log },
    tileSize: t.layer.tile_size,
    rgb: t.layer.kind === "rgb" && !minus ? { gamma: style.gamma } : null,
    classes:
      t.layer.classes && !minus ? classTable(t.layer.classes, getLut(colormaps, style.colormap)) : null,
  };
}

/** A pane's render spec: its layer, or A − B in difference mode. */
function paneSpec(p: PaneState, colormaps: ReturnType<typeof useStore.getState>["colormaps"]) {
  const diff = p.compare.mode === "diff";
  return p.styleReady
    ? makeSpec(p.main, diff ? p.diffStyle : p.style, colormaps, diff ? p.compare : null)
    : null;
}

const keyOf = (s: RenderSpec | null, cmap: string) =>
  s
    ? JSON.stringify([s.data, s.minus, cmap, s.scale.vmin, s.scale.vmax, s.scale.log, s.rgb, !!s.classes])
    : "none";

export function sourceLabel(layer: LayerMeta | null, source: number | null, level: number | null): string {
  if (!layer || source === null) return "";
  const s = layer.sources[source];
  if (!s) return "";
  const lv = level !== null ? s.levels[level] : null;
  const res = lv ? lv.resolution.split(" (")[0] : s.resolution.split(" (")[0];
  const tag = lv?.origin === "overview" ? " overview" : lv?.origin === "subsampled" ? " subsampled" : "";
  return `${s.dataset_title.split(" (")[0]} · ${res}${tag}`;
}

let lastTileError = 0;
function onTileError(msg: string) {
  const now = Date.now();
  if (now - lastTileError > 8000) {
    lastTileError = now;
    useStore.getState().notify("error", `Some tiles failed to load: ${msg}`);
  }
}

export function MapView() {
  const layout = useStore((s) => s.layout);
  const active = useStore((s) => s.active);
  const zoomed = useStore((s) => s.zoomed);
  const main = useStore((s) => s.main);
  const style = useStore((s) => s.style);
  const styleReady = useStore((s) => s.styleReady);
  const compare = useStore((s) => s.compare);
  const diffStyle = useStore((s) => s.diffStyle);
  const colormaps = useStore((s) => s.colormaps);
  const catalog = useStore((s) => s.catalog);
  const vectorsOn = useStore((s) => s.vectorsOn);
  const vectors = useMemo(() => visibleVectors(catalog, vectorsOn), [catalog, vectorsOn]);
  const { setView, setSummary, setPaneView } = useStore.getState();

  const ids = useMemo(() => paneIds(layout), [layout]);
  const multi = ids.length > 1;
  const boxes = useMemo<Record<string, Rect>>(
    () => (zoomed ? { [zoomed]: FULL } : rects(layout)),
    [layout, zoomed],
  );
  const links = useStore(
    useShallow((s) => ids.map((id) => (id === s.active ? s.linkView : (s.panes[id]?.linkView ?? true)))),
  );

  const maps = useRef<Record<string, MlMap>>({});
  const draws = useRef<Record<string, PaneDraw>>({});
  const area = useRef<HTMLDivElement>(null);
  const [mapsReady, setMapsReady] = useState(0);
  const [hover, setHover] = useState<Hover | null>(null);

  // ---- provenance summary of the active pane
  const diff = compare.mode === "diff";
  const specA = useMemo(
    () => (styleReady ? makeSpec(main, diff ? diffStyle : style, colormaps, diff ? compare : null) : null),
    [main, style, diffStyle, colormaps, compare, diff, styleReady],
  );
  const specRef = useLatest<RenderSpec | null>(specA);
  const layerRef = useLatest(main.layer);
  const activeRef = useLatest(active);
  const refreshSummary = useCallback(() => {
    const m = maps.current[activeRef.current];
    const spec = specRef.current;
    const layer = layerRef.current;
    if (!m || !spec || !layer) return setSummary(null);
    setSummary(summarize(m, spec, layer));
  }, [setSummary, specRef, layerRef, activeRef]);

  useEffect(() => setPaneMaps(maps.current, ids), [ids, mapsReady]);
  useEffect(() => {
    const m = maps.current[active];
    setMainMap(m ?? null); // image export and end-to-end tests use the active pane
    if (!m) return;
    let raf = 0;
    const onIdle = () => refreshSummary();
    const onData = (e: { sourceId?: string; tile?: unknown }) => {
      if (!e.tile) return;
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(refreshSummary);
    };
    m.on("idle", onIdle);
    m.on("sourcedata", onData);
    refreshSummary();
    return () => {
      m.off("idle", onIdle);
      m.off("sourcedata", onData);
      cancelAnimationFrame(raf);
    };
  }, [mapsReady, active, refreshSummary]);

  // ---- each pane's camera, for links; the active one also refreshes the summary
  useEffect(() => {
    const offs = ids.map((id) => {
      const m = maps.current[id];
      if (!m) return () => {};
      const onEnd = () => {
        const c = m.getCenter();
        if (id !== activeRef.current) return setPaneView(id, { center: [c.lng, c.lat], zoom: m.getZoom() });
        const b = m.getBounds();
        setView({
          center: [c.lng, c.lat],
          zoom: m.getZoom(),
          bounds: [b.getWest(), Math.max(b.getSouth(), -90), b.getEast(), Math.min(b.getNorth(), 90)],
        });
        refreshSummary();
      };
      m.on("moveend", onEnd);
      return () => m.off("moveend", onEnd);
    });
    return () => offs.forEach((off) => off());
  }, [ids, mapsReady, activeRef, refreshSummary, setView, setPaneView]);

  // ---- first layer: frame its extent (unless the URL gave a view)
  const framed = useRef(!!useStore.getState().view);
  useEffect(() => {
    const m = maps.current[active];
    if (!m || !main.layer || framed.current) return;
    framed.current = true;
    const [w, s, e, n] = main.layer.bounds;
    if (e - w < 300)
      m.fitBounds(
        [
          [w, Math.max(s, -80)],
          [e, Math.min(n, 80)],
        ],
        { padding: 60, duration: 0, maxZoom: 9 },
      );
  }, [main.layer, mapsReady, active]);

  // ---- linked panes share the camera; a pane that joins takes the group's camera
  const prevLinked = useRef<string[]>([]);
  const linkKey = ids.map((id, i) => `${id}:${links[i] ? 1 : 0}`).join(",");
  useEffect(() => {
    const linked = ids.filter((id, i) => links[i] && maps.current[id]);
    const joined = linked.filter((id) => !prevLinked.current.includes(id));
    prevLinked.current = linked;
    if (linked.length < 2) return;
    let lock = false;
    const follow = (src: MlMap) => () => {
      if (lock) return;
      lock = true;
      const cam = {
        center: src.getCenter(),
        zoom: src.getZoom(),
        bearing: src.getBearing(),
        pitch: src.getPitch(),
      };
      for (const id of linked) if (maps.current[id] !== src) maps.current[id].jumpTo(cam);
      lock = false;
    };
    const handlers = linked.map((id) => {
      const m = maps.current[id];
      const f = follow(m);
      m.on("move", f);
      return [m, f] as const;
    });
    const lead =
      linked.find((id) => !joined.includes(id)) ??
      (linked.includes(activeRef.current) ? activeRef.current : linked[0]);
    follow(maps.current[lead])();
    return () => handlers.forEach(([m, f]) => m.off("move", f));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- linkKey stands for ids and links
  }, [linkKey, mapsReady, activeRef]);

  const onMap = useCallback(
    (id: string) => (m: MlMap | null) => {
      if (m) maps.current[id] = m;
      else delete maps.current[id];
      setMapsReady((k) => k + 1);
    },
    [],
  );

  const shown = useMemo(() => ids.filter((id) => boxes[id]), [ids, boxes]);
  const shownRef = useLatest(shown);
  const allIds = useLatest(ids);
  const onHover = useCallback(
    (id: string) => (e: MapEventInfo | null) => {
      if (!e) return setHover(null);
      const many = shownRef.current.length > 1;
      const rows = shownRef.current.map((p) => {
        const d = draws.current[p];
        const m = maps.current[p];
        const value = d?.spec && d.layer && m ? valueAt(d.spec, d.layer, m.getZoom(), e.lng, e.lat) : null;
        const tag = many ? String(allIds.current.indexOf(p) + 1) : d?.diff ? "A − B" : "";
        return { tag, value, layer: d?.layer ?? null, units: d?.units ?? "", diff: !!d?.diff };
      });
      const d = draws.current[id];
      const m = maps.current[id];
      const st = e.station;
      const stationGrid =
        st && d?.spec && d.layer && !d.diff && m
          ? valueAt(d.spec, d.layer, m.getZoom(), st.lon, st.lat)
          : undefined;
      setHover({ pane: id, ...e, rows, units: d?.units ?? "", stationGrid });
    },
    [shownRef, allIds],
  );

  const report = useCallback((id: string, d: PaneDraw) => {
    draws.current[id] = d;
  }, []);

  return (
    <div className={`map-view${multi ? " multi" : ""}`} ref={area}>
      {ids.map((id, i) => (
        <PaneMap
          key={id}
          id={id}
          index={i}
          rect={boxes[id]}
          multi={multi}
          hover={hover}
          onMap={onMap}
          onHover={onHover}
          onSpecShown={refreshSummary}
          report={report}
        />
      ))}
      {!zoomed && dividers(layout).map((d) => <DividerHandle key={d.path || "root"} d={d} area={area} />)}
      {hover && boxes[hover.pane] && (
        <HoverTip hover={hover} vectors={vectors} box={boxes[hover.pane]} area={area} />
      )}
    </div>
  );
}

function PaneMap({
  id,
  index,
  rect,
  multi,
  hover,
  onMap,
  onHover,
  onSpecShown,
  report,
}: {
  id: string;
  index: number;
  rect: Rect | undefined;
  multi: boolean;
  hover: Hover | null;
  onMap: (id: string) => (m: MlMap | null) => void;
  onHover: (id: string) => (e: MapEventInfo | null) => void;
  onSpecShown: () => void;
  report: (id: string, d: PaneDraw) => void;
}) {
  const pane = useStore(useShallow((s) => paneOf(s, id)));
  const isActive = useStore((s) => s.active === id);
  const colormaps = useStore((s) => s.colormaps);
  const basemap = useStore((s) => s.basemap);
  const globe = useStore((s) => s.globe);
  const inspect = useStore((s) => s.inspect);
  const catalog = useStore((s) => s.catalog);
  const vectorsOn = useStore((s) => s.vectorsOn);
  const showPoints = useStore((s) => s.showPoints);
  const overlay = useStore((s) => s.overlay);
  const vectors = useMemo(() => visibleVectors(catalog, vectorsOn), [catalog, vectorsOn]);
  const { setInspect, notify, activatePane } = useStore.getState();

  const p = pane ?? null;
  const diff = p?.compare.mode === "diff";
  const st = p ? (diff ? p.diffStyle : p.style) : null;
  const spec = useMemo(() => (p ? paneSpec(p, colormaps) : null), [p, colormaps]);
  const key = keyOf(spec, st?.colormap ?? "");
  const layer = p?.main.layer ?? null;
  useEffect(
    () => report(id, { spec, layer, diff: !!diff, units: unitLabel(layer?.units) }),
    [report, id, spec, layer, diff],
  );

  // ---- point observations: the layer itself, or an overlay (not in difference mode)
  const pointsLayer = p ? pointsLayerOf({ main: p.main, compare: p.compare, showPoints, overlay }) : null;
  const time = p?.main.time ?? null;
  const pointsKey = pointsLayer && time ? JSON.stringify([pointsLayer, time]) : null;
  const { data: points, error: pointsError } = useFetch<PointsResult>(pointsKey, (signal) =>
    fetchPoints(pointsLayer!, time!, signal),
  );
  const shownPoints = pointsKey ? points : null;
  useEffect(() => {
    if (isActive) useStore.getState().setPoints(shownPoints);
  }, [isActive, shownPoints]);
  useEffect(() => {
    if (pointsError) notify("error", `Stations could not be loaded: ${pointsError}`);
  }, [pointsError, notify]);
  const geojson = useMemo(() => toGeoJSON(shownPoints), [shownPoints]);
  const pointColor = useMemo(
    () =>
      !p || diff || !p.styleReady
        ? null
        : colorExpression({
            lut: getLut(colormaps, p.style.colormap),
            vmin: p.style.vmin,
            vmax: p.style.vmax,
            log: p.style.log,
          }),
    [p, diff, colormaps],
  );

  const onClick = (e: MapEventInfo) => {
    if (e.station && shownPoints)
      setInspect({
        lng: e.station.lon,
        lat: e.station.lat,
        station: {
          source: shownPoints.source,
          id: e.station.id,
          name: e.station.name,
          variable: shownPoints.variable,
          elevation: e.station.elevation,
        },
      });
    else if (layer?.kind !== "points") setInspect({ lng: e.lng, lat: e.lat });
  };

  if (!p) return null;
  const box = rect
    ? {
        left: `${rect.x * 100}%`,
        top: `${rect.y * 100}%`,
        width: `${rect.w * 100}%`,
        height: `${rect.h * 100}%`,
      }
    : { display: "none" };
  return (
    <div
      className={`map-pane${multi && isActive ? " active" : ""}${rect && rect.y + rect.h > 0.999 ? " at-bottom" : ""}`}
      style={box}
      data-pane={index + 1}
      onPointerDownCapture={() => {
        if (!isActive) activatePane(id);
      }}
    >
      <MapCanvas
        spec={spec}
        specKey={key}
        maxzoom={layer?.maxzoom ?? 12}
        bounds={layer?.bounds ?? null}
        opacity={p.style.opacity}
        basemap={basemap}
        globe={globe}
        marker={inspect}
        cursor={hover && hover.pane !== id ? hover : null}
        initialView={p.view}
        onMap={onMap(id)}
        onHover={onHover(id)}
        onClick={onClick}
        points={geojson}
        pointColor={pointColor}
        onSpecShown={isActive ? onSpecShown : undefined}
        onTileError={onTileError}
        vectors={vectors}
      />
      <PaneBox id={id} index={index} multi={multi} pane={p} isActive={isActive} />
    </div>
  );
}

/** A pane's corner box: number, layer, time, colour legend, links, split / zoom / close. */
function PaneBox({
  id,
  index,
  multi,
  pane: p,
  isActive,
}: {
  id: string;
  index: number;
  multi: boolean;
  pane: PaneState;
  isActive: boolean;
}) {
  const colormaps = useStore((s) => s.colormaps);
  const zoomed = useStore((s) => s.zoomed === id);
  const { splitPane, closePane, toggleZoom, setLinks, activatePane } = useStore.getState();
  // the buttons act on the active pane: make this one active first
  const act = (f: () => void) => (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!isActive) activatePane(id);
    f();
  };
  const layer = p.main.layer;
  const diff = p.compare.mode === "diff";
  const st = diff ? p.diffStyle : p.style;
  const name = (l: LayerMeta | null) =>
    !l
      ? ""
      : l.kind === "dataset" && l.sources[0] // the same variable of several datasets: say which one
        ? `${l.title} · ${l.sources[0].dataset_title.split(" (")[0]}`
        : l.title;
  const title = diff ? `${name(layer)} − ${name(p.compare.layer)}` : name(layer);
  const legend =
    layer &&
    p.styleReady &&
    layer.kind !== "rgb" &&
    !layer.bands &&
    !layer.classes &&
    layer.kind !== "points";
  return (
    <div className={`pane-box${multi ? " multi" : ""}`} onPointerDown={(e) => e.stopPropagation()}>
      {multi && (
        <>
          <span
            className="pane-num"
            title={isActive ? "Active pane: the panels act on it" : "Click to make active"}
          >
            {index + 1}
          </span>
          <span className="pane-title" title={title}>
            {title}
            {p.main.time && <span className="pane-time"> · {p.main.time}</span>}
          </span>
          {legend && (
            <span className="pane-legend" title={`${st.colormap}${st.log ? ", log scale" : ""}`}>
              <span
                className="pane-grad"
                style={{ background: cssGradient(getLut(colormaps, st.colormap)) }}
              />
              {fmt(st.vmin)} – {fmt(st.vmax)} {diff ? "" : unitLabel(layer.units)}
            </span>
          )}
          <button
            className={p.linkView ? "on" : ""}
            title={
              p.linkView
                ? "Camera linked to the other panes (click to unlink)"
                : "Camera not linked (click to link)"
            }
            onClick={act(() => void setLinks({ view: !p.linkView }))}
          >
            <Icon name="link" />
          </button>
          <button
            className={p.linkTime ? "on" : ""}
            title={
              p.linkTime
                ? "Time linked to the other panes (click to unlink)"
                : "Time not linked (click to link)"
            }
            onClick={act(() => void setLinks({ time: !p.linkTime }))}
          >
            <Icon name="clock" />
          </button>
          <span className="pane-sep" />
        </>
      )}
      <button title="Split right (% or |)" onClick={act(() => splitPane("row"))}>
        <Icon name="splitRight" />
      </button>
      <button title='Split down (")' onClick={act(() => splitPane("col"))}>
        <Icon name="splitDown" />
      </button>
      {(multi || zoomed) && (
        <button
          className={zoomed ? "on" : ""}
          title={zoomed ? "Show all panes (z)" : "Zoom: show this pane alone (z)"}
          onClick={act(() => toggleZoom())}
        >
          <Icon name="expand" />
        </button>
      )}
      {multi && (
        <button title="Close pane (x)" onClick={act(() => closePane())}>
          <Icon name="close" />
        </button>
      )}
    </div>
  );
}

/** A divider between two panes: drag to resize, double-click for equal halves. */
function DividerHandle({ d, area }: { d: Divider; area: RefObject<HTMLDivElement | null> }) {
  const { setPaneRatio } = useStore.getState();
  const drag = useRef(false);
  const row = d.dir === "row";
  const pos = row
    ? {
        left: `${(d.parent.x + d.parent.w * d.ratio) * 100}%`,
        top: `${d.parent.y * 100}%`,
        height: `${d.parent.h * 100}%`,
      }
    : {
        top: `${(d.parent.y + d.parent.h * d.ratio) * 100}%`,
        left: `${d.parent.x * 100}%`,
        width: `${d.parent.w * 100}%`,
      };
  const move = (e: React.PointerEvent) => {
    if (!drag.current || !area.current) return;
    const r = area.current.getBoundingClientRect();
    const size = row ? d.parent.w * r.width : d.parent.h * r.height;
    const at = row
      ? (e.clientX - r.left) / r.width - d.parent.x
      : (e.clientY - r.top) / r.height - d.parent.y;
    const min = Math.min(0.45, MIN_PANE_PX / size);
    setPaneRatio(d.path, Math.min(1 - min, Math.max(min, (at * (row ? r.width : r.height)) / size)));
  };
  return (
    <div
      className={`pane-divider ${d.dir}`}
      style={pos}
      title="Drag to resize; double-click for equal sizes"
      onPointerDown={(e) => {
        e.preventDefault();
        (e.target as HTMLElement).setPointerCapture(e.pointerId);
        drag.current = true;
      }}
      onPointerMove={move}
      onPointerUp={() => (drag.current = false)}
      onPointerCancel={() => (drag.current = false)}
      onDoubleClick={() => setPaneRatio(d.path, 0.5)}
    />
  );
}

/** The hover read-out, above all panes; it flips left or up near the edges of the map area. */
function HoverTip({
  hover: h,
  vectors,
  box,
  area,
}: {
  hover: Hover;
  vectors: VectorInfo[];
  box: Rect;
  area: RefObject<HTMLDivElement | null>;
}) {
  const W = area.current?.clientWidth ?? 0;
  const H = area.current?.clientHeight ?? 0;
  const px = box.x * W + h.x;
  const py = box.y * H + h.y;
  const flipX = px > W - 330;
  const flipY = py > H - 230;
  const place = {
    left: flipX ? px - 14 : px + 14,
    top: flipY ? py - 14 : py + 14,
    transform: `translate(${flipX ? "-100%" : "0"}, ${flipY ? "-100%" : "0"})`,
  };
  const rows = h.rows.map((r, i) => {
    const v = r.value;
    const has = v !== null && v.value !== null;
    return (
      <div className="tip-row" key={i}>
        {r.tag && <span className="tip-tag">{r.tag}</span>}
        <span className="tip-value">
          {v?.bands
            ? v.bands.map((b, k) => `${"RGB"[k]} ${b === null ? "–" : fmt(b)}`).join("  ")
            : has && r.layer?.classes
              ? (r.layer.classes.find((c) => c.value === Math.round(v!.value!))?.name ??
                `class ${fmt(v!.value)}`)
              : has
                ? fmt(v!.value)
                : v
                  ? "no data"
                  : h.rows.length > 1
                    ? "–"
                    : "loading…"}
        </span>
        {has && <span className="tip-unit">{r.units}</span>}
        {has && v!.source !== null && !r.diff && (
          <span className="tip-src">
            {sourceLabel(r.layer, v!.source, v!.level)}
            {v!.approximate ? " (coarser tile)" : ""}
          </span>
        )}
      </div>
    );
  });
  const u = h.units;
  const st = h.station;
  const grid = h.stationGrid?.value ?? null;
  const fv = h.feature && vectors.find((v) => v.id === h.feature!.vector);
  const fname = fv ? featureLabel(h.feature!.props, fv.label) : null;
  return (
    <div className="hover-tip" style={place}>
      {fv && !st && (
        <div className="tip-feature">
          <div className="tip-row">
            <span className="tip-tag">{fv.title}</span>
            {fname && <b>{fname}</b>}
          </div>
          {featureDetails(h.feature!.props, fv.label ?? "name").map((d) => (
            <div className="tip-src" key={d}>
              {d}
            </div>
          ))}
        </div>
      )}
      {st ? (
        <>
          <div className="tip-row">
            <span className="tip-tag">station</span>
            <b>{st.name}</b>
            {st.elevation !== null && <span className="tip-src">{Math.round(st.elevation)} m</span>}
          </div>
          <div className="tip-row">
            <span className="tip-value">{fmt(st.value)}</span>
            <span className="tip-unit">{u} observed</span>
          </div>
          {grid !== null && (
            <div className="tip-row">
              <span className="tip-src">
                map {fmt(grid)} {u} · observed − map {st.value - grid >= 0 ? "+" : ""}
                {fmt(st.value - grid, 3)}
              </span>
            </div>
          )}
        </>
      ) : (
        rows
      )}
      <div className="tip-coord">{fmtLonLat(h.lng, h.lat)} · click for details</div>
    </div>
  );
}
