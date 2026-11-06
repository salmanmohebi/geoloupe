// Vector overlays (regions, rivers, tracks, sites): MapLibre layers, visibility and hover labels.

import type { LayerSpecification } from "maplibre-gl";
import type { Catalog, VectorInfo } from "../api/types";

export const VEC_PREFIX = "ts-vec-";

export function vectorSourceId(id: string): string {
  return `${VEC_PREFIX}src-${id}`;
}

/** The overlays to draw: the user's choice, else those marked visible in the configuration. */
export function visibleVectors(catalog: Catalog | null, chosen: string[] | null): VectorInfo[] {
  const all = catalog?.vectors ?? [];
  return chosen === null ? all.filter((v) => v.visible) : all.filter((v) => chosen.includes(v.id));
}

const isType = (...types: string[]) => ["match", ["geometry-type"], types, true, false];

/**
 * MapLibre layers of one overlay. Polygons always get a fill layer (transparent without a fill
 * colour) so that hovering inside a region finds it; lines and outlines; points as circles.
 */
export function vectorLayers(v: VectorInfo): LayerSpecification[] {
  const source = vectorSourceId(v.id);
  const s = v.style;
  const metadata = { vector: v.id };
  return [
    {
      id: `${VEC_PREFIX}fill-${v.id}`,
      type: "fill",
      source,
      metadata,
      filter: isType("Polygon", "MultiPolygon"),
      paint: { "fill-color": s.fill ?? s.color, "fill-opacity": s.fill ? s.fill_opacity : 0 },
    },
    {
      id: `${VEC_PREFIX}line-${v.id}`,
      type: "line",
      source,
      metadata,
      filter: isType("LineString", "MultiLineString", "Polygon", "MultiPolygon"),
      layout: { "line-join": "round", "line-cap": "round" },
      paint: {
        "line-color": s.color,
        "line-width": ["interpolate", ["linear"], ["zoom"], 2, s.width * 0.6, 8, s.width, 14, s.width * 1.8],
      },
    },
    {
      id: `${VEC_PREFIX}pt-${v.id}`,
      type: "circle",
      source,
      metadata,
      filter: isType("Point", "MultiPoint"),
      paint: {
        "circle-color": s.fill ?? s.color,
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 2, 3, 10, 6],
        "circle-stroke-color": "#ffffff",
        "circle-stroke-width": 1,
      },
    },
  ] as LayerSpecification[];
}

/** The text shown on hover: the label attribute, else the first name-like one. */
export function featureLabel(props: Record<string, unknown>, label: string | null): string | null {
  const keys = label ? [label] : Object.keys(props).filter((k) => /^(name|name_en|label|title|id)$/i.test(k));
  for (const k of keys) {
    const v = props[k];
    if (v !== null && v !== undefined && v !== "") return String(v);
  }
  return null;
}

/** A few other attributes for the hover read-out ("key: value"), short values only. */
export function featureDetails(props: Record<string, unknown>, label: string | null, max = 3): string[] {
  return Object.entries(props)
    .filter(([k, v]) => k !== label && v !== null && v !== "" && String(v).length <= 40)
    .slice(0, max)
    .map(([k, v]) => `${k}: ${typeof v === "number" ? Number(v.toPrecision(6)) : String(v)}`);
}
