// The map area: one map, or two synchronised maps (split comparison), plus
// the hover read-out. Difference mode draws A − B on one map.

import type { Map as MlMap } from "maplibre-gl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { LayerMeta, VectorInfo } from "../api/types";
import { classTable } from "../lib/classes";
import { getLut } from "../lib/colormap";
import { setMainMap } from "../lib/mapRegistry";
import { useLatest } from "../lib/useLatest";
import { fmt, fmtLonLat, unitLabel } from "../lib/format";
import type { RenderSpec } from "../lib/protocol";
import { summarize, valueAt, type HoverValue } from "../lib/view";
import { pointsLayerOf, useStore, type Style, type Target } from "../state/store";
import { colorExpression, fetchPoints, toGeoJSON, type PointsResult } from "../lib/points";
import { useFetch } from "../lib/useFetch";
import { featureDetails, featureLabel, visibleVectors } from "../lib/vectors";
import { MapCanvas, type FeatureHit, type MapEventInfo, type StationHit } from "./MapCanvas";

interface Hover {
  pane: 0 | 1;
  x: number;
  y: number;
  lng: number;
  lat: number;
  values: (HoverValue | null)[];
  station?: StationHit;
  stationGrid?: HoverValue | null; // grid value at the station's location
  feature?: FeatureHit; // vector-overlay feature under the cursor
}

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
  const native = !lv || lv.origin !== "overview";
  return `${s.dataset_title.split(" (")[0]} · ${res}${native ? "" : " overview"}`;
}

export function MapView() {
  const main = useStore((s) => s.main);
  const style = useStore((s) => s.style);
  const styleReady = useStore((s) => s.styleReady);
  const compare = useStore((s) => s.compare);
  const diffStyle = useStore((s) => s.diffStyle);
  const colormaps = useStore((s) => s.colormaps);
  const basemap = useStore((s) => s.basemap);
  const globe = useStore((s) => s.globe);
  const inspect = useStore((s) => s.inspect);
  const initialView = useStore((s) => s.view);
  const catalog = useStore((s) => s.catalog);
  const vectorsOn = useStore((s) => s.vectorsOn);
  const vectors = useMemo(() => visibleVectors(catalog, vectorsOn), [catalog, vectorsOn]);
  const { setInspect, setView, setSummary, notify } = useStore.getState();

  const diff = compare.mode === "diff";
  const split = compare.mode === "split";
  const specA = useMemo(
    () => (styleReady ? makeSpec(main, diff ? diffStyle : style, colormaps, diff ? compare : null) : null),
    [main, style, diffStyle, colormaps, compare, diff, styleReady],
  );
  const specB = useMemo(
    () => (split && styleReady ? makeSpec(compare, compare.ownStyle ?? style, colormaps, null) : null),
    [split, compare, style, colormaps, styleReady],
  );
  const keyA = keyOf(specA, diff ? diffStyle.colormap : style.colormap);
  const keyB = keyOf(specB, style.colormap);

  const maps = useRef<(MlMap | null)[]>([null, null]);
  const [mapsReady, setMapsReady] = useState(0);
  const [hover, setHover] = useState<Hover | null>(null);

  // ---- provenance summary of the main map
  const specRef = useLatest<RenderSpec | null>(specA);
  const layerRef = useLatest(main.layer);
  const refreshSummary = useCallback(() => {
    const m = maps.current[0];
    const spec = specRef.current;
    const layer = layerRef.current;
    if (!m || !spec || !layer) return setSummary(null);
    setSummary(summarize(m, spec, layer));
  }, [setSummary, specRef, layerRef]);

  useEffect(() => {
    const m = maps.current[0];
    if (!m) return;
    let raf = 0;
    const onIdle = () => refreshSummary();
    const onData = (e: { sourceId?: string; tile?: unknown }) => {
      if (!e.tile) return;
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(refreshSummary);
    };
    const onEnd = () => {
      const c = m.getCenter();
      const b = m.getBounds();
      setView({
        center: [c.lng, c.lat],
        zoom: m.getZoom(),
        bounds: [b.getWest(), Math.max(b.getSouth(), -90), b.getEast(), Math.min(b.getNorth(), 90)],
      });
      refreshSummary();
    };
    m.on("idle", onIdle);
    m.on("sourcedata", onData);
    m.on("moveend", onEnd);
    return () => {
      m.off("idle", onIdle);
      m.off("sourcedata", onData);
      m.off("moveend", onEnd);
      cancelAnimationFrame(raf);
    };
  }, [mapsReady, refreshSummary, setView]);

  // ---- first layer: frame its extent (unless the URL gave a view)
  const framed = useRef(!!initialView);
  useEffect(() => {
    const m = maps.current[0];
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
  }, [main.layer, mapsReady]);

  // ---- synchronise the two maps in split mode
  useEffect(() => {
    const [a, b] = maps.current;
    if (!split || !a || !b) return;
    let lock = false;
    const sync = (src: MlMap, dst: MlMap) => () => {
      if (lock) return;
      lock = true;
      dst.jumpTo({
        center: src.getCenter(),
        zoom: src.getZoom(),
        bearing: src.getBearing(),
        pitch: src.getPitch(),
      });
      lock = false;
    };
    const fa = sync(a, b);
    const fb = sync(b, a);
    fa();
    a.on("move", fa);
    b.on("move", fb);
    a.resize();
    return () => {
      a.off("move", fa);
      b.off("move", fb);
    };
  }, [split, mapsReady]);

  useEffect(() => {
    if (!split) maps.current[0]?.resize();
  }, [split]);

  const onMap = (i: 0 | 1) => (m: MlMap | null) => {
    maps.current[i] = m;
    if (i === 0) setMainMap(m);
    setMapsReady((k) => k + 1);
  };

  const onHover = (pane: 0 | 1) => (e: MapEventInfo | null) => {
    if (!e) return setHover(null);
    const m = maps.current[pane];
    if (!m) return;
    const z = m.getZoom();
    const values = [specA && main.layer ? valueAt(specA, main.layer, z, e.lng, e.lat) : null];
    if (split && specB && compare.layer) values.push(valueAt(specB, compare.layer, z, e.lng, e.lat));
    const st = e.station;
    const stationGrid =
      st && specA && main.layer && !diff ? valueAt(specA, main.layer, z, st.lon, st.lat) : undefined;
    setHover({ pane, ...e, values, stationGrid });
  };

  // ---- point observations: the main layer itself, or an overlay (not in difference mode)
  const pointsLayer = useStore(pointsLayerOf);
  const pointsKey = pointsLayer && main.time ? JSON.stringify([pointsLayer, main.time]) : null;
  const { data: points, error: pointsError } = useFetch<PointsResult>(pointsKey, (signal) =>
    fetchPoints(pointsLayer!, main.time!, signal),
  );
  const shownPoints = pointsKey ? points : null;

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
    else if (main.layer?.kind !== "points") setInspect({ lng: e.lng, lat: e.lat });
  };

  useEffect(() => useStore.getState().setPoints(shownPoints), [shownPoints]);
  useEffect(() => {
    if (pointsError) notify("error", `Stations could not be loaded: ${pointsError}`);
  }, [pointsError, notify]);
  const geojson = useMemo(() => toGeoJSON(shownPoints), [shownPoints]);
  const pointColor = useMemo(
    () =>
      diff || !styleReady
        ? null
        : colorExpression({
            lut: getLut(colormaps, style.colormap),
            vmin: style.vmin,
            vmax: style.vmax,
            log: style.log,
          }),
    [diff, styleReady, colormaps, style],
  );

  const lastErr = useRef(0);
  const onTileError = (msg: string) => {
    const now = Date.now();
    if (now - lastErr.current > 8000) {
      lastErr.current = now;
      notify("error", `Some tiles failed to load: ${msg}`);
    }
  };

  const units = unitLabel(main.layer?.units);
  const canvas = (
    pane: 0 | 1,
    spec: RenderSpec | null,
    key: string,
    layer: LayerMeta | null,
    label?: string,
  ) => (
    <div className="map-pane" key={pane}>
      <MapCanvas
        spec={spec}
        specKey={key}
        maxzoom={layer?.maxzoom ?? 12}
        bounds={layer?.bounds ?? null}
        opacity={style.opacity}
        basemap={basemap}
        globe={globe}
        marker={inspect}
        initialView={initialView}
        onMap={onMap(pane)}
        onHover={onHover(pane)}
        onClick={onClick}
        points={pane === 0 ? geojson : null}
        pointColor={pointColor}
        onSpecShown={pane === 0 ? refreshSummary : undefined}
        onTileError={onTileError}
        label={label}
        vectors={vectors}
      />
      {hover && hover.pane === pane && (
        <HoverTip
          hover={hover}
          units={units}
          split={split}
          diff={diff}
          layers={[main.layer, compare.layer]}
          vectors={vectors}
        />
      )}
    </div>
  );

  const labelA = split ? `A · ${main.layer?.title ?? ""}${main.time ? ` · ${main.time}` : ""}` : undefined;
  const labelB = split
    ? `B · ${compare.layer?.title ?? ""}${compare.time ? ` · ${compare.time}` : ""}${compare.ownStyle ? " · own colour scale" : ""}`
    : undefined;
  return (
    <div className={`map-view ${split ? "split" : ""}`}>
      {canvas(0, specA, keyA, main.layer, labelA)}
      {split && canvas(1, specB, keyB, compare.layer, labelB)}
    </div>
  );
}

function HoverTip({
  hover: h,
  units: u,
  split: sp,
  diff: df,
  layers,
  vectors,
}: {
  hover: Hover;
  units: string;
  split: boolean;
  diff: boolean;
  layers: (LayerMeta | null)[];
  vectors: VectorInfo[];
}) {
  const rows = h.values.map((v, i) => {
    const layer = layers[i];
    const name = sp ? (i === 0 ? "A" : "B") : df ? "A − B" : "";
    const has = v !== null && v.value !== null;
    return (
      <div className="tip-row" key={i}>
        {name && <span className="tip-tag">{name}</span>}
        <span className="tip-value">
          {v?.bands
            ? v.bands.map((b, k) => `${"RGB"[k]} ${b === null ? "–" : fmt(b)}`).join("  ")
            : has && layer?.classes
              ? (layer.classes.find((c) => c.value === Math.round(v!.value!))?.name ??
                `class ${fmt(v!.value)}`)
              : has
                ? fmt(v!.value)
                : v
                  ? "no data"
                  : "loading…"}
        </span>
        {has && <span className="tip-unit">{u}</span>}
        {has && v!.source !== null && !df && (
          <span className="tip-src">
            {sourceLabel(layer, v!.source, v!.level)}
            {v!.approximate ? " (coarser tile)" : ""}
          </span>
        )}
      </div>
    );
  });
  const st = h.station;
  const grid = h.stationGrid?.value ?? null;
  const fv = h.feature && vectors.find((v) => v.id === h.feature!.vector);
  const fname = fv ? featureLabel(h.feature!.props, fv.label) : null;
  return (
    <div className="hover-tip" style={{ left: h.x + 14, top: h.y + 14 }}>
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
