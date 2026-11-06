// Export the current map as a PNG with a title, a colour bar and the data
// attribution, ready for slides or a report.

import { lutColor, scalePosition, type ColorScale } from "./colormap";
import { fmtTick } from "./format";
import { getMainMap } from "./mapRegistry";

export interface FigureText {
  title: string;
  subtitle: string;
  units: string;
  ticks: number[];
  credit: string;
}

/** Copy the WebGL canvas right after a render (no preserveDrawingBuffer needed). */
function grabMap(): Promise<HTMLCanvasElement> {
  const map = getMainMap();
  if (!map) return Promise.reject(new Error("map not ready"));
  return new Promise((resolve) => {
    map.once("render", () => {
      const src = map.getCanvas();
      const c = document.createElement("canvas");
      c.width = src.width;
      c.height = src.height;
      c.getContext("2d")!.drawImage(src, 0, 0);
      resolve(c);
    });
    map.triggerRepaint();
  });
}

export async function exportPng(scale: ColorScale, text: FigureText): Promise<void> {
  const mapImg = await grabMap();
  const dpr = mapImg.width / (getMainMap()?.getContainer().clientWidth || mapImg.width);
  const pad = Math.round(16 * dpr);
  const head = Math.round(58 * dpr);
  const foot = Math.round(70 * dpr);
  const W = mapImg.width;
  const H = mapImg.height + head + foot;
  const c = document.createElement("canvas");
  c.width = W;
  c.height = H;
  const ctx = c.getContext("2d")!;
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = "#0b0b0b";
  ctx.font = `600 ${Math.round(18 * dpr)}px system-ui, sans-serif`;
  ctx.fillText(text.title, pad, pad + 16 * dpr);
  ctx.fillStyle = "#52514e";
  ctx.font = `${Math.round(13 * dpr)}px system-ui, sans-serif`;
  ctx.fillText(text.subtitle, pad, pad + 36 * dpr);
  ctx.drawImage(mapImg, 0, head);
  // colour bar
  const bw = Math.min(W - 2 * pad, Math.round(420 * dpr));
  const by = head + mapImg.height + Math.round(14 * dpr);
  const bh = Math.round(12 * dpr);
  for (let x = 0; x < bw; x++) {
    ctx.fillStyle = lutColor(scale.lut, x / (bw - 1));
    ctx.fillRect(pad + x, by, 1, bh);
  }
  ctx.fillStyle = "#52514e";
  ctx.font = `${Math.round(11 * dpr)}px system-ui, sans-serif`;
  ctx.textAlign = "center";
  for (const t of text.ticks) {
    const p = scalePosition(t, scale);
    if (p >= 0 && p <= 1) ctx.fillText(fmtTick(t), pad + p * bw, by + bh + 14 * dpr);
  }
  ctx.textAlign = "left";
  ctx.fillText(text.units, pad + bw + 8 * dpr, by + bh - 1 * dpr);
  ctx.textAlign = "right";
  ctx.fillText(text.credit, W - pad, H - 10 * dpr);
  const blob: Blob = await new Promise((res) => c.toBlob((b) => res(b!), "image/png"));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `terrascope_${text.title.replace(/[^\w.-]+/g, "_").slice(0, 60)}.png`;
  a.click();
  URL.revokeObjectURL(a.href);
}
