import type { TimeAxisCompact } from "../lib/timeline";

// Types of the GeoLoupe HTTP API (see src/geoloupe/api/app.py).

export interface DatasetInfo {
  id: string;
  title: string;
  description: string | null;
  attribution: string | null;
  status: "ok" | "error";
  error: string | null;
  variables: string[];
  open_seconds: number;
}

export interface LayerSummary {
  id: string;
  title: string;
  kind: "dataset" | "composite" | "points" | "rgb";
  units: string | null;
  datasets: string[];
  has_time: boolean;
  dims: string[];
}

export interface PointSourceInfo {
  id: string;
  title: string;
  attribution: string | null;
  status: "ok" | "error";
  error: string | null;
  layers: Record<string, string>;
  variables?: string[];
}

export interface Catalog {
  title: string;
  description?: string | null;
  version: string;
  datasets: DatasetInfo[];
  layers: LayerSummary[];
  points?: PointSourceInfo[];
  views?: FeaturedView[];
  vectors?: VectorInfo[];
  allow_open?: boolean; // "Add data": remote URLs
  open_roots?: string[]; // "Add data": local folders
}

export interface AddDataResult {
  layers: string[];
  vectors: string[];
  errors: string[];
}

/** A vector overlay (regions, rivers, tracks, sites): GeoJSON from /api/vectors/{id}.geojson. */
export interface VectorInfo {
  id: string;
  title: string;
  attribution: string | null;
  visible: boolean; // shown by default
  style: { color: string; width: number; fill: string | null; fill_opacity: number };
  label: string | null; // attribute shown on hover
  status: "ok" | "error";
  error: string | null;
  count: number | null;
  bounds: [number, number, number, number] | null;
}

/** A ready-made map from the configuration (`views:`), listed in the gallery. */
export interface FeaturedView {
  id: number;
  title: string;
  description?: string;
  layer: string;
  time?: string;
  dims?: Record<string, string | number>;
  center?: [number, number];
  zoom?: number;
  colormap?: string;
  vmin?: number;
  vmax?: number;
  log?: boolean;
  basemap?: "minimal" | "light" | "dark";
  globe?: boolean;
  compare?: string;
  compare_mode?: "split" | "diff";
  compare_time?: string;
  overlay?: string;
  vectors?: string[];
}

export interface LevelInfo {
  resolution: string;
  shape: [number, number];
  origin: "native" | "copy" | "overview" | "subsampled"; // subsampled: every n-th cell, on the fly
  times: number | null;
}

export interface SourceMeta {
  id: string;
  dataset: string;
  dataset_title: string;
  variable: string;
  long_name: string | null;
  units: string | null;
  crs: string;
  shape: [number, number];
  resolution: string;
  cell_size_m: number;
  bounds: [number, number, number, number];
  levels: LevelInfo[];
  time_steps: number | null;
  time_range: [string, string] | null;
  fixed_time?: string | null; // one time used for every map time (static fields)
  time_step: string | null;
  calendar: string | null;
  min_zoom: number; // tile zoom from which the read budget allows the source
  native_zoom: number;
  zoom_window: [number | null, number | null];
}

export interface LayerMeta {
  id: string;
  title: string;
  description: string | null;
  kind: "dataset" | "composite" | "points" | "rgb";
  units: string | null;
  style: {
    colormap: string | null;
    vmin: number | null;
    vmax: number | null;
    resampling: string | null;
    gamma?: number;
  };
  bands?: (string | null)[] | null; // RGB layers: the band of each channel
  classes?: { value: number; name: string; color: string | null }[] | null; // categorical data
  bounds: [number, number, number, number];
  times: string[] | null; // null for long regular axes: see time_axis
  time_axis?: TimeAxisCompact | null;
  time_step: string | null;
  dims: Record<string, (number | string)[]>;
  tile_size: number;
  minzoom: number;
  maxzoom: number;
  sources: SourceMeta[];
  // the point data drawn on this layer: itself for point layers, the configured overlay otherwise
  points: { source: string; variable: string; title: string; units: string | null; layer: string } | null;
}

export interface Colormap {
  category: "sequential" | "diverging" | "cyclic" | "other";
  provider: string;
  lut: string; // 256 × RGB as hex
}

export interface CellInfo {
  lon: number;
  lat: number;
  row: number;
  col: number;
  x?: number;
  y?: number;
}

export interface PointSource {
  source: number;
  id: string;
  resolution: string;
  status: string;
  value?: number | null;
  cell?: CellInfo;
}

export interface PointResult {
  lon: number;
  lat: number;
  time: string | null;
  units: string | null;
  value: number | null;
  source: number | null;
  sources: PointSource[];
}

export interface Series {
  source: number;
  id: string;
  resolution: string;
  cell: CellInfo;
  times: string[];
  values: (number | null)[];
}

export interface TimeSeriesResult {
  lon: number;
  lat: number;
  units: string | null;
  series: Series[];
}

export interface Stats {
  count: number;
  zoom: number;
  min?: number;
  max?: number;
  mean?: number;
  p2?: number;
  p25?: number;
  median?: number;
  p75?: number;
  p98?: number;
  histogram?: { counts: number[]; edges: number[] };
}
