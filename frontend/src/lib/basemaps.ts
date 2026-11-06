// Basemaps. "minimal" works offline (bundled Natural Earth borders, public
// domain); "light" and "dark" are OpenFreeMap vector styles (no API key).

import type { StyleSpecification } from "maplibre-gl";
import type { Basemap } from "../state/store";

export const BASEMAPS: { id: Basemap; label: string; online: boolean }[] = [
  { id: "minimal", label: "Minimal (offline)", online: false },
  { id: "light", label: "Light with labels", online: true },
  { id: "dark", label: "Dark with labels", online: true },
];

const OPENFREEMAP: Record<string, string> = {
  light: "https://tiles.openfreemap.org/styles/positron",
  dark: "https://tiles.openfreemap.org/styles/dark",
};

export function prefersDark(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches;
}

export function basemapStyle(id: Basemap): string | StyleSpecification {
  if (id !== "minimal") return OPENFREEMAP[id];
  const dark = prefersDark();
  return {
    version: 8,
    sources: {
      countries: { type: "geojson", data: "./countries-110m.geojson", attribution: "Natural Earth" },
    },
    layers: [
      { id: "background", type: "background", paint: { "background-color": dark ? "#151b22" : "#dfe6ec" } },
      {
        id: "land",
        type: "fill",
        source: "countries",
        paint: { "fill-color": dark ? "#262c33" : "#f4f3ef", "fill-antialias": false },
      },
      {
        id: "boundary-countries",
        type: "line",
        source: "countries",
        paint: {
          "line-color": dark ? "rgba(235,235,235,0.55)" : "rgba(30,30,30,0.55)",
          "line-width": ["interpolate", ["linear"], ["zoom"], 1, 0.5, 6, 1.1],
        },
      },
    ],
  };
}

/** Id of the first basemap layer that should stay above the data (borders, labels). */
export function firstOverlayLayer(layers: { id: string; type: string }[]): string | undefined {
  const hit = layers.find(
    (l) =>
      l.type === "symbol" ||
      (l.type === "line" && /boundar|admin|coast/i.test(l.id)) ||
      l.id === "boundary-countries",
  );
  return hit?.id;
}
