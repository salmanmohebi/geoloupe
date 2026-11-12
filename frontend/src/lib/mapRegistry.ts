// The main MapLibre map (the active pane's), for actions outside the map component (image
// export). It is also exposed as `window.__geoloupeMap`, and all panes' maps by pane number as
// `window.__geoloupeMaps`, for end-to-end tests and debugging.
import type { Map as MlMap } from "maplibre-gl";

let mainMap: MlMap | null = null;
export const setMainMap = (m: MlMap | null) => {
  mainMap = m;
  (window as unknown as { __geoloupeMap?: MlMap | null }).__geoloupeMap = m;
};
export const getMainMap = () => mainMap;
export const setPaneMaps = (maps: Record<string, MlMap>, order: string[]) => {
  (window as unknown as { __geoloupeMaps?: (MlMap | undefined)[] }).__geoloupeMaps = order.map(
    (id) => maps[id],
  );
};
