// End-to-end test of the split panes (tmux-like), against a server with the demo data
// (`geoloupe demo-data demo && geoloupe serve demo/demo.yaml`).
//
//   node e2e/panes.mjs http://127.0.0.1:8000 out-dir
//
// Splits with the keys, picks a layer in a pane, hovers (one row per pane, a cross in the other
// pane), drags a divider, checks linked time and camera, unlinks the camera, closes a pane, and
// reloads the shared link. Exits non-zero on a failed check or a console error.

import { chromium } from "@playwright/test";
import fs from "node:fs";

const [base = "http://127.0.0.1:8000", out = "screenshots"] = process.argv.slice(2);
fs.mkdirSync(out, { recursive: true });

const browser = await chromium.launch({
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
const errors = [];
page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
page.on("pageerror", (e) => errors.push(String(e)));

let failed = 0;
const check = (ok, what) => {
  if (ok) console.log(`✓ ${what}`);
  else {
    console.error(`✗ ${what}`);
    failed++;
  }
};

async function settle() {
  await page.waitForSelector(".provenance", { timeout: 60_000 });
  let quiet = 0;
  for (let i = 0; i < 600 && quiet < 6; i++) {
    const busy = await page.evaluate(() => !!document.querySelector(".loading-bar.on"));
    quiet = busy ? 0 : quiet + 1;
    await page.waitForTimeout(250);
  }
}

const panes = () =>
  page.evaluate(() =>
    [...document.querySelectorAll(".map-pane")]
      .filter((p) => p.style.display !== "none")
      .map((p) => {
        const r = p.getBoundingClientRect();
        return {
          n: p.dataset.pane,
          active: p.classList.contains("active"),
          title: p.querySelector(".pane-title")?.textContent ?? "",
          x: r.x,
          y: r.y,
          w: r.width,
          h: r.height,
        };
      }),
  );
const hash = () => page.evaluate(() => decodeURIComponent(window.location.hash));
const press = async (key) => {
  await page.locator(".map-pane.active .maplibregl-canvas, .map-pane .maplibregl-canvas").first().focus();
  await page.keyboard.press(key);
  await page.waitForTimeout(300);
};
const centerOf = (p, fx = 0.5, fy = 0.5) => ({ x: p.x + p.w * fx, y: p.y + p.h * fy });

// 1. One pane; % splits right, the new pane is active
// (ov=none: no stations, so the hover reads the grids)
await page.goto(`${base}/#layer=tas&t=2021-07-15&c=10.2,46.4&z=6.5&ov=none`, { waitUntil: "load" });
await settle();
check((await panes()).length === 1, "starts with one pane");
await press("%");
await settle();
let ps = await panes();
check(
  ps.length === 2 && ps[1].active && Math.abs(ps[0].w - ps[1].w) < 4,
  "% splits right; the new pane is active",
);

// 2. The layer panel acts on the active pane: precipitation in pane 2
const layerSelect = page.locator(".layer-panel label.field select").first();
const prId = await layerSelect.evaluate(
  (s) => [...s.options].find((o) => /precipitation/i.test(o.text))?.value,
);
await layerSelect.selectOption(prId);
await settle();
ps = await panes();
check(
  /precipitation/i.test(ps[1].title) && /temperature/i.test(ps[0].title),
  "pane 2 shows precipitation, pane 1 temperature",
);

// 3. Hover pane 1: one row per pane, and a cross in pane 2
await page.mouse.move(centerOf(ps[0]).x, centerOf(ps[0]).y, { steps: 4 });
await page.waitForSelector(".hover-tip", { timeout: 10_000 });
const tags = await page.evaluate(() =>
  [...document.querySelectorAll(".hover-tip .tip-tag")].map((t) => t.textContent),
);
check(tags.join(",") === "1,2", `hover lists both panes (${tags.join(",")})`);
check(
  (await page.locator('.map-pane[data-pane="2"] .cursor-cross').count()) === 1,
  "pane 2 marks the hovered point",
);
await page.screenshot({ path: `${out}/panes-01-hover.png` });

// 4. Linked time: a new time in pane 2 reaches pane 1
const timeInput = page.locator(".time-bar .time-input");
await timeInput.fill("2021-08-01");
await timeInput.press("Enter");
await settle();
ps = await panes();
check(
  ps.every((p) => p.title.includes("2021-08-01")),
  "linked time: both panes show 2021-08-01",
);

// 5. Drag the divider to 30 %
const area = await page.locator(".map-view").boundingBox();
const div = await page.locator(".pane-divider.row").boundingBox();
await page.mouse.move(div.x + div.width / 2, div.y + div.height / 2);
await page.mouse.down();
await page.mouse.move(area.x + area.width * 0.3, div.y + div.height / 2, { steps: 8 });
await page.mouse.up();
await page.waitForTimeout(400);
ps = await panes();
check(
  Math.abs(ps[0].w / area.width - 0.3) < 0.02,
  `divider dragged (pane 1 is ${Math.round((100 * ps[0].w) / area.width)} %)`,
);

// 6. " splits pane 2 down; x closes it again
await press('"');
await settle();
ps = await panes();
check(ps.length === 3 && ps[2].active && Math.abs(ps[1].h - ps[2].h) < 4, '" splits down');
await press("x");
await page.waitForTimeout(400);
check((await panes()).length === 2, "x closes the active pane");

// 7. Linked camera: zooming pane 1 zooms pane 2; after unlinking pane 2 it stays
const zooms = () => page.evaluate(() => window.__geoloupeMaps.map((m) => m.getZoom().toFixed(3)));
ps = await panes();
const free = centerOf(ps[0], 0.88, 0.45); // pane 1 is narrow now: right of the layer panel
await press("Alt+ArrowLeft"); // pane 1 active (a click would open the point details)
check((await panes())[0].active, "Alt+← moves to pane 1");
await page.mouse.move(free.x, free.y);
await page.mouse.wheel(0, -600);
await settle();
let z = await zooms();
check(z[0] === z[1], `linked camera: the same zoom in both panes (${z.join(" / ")})`);
await page.screenshot({ path: `${out}/panes-02-linked.png` });
await page.locator('.map-pane[data-pane="2"] .pane-box button[title^="Camera linked"]').click();
await page.waitForTimeout(300);
await press("Alt+ArrowLeft");
await page.mouse.move(free.x, free.y);
await page.mouse.wheel(0, 900);
await settle();
z = await zooms();
check(z[0] !== z[1], `unlinked camera: pane 2 keeps its zoom (${z.join(" / ")})`);
const raw = await page.evaluate(() => window.location.hash);
check(/p1=[^&]*lv%3D0/.test(raw), "the link records the unlinked camera (lv=0 in pane 2)");

// 8. The link keeps the layout: reload and compare
const before = (await panes()).map((p) => [p.title, Math.round(p.w)]);
await page.reload({ waitUntil: "load" });
await settle();
const after = (await panes()).map((p) => [p.title, Math.round(p.w)]);
check(
  JSON.stringify(before) === JSON.stringify(after),
  `a reload restores the panes (${JSON.stringify(after)})`,
);
await page.screenshot({ path: `${out}/panes-03-reload.png` });

// 9. A 0.1 link (cmp=split) opens as two panes
await page.goto("about:blank");
await page.goto(`${base}/#layer=alps:tas&t=2021-07-15&c=10.2,46.4&z=6.5&cmp=split&cl=global:tas`, {
  waitUntil: "load",
});
await settle();
ps = await panes();
check(
  ps.length === 2 && /alps/i.test(ps[0].title) && /global/i.test(ps[1].title),
  "a 0.1 link with cmp=split opens two panes",
);
await page.screenshot({ path: `${out}/panes-04-old-link.png` });

check(errors.length === 0, `no console errors${errors.length ? ": " + errors.slice(0, 3).join(" | ") : ""}`);
await browser.close();
process.exit(failed ? 1 : 0);
