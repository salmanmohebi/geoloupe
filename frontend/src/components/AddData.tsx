// "Add data": open a file or URL on the server while it runs, or browse an S3 bucket (AWS or an
// S3-compatible store) and open a dataset from it, in the active pane or in a new pane. Only shown
// when the server was started with --allow-open (remote data) or --open-root DIR (local files).

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api/client";
import type { BrowseEntry, BrowseResult, S3Access } from "../api/types";
import { visibleVectors } from "../lib/vectors";
import { useStore } from "../state/store";
import { Icon } from "./Icon";

type Where = "pane" | "new";

const KIND_LABEL: Record<NonNullable<BrowseEntry["kind"]>, string> = {
  zarr: "Zarr",
  netcdf: "NetCDF",
  grib: "GRIB",
  geotiff: "GeoTIFF",
  table: "Table",
  vector: "Vector",
  reference: "Kerchunk",
};

const STORE_KEY = "geoloupe.s3"; // the last bucket, endpoint and access (no secrets)

function remembered(): { url: string; endpoint: string; access: "public" | "credentials"; profile: string } {
  const empty = { url: "", endpoint: "", access: "public" as const, profile: "" };
  try {
    return { ...empty, ...JSON.parse(localStorage.getItem(STORE_KEY) ?? "{}") };
  } catch {
    return empty;
  }
}

/** A long file name shortened in the middle: the end (tile, date, extension) tells files apart. */
function shortName(name: string, max = 52): string {
  if (name.length <= max) return name;
  const tail = Math.ceil(max * 0.55);
  return `${name.slice(0, max - tail - 1)}…${name.slice(-tail)}`;
}

function fmtBytes(n: number | null): string {
  if (n === null || n === undefined) return "";
  const units = ["B", "kB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1000 && i < units.length - 1) [v, i] = [v / 1000, i + 1];
  return `${v < 10 && i ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

export function AddDataButton() {
  const catalog = useStore((s) => s.catalog);
  const [open, setOpen] = useState(false);
  if (!catalog?.allow_open && !catalog?.open_roots?.length) return null;
  return (
    <>
      <button className="btn tool" onClick={() => setOpen(true)}>
        Add data
      </button>
      {/* in the page body: inside the toolbar, the backdrop would cover only the toolbar */}
      {open && createPortal(<AddDataDialog onClose={() => setOpen(false)} />, document.body)}
    </>
  );
}

function AddDataDialog({ onClose }: { onClose: () => void }) {
  const catalog = useStore((s) => s.catalog);
  const remote = !!catalog?.allow_open;
  const [tab, setTab] = useState<"path" | "s3">("path");
  const [where, setWhere] = useState<Where>("pane");
  const [busy, setBusy] = useState<string | null>(null); // what is being opened
  const [error, setError] = useState<string | null>(null);

  /** Open a location and show its first layer in the chosen pane. */
  const open = async (path: string, name = "", access: S3Access = {}) => {
    if (busy) return;
    setBusy(path);
    setError(null);
    try {
      const res = await api.addData(path, name, access);
      const st = useStore.getState();
      const shown = visibleVectors(st.catalog, st.vectorsOn).map((v) => v.id);
      await st.refreshCatalog();
      if (res.vectors.length) st.setVectorsOn([...shown, ...res.vectors]);
      if (res.layers.length) {
        if (where === "new") st.splitPane("row"); // the new pane becomes the active one
        await useStore.getState().setLayer(res.layers[0]);
      }
      for (const err of res.errors) st.notify("error", err);
      const cat = useStore.getState().catalog;
      const title = (id: string) =>
        cat?.layers.find((l) => l.id === id)?.title ?? cat?.vectors?.find((v) => v.id === id)?.title ?? id;
      const names = [...res.layers, ...res.vectors].map(title);
      const listed = names.slice(0, 3).join(", ") + (names.length > 3 ? ` and ${names.length - 3} more` : "");
      st.notify(
        "info",
        `Added ${listed}.${res.layers.length > 1 ? " All new layers are in the layer list." : ""}`,
      );
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="gallery-backdrop" onClick={onClose}>
      <div
        className="add-data panel"
        role="dialog"
        aria-modal="true"
        aria-label="Add data"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="gallery-head">
          <div>
            <div className="eyebrow">Add data</div>
            <h1>{tab === "path" ? "Open a file or URL" : "Browse an S3 bucket"}</h1>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            <Icon name="close" />
          </button>
        </div>
        {remote && (
          <div className="seg add-tabs" role="tablist">
            <button
              role="tab"
              aria-selected={tab === "path"}
              className={tab === "path" ? "sel" : ""}
              onClick={() => setTab("path")}
            >
              Path or URL
            </button>
            <button
              role="tab"
              aria-selected={tab === "s3"}
              className={tab === "s3" ? "sel" : ""}
              onClick={() => setTab("s3")}
            >
              Browse S3
            </button>
          </div>
        )}
        {tab === "path" ? <PathTab busy={!!busy} onOpen={open} /> : <S3Tab busy={busy} onOpen={open} />}
        {error && <div className="add-data-error">{error}</div>}
        <div className="gallery-foot">
          <span className="add-where">
            <span className="muted small">Open in</span>
            <label className="radio">
              <input type="radio" name="where" checked={where === "pane"} onChange={() => setWhere("pane")} />
              the active pane
            </label>
            <label className="radio">
              <input type="radio" name="where" checked={where === "new"} onChange={() => setWhere("new")} />a
              new pane on the right
            </label>
          </span>
          {busy && <span className="muted small">Opening… remote data can take a few seconds</span>}
          {tab === "path" && (
            <button className="btn on" type="submit" form="add-path" disabled={!!busy}>
              {busy ? "Opening…" : "Open"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function PathTab({ busy, onOpen }: { busy: boolean; onOpen: (path: string, name: string) => void }) {
  const catalog = useStore((s) => s.catalog);
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const roots = catalog?.open_roots ?? [];
  return (
    <form
      id="add-path"
      className="add-data-body"
      onSubmit={(e) => {
        e.preventDefault();
        if (path.trim() && !busy) onOpen(path.trim(), name.trim());
      }}
    >
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
    </form>
  );
}

function S3Tab({
  busy,
  onOpen,
}: {
  busy: string | null;
  onOpen: (path: string, name: string, access: S3Access) => void;
}) {
  const [first] = useState(remembered);
  const [url, setUrl] = useState(first.url);
  const [endpoint, setEndpoint] = useState(first.endpoint);
  const [mode, setMode] = useState<"public" | "credentials">(first.access);
  const [profile, setProfile] = useState(first.profile);
  const [profiles, setProfiles] = useState<string[]>([]);
  const [match, setMatch] = useState("");
  const [listing, setListing] = useState<BrowseResult | null>(null);
  const [listedMatch, setListedMatch] = useState(""); // the filter of the current listing
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ctl = useRef<AbortController | null>(null);

  useEffect(() => {
    api
      .s3Profiles()
      .then((r) => setProfiles(r.profiles))
      .catch(() => setProfiles([]));
  }, []);

  const access: S3Access = {
    endpoint: endpoint.trim() || undefined,
    ...(mode === "public" ? { anon: true } : profile ? { profile } : { anon: false }),
  };

  const list = async (where: string, filter = "") => {
    if (!where.trim()) return;
    ctl.current?.abort();
    const c = new AbortController();
    ctl.current = c;
    setLoading(true);
    setError(null);
    try {
      const res = await api.browse(where.trim(), access, filter || undefined, c.signal);
      setListing(res);
      setListedMatch(filter);
      setUrl(res.url);
      try {
        localStorage.setItem(STORE_KEY, JSON.stringify({ url: res.url, endpoint, access: mode, profile }));
      } catch {
        /* storage unavailable: nothing to remember */
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        setListing(null);
        setError((e as Error).message);
      }
    } finally {
      if (ctl.current === c) setLoading(false);
    }
  };

  const go = (where: string) => {
    setMatch("");
    void list(where);
  };
  const crumbs = listing ? listing.url.slice(5).replace(/\/$/, "").split("/") : [];

  return (
    <div className="add-data-body s3-tab">
      <form
        className="s3-row"
        onSubmit={(e) => {
          e.preventDefault();
          go(url);
        }}
      >
        <label className="grow">
          <span className="muted small">Bucket and prefix</span>
          <input
            autoFocus
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="s3://my-bucket/some/prefix/"
            spellCheck={false}
          />
        </label>
        <button className="btn on" type="submit" disabled={loading || !url.trim()}>
          {loading ? "Listing…" : "List"}
        </button>
      </form>
      <div className="s3-row">
        <label className="grow">
          <span className="muted small">
            Endpoint (empty for AWS; e.g. https://os.example.org for MinIO, Ceph, SWITCH, R2)
          </span>
          <input
            value={endpoint}
            onChange={(e) => setEndpoint(e.target.value)}
            placeholder="AWS S3"
            spellCheck={false}
          />
        </label>
      </div>
      <div className="s3-access">
        <label className="radio">
          <input type="radio" name="access" checked={mode === "public"} onChange={() => setMode("public")} />
          Public bucket (no credentials)
        </label>
        <label className="radio">
          <input
            type="radio"
            name="access"
            checked={mode === "credentials"}
            onChange={() => setMode("credentials")}
          />
          With the server's credentials
        </label>
        {mode === "credentials" && (
          <select value={profile} onChange={(e) => setProfile(e.target.value)} aria-label="AWS profile">
            <option value="">environment / default profile</option>
            {profiles.map((p) => (
              <option key={p} value={p}>
                profile {p}
              </option>
            ))}
          </select>
        )}
      </div>
      {mode === "credentials" && (
        <p className="muted small">
          Keys stay on the server: set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY where GeoLoupe runs, or a
          profile in ~/.aws/credentials.
        </p>
      )}
      {error && <div className="add-data-error">{error}</div>}
      {listing && (
        <>
          <div className="s3-crumbs">
            <span className="muted">s3://</span>
            {crumbs.map((c, i) => (
              <span key={i}>
                <button className="link-btn" onClick={() => go(`s3://${crumbs.slice(0, i + 1).join("/")}/`)}>
                  {c}
                </button>
                <span className="muted">/</span>
              </span>
            ))}
            <input
              className="s3-filter"
              value={match}
              placeholder="Filter names…"
              onChange={(e) => setMatch(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && match !== listedMatch && list(listing.url, match)}
              onBlur={() => match !== listedMatch && list(listing.url, match)}
              aria-label="Filter names (press Enter)"
            />
          </div>
          {listing.zarr && (
            <div className="s3-zarr">
              This folder is a Zarr store.
              <button className="btn on" disabled={!!busy} onClick={() => onOpen(listing.url, "", access)}>
                {busy === listing.url ? "Opening…" : "Open it"}
              </button>
            </div>
          )}
          <ul className="s3-list" aria-label="Bucket contents">
            {listing.parent && (
              <li>
                <button className="s3-entry" onClick={() => go(listing.parent!)}>
                  <Icon name="up" /> <span className="s3-name">..</span>
                </button>
              </li>
            )}
            {listing.entries.map((e) => (
              <li key={e.url} className={!e.dir && !e.kind ? "dim" : ""}>
                {e.dir && e.kind !== "zarr" ? (
                  <button className="s3-entry" onClick={() => go(e.url)}>
                    <Icon name="right" /> <span className="s3-name">{e.name}/</span>
                  </button>
                ) : (
                  <span className="s3-entry">
                    <span className="s3-name" title={e.url}>
                      {shortName(e.name)}
                      {e.dir ? "/" : ""}
                    </span>
                    <span className="s3-kind">{e.kind ? KIND_LABEL[e.kind] : ""}</span>
                    <span className="s3-size">{fmtBytes(e.size)}</span>
                    {e.kind && (
                      <button
                        className="btn btn-sm"
                        disabled={!!busy}
                        onClick={() => onOpen(e.url, "", access)}
                      >
                        {busy === e.url ? "Opening…" : "Open"}
                      </button>
                    )}
                    {e.dir && (
                      <button className="link-btn small" onClick={() => go(e.url)} title="Look inside">
                        inside
                      </button>
                    )}
                  </span>
                )}
              </li>
            ))}
            {!listing.entries.length && (
              <li className="muted small">Nothing here{match ? " matches the filter" : ""}.</li>
            )}
          </ul>
          {listing.truncated && (
            <p className="muted small">
              Showing {listing.entries.length} of {listing.total}: type in the filter (and press Enter) to
              find a file.
            </p>
          )}
        </>
      )}
      {!listing && !error && (
        <p className="muted small">
          List a bucket to browse it, e.g. <code>s3://esa-worldcover/v200/2021/map/</code> (public, AWS). Zarr
          stores, NetCDF, GeoTIFF/COG, station tables and vector files can be opened.
        </p>
      )}
    </div>
  );
}
