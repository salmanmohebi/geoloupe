// The map area: one or more panes, split like tmux (lib/layout). Each pane is a MapLibre map with
// its own layer, time and colour scale; linked panes share the camera. Panes are placed
// absolutely from the layout tree, so splitting or resizing never re-creates a map. The hover
// read-out lists the value of every pane; the other panes mark the hovered point with a cross.

import type { Map as MlMap } from "maplibre-gl";
import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { useShallow } from "zustand/react/shallow";
import { api } from "../api/client";
import type { ErrorStats, LayerMeta, VectorInfo } from "../api/types";
import { classTable, type ClassDef } from "../lib/classes";
import { cssGradient, getLut } from "../lib/colormap";
import { dividers, paneIds, rects, type Divider, type Rect } from "../lib/layout";
import {
  drawStyle,
  drawTitle,
  drawUnits,
  errorQuery,
  exactness,
  isErrorPane,
  paneError,
  statsLine,
} from "../lib/paneDraw";
import { setMainMap, setPaneMaps } from "../lib/mapRegistry";
import { useLatest } from "../lib/useLatest";
import { fmt, fmtLonLat, unitLabel } from "../lib/format";
import { getSpec, type RenderSpec } from "../lib/protocol";
import { summarize, valueAt, type HoverValue } from "../lib/view";
import { fmtOffset, paneOf, pointsLayerOf, useStore, type PaneState, type Target } from "../state/store";
import { colorExpression, fetchPoints, toGeoJSON, type PointsResult } from "../lib/points";
import { useFetch } from "../lib/useFetch";
import { tileZoom } from "../lib/mercator";
import { featureDetails, featureLabel, visibleVectors } from "../lib/vectors";
import { Icon } from "./Icon";
import { MapCanvas, type FeatureHit, type MapEventInfo, type StationHit } from "./MapCanvas";

interface HoverRow {
  tag: string;
  value: HoverValue | null;
  layer: LayerMeta | null;
  units: string;
  diff: boolean; // an error map (no single source to name)
  classes: ClassDef[] | null;
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
  classes: ClassDef[] | null;
}

const FULL: Rect = { x: 0, y: 0, w: 1, h: 1 };
const MIN_PANE_PX = 140;

type Colormaps = ReturnType<typeof useStore.getState>["colormaps"];
type Drawable = Pick<PaneState, "main" | "style" | "styleReady" | "compare" | "diffStyle">;

const ref = (t: Target) => ({ layerId: t.layerId!, time: t.time, dims: t.dims });

/** A pane's render spec: its layer, or the error map of its layer (P) against T: a preview
 * from the two display tiles, or (`exact`) the server's tiles computed from native cells. */
export function paneSpec(p: Drawable, colormaps: Colormaps, exact = false): RenderSpec | null {
  const t = p.main;
  if (!p.styleReady || !t.layer || !t.layerId || t.layer.kind === "points") return null; // points are not tiles
  const style = drawStyle(p);
  const scale = {
    lut: getLut(colormaps, style.colormap),
    vmin: style.vmin,
    vmax: style.vmax,
    log: style.log,
  };
  const base = { data: ref(t), scale, tileSize: t.layer.tile_size };
  if (!isErrorPane(p))
    return {
      ...base,
      ref: null,
      error: null,
      rgb: t.layer.kind === "rgb" ? { gamma: style.gamma } : null,
      classes: t.layer.classes ? classTable(t.layer.classes, getLut(colormaps, style.colormap)) : null,
    };
  const c = p.compare;
  const pe = paneError(p);
  if (!c.layer || !c.layerId || c.layer.kind === "points" || !pe?.e) return null;
  return {
    ...base,
    ref: ref(c),
    error: {
      fn: pe.e.fn,
      scale: pe.conv.scale,
      offset: pe.conv.offset,
      key: pe.key,
      server: exact ? errorQuery(p) : null,
    },
    rgb: null,
    classes: pe.e.classes.length ? classTable(pe.e.classes, getLut(colormaps, style.colormap)) : null,
  };
}

/** Classes of what a pane draws (for hover names), or null. */
function drawClasses(p: Drawable): ClassDef[] | null {
  if (!isErrorPane(p)) return p.main.layer?.classes ?? null;
  const cls = paneError(p)?.e?.classes;
  return cls?.length ? cls : null;
}

const keyOf = (s: RenderSpec | null, cmap: string) =>
  s
    ? JSON.stringify([
        s.data,
        s.ref,
        s.error?.key,
        s.error?.server,
        cmap,
        s.scale.vmin,
        s.scale.vmax,
        s.scale.log,
        s.rgb,
        !!s.classes,
      ])
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
  const picking = useStore((s) => s.picking);
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
  const specA = useMemo(
    () => paneSpec({ main, style, styleReady, compare, diffStyle }, colormaps),
    [main, style, diffStyle, colormaps, compare, styleReady],
  );
  const specRef = useLatest<RenderSpec | null>(specA);
  const layerRef = useLatest(main.layer);
  const activeRef = useLatest(active);
  const refreshSummary = useCallback(() => {
    const m = maps.current[activeRef.current];
    // the spec the active pane shows (error panes: preview or exact tiles)
    const spec = draws.current[activeRef.current]?.spec ?? specRef.current;
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
        const b = m.getBounds();
        const v = {
          center: [c.lng, c.lat] as [number, number],
          zoom: m.getZoom(),
          bounds: [b.getWest(), Math.max(b.getSouth(), -90), b.getEast(), Math.min(b.getNorth(), 90)] as [
            number,
            number,
            number,
            number,
          ],
        };
        if (id !== activeRef.current) return setPaneView(id, v);
        setView(v);
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
        const tag = many ? String(allIds.current.indexOf(p) + 1) : "";
        return {
          tag,
          value,
          layer: d?.layer ?? null,
          units: d?.units ?? "",
          diff: !!d?.diff,
          classes: d?.classes ?? null,
        };
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
    <div className={`map-view${multi ? " multi" : ""}${picking ? " picking" : ""}`} ref={area}>
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
        <HoverTip hover={hover} vectors={vectors} box={boxes[hover.pane]} area={area} picking={picking} />
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
  const panelOpen = useStore((s) => s.panelOpen);
  const picking = useStore((s) => s.picking);
  const vectors = useMemo(() => visibleVectors(catalog, vectorsOn), [catalog, vectorsOn]);
  const { setInspect, notify, activatePane } = useStore.getState();

  const p = pane ?? null;
  const diff = !!p && isErrorPane(p);
  const st = p ? drawStyle(p) : null;
  // Error maps: the preview (from P's and T's display tiles) first, then the exact tiles of the
  // server once the preview is on screen. `refined` names the data the exact tiles are for, so
  // colour changes keep them; a new time, layer or definition starts with a preview again.
  const preview = useMemo(() => (p ? paneSpec(p, colormaps) : null), [p, colormaps]);
  const exactSpec = useMemo(() => (p && diff ? paneSpec(p, colormaps, true) : null), [p, diff, colormaps]);
  const dataKey = exactSpec ? JSON.stringify([exactSpec.error?.server, exactSpec.error?.key]) : "";
  const [refined, setRefined] = useState("");
  const spec = exactSpec && refined === dataKey ? exactSpec : preview;
  const key = keyOf(spec, st?.colormap ?? "");
  // MapCanvas calls the latest version of this (through a ref) when a spec is on screen
  // The spec on screen: MapCanvas swaps a new spec in only once its tiles are loaded, so hover
  // values and the summary must read the shown spec, not the newest one.
  const [shownSpec, setShownSpec] = useState<RenderSpec | null>(null);
  const specShown = (sid: string) => {
    const now = getSpec(sid) ?? spec;
    setShownSpec(now);
    if (dataKey && !now?.error?.server) setRefined(dataKey);
  };
  const layer = p?.main.layer ?? null;

  // view statistics of an error pane (native cells in view), once its exact tiles are asked for
  const q = useMemo(() => (p && diff ? errorQuery(p) : null), [p, diff]);
  const vb = p?.view?.bounds;
  const viewBox = vb ? [Math.max(vb[0], -180), vb[1], Math.min(vb[2], 180), vb[3]] : null;
  const tz = layer && p?.view ? tileZoom(p.view.zoom, layer.tile_size, layer.maxzoom) : null;
  const statsKey =
    q && viewBox && viewBox[0] < viewBox[2] && tz !== null && refined === dataKey
      ? JSON.stringify([q, viewBox.map((v) => Number(v.toFixed(3))), tz])
      : null;
  const { data: errStats } = useFetch<ErrorStats>(statsKey, (signal) =>
    api.errorStats(q!, viewBox!, tz!, signal),
  );
  const shownStats = statsKey ? (errStats ?? null) : null;
  useEffect(() => useStore.getState().setErrorStats(id, shownStats), [id, shownStats]);

  const units = p ? unitLabel(drawUnits(p)) : "";
  const classes = useMemo(() => (p ? drawClasses(p) : null), [p]);
  const drawn = spec ? shownSpec : null;
  useEffect(
    () => report(id, { spec: drawn, layer, diff, units, classes }),
    [report, id, drawn, layer, diff, units, classes],
  );
  useEffect(() => {
    if (isActive && drawn) onSpecShown(); // after report(): the summary reads the shown spec
  }, [isActive, drawn, onSpecShown]);

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
    if (!useStore.getState().picking) return; // a click only pans, unless "Point" is on
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
      className={[
        "map-pane",
        multi && isActive && "active",
        rect && rect.y + rect.h > 0.999 && "at-bottom", // its box goes above the time bar
        rect && rect.x < 0.001 && panelOpen && "under-panel", // … and right of the side panel
        rect && rect.x + rect.w > 0.999 && inspect && "under-inspector", // … left of the details
      ]
        .filter(Boolean)
        .join(" ")}
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
        pickable={picking}
        initialView={p.view}
        onMap={onMap(id)}
        onHover={onHover(id)}
        onClick={onClick}
        points={geojson}
        pointColor={pointColor}
        onSpecShown={specShown}
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
  const errStats = useStore((s) => s.errorStats[id] ?? null);
  const [folded, setFolded] = useState(false); // only the pane number, to keep the map free
  const { splitPane, closePane, toggleZoom, setLinks, activatePane } = useStore.getState();
  // the buttons act on the active pane: make this one active first
  const act = (f: () => void) => (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!isActive) activatePane(id);
    f();
  };
  const layer = p.main.layer;
  const diff = isErrorPane(p);
  const st = drawStyle(p);
  const title = drawTitle(p);
  const legend = diff
    ? p.styleReady && !drawClasses(p)
    : layer &&
      p.styleReady &&
      layer.kind !== "rgb" &&
      !layer.bands &&
      !layer.classes &&
      layer.kind !== "points";
  if (multi && folded)
    return (
      <div className="pane-box multi folded" onPointerDown={(e) => e.stopPropagation()}>
        <button
          className="pane-num"
          title="Unfold: layer, colours and buttons of this pane"
          onClick={(e) => {
            e.stopPropagation();
            setFolded(false);
          }}
        >
          {index + 1}
        </button>
      </div>
    );
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
            {p.main.time && !title.includes(` · ${p.main.time} vs `) && (
              <span className="pane-time">
                {" "}
                · {p.main.time}
                {p.timeOffset && p.linkTime ? ` (${fmtOffset(p.timeOffset)})` : ""}
              </span>
            )}
          </span>
          {legend && (
            <span className="pane-legend" title={`${st.colormap}${st.log ? ", log scale" : ""}`}>
              <span
                className="pane-grad"
                style={{ background: cssGradient(getLut(colormaps, st.colormap)) }}
              />
              {fmt(st.vmin)} – {fmt(st.vmax)} {unitLabel(drawUnits(p))}
            </span>
          )}
          {diff && errStats?.count ? (
            <span className={`pane-stats${errStats.exact ? " exact" : ""}`} title={exactness(errStats)}>
              {statsLine(errStats, p.compare.error, unitLabel(drawUnits(p)))}
            </span>
          ) : null}
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
      {multi && (
        <button
          className="pane-fold"
          title="Fold this box to its number (click the number to unfold)"
          onClick={(e) => {
            e.stopPropagation();
            setFolded(true);
          }}
        >
          <Icon name="right" />
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
  picking,
}: {
  picking: boolean;
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
            : has && r.classes
              ? (r.classes.find((c) => c.value === Math.round(v!.value!))?.name ?? `class ${fmt(v!.value)}`)
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
      <div className="tip-coord">
        {fmtLonLat(h.lng, h.lat)} · {picking ? "click for details" : "Point (i) to pick it"}
      </div>
    </div>
  );
}
