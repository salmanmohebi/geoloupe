// Shareable links: the view state lives in the URL hash, e.g.
// #layer=tas&t=1978-07-15&cmap=RdYlBu_r&range=250,310&c=8.5,46.5&z=6.2

import type { Basemap, CompareMode, UrlState } from "../state/store";

export function parseHash(hash: string): Partial<UrlState> {
  const q = new URLSearchParams(hash.replace(/^#/, ""));
  const out: Partial<UrlState> = {};
  const num = (s: string | null) => (s !== null && s !== "" && isFinite(Number(s)) ? Number(s) : undefined);
  const pair = (s: string | null): [number, number] | undefined => {
    if (!s) return undefined;
    const [a, b] = s.split(",").map(Number);
    return isFinite(a) && isFinite(b) ? [a, b] : undefined;
  };
  if (q.get("layer")) out.layer = q.get("layer")!;
  if (q.get("t")) out.t = q.get("t")!;
  if (q.get("dims")) {
    out.dims = Object.fromEntries(
      q
        .get("dims")!
        .split(",")
        .map((p) => p.split(":"))
        .filter((p) => p.length === 2),
    );
  }
  if (q.get("cmap")) out.cmap = q.get("cmap")!;
  out.range = pair(q.get("range"));
  if (q.get("log") === "1") out.log = true;
  out.opacity = num(q.get("op"));
  out.center = pair(q.get("c"));
  out.zoom = num(q.get("z"));
  const bm = q.get("bm");
  if (bm === "minimal" || bm === "light" || bm === "dark") out.basemap = bm as Basemap;
  if (q.get("globe") === "1") out.globe = true;
  const cmp = q.get("cmp");
  if (cmp === "split" || cmp === "diff") out.cmp = cmp as CompareMode;
  if (q.get("cl")) out.cmpLayer = q.get("cl")!;
  if (q.get("ct")) out.cmpT = q.get("ct")!;
  out.pt = pair(q.get("pt"));
  if (q.get("ov")) out.ov = q.get("ov")!;
  const vec = q.get("vec");
  if (vec !== null) out.vec = vec === "-" || vec === "" ? [] : vec.split(",");
  for (const k of Object.keys(out) as (keyof UrlState)[]) if (out[k] === undefined) delete out[k];
  return out;
}

export function buildHash(s: Partial<UrlState>): string {
  const q: string[] = [];
  const add = (k: string, v: string | undefined) => {
    if (v !== undefined && v !== "")
      q.push(`${k}=${encodeURIComponent(v).replace(/%3A/g, ":").replace(/%2C/g, ",")}`);
  };
  add("layer", s.layer);
  add("t", s.t);
  if (s.dims && Object.keys(s.dims).length)
    add(
      "dims",
      Object.entries(s.dims)
        .map(([k, v]) => `${k}:${v}`)
        .join(","),
    );
  add("cmap", s.cmap);
  if (s.range) add("range", s.range.map((v) => Number(v.toPrecision(5))).join(","));
  if (s.log) add("log", "1");
  if (s.opacity !== undefined && s.opacity !== 1) add("op", s.opacity.toFixed(2));
  if (s.center) add("c", s.center.map((v) => v.toFixed(4)).join(","));
  if (s.zoom !== undefined) add("z", s.zoom.toFixed(2));
  if (s.basemap && s.basemap !== "minimal") add("bm", s.basemap);
  if (s.globe) add("globe", "1");
  if (s.cmp && s.cmp !== "off") {
    add("cmp", s.cmp);
    add("cl", s.cmpLayer);
    add("ct", s.cmpT);
  }
  if (s.pt) add("pt", s.pt.map((v) => v.toFixed(4)).join(","));
  add("ov", s.ov);
  if (s.vec) add("vec", s.vec.length ? s.vec.join(",") : "-");
  return q.length ? `#${q.join("&")}` : "";
}
