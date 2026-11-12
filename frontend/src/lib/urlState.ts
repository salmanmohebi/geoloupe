// Shareable links: the view state lives in the URL hash, e.g.
// #layer=tas&t=1978-07-15&cmap=RdYlBu_r&range=250,310&c=8.5,46.5&z=6.2
// With split panes, the top-level fields are pane `pa`; `lay` is the layout and `p1=…`, `p2=…`
// hold the other panes, each a link of its own (encoded), e.g.
// #layer=tas&t=1978-07-15&lay=r50(0,1)&pa=0&p1=layer%3Dpr%26t%3D1978-07-15
// An error pane adds cmp=error, cl (T's layer), em (definition), ep (parameters, k:v,…), ef (custom
// formula), eu (its units), fp/ft (the panes P and T follow, by index).

import type { Basemap, UrlState } from "../state/store";

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
  if (cmp === "split" || cmp === "diff" || cmp === "error") out.cmp = cmp;
  if (q.get("cl")) out.cmpLayer = q.get("cl")!;
  if (q.get("ct")) out.cmpT = q.get("ct")!;
  if (q.get("em")) out.em = q.get("em")!;
  if (q.get("ef")) out.ef = q.get("ef")!;
  if (q.get("eu")) out.eu = q.get("eu")!;
  if (q.get("ep")) {
    const ep: Record<string, number> = {};
    for (const part of q.get("ep")!.split(",")) {
      const [k, v] = part.split(":");
      if (k && v !== undefined && v !== "" && isFinite(Number(v))) ep[k] = Number(v);
    }
    if (Object.keys(ep).length) out.ep = ep;
  }
  out.fp = num(q.get("fp"));
  out.ft = num(q.get("ft"));
  out.pt = pair(q.get("pt"));
  if (q.get("ov")) out.ov = q.get("ov")!;
  const vec = q.get("vec");
  if (vec !== null) out.vec = vec === "-" || vec === "" ? [] : vec.split(",");
  if (q.get("lay")) out.lay = q.get("lay")!;
  out.pa = num(q.get("pa"));
  if (q.get("lv") === "0") out.lv = false;
  if (q.get("lt") === "0") out.lt = false;
  const panes: Record<number, Partial<UrlState>> = {};
  for (const [k, v] of q.entries()) {
    const m = /^p(\d)$/.exec(k);
    if (!m) continue;
    const sub = parseHash(v);
    delete sub.panes; // panes do not nest
    delete sub.lay;
    delete sub.pa;
    panes[Number(m[1])] = sub;
  }
  if (Object.keys(panes).length) out.panes = panes;
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
    add("em", s.em);
    if (s.ep && Object.keys(s.ep).length)
      add(
        "ep",
        Object.entries(s.ep)
          .map(([k, v]) => `${k}:${Number(v.toPrecision(6))}`)
          .join(","),
      );
    add("ef", s.ef);
    add("eu", s.eu);
    add("fp", s.fp !== undefined ? String(s.fp) : undefined);
    add("ft", s.ft !== undefined ? String(s.ft) : undefined);
  }
  if (s.pt) add("pt", s.pt.map((v) => v.toFixed(4)).join(","));
  add("ov", s.ov);
  if (s.vec) add("vec", s.vec.length ? s.vec.join(",") : "-");
  if (s.lay && s.lay !== "0") {
    add("lay", s.lay);
    add("pa", s.pa !== undefined ? String(s.pa) : undefined);
  }
  if (s.lv === false) add("lv", "0");
  if (s.lt === false) add("lt", "0");
  for (const [i, p] of Object.entries(s.panes ?? {})) {
    const sub = buildHash(p).slice(1);
    if (sub) q.push(`p${i}=${encodeURIComponent(sub)}`);
  }
  return q.length ? `#${q.join("&")}` : "";
}
