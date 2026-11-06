// The gallery: featured views from the configuration, as cards with a thumbnail. Opening a view is
// the same as opening its shareable link.

import { useEffect, useState } from "react";
import type { FeaturedView, LayerSummary } from "../api/types";
import { thumbnailUrl, viewHash } from "../lib/gallery";
import { useStore } from "../state/store";
import { Icon } from "./Icon";

function Thumb({ src, alt }: { src: string; alt: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) return null;
  return <img src={src} alt={alt} loading="lazy" onError={() => setFailed(true)} />;
}

function Card({ view, layers }: { view: FeaturedView; layers: Map<string, LayerSummary> }) {
  const layer = layers.get(view.layer);
  const other = view.compare ? layers.get(view.compare) : undefined;
  const isPoints = layer?.kind === "points";
  const split = !!other && (view.compare_mode ?? "split") === "split";
  const open = () => {
    const hash = viewHash(view);
    if (hash === window.location.hash) useStore.getState().setGalleryOpen(false);
    else window.location.hash = hash; // the app reloads into the view (see App.tsx)
  };
  return (
    <button className="gallery-card" onClick={open} data-view={view.id}>
      <div className={`thumb ${isPoints ? "points" : ""}`}>
        {isPoints ? (
          <span className="thumb-dots" aria-hidden />
        ) : (
          <>
            <Thumb src={thumbnailUrl(view)} alt="" />
            {split && other?.kind !== "points" && (
              <div className="thumb-b">
                <Thumb src={thumbnailUrl(view, view.compare, view.compare_time ?? view.time)} alt="" />
              </div>
            )}
          </>
        )}
        <span className="thumb-kind">
          {isPoints ? "stations" : split ? "side by side" : other ? "difference" : (layer?.kind ?? "")}
        </span>
      </div>
      <div className="card-text">
        <div className="card-title">{view.title}</div>
        {view.description && <div className="card-desc">{view.description}</div>}
      </div>
    </button>
  );
}

export function Gallery() {
  const catalog = useStore((s) => s.catalog);
  const open = useStore((s) => s.galleryOpen);
  const { setGalleryOpen } = useStore.getState();
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setGalleryOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, setGalleryOpen]);
  const views = catalog?.views ?? [];
  if (!open || !catalog || !views.length) return null;
  const layers = new Map(catalog.layers.map((l) => [l.id, l]));
  return (
    <div className="gallery-backdrop" onClick={() => setGalleryOpen(false)}>
      <div
        className="gallery panel"
        role="dialog"
        aria-modal="true"
        aria-label="Gallery"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="gallery-head">
          <div>
            <div className="eyebrow">Gallery</div>
            <h1>{catalog.title}</h1>
            {catalog.description && <p className="gallery-intro">{catalog.description}</p>}
          </div>
          <button className="icon-btn" onClick={() => setGalleryOpen(false)} aria-label="Close the gallery">
            <Icon name="close" />
          </button>
        </div>
        <div className="gallery-grid">
          {views.map((v) => (
            <Card key={v.id} view={v} layers={layers} />
          ))}
        </div>
        <div className="gallery-foot">
          <span className="muted small">
            {catalog.layers.length} layers from {catalog.datasets.length + (catalog.points?.length ?? 0)}{" "}
            sources · every view is a link you can share
          </span>
          <button className="btn" onClick={() => setGalleryOpen(false)}>
            Explore freely
          </button>
        </div>
      </div>
    </div>
  );
}
