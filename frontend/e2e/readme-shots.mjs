// The README pictures of the panes and of Add data, from a running `geoloupe gallery --allow-open`.
//
//   node e2e/readme-shots.mjs http://127.0.0.1:8000 out-dir
//
// Writes four-panes.png (hover with all four values), storm-panes.png (point details of three
// panes), layout-export.png (Export → all panes as one image), s3-browse.png (Browse S3),
// error-panes.png (a model against the reanalysis, with error panes) and error-point.png (the
// error at a point over time).
// The other README pictures come from e2e/gallery.mjs; convert all to JPEG for docs/images.

import { chromium } from "@playwright/test";
import fs from "node:fs";

const [base = "http://127.0.0.1:8000", out = "readme-shots"] = process.argv.slice(2);
fs.mkdirSync(out, { recursive: true });
const browser = await chromium.launch({
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
});
const page = await browser.newPage({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
  acceptDownloads: true,
});
const errors = [];
page.on("console", (m) => m.type() === "error" && errors.push(m.text()));

async function settle() {
  await page.waitForSelector(".provenance", { timeout: 90_000 });
  let quiet = 0;
  for (let i = 0; i < 600 && quiet < 8; i++) {
    const busy = await page.evaluate(() => !!document.querySelector(".loading-bar.on"));
    quiet = busy ? 0 : quiet + 1;
    await page.waitForTimeout(250);
  }
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

const area = () => page.locator(".map-view").boundingBox();

// 1. four panes, the hover read-out with all four values
await openView("The Western Alps in four panes");
let b = await area();
await page.mouse.move(b.x + b.width * 0.66, b.y + b.height * 0.22, { steps: 4 });
await page.waitForSelector(".hover-tip", { timeout: 15_000 });
await page.waitForTimeout(800);
await page.screenshot({ path: `${out}/four-panes.png` });

// 2. three panes of a storm, with the point details of all panes
await openView("Storm Ciarán in three panes");
b = await area();
await page.click("button.btn.tool:has-text('Point')");
await page.mouse.click(b.x + b.width * 0.3, b.y + b.height * 0.42);
await page.waitForSelector(".pane-values canvas", { timeout: 90_000 });
await page.waitForTimeout(1500);
await page.mouse.move(120, 40); // over the side panel: no hover read-out
await page.screenshot({ path: `${out}/storm-panes.png` });

// 3. the layout as one image (Export)
await openView("A climate model next to the reanalysis");
await page.click("button.btn.tool:has-text('Export')");
const [dl] = await Promise.all([
  page.waitForEvent("download", { timeout: 60_000 }),
  page.click(".menu-pop button:has-text('panes as one image')"),
]);
await dl.saveAs(`${out}/layout-export.png`);
await page.keyboard.press("Escape");

// 3b. error panes: the model against the reanalysis, bias and absolute error with their statistics
await openView("How far is the model from the reanalysis?");
await page.waitForSelector(".map-pane .pane-stats", { timeout: 120_000 }).catch(() => null);
await page.waitForTimeout(1500);
await page.mouse.move(120, 40);
await page.screenshot({ path: `${out}/error-panes.png` });
// 3c. the error at a point, over time (point details of the bias pane)
b = await area();
await page.mouse.click(b.x + b.width * 0.25, b.y + b.height * 0.75); // pane 3 becomes active
await page.waitForTimeout(500);
await page.click("button.btn.tool:has-text('Point')");
await page.mouse.click(b.x + b.width * 0.36, b.y + b.height * 0.66); // right of the side panel
await page.waitForSelector(".error-series canvas", { timeout: 120_000 }).catch(() => null);
await page.locator(".error-series").scrollIntoViewIfNeeded().catch(() => null);
await page.waitForTimeout(1500);
await page.mouse.move(120, 40);
await page.screenshot({ path: `${out}/error-point.png` });
await page.keyboard.press("Escape");
// 4. Add data → Browse S3 (a public AWS bucket), filtered
await page.goto(`${base}/#layer=landcover:landcover&c=6.6,46.42&z=9`, { waitUntil: "load" });
await settle();
await page.click("button.btn.tool:has-text('Add data')");
await page.click(".add-tabs button:has-text('Browse S3')");
await page.fill(".s3-tab input[placeholder^='s3://']", "s3://esa-worldcover/v200/2021/map/");
await page.click(".s3-tab button:has-text('List')");
await page.waitForSelector(".s3-list li", { timeout: 60_000 });
await page.fill(".s3-filter", "N45E00");
await page.press(".s3-filter", "Enter");
await page.waitForFunction(() => document.querySelectorAll(".s3-list li").length <= 12, null, {
  timeout: 30_000,
});
await page.check("input[name=where] >> nth=1");
await page.waitForSelector(".s3-tab button:has-text('List')", { timeout: 30_000 }); // listing done
await page.waitForTimeout(500);
await page.screenshot({ path: `${out}/s3-browse.png` });

await browser.close();
console.log(`README pictures in ${out}${errors.length ? `; console errors: ${errors.join(" | ")}` : ""}`);
process.exit(errors.length ? 1 : 0);
