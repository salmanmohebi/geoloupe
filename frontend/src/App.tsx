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
import { neighbour, type Side } from "./lib/layout";
import { buildHash, parseHash } from "./lib/urlState";
import { linkState, useStore } from "./state/store";

/** Keep the URL hash in sync with the state, so every view is a shareable link. */
function useUrlSync() {
  useEffect(() => {
    let timer = 0;
    return useStore.subscribe((s) => {
      if (!s.main.layerId) return;
      clearTimeout(timer);
      timer = window.setTimeout(() => {
        const hash = buildHash(linkState(s));
        if (hash !== window.location.hash) history.replaceState(null, "", hash || window.location.pathname);
      }, 250);
    });
  }, []);
}

const ARROWS: Record<string, Side> = {
  ArrowLeft: "left",
  ArrowRight: "right",
  ArrowUp: "up",
  ArrowDown: "down",
};

/** Pane keys, as in tmux: % or | split right, " split down, x close, z zoom, Alt+arrows move. */
function usePaneKeys() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t?.closest("input, textarea, select, [contenteditable]")) return;
      if (e.metaKey || (e.ctrlKey && !e.altKey)) return; // Ctrl+Alt is AltGr (| on many keyboards)
      const s = useStore.getState();
      if (s.galleryOpen) return;
      if (e.altKey && ARROWS[e.key]) {
        const next = neighbour(s.layout, s.active, ARROWS[e.key]);
        if (next) s.activatePane(next);
      } else if (e.key === "|" || e.key === "%") s.splitPane("row");
      else if (e.key === '"') s.splitPane("col");
      else if (e.key === "x" && !e.altKey) s.closePane();
      else if (e.key === "z" && !e.altKey) s.toggleZoom();
      else if (e.key === "i" && !e.altKey)
        s.setPicking(!s.picking); // "Point": pick points
      else if (e.key === "Escape" && s.picking) s.setPicking(false);
      else return;
      e.preventDefault();
      e.stopPropagation(); // capture phase: the map's own keys do not see it
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);
}

export default function App() {
  const catalog = useStore((s) => s.catalog);
  const main = useStore((s) => s.main);
  useUrlSync();
  usePaneKeys();

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
          <div>Connecting to the GeoLoupe server…</div>
        </div>
      )}
    </div>
  );
}
