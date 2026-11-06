// A layer's time axis: an explicit list of ISO labels, or (long, regular axes such as decades
// of hourly data) start + step × index. All time navigation goes through this, never through
// the raw list, so a million-step axis costs nothing.

export interface TimeAxisCompact {
  start: string;
  end: string;
  count: number;
  step_seconds: number;
  format: "date" | "datetime";
}

export interface Timeline {
  count: number;
  at(i: number): string;
  /** Index of the label nearest to `label` (exact labels map to themselves). */
  nearest(label: string): number;
  /** Index of `label`, or -1 if it is not a label of this axis. */
  indexOf(label: string): number;
}

const EMPTY: Timeline = { count: 0, at: () => "", nearest: () => -1, indexOf: () => -1 };

/** Milliseconds of an ISO label (YYYY-MM-DD or YYYY-MM-DDTHH:MM), read as UTC. */
function utc(label: string): number {
  if (label.length <= 10) return Date.parse(`${label}T00:00:00Z`);
  return Date.parse(label.length <= 16 ? `${label}:00Z` : `${label}Z`);
}

function format(ms: number, f: "date" | "datetime"): string {
  const iso = new Date(ms).toISOString(); // YYYY-MM-DDTHH:MM:SS.sssZ
  return f === "date" ? iso.slice(0, 10) : iso.slice(0, 16);
}

export function timelineOf(
  layer: { times: string[] | null; time_axis?: TimeAxisCompact | null } | null,
): Timeline {
  if (!layer) return EMPTY;
  const c = layer.time_axis;
  if (c) {
    const t0 = utc(c.start);
    const step = c.step_seconds * 1000;
    const clamp = (i: number) => Math.max(0, Math.min(c.count - 1, i));
    return {
      count: c.count,
      at: (i) => format(t0 + clamp(i) * step, c.format),
      nearest: (label) => {
        const t = utc(label);
        return Number.isFinite(t) ? clamp(Math.round((t - t0) / step)) : c.count - 1;
      },
      indexOf: (label) => {
        const t = utc(label);
        const i = Math.round((t - t0) / step);
        return i >= 0 && i < c.count && format(t0 + i * step, c.format) === label ? i : -1;
      },
    };
  }
  const times = layer.times ?? [];
  if (!times.length) return EMPTY;
  return {
    count: times.length,
    at: (i) => times[Math.max(0, Math.min(times.length - 1, i))],
    nearest: (label) => {
      // binary search: ISO labels sort like times
      let lo = 0;
      let hi = times.length;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (times[mid] < label) lo = mid + 1;
        else hi = mid;
      }
      if (lo >= times.length) return times.length - 1;
      if (lo === 0 || times[lo] === label) return lo;
      const a = utc(times[lo - 1]);
      const b = utc(times[lo]);
      const t = utc(label);
      if (Number.isFinite(a) && Number.isFinite(b) && Number.isFinite(t)) return t - a <= b - t ? lo - 1 : lo;
      return lo - 1;
    },
    indexOf: (label) => {
      const i = times.indexOf(label);
      return i;
    },
  };
}
