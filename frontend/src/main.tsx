import "maplibre-gl/dist/maplibre-gl.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { api } from "./api/client";
import App from "./App";
import { viewHash } from "./lib/gallery";
import { parseHash } from "./lib/urlState";
import { useStore } from "./state/store";
import "./styles.css";

// The map is created on the first render, so the view from a shared link must
// be in the store before that. A plain URL opens the gallery (when the server has
// featured views), over the first view.
async function start() {
  const plain = !window.location.hash;
  if (plain) {
    const views = await api
      .catalog()
      .then((c) => c.views ?? [])
      .catch(() => []);
    if (views.length) history.replaceState(null, "", viewHash(views[0]));
  }
  const initial = parseHash(window.location.hash);
  useStore.setState({
    view:
      initial.center && initial.zoom !== undefined ? { center: initial.center, zoom: initial.zoom } : null,
    basemap: initial.basemap ?? "minimal",
    globe: initial.globe ?? false,
    galleryOpen: plain,
  });
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void start();
