// A short scripted tour through the gallery, saved as frames for an animated GIF (README).
//
//   node e2e/tour.mjs http://127.0.0.1:8000 out-dir      # a server running `geoloupe gallery`
//   cd out-dir && bash make-gif.sh                        # needs ImageMagick (convert)
//
// Every frame is taken after the map has finished loading, so the tour shows real data as it
// appears, not loading states. out-dir/frames.txt lists "file delay" (1/100 s) per frame.

import { chromium } from "@playwright/test";
import fs from "node:fs";

const [base = "http://127.0.0.1:8000", out = "tour"] = process.argv.slice(2);
fs.mkdirSync(out, { recursive: true });
const browser = await chromium.launch({
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
});
const page = await browser.newPage({ viewport: { width: 1280, height: 760 }, deviceScaleFactor: 1 });
const frames = [];
let n = 0;

async function settle(quietSteps = 6) {
  await page.waitForSelector(".provenance, .colorbar", { timeout: 90_000 });
  let quiet = 0;
  for (let i = 0; i < 800 && quiet < quietSteps; i++) {
    const busy = await page.evaluate(() => !!document.querySelector(".loading-bar.on"));
    quiet = busy ? 0 : quiet + 1;
    await page.waitForTimeout(200);
  }
}

async function frame(delay) {
  const file = `f${String(n++).padStart(3, "0")}.png`;
  await page.screenshot({ path: `${out}/${file}` });
  frames.push(`${file} ${delay}`);
}

async function openView(title) {
  await page.goto(`${base}/`, { waitUntil: "load" });
  const card = page.locator(".gallery-card", { hasText: title }).first();
  await card.waitFor({ timeout: 90_000 });
  await card.click();
  await page.waitForTimeout(500);
  await page.waitForLoadState("load");
  await settle();
}

// 1. the start screen
await page.goto(`${base}/`, { waitUntil: "load" });
await page.waitForSelector(".gallery-card img");
await page.waitForFunction(() =>
  [...document.querySelectorAll(".gallery-card img")].every((i) => i.complete),
);
await frame(250);

// 2. a view, a hover read-out, then hour by hour
await openView("Europe's heatwave");
const box = await page.locator(".map-el").first().boundingBox();
await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
await page.mouse.move(box.x + box.width * 0.5 + 3, box.y + box.height * 0.5 + 2);
await page.waitForSelector(".tip-feature", { timeout: 15_000 });
await frame(220);
await page.mouse.move(box.x + 5, box.y + box.height - 5); // hide the read-out
for (let k = 0; k < 6; k++) {
  await page.getByRole("button", { name: "Next time" }).click();
  await settle(4);
  await frame(55);
}

// 3. from 31 km to 30 m: zoom in on the Matterhorn
await openView("From 31 km to 30 m");
for (const z of [5.5, 6.5, 7.5, 8.5, 9.5, 10.5]) {
  await page.evaluate((zoom) => window.__geoloupeMap.jumpTo({ zoom }), z);
  await settle(4);
  await frame(z === 10.5 ? 200 : 70);
}

// 4. split panes: a storm split into three variables with the keys, then the hover of all three
await openView("Storm Ciarán reaches Europe");
await frame(120);
const layerSelect = page.locator(".layer-panel label.field select").first();
for (const [key, layer] of [
  ["%", "wind-gust"],
  ['"', "water-vapour"],
]) {
  await page.locator(".map-pane.active .maplibregl-canvas, .map-pane .maplibregl-canvas").first().focus();
  await page.keyboard.press(key); // tmux keys: % splits right, " splits down
  await settle(4);
  await layerSelect.selectOption(layer); // the new pane is the active one
  await settle(4);
  await frame(110);
}
const area = await page.locator(".map-view").boundingBox();
await page.mouse.move(area.x + area.width * 0.32, area.y + area.height * 0.45, { steps: 4 });
await page.waitForSelector(".hover-tip", { timeout: 15_000 });
await frame(260);

// 5. satellite: true vs false colour in two panes, and the Alps in four
await openView("True vs false colour");
await frame(180);
await openView("The Western Alps in four panes");
await frame(230);

// 6. a climate model's ocean on its own grid: 165 years of warming
await openView("165 years of ocean warming");
await frame(260);

fs.writeFileSync(`${out}/frames.txt`, frames.join("\n") + "\n");
fs.writeFileSync(
  `${out}/make-gif.sh`,
  `# Assemble geoloupe-tour.gif (960 px wide, 128 colours) from the frames
set -e
args=$(awk '{printf "-delay %s %s ", $2, $1}' frames.txt)
convert $args -loop 0 -resize 960x -colors 128 -layers Optimize geoloupe-tour.gif
ls -la geoloupe-tour.gif
`,
);
await browser.close();
console.log(`${frames.length} frames in ${out}`);
