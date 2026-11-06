// End-to-end smoke test and screenshots, against a running TerraScope server.
//
//   node e2e/smoke.mjs http://127.0.0.1:8000 out-dir [scenario.json]
//
// A scenario is a list of steps (see DEFAULT below). Each step opens a URL
// hash, waits until all tiles are loaded, optionally clicks or hovers, checks
// the page and saves a screenshot. Exits non-zero on console errors or
// failed checks.

import { chromium } from "@playwright/test";
import fs from "node:fs";

const [base = "http://127.0.0.1:8000", out = "screenshots", scenarioFile] = process.argv.slice(2);

const DEFAULT = [
  { name: "01-global", hash: "#layer=tas&t=2021-07-15&c=10,30&z=1.4", expect: ["Global reanalysis"] },
  {
    name: "02-alps-1km",
    hash: "#layer=tas&t=2021-07-15&c=10.2,46.4&z=7.3",
    expect: ["Alps downscaled", "native"],
    hover: [0.55, 0.5],
  },
  {
    name: "03-inspect",
    hash: "#layer=tas&t=2021-07-15&c=10.2,46.4&z=7.3&pt=10.30,46.45",
    waitFor: ".ts-chart canvas",
    expect: ["Time series"],
  },
  {
    name: "04-split",
    hash: "#layer=alps:tas&t=2021-07-15&c=10.2,46.4&z=6.5&cmp=split&cl=global:tas",
    expect: ["B ·"],
  },
  {
    name: "05-difference",
    hash: "#layer=alps:tas&t=2021-07-15&c=10.2,46.4&z=6.5&cmp=diff&cl=global:tas",
    expect: ["Difference A − B"],
  },
  { name: "06-laea-projected", hash: "#layer=laea:elevation&c=10,50&z=3.2", expect: ["EPSG:3035"] },
  {
    name: "07-stations",
    hash: "#layer=tas&t=2021-07-15&c=10.2,46.4&z=7&range=262,296",
    station: true,
    expect: ["Weather stations", "Observed", "observed − map"],
  },
  {
    name: "08-points-layer",
    hash: "#layer=stations:tas&t=2021-07-15&c=10.2,46.4&z=6.5",
    station: true,
    expect: ["stations with data", "Station"],
  },
];

const scenario = scenarioFile ? JSON.parse(fs.readFileSync(scenarioFile, "utf8")) : DEFAULT;
fs.mkdirSync(out, { recursive: true });

const browser = await chromium.launch({
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
const errors = [];
page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
page.on("pageerror", (e) => errors.push(String(e)));

async function settle() {
  // the provenance badge exists and no tile request is in flight for a while
  await page.waitForSelector(".provenance", { timeout: 60_000 });
  let quiet = 0;
  for (let i = 0; i < 600 && quiet < 6; i++) {
    const busy = await page.evaluate(() => !!document.querySelector(".loading-bar.on"));
    quiet = busy ? 0 : quiet + 1;
    await page.waitForTimeout(250);
  }
}

let failed = 0;
for (const step of scenario) {
  const t0 = Date.now();
  await page.goto(`${base}/${step.hash}`, { waitUntil: "load" });
  await settle();
  if (step.waitFor) await page.waitForSelector(step.waitFor, { timeout: 60_000 });
  if (step.station) {
    // hover a station circle, check the tooltip, then click it and wait for its series
    const pos = await page.evaluate(() => {
      // a station in the open map area (not under a panel)
      const m = window.__terrascopeMap;
      const r = m.getContainer().getBoundingClientRect();
      const free = (p) => p.x > 400 && p.x < r.width - 450 && p.y > 120 && p.y < r.height - 160;
      const f = m
        ?.queryRenderedFeatures({ layers: ["ts-points-lyr"] })
        ?.find((f) => free(m.project(f.geometry.coordinates)));
      if (!f) return null;
      const p = m.project(f.geometry.coordinates);
      return { x: r.left + p.x, y: r.top + p.y };
    });
    if (!pos) throw new Error(`${step.name}: no station on screen`);
    await page.mouse.move(pos.x, pos.y, { steps: 5 });
    await page.waitForSelector(".hover-tip:has-text('observed')", { timeout: 10_000 });
    await page.screenshot({ path: `${out}/${step.name}-hover.png` });
    await page.mouse.click(pos.x, pos.y);
    await page.waitForSelector(".inspector .eyebrow:has-text('Station')", { timeout: 20_000 });
    await page.waitForSelector(".ts-chart canvas", { timeout: 60_000 });
  }
  if (step.hover) {
    const box = await page.locator(".map-pane").first().boundingBox();
    await page.mouse.move(box.x + box.width * step.hover[0], box.y + box.height * step.hover[1]);
    await page.waitForSelector(".hover-tip", { timeout: 10_000 });
  }
  await settle();
  const text = await page.evaluate(() => document.body.innerText);
  for (const e of step.expect ?? []) {
    if (!text.toLowerCase().includes(e.toLowerCase())) {
      console.error(`✗ ${step.name}: page does not contain ${JSON.stringify(e)}`);
      failed++;
    }
  }
  await page.screenshot({ path: `${out}/${step.name}.png` });
  const badge = await page.evaluate(() => document.querySelector(".provenance")?.textContent ?? "");
  console.log(
    `✓ ${step.name} (${((Date.now() - t0) / 1000).toFixed(1)} s): ${badge.replace(/\s+/g, " ").trim()}`,
  );
}

// "Add data" (only when the server allows it, e.g. --open-root demo): open a file from the browser
const cat = await page.evaluate(() => window.fetch("api/catalog").then((r) => r.json()));
if (cat.allow_open || cat.open_roots?.length) {
  const t0 = Date.now();
  const file = process.env.SMOKE_ADD_FILE ?? "global-plev-monthly.nc";
  await page.goto(`${base}/#layer=tas&t=2021-07-15&c=10,30&z=1.4`, { waitUntil: "load" });
  await settle();
  await page.click("button.btn.tool:has-text('Add data')");
  await page.fill(".add-data input", file);
  await page.click(".add-data button[type=submit]");
  const closed = await page
    .waitForSelector(".add-data", { state: "detached", timeout: 60_000 })
    .then(() => true)
    .catch(() => false);
  const errText = closed
    ? null
    : await page
        .locator(".add-data-error")
        .innerText()
        .catch(() => "?");
  await settle();
  const hash = await page.evaluate(() => decodeURIComponent(window.location.hash));
  const stem = file.split("/").pop().split(".")[0];
  await page.screenshot({ path: `${out}/09-add-data.png` });
  if (!closed || !hash.includes(`layer=${stem}`)) {
    console.log(`✗ 09-add-data: ${errText ?? hash}`);
    failed++;
  } else
    console.log(
      `✓ 09-add-data (${((Date.now() - t0) / 1000).toFixed(1)} s): opened ${file} → ${hash.split("&")[0]}`,
    );
}

await browser.close();
if (errors.length) {
  console.error(`console errors:\n  ${[...new Set(errors)].join("\n  ")}`);
  failed++;
}
process.exit(failed ? 1 : 0);
