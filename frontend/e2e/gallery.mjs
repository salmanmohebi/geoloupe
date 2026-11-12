// Browser test of the gallery (featured views), against a running server with `views:`.
//
//   node e2e/gallery.mjs http://127.0.0.1:8000 out-dir
//
// Opens the plain URL (the gallery must appear over the first view), waits for the thumbnails, then
// opens every view from its card and waits for its tiles. Saves a screenshot of each and exits
// non-zero on console errors, missing thumbnails or a view that does not open its layer.

import { chromium } from "@playwright/test";
import fs from "node:fs";

const [base = "http://127.0.0.1:8000", out = "gallery-shots"] = process.argv.slice(2);
fs.mkdirSync(out, { recursive: true });

const browser = await chromium.launch({
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
const errors = [];
page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
page.on("pageerror", (e) => errors.push(String(e)));

async function settle() {
  await page.waitForSelector(".provenance, .colorbar", { timeout: 90_000 });
  let quiet = 0;
  for (let i = 0; i < 800 && quiet < 6; i++) {
    const busy = await page.evaluate(() => !!document.querySelector(".loading-bar.on"));
    quiet = busy ? 0 : quiet + 1;
    await page.waitForTimeout(250);
  }
}

let failed = 0;
const fail = (msg) => {
  failed++;
  console.log(`  FAIL ${msg}`);
};

// 1. the start screen
let t0 = Date.now();
await page.goto(`${base}/`, { waitUntil: "load" });
await page.waitForSelector(".gallery-card", { timeout: 90_000 });
const views = await page.evaluate(() =>
  window
    .fetch("api/catalog")
    .then((r) => r.json())
    .then((c) => c.views),
);
// thumbnails: every <img> finished (loaded or failed)
for (let i = 0; i < 240; i++) {
  const pending = await page.evaluate(
    () => [...document.querySelectorAll(".gallery-card img")].filter((im) => !im.complete).length,
  );
  if (!pending) break;
  await page.waitForTimeout(500);
}
const thumbs = await page.evaluate(() =>
  [...document.querySelectorAll(".gallery-card")].map((c) => ({
    id: c.getAttribute("data-view"),
    imgs: [...c.querySelectorAll("img")].map((im) => im.naturalWidth),
    dots: !!c.querySelector(".thumb-dots"),
  })),
);
for (const t of thumbs)
  if (!t.dots && (!t.imgs.length || t.imgs.some((w) => !w))) fail(`thumbnail of view ${t.id}`);
await page.screenshot({ path: `${out}/00-gallery.png` });
console.log(`ok   gallery: ${views.length} views, thumbnails in ${((Date.now() - t0) / 1000).toFixed(1)} s`);

// 2. every view, opened from its card
for (const v of views) {
  t0 = Date.now();
  await page.goto(`${base}/`, { waitUntil: "load" });
  await page.waitForSelector(`.gallery-card[data-view="${v.id}"]`, { timeout: 90_000 });
  // a new hash reloads the page into the view; the first view is already behind the gallery
  await page.click(`.gallery-card[data-view="${v.id}"]`);
  await page.waitForTimeout(500);
  await page.waitForLoadState("load");
  await settle();
  const state = await page.evaluate(() => ({
    hash: decodeURIComponent(window.location.hash),
    gallery: !!document.querySelector(".gallery"),
    title: document.title,
  }));
  if (!state.hash.includes(`layer=${v.layer}`)) fail(`view ${v.id}: hash ${state.hash}`);
  if (v.compare_time && !state.hash.includes(`ct=${v.compare_time}`))
    fail(`view ${v.id}: the comparison time was lost (${state.hash})`);
  if (v.vectors?.length) {
    // an overlay is drawn and found under the cursor: hover the map centre
    const box = await page.locator(".map-el").first().boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.move(box.x + box.width / 2 + 3, box.y + box.height / 2 + 2);
    const tip = await page
      .waitForSelector(".tip-feature", { timeout: 15_000 })
      .then((el) => el.innerText())
      .catch(() => null);
    if (!tip) fail(`view ${v.id}: no overlay feature under the cursor`);
    else console.log(`     hover: ${tip.replace(/\s+/g, " ")}`);
  }
  if (state.gallery) fail(`view ${v.id}: gallery still open`);
  const name = `${String(v.id + 1).padStart(2, "0")}-${v.title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/-$/, "")}`;
  await page.screenshot({ path: `${out}/${name}.png` });
  console.log(`ok   ${name} (${((Date.now() - t0) / 1000).toFixed(1)} s) · ${state.title}`);
}

// 3. the toolbar button reopens the gallery
await page.click("button.btn.tool:has-text('Gallery')");
await page.waitForSelector(".gallery", { timeout: 5_000 }).catch(() => fail("Gallery button"));

// 4. "Add data" (when the server allows remote data): browse a public S3 bucket and open a COG in a
//    new pane on the right
const cat = await page.evaluate(() => window.fetch("api/catalog").then((r) => r.json()));
if (cat.allow_open) {
  t0 = Date.now();
  await page.goto(`${base}/#layer=landcover:landcover&c=6.6,46.42&z=9`, { waitUntil: "load" });
  await settle();
  await page.click("button.btn.tool:has-text('Add data')");
  await page.click(".add-tabs button:has-text('Browse S3')");
  await page.fill(".s3-tab input[placeholder^='s3://']", "s3://esa-worldcover/v200/2021/map/");
  await page.click(".s3-tab button:has-text('List')");
  await page.waitForSelector(".s3-list li", { timeout: 60_000 });
  await page.fill(".s3-filter", "N45E006");
  await page.press(".s3-filter", "Enter");
  await page.waitForFunction(() => document.querySelectorAll(".s3-list li").length <= 3, null, {
    timeout: 30_000,
  });
  await page.check("input[name=where] >> nth=1"); // a new pane on the right
  await page.click(".s3-list button:has-text('Open')");
  await page
    .waitForSelector(".add-data", { state: "detached", timeout: 120_000 })
    .catch(() => fail("S3 open"));
  await settle();
  const titles = await page.evaluate(() =>
    [...document.querySelectorAll(".map-pane")].map((p) => p.querySelector(".pane-title")?.textContent ?? ""),
  );
  if (titles.length !== 2 || !titles[1].includes("N45E006"))
    fail(`S3 COG in a new pane: ${JSON.stringify(titles)}`);
  await page.screenshot({ path: `${out}/14-add-data-s3.png` });
  console.log(`ok   14-add-data-s3 (${((Date.now() - t0) / 1000).toFixed(1)} s) · ${titles.join(" | ")}`);
}

await browser.close();
for (const e of errors) console.log(`  console error: ${e}`);
if (errors.length || failed) {
  console.log(`FAILED: ${failed} checks, ${errors.length} console errors`);
  process.exit(1);
}
console.log("gallery test passed");
