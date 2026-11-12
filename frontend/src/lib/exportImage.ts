// Export the current map as a PNG with a title, a colour bar and the data
// attribution, ready for slides or a report.

import { lutColor, scalePosition, type ColorScale } from "./colormap";
import { fmtTick } from "./format";
import type { Map as MlMap } from "maplibre-gl";
import type { Rect } from "./layout";
import { getMainMap } from "./mapRegistry";

export interface FigureText {
  title: string;
  subtitle: string;
  units: string;
  ticks: number[];
  credit: string;
}

/** Copy the WebGL canvas right after a render (no preserveDrawingBuffer needed). */
function grabMap(map: MlMap | null = getMainMap()): Promise<HTMLCanvasElement> {
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
  await download(c, text.title);
}

async function download(c: HTMLCanvasElement, title: string): Promise<void> {
  const blob: Blob = await new Promise((res) => c.toBlob((b) => res(b!), "image/png"));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `geoloupe_${title.replace(/[^\w.-]+/g, "_").slice(0, 60)}.png`;
  a.click();
  URL.revokeObjectURL(a.href);
}

/** One pane of a layout figure. `scale` is null for RGB and class layers (no colour bar). */
export interface PaneFigure {
  map: MlMap;
  rect: Rect; // in fractions of the map area
  label: string; // "1 · Air temperature · 2023-11-02T00:00"
  scale: ColorScale | null;
  units: string;
  ticks: number[];
}

/** All panes as one image, placed as on screen, each with its label and colour bar. */
export async function exportLayoutPng(
  panes: PaneFigure[],
  area: { width: number; height: number },
  text: { title: string; subtitle: string; credit: string },
): Promise<void> {
  const images = await Promise.all(panes.map((p) => grabMap(p.map)));
  const dpr = images[0].width / (panes[0].map.getContainer().clientWidth || images[0].width);
  const px = (v: number) => Math.round(v * dpr);
  const pad = px(16);
  const head = px(58);
  const foot = px(30);
  const W = Math.round(area.width * dpr);
  const mapH = Math.round(area.height * dpr);
  const c = document.createElement("canvas");
  c.width = W;
  c.height = head + mapH + foot;
  const ctx = c.getContext("2d")!;
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, c.width, c.height);
  ctx.fillStyle = "#0b0b0b";
  ctx.font = `600 ${px(18)}px system-ui, sans-serif`;
  ctx.fillText(text.title, pad, pad + px(16));
  ctx.fillStyle = "#52514e";
  ctx.font = `${px(13)}px system-ui, sans-serif`;
  ctx.fillText(text.subtitle, pad, pad + px(36));
  panes.forEach((p, i) => {
    const x = Math.round(p.rect.x * W);
    const y = head + Math.round(p.rect.y * mapH);
    const w = Math.round(p.rect.w * W);
    const h = Math.round(p.rect.h * mapH);
    ctx.drawImage(images[i], x, y, w, h);
    // label, top left
    ctx.font = `600 ${px(12)}px system-ui, sans-serif`;
    const label = fitText(ctx, p.label, w - 2 * px(8));
    const lw = ctx.measureText(label).width + px(12);
    ctx.fillStyle = "rgba(255,255,255,0.88)";
    ctx.fillRect(x + px(8), y + px(8), lw, px(22));
    ctx.fillStyle = "#0b0b0b";
    ctx.fillText(label, x + px(14), y + px(23));
    // colour bar, bottom left
    if (p.scale) {
      const bw = Math.max(px(60), Math.min(w - px(40), px(220)));
      const bx = x + px(14);
      const by = y + h - px(36);
      const bh = px(9);
      ctx.fillStyle = "rgba(255,255,255,0.88)";
      ctx.fillRect(x + px(8), by - px(6), bw + px(12) + px(40), px(38));
      for (let k = 0; k < bw; k++) {
        ctx.fillStyle = lutColor(p.scale.lut, k / (bw - 1));
        ctx.fillRect(bx + k, by, 1, bh);
      }
      ctx.fillStyle = "#52514e";
      ctx.font = `${px(10)}px system-ui, sans-serif`;
      ctx.textAlign = "center";
      for (const t of p.ticks) {
        const pos = scalePosition(t, p.scale);
        if (pos >= 0 && pos <= 1) ctx.fillText(fmtTick(t), bx + pos * bw, by + bh + px(12));
      }
      ctx.textAlign = "left";
      ctx.fillText(p.units, bx + bw + px(6), by + bh - px(1));
    }
    // dividers
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = px(2);
    ctx.strokeRect(x, y, w, h);
  });
  ctx.fillStyle = "#52514e";
  ctx.font = `${px(11)}px system-ui, sans-serif`;
  ctx.textAlign = "right";
  ctx.fillText(fitText(ctx, text.credit, W - 2 * pad), W - pad, c.height - px(10));
  ctx.textAlign = "left";
  await download(c, text.title);
}

function fitText(ctx: CanvasRenderingContext2D, s: string, max: number): string {
  if (ctx.measureText(s).width <= max) return s;
  let t = s;
  while (t.length > 1 && ctx.measureText(`${t}…`).width > max) t = t.slice(0, -1);
  return `${t}…`;
}
