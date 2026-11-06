// "Add data": open a file or URL on the server while it runs. Only shown when the server was
// started with --allow-open (remote URLs) or --open-root DIR (local files below DIR).

import { useState } from "react";
import { api } from "../api/client";
import { visibleVectors } from "../lib/vectors";
import { useStore } from "../state/store";
import { Icon } from "./Icon";

export function AddDataButton() {
  const catalog = useStore((s) => s.catalog);
  const [open, setOpen] = useState(false);
  if (!catalog?.allow_open && !catalog?.open_roots?.length) return null;
  return (
    <>
      <button className="btn tool" onClick={() => setOpen(true)}>
        Add data
      </button>
      {open && <AddDataDialog onClose={() => setOpen(false)} />}
    </>
  );
}

function AddDataDialog({ onClose }: { onClose: () => void }) {
  const catalog = useStore((s) => s.catalog);
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const roots = catalog?.open_roots ?? [];

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!path.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.addData(path.trim(), name.trim());
      const st = useStore.getState();
      const shown = visibleVectors(st.catalog, st.vectorsOn).map((v) => v.id);
      await st.refreshCatalog();
      if (res.vectors.length) st.setVectorsOn([...shown, ...res.vectors]);
      if (res.layers.length) await st.setLayer(res.layers[0]);
      for (const err of res.errors) st.notify("error", err);
      const what = [...res.layers, ...res.vectors].join(", ");
      st.notify(
        "info",
        `Added ${what}. ${res.layers.length > 1 ? "All new layers are in the layer list." : ""}`,
      );
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="gallery-backdrop" onClick={onClose}>
      <form
        className="add-data panel"
        role="dialog"
        aria-modal="true"
        aria-label="Add data"
        onClick={(e) => e.stopPropagation()}
        onSubmit={submit}
      >
        <div className="gallery-head">
          <div>
            <div className="eyebrow">Add data</div>
            <h1>Open a file or URL</h1>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            <Icon name="close" />
          </button>
        </div>
        <div className="add-data-body">
          <label>
            <span className="muted small">Path or URL</span>
            <input
              autoFocus
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder={catalog?.allow_open ? "https://…/data.zarr, s3://…/scene.tif" : "data.nc"}
            />
          </label>
          <label>
            <span className="muted small">Name (optional)</span>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="from the file name" />
          </label>
          <p className="muted small">
            Gridded data (Zarr, NetCDF, GRIB, GeoTIFF/COG), stations (CSV, Parquet) or vector files (GeoJSON,
            Shapefile, GeoPackage). {catalog?.allow_open ? "Remote URLs are allowed. " : ""}
            {roots.length ? `Local files: inside ${roots.join(", ")} (relative paths start there).` : ""}
          </p>
          {error && <div className="add-data-error">{error}</div>}
        </div>
        <div className="gallery-foot">
          <span className="muted small">{busy ? "Opening… remote data can take a few seconds" : ""}</span>
          <button className="btn on" type="submit" disabled={busy || !path.trim()}>
            {busy ? "Opening…" : "Open"}
          </button>
        </div>
      </form>
    </div>
  );
}
