// The main MapLibre map, for actions outside the map component (image export).
// It is also exposed as `window.__geoloupeMap` for end-to-end tests and debugging.
import type { Map as MlMap } from "maplibre-gl";

let mainMap: MlMap | null = null;
export const setMainMap = (m: MlMap | null) => {
  mainMap = m;
  (window as unknown as { __geoloupeMap?: MlMap | null }).__geoloupeMap = m;
};
export const getMainMap = () => mainMap;
