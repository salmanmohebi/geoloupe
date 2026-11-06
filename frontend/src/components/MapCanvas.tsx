// One MapLibre map showing one render spec. Changes of the spec are
// double-buffered: the new layer is added on top and the old one removed when
// the new tiles have loaded, so stepping through time does not flicker.

import type { FeatureCollection } from "geojson";
import * as maplibregl from "maplibre-gl";
import type { ExpressionSpecification, Map as MlMap } from "maplibre-gl";
import { useEffect, useRef, useState } from "react";
import { basemapStyle, firstOverlayLayer } from "../lib/basemaps";
import { stationRadius } from "../lib/points";
import { registerProtocol, registerSpec, type RenderSpec } from "../lib/protocol";
import { useLatest } from "../lib/useLatest";
import type { Basemap } from "../state/store";

/** A station under the cursor. */
export interface StationHit {
  id: string;
  name: string;
  value: number;
  lon: number;
  lat: number;
  elevation: number | null;
}

export interface MapEventInfo {
  lng: number;
  lat: number;
  x: number;
  y: number;
  station?: StationHit;
}

export interface MapCanvasProps {
  spec: RenderSpec | null;
  specKey: string;
  maxzoom: number;
  bounds: [number, number, number, number] | null;
  opacity: number;
  basemap: Basemap;
  globe: boolean;
  marker: { lng: number; lat: number } | null;
  initialView: { center: [number, number]; zoom: number } | null;
  onMap?: (map: MlMap | null) => void;
  onHover?: (e: MapEventInfo | null) => void;
  onClick?: (e: MapEventInfo) => void;
  points?: FeatureCollection | null;
  pointColor?: ExpressionSpecification | null;
  onSpecShown?: (specId: string) => void;
  onTileError?: (message: string) => void;
  label?: string;
}

const PREFIX = "ts-";
const POINTS_SRC = "ts-points";
const POINTS_LYR = "ts-points-lyr";
const EMPTY: FeatureCollection = { type: "FeatureCollection", features: [] };

/** The station (if any) within a few pixels of a screen point. */
function stationAt(map: MlMap, x: number, y: number): StationHit | undefined {
  if (!map.getLayer(POINTS_LYR)) return undefined;
  const f = map.queryRenderedFeatures(
    [
      [x - 5, y - 5],
      [x + 5, y + 5],
    ],
    { layers: [POINTS_LYR] },
  )[0];
  if (!f || f.geometry.type !== "Point") return undefined;
  const [lon, lat] = f.geometry.coordinates as [number, number];
  const pr = f.properties as { id: string; name: string; value: number; elevation: number | null };
  return {
    id: String(pr.id),
    name: String(pr.name),
    value: Number(pr.value),
    lon,
    lat,
    elevation: pr.elevation ?? null,
  };
}

function whenLoaded(map: MlMap, sourceId: string, timeout = 5000): Promise<void> {
  return new Promise((resolve) => {
    let sawTile = false;
    const done = () => {
      clearTimeout(timer);
      map.off("sourcedata", onData);
      map.off("idle", onIdle);
      resolve();
    };
    const onData = (e: maplibregl.MapSourceDataEvent) => {
      if (e.sourceId !== sourceId) return;
      if (e.tile) sawTile = true;
      if (sawTile && map.getSource(sourceId) && map.isSourceLoaded(sourceId)) done();
    };
    const onIdle = () => done();
    const timer = setTimeout(done, timeout);
    map.on("sourcedata", onData);
    map.on("idle", onIdle);
  });
}

function dataLayerIds(map: MlMap): string[] {
  return (map.getStyle()?.layers ?? []).filter((l) => l.id.startsWith(PREFIX + "lyr-")).map((l) => l.id);
}

export function MapCanvas(p: MapCanvasProps) {
  const el = useRef<HTMLDivElement>(null);
  const [map, setMap] = useState<MlMap | null>(null);
  const [styleTick, setStyleTick] = useState(0);
  const shown = useRef<string | null>(null);
  const propsRef = useLatest(p);

  // create the map once
  useEffect(() => {
    registerProtocol();
    const iv = propsRef.current.initialView;
    const m = new maplibregl.Map({
      container: el.current!,
      style: basemapStyle(propsRef.current.basemap),
      center: iv?.center ?? [10, 30],
      zoom: iv?.zoom ?? 1.3,
      attributionControl: { compact: true },
      maxPitch: 60,
      renderWorldCopies: true,
    });
    m.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
    m.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
    m.on("style.load", () => {
      m.setProjection({ type: propsRef.current.globe ? "globe" : "mercator" });
      shown.current = null;
      setStyleTick((t) => t + 1);
    });
    m.on("mousemove", (e) => {
      const station = stationAt(m, e.point.x, e.point.y);
      m.getCanvas().style.cursor = station ? "pointer" : "";
      propsRef.current.onHover?.({
        lng: e.lngLat.lng,
        lat: e.lngLat.lat,
        x: e.point.x,
        y: e.point.y,
        station,
      });
    });
    m.on("mouseout", () => propsRef.current.onHover?.(null));
    m.on("click", (e) => {
      const station = stationAt(m, e.point.x, e.point.y);
      propsRef.current.onClick?.({
        lng: e.lngLat.wrap().lng,
        lat: e.lngLat.lat,
        x: e.point.x,
        y: e.point.y,
        station,
      });
    });
    m.on("error", (e) => {
      const msg = (e as unknown as { error?: Error }).error?.message ?? "";
      if (msg && !/abort/i.test(msg)) propsRef.current.onTileError?.(msg);
    });
    setMap(m);
    const onMap = propsRef.current.onMap;
    onMap?.(m);
    return () => {
      onMap?.(null);
      m.remove();
    };
  }, [propsRef]);

  // basemap
  const firstBasemap = useRef(true);
  useEffect(() => {
    if (!map) return;
    if (firstBasemap.current) {
      firstBasemap.current = false;
      return;
    }
    map.setStyle(basemapStyle(p.basemap), { diff: false });
  }, [map, p.basemap]);

  // projection (isStyleLoaded() is false while tiles load; the style exists once styleTick > 0)
  useEffect(() => {
    if (map && styleTick > 0) map.setProjection({ type: p.globe ? "globe" : "mercator" });
  }, [map, p.globe, styleTick]);

  // data layer (double-buffered)
  useEffect(() => {
    if (!map || styleTick === 0) return; // wait for the basemap style
    const spec = propsRef.current.spec;
    if (!spec) {
      for (const id of dataLayerIds(map)) {
        map.removeLayer(id);
        map.removeSource(id.replace("lyr-", "src-"));
      }
      shown.current = null;
      return;
    }
    const sid = registerSpec(spec);
    const src = `${PREFIX}src-${sid}`;
    const lyr = `${PREFIX}lyr-${sid}`;
    // Source bounds avoid requests outside the data. MapLibre bounds cannot cross the
    // antimeridian, so grids such as 160…200° E get no bounds (all tiles are requested).
    const raw = propsRef.current.bounds;
    const b = raw && raw[0] >= -180 && raw[2] <= 180 ? raw : null;
    map.addSource(src, {
      type: "raster",
      tiles: [`terrascope://${sid}/{z}/{x}/{y}`],
      tileSize: spec.tileSize,
      minzoom: 0,
      maxzoom: propsRef.current.maxzoom,
      ...(b
        ? {
            bounds: [
              Math.max(b[0], -180),
              Math.max(b[1], -85.05),
              Math.min(b[2], 180),
              Math.min(b[3], 85.05),
            ],
          }
        : {}),
    });
    // data below the stations, both below borders and labels
    const before = map.getLayer(POINTS_LYR) ? POINTS_LYR : firstOverlayLayer(map.getStyle().layers);
    map.addLayer(
      {
        id: lyr,
        type: "raster",
        source: src,
        paint: {
          "raster-resampling": "nearest",
          "raster-opacity": propsRef.current.opacity,
          "raster-fade-duration": 0,
        },
      },
      before,
    );
    let cancelled = false;
    whenLoaded(map, src).then(() => {
      if (cancelled || !map.getLayer(lyr)) return;
      for (const id of dataLayerIds(map)) {
        if (id === lyr) break; // layers above (newer) stay
        map.removeLayer(id);
        map.removeSource(id.replace("lyr-", "src-"));
      }
      shown.current = sid;
      propsRef.current.onSpecShown?.(sid);
    });
    return () => {
      cancelled = true;
    };
  }, [map, p.specKey, styleTick, propsRef]);

  // station observations (above the data, below borders and labels)
  useEffect(() => {
    if (!map || styleTick === 0) return;
    const data = p.points ?? EMPTY;
    const src = map.getSource(POINTS_SRC) as maplibregl.GeoJSONSource | undefined;
    const radius = stationRadius(data.features.length) as ExpressionSpecification;
    if (src) {
      src.setData(data);
      if (map.getLayer(POINTS_LYR)) map.setPaintProperty(POINTS_LYR, "circle-radius", radius);
      return;
    }
    map.addSource(POINTS_SRC, { type: "geojson", data });
    map.addLayer(
      {
        id: POINTS_LYR,
        type: "circle",
        source: POINTS_SRC,
        paint: {
          "circle-color": propsRef.current.pointColor ?? "#888888",
          "circle-radius": radius,
          "circle-stroke-color": "#ffffff",
          "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 2, 0.6, 8, 1.5],
          "circle-stroke-opacity": 0.95,
        },
      },
      firstOverlayLayer(map.getStyle().layers),
    );
  }, [map, styleTick, p.points, propsRef]);

  const colorKey = JSON.stringify(p.pointColor ?? null);
  useEffect(() => {
    if (!map || !map.getLayer(POINTS_LYR)) return;
    const c = propsRef.current.pointColor;
    if (c) map.setPaintProperty(POINTS_LYR, "circle-color", c);
  }, [map, styleTick, colorKey, propsRef]);

  // opacity without re-rendering tiles
  useEffect(() => {
    if (!map || styleTick === 0) return;
    for (const id of dataLayerIds(map)) map.setPaintProperty(id, "raster-opacity", p.opacity);
  }, [map, p.opacity, styleTick]);

  // inspection marker
  const markerRef = useRef<maplibregl.Marker | null>(null);
  useEffect(() => {
    if (!map) return;
    if (!p.marker) {
      markerRef.current?.remove();
      markerRef.current = null;
      return;
    }
    if (!markerRef.current) {
      const dot = document.createElement("div");
      dot.className = "inspect-marker";
      markerRef.current = new maplibregl.Marker({ element: dot }).setLngLat(p.marker).addTo(map);
    } else markerRef.current.setLngLat(p.marker);
  }, [map, p.marker]);

  return (
    <div className="map-canvas">
      <div ref={el} className="map-el" />
      {p.label && <div className="map-label">{p.label}</div>}
    </div>
  );
}
