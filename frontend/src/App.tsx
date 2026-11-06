import { useEffect } from "react";
import { Colorbar } from "./components/Colorbar";
import { Gallery } from "./components/Gallery";
import { Inspector } from "./components/Inspector";
import { LayerPanel } from "./components/LayerPanel";
import { MapView } from "./components/MapView";
import { LoadingBar, Notices, ProvenanceBadge } from "./components/StatusBar";
import { TimeBar } from "./components/TimeBar";
import { Toolbar } from "./components/Toolbar";
import { onActivity } from "./lib/tiles";
import { buildHash, parseHash } from "./lib/urlState";
import { useStore } from "./state/store";

/** Keep the URL hash in sync with the state, so every view is a shareable link. */
function useUrlSync() {
  useEffect(() => {
    let timer = 0;
    return useStore.subscribe((s) => {
      if (!s.main.layerId) return;
      clearTimeout(timer);
      timer = window.setTimeout(() => {
        const hash = buildHash({
          layer: s.main.layerId!,
          t: s.main.time ?? undefined,
          dims: s.main.dims,
          cmap: s.style.colormap,
          range: [s.style.vmin, s.style.vmax],
          log: s.style.log,
          opacity: s.style.opacity,
          center: s.view?.center,
          zoom: s.view?.zoom,
          basemap: s.basemap,
          globe: s.globe,
          cmp: s.compare.mode,
          cmpLayer: s.compare.layerId ?? undefined,
          cmpT: s.compare.linkTime ? undefined : (s.compare.time ?? undefined),
          pt: s.inspect ? [s.inspect.lng, s.inspect.lat] : undefined,
          ov: !s.showPoints ? "none" : (s.overlay ?? undefined),
          vec: s.vectorsOn ?? undefined,
        });
        if (hash !== window.location.hash) history.replaceState(null, "", hash || window.location.pathname);
      }, 250);
    });
  }, []);
}

export default function App() {
  const catalog = useStore((s) => s.catalog);
  const main = useStore((s) => s.main);
  useUrlSync();

  useEffect(() => {
    useStore.getState().init(parseHash(window.location.hash)); // also restores layer, time, style
    // The app writes the hash with replaceState (no event); a hashchange means
    // the user edited the link or went back/forward: open that view.
    const onHash = () => window.location.reload();
    window.addEventListener("hashchange", onHash);
    const off = onActivity((n) => useStore.getState().setLoading(n));
    return () => {
      off();
      window.removeEventListener("hashchange", onHash);
    };
  }, []);

  useEffect(() => {
    if (catalog) document.title = main.layer ? `${main.layer.title} · ${catalog.title}` : catalog.title;
  }, [catalog, main.layer]);

  return (
    <div className="app">
      <LoadingBar />
      <MapView />
      <div className="overlay top-left">
        <LayerPanel />
      </div>
      <div className="overlay top-center">
        <ProvenanceBadge />
      </div>
      <div className="overlay top-right">
        <Toolbar />
      </div>
      <div className="overlay bottom-left">
        <Colorbar />
      </div>
      <div className="overlay bottom-center">
        <TimeBar />
      </div>
      <Inspector />
      <Notices />
      <Gallery />
      {!catalog && (
        <div className="splash">
          <div className="spinner big" />
          <div>Connecting to the TerraScope server…</div>
        </div>
      )}
    </div>
  );
}
