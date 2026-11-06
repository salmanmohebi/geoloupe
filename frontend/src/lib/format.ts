// Number and coordinate formatting.

export function fmt(v: number | null | undefined, digits = 4): string {
  if (v === null || v === undefined || !isFinite(v)) return "–";
  const a = Math.abs(v);
  if (a !== 0 && (a >= 1e6 || a < 1e-3)) return v.toExponential(2);
  return Number(v.toPrecision(digits)).toLocaleString("en-US", { maximumFractionDigits: 6 });
}

export function fmtTick(v: number): string {
  const a = Math.abs(v);
  if (a !== 0 && (a >= 1e5 || a < 1e-2)) return v.toExponential(1);
  return String(Number(v.toPrecision(4)));
}

export function fmtLonLat(lon: number, lat: number, digits = 3): string {
  const ns = lat >= 0 ? "N" : "S";
  const ew = lon >= 0 ? "E" : "W";
  return `${Math.abs(lat).toFixed(digits)}° ${ns}, ${Math.abs(lon).toFixed(digits)}° ${ew}`;
}

export function unitLabel(u: string | null | undefined): string {
  if (!u) return "";
  return u
    .replace(/\bm-2\b/g, "m⁻²")
    .replace(/\bs-1\b/g, "s⁻¹")
    .replace(/\bday-1\b/g, "day⁻¹")
    .replace(/\bm s-1\b/g, "m s⁻¹")
    .replace(/\*\*-?(\d)/g, (_, d) => "⁻" + "⁰¹²³⁴⁵⁶⁷⁸⁹"[Number(d)]);
}
