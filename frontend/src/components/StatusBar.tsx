// What the user is looking at (sources and resolution levels in view),
// loading state, and notices.

import type { LayerMeta } from "../api/types";
import { exactness } from "../lib/paneDraw";
import { useStore } from "../state/store";

/** "Alps downscaled 0.01° · Global reanalysis 1°": the native data of a layer. */
const nativeOf = (l: LayerMeta | null) =>
  (l?.sources ?? [])
    .map((s) => `${s.dataset_title.split(" (")[0]} ${s.resolution.split(" (")[0]}`)
    .join(" · ");

/** Error maps: what the error is computed from, and whether from native cells. */
function ErrorProvenance() {
  const main = useStore((s) => s.main);
  const compare = useStore((s) => s.compare);
  const loading = useStore((s) => s.loading);
  const stats = useStore((s) => s.errorStats[s.active] ?? null);
  return (
    <div className="provenance panel" role="status" aria-live="polite">
      {loading > 0 && <span className="spinner" aria-label="loading" />}
      <span className="eyebrow">Error of</span>
      <span className="prov-part" title={main.layer?.title}>
        <b>P</b> {nativeOf(main.layer)}
      </span>
      <span className="prov-part" title={compare.layer?.title}>
        <b>T</b> {nativeOf(compare.layer)}
      </span>
      {stats?.count ? (
        <span className={`tag ${stats.exact ? "nat" : "ov"}`} title={exactness(stats)}>
          {stats.exact ? "native cells" : "coarser grid"}
        </span>
      ) : (
        <span className="tag ov" title="Computed from the map's pixels until the exact tiles arrive">
          preview
        </span>
      )}
    </div>
  );
}

export function ProvenanceBadge() {
  const summary = useStore((s) => s.summary);
  const main = useStore((s) => s.main);
  const loading = useStore((s) => s.loading);
  const compare = useStore((s) => s.compare);
  const layer = main.layer;
  const points = useStore((s) => s.points);
  if (!layer) return null;
  if (compare.mode === "error") return <ErrorProvenance />;
  if (layer.kind === "points")
    return (
      <div className="provenance panel" role="status" aria-live="polite">
        {loading > 0 && <span className="spinner" aria-label="loading" />}
        <span className="eyebrow">Showing</span>
        <span className="prov-part">
          <b>{layer.points?.title ?? layer.title}</b>{" "}
          {points ? `${points.count.toLocaleString()} stations` : "…"}
          <span className="tag nat">observed</span>
        </span>
      </div>
    );
  const parts = summary?.parts.filter((p) => p.fraction >= 0.005) ?? [];
  const hidden = summary?.skipped.filter((s) => s.reason === "cost") ?? [];
  // one entry per dataset and level: the bands of one RGB file, or the files of a mosaic, together
  const byKey = new Map<
    string,
    {
      key: string;
      label: string;
      res: string;
      overview: boolean;
      kind: string;
      fraction: number;
      title: string;
    }
  >();
  for (const p of parts) {
    const s = layer.sources[p.source];
    const lv = s?.levels[p.level];
    if (!s || !lv) continue;
    const label = s.dataset_title.split(" (")[0];
    const key = `${label}|${lv.resolution}|${lv.origin}`;
    const g = byKey.get(key);
    if (g) g.fraction += p.fraction;
    else
      byKey.set(key, {
        key,
        label,
        res: lv.resolution,
        overview: lv.origin === "overview" || lv.origin === "subsampled",
        kind: lv.origin === "subsampled" ? "subsampled" : lv.origin === "overview" ? "overview" : "native",
        fraction: p.fraction,
        title: `${s.dataset_title}: ${lv.origin} level ${p.level}, ${lv.shape.join(" × ")} cells`,
      });
  }
  const groups = [...byKey.values()];
  return (
    <div className="provenance panel" role="status" aria-live="polite">
      {loading > 0 && <span className="spinner" aria-label="loading" />}
      <span className="eyebrow">Showing</span>
      {parts.length === 0 && <span className="muted">{loading ? "loading…" : "no data in view"}</span>}
      {groups.map((g) => (
        <span key={g.key} className="prov-part" title={g.title}>
          <b>{g.label}</b> {g.res}
          <span className={`tag ${g.overview ? "ov" : "nat"}`}>{g.kind}</span>
          {groups.length > 1 && layer.kind !== "rgb" && (
            <span className="muted"> {Math.round(g.fraction * 100)}%</span>
          )}
        </span>
      ))}
      {hidden.map((h) => (
        <span key={h.source} className="prov-hint">
          {layer.sources[h.source]?.dataset_title.split(" (")[0]} appears when you zoom in
        </span>
      ))}
    </div>
  );
}

export function LoadingBar() {
  const loading = useStore((s) => s.loading);
  return <div className={`loading-bar ${loading > 0 ? "on" : ""}`} aria-hidden />;
}

export function Notices() {
  const notices = useStore((s) => s.notices);
  const { dismiss } = useStore.getState();
  return (
    <div className="notices" aria-live="assertive">
      {notices.map((n) => (
        <div key={n.id} className={`notice ${n.kind}`} role={n.kind === "error" ? "alert" : "status"}>
          <span>{n.text}</span>
          <button className="icon-btn" onClick={() => dismiss(n.id)} aria-label="Dismiss">
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
