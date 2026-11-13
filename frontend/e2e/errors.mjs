// End-to-end test of error panes, against a server with the demo data
// (`geoloupe demo-data demo && geoloupe serve demo/demo.yaml`).
//
//   node e2e/errors.mjs http://127.0.0.1:8000 out-dir
//
// Two panes (fine Alps grid, coarse global grid) → "Add error pane" → the error map draws, hovers
// and has a legend; definitions, parameters and a custom formula change it; it follows a pane
// that changes layer; closing that pane keeps the layer; the link reopens it; links from 0.2
// (cmp=diff) still work. Exits non-zero on a failed check or a console error.

import { chromium } from "@playwright/test";
import fs from "node:fs";
import { annotate, reportCrashes } from "./report.mjs";

const [base = "http://127.0.0.1:8000", out = "screenshots"] = process.argv.slice(2);
fs.mkdirSync(out, { recursive: true });

const browser = await chromium.launch({
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
const errors = [];
page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
page.on("pageerror", (e) => errors.push(String(e)));
let exactRequests = 0;
page.on("request", (r) => r.url().includes("/api/error/tiles/") && exactRequests++);

let failed = 0;
reportCrashes("errors");
const check = (ok, what) => {
  if (ok) console.log(`✓ ${what}`);
  else {
    console.error(`✗ ${what}`);
    annotate("errors", what);
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
          active: p.classList.contains("active"),
          title: p.querySelector(".pane-title")?.textContent ?? "",
          legend: p.querySelector(".pane-legend")?.textContent ?? "",
          x: r.x,
          y: r.y,
          w: r.width,
          h: r.height,
        };
      }),
  );
const activePane = async () => (await panes()).find((p) => p.active);
const legendTitle = () =>
  page.evaluate(() => document.querySelector(".colorbar .cb-title")?.textContent ?? "");
const notices = () =>
  page.evaluate(() => [...document.querySelectorAll(".notice")].map((n) => n.textContent));

/** Bars of the colour bar's histogram: values of the active pane's map on screen. */
const histBars = () =>
  page.evaluate(
    () =>
      [...document.querySelectorAll(".colorbar .cb-hist rect")].filter(
        (r) => Number(r.getAttribute("height")) > 0,
      ).length,
  );

const openCompare = async () => {
  await page.locator("button.btn.tool", { hasText: "Compare" }).click();
  await page.waitForTimeout(200);
};

// 1. Two panes: the fine Alps grid (pane 1) and the coarse global grid (pane 2)
const p1 = encodeURIComponent("layer=global:tas");
await page.goto(`${base}/#layer=alps:tas&t=2021-07-15&c=10.2,46.4&z=6.5&ov=none&lay=r50(0,1)&pa=0&p1=${p1}`, {
  waitUntil: "load",
});
await settle();
check((await panes()).length === 2, "starts with two panes");

// 2. Compare → Add error pane: P = pane 1, T = pane 2 by default
await openCompare();
check(await page.locator(".new-error").isVisible(), "the Compare menu offers error maps");
await page.locator(".new-error button", { hasText: "Add error pane" }).click();
await settle(); // the menu closes itself
let ps = await panes();
let act = await activePane();
check(ps.length === 3, "an error pane is added");
check(act?.title.startsWith("P − T · "), `the error pane is active and titled P − T (${act?.title})`);
check((await legendTitle()).startsWith("P − T"), "the colour bar names the error");
check(/K/.test(act?.legend ?? ""), `the pane legend has units (${act?.legend})`);
check((await histBars()) > 5, "the error map draws (values on screen)");
await page.screenshot({ path: `${out}/errors-1-bias.png` });

// 3. Hover the error pane: a number in K
const c = { x: act.x + act.w * 0.5, y: act.y + act.h * 0.5 };
await page.mouse.move(c.x, c.y, { steps: 4 });
await page.waitForSelector(".hover-tip", { timeout: 10_000 }).catch(() => null);
const tip = await page.evaluate(() => document.querySelector(".hover-tip")?.textContent ?? "");
check(/-?\d/.test(tip) && /K/.test(tip), `hover reads the error (${tip.slice(0, 80)})`);
await page.mouse.move(5, 450);

// 3b. After the preview, the exact tiles of the server, and statistics of the view
await page.waitForSelector(".cb-stats", { timeout: 60_000 }).catch(() => null);
const cbStats = await page.evaluate(() => document.querySelector(".cb-stats")?.textContent ?? "");
check(exactRequests > 0, `exact error tiles are fetched from the server (${exactRequests})`);
check(/bias/.test(cbStats) && /MAE/.test(cbStats) && /RMSE/.test(cbStats), `view statistics (${cbStats})`);
check(/exact|grid/.test(cbStats), "with how they were computed");
const paneStats = await page.evaluate(
  () => document.querySelector(".map-pane.active .pane-stats")?.textContent ?? "",
);
check(/MAE/.test(paneStats), `the pane box shows them too (${paneStats})`);

// 3c. Point details: the error at the point, over time, and in the list of all panes
await page.keyboard.press("i");
await page.mouse.click(c.x, c.y);
await page.waitForSelector(".error-series", { timeout: 30_000 }).catch(() => null);
const chips = await page.evaluate(() =>
  [...document.querySelectorAll(".error-series .stat-chip")].map((e) => e.textContent),
);
check(chips.length === 3 && /RMS/.test(chips.join()), `error over time at the point (${chips.join(" | ")})`);
check((await page.locator(".error-series canvas").count()) > 0, "with a chart");
const rows = await page.evaluate(() =>
  [...document.querySelectorAll(".pane-values tr")].map((r) => r.textContent),
);
check(
  rows.some((r) => r.startsWith("2") && r.includes("P − T") && /K/.test(r)),
  `the error pane's value in "all panes" (${rows.join(" | ").slice(0, 160)})`,
);
await page.screenshot({ path: `${out}/errors-1b-inspect.png` });
await page.keyboard.press("Escape");
await page.locator(".inspector .icon-btn").click();

// 4. The side panel sets it up: absolute error, then relative error with a parameter
const def = page.locator("select[aria-label='Error definition']");
check(await def.isVisible(), "the side panel shows the error section");
await def.selectOption("abs");
await settle();
act = await activePane();
check(act?.title.startsWith("|P − T|"), "absolute error");
check(
  await page.evaluate(() =>
    window
      .getComputedStyle(document.querySelector(".map-pane.active .pane-grad"))
      .backgroundImage.includes("rgb"),
  ),
  "with a colour scale",
);
await def.selectOption("rel");
await page.locator(".error-panel .param input").fill("0.5");
await page.locator(".error-panel .param input").press("Enter");
await settle();
check((await legendTitle()).includes("(P − T) / T"), "relative error");
check(/%/.test((await activePane())?.legend ?? ""), "relative error is in %");

// 4b. Events: scores of the view
await def.selectOption("events");
await page.locator(".error-panel .param input").fill("290");
await page.locator(".error-panel .param input").press("Enter");
await settle();
await page.waitForSelector(".cb-stats", { timeout: 60_000 }).catch(() => null);
const scores = await page.evaluate(() => document.querySelector(".cb-stats")?.textContent ?? "");
check(/POD/.test(scores) && /CSI/.test(scores), `event scores of the view (${scores})`);

// 5. Events: a class legend with names
const classes = await page.evaluate(() =>
  [...document.querySelectorAll(".class-legend .class-name")].map((e) => e.textContent),
);
check(
  classes.includes("Hit") && classes.includes("False alarm"),
  `event classes in the legend (${classes.join(", ")})`,
);

// 6. A custom formula: an invalid one is explained, a valid one draws
await def.selectOption("custom");
const fx = page.locator(".formula-input");
check((await fx.inputValue()) === "P - T", "a custom formula starts from the current one");
await fx.fill("abs(P - X)");
check(
  (await page.locator(".error-panel .error-text").first().textContent())?.includes("unknown name 'X'"),
  "an invalid formula is explained",
);
await fx.fill("abs(P - T) > 1");
await fx.press("Enter");
await settle();
check((await activePane())?.title.startsWith("abs(P - T) > 1"), "a custom formula draws");
await page.screenshot({ path: `${out}/errors-2-custom.png` });

// 7. The link reopens the error pane (definition and followed panes)
const url = page.url();
check(
  /cmp=error/.test(url) && /em=custom/.test(url) && /fp=0/.test(url) && /ft=2/.test(url),
  "the link holds the error pane",
);
await page.goto("about:blank");
await page.goto(url, { waitUntil: "load" });
await settle();
ps = await panes();
check(ps.length === 3 && ps.some((p) => p.title.startsWith("abs(P - T) > 1")), "the link reopens it");

// 8. The error pane follows T's pane: a new layer there changes T. The error pane split pane 1,
// so the order is: 1 = P (Alps), 2 = the error pane, 3 = T (global).
const paneT = ps[2];
await page.mouse.click(paneT.x + paneT.w * 0.5, paneT.y + paneT.h * 0.35);
await page.waitForTimeout(300);
await page.locator(".layer-panel select").first().selectOption("global:pr");
await settle();
ps = await panes();
await page.screenshot({ path: `${out}/errors-3-follow.png` });
check(
  ps.some((p) => p.title.includes("vs") && /pr/i.test(p.title.split("vs")[1] ?? "")),
  "the error pane follows its T pane",
);

// 9. Closing the followed pane keeps the layer
await page.keyboard.press("x");
await page.waitForTimeout(500);
await settle();
ps = await panes();
check(ps.length === 2, "T's pane is closed");
check(
  (await notices()).some((t) => t?.includes("was closed")),
  "a notice says the error map keeps the layer",
);
check(
  ps.some((p) => p.title.includes(" vs ")),
  "the error pane still draws",
);

// 10. Links from 0.2 (cmp=diff) open as P − T
await page.goto("about:blank");
await page.goto(`${base}/#layer=alps:tas&t=2021-07-15&c=10.2,46.4&z=6.5&ov=none&cmp=diff&cl=global:tas`, {
  waitUntil: "load",
});
await settle();
check((await legendTitle()).startsWith("P − T"), "an old difference link opens as an error map");
check((await histBars()) > 5, "and draws");

// 11. One layer at two times: T has its own time, named in the title and set in the side panel
await page.goto("about:blank");
await page.goto(
  `${base}/#layer=alps:tas&t=2021-07-15&c=10.2,46.4&z=6.5&ov=none&cmp=error&cl=alps:tas&ct=2021-07-01`,
  { waitUntil: "load" },
);
await settle();
check(
  (await legendTitle()).includes("2021-07-15 vs 2021-07-01"),
  `the title names both times (${await legendTitle()})`,
);
const tTime = page.locator("input[aria-label='Time of T (ISO 8601)']");
check(
  (await tTime.count()) === 1 && (await tTime.inputValue()) === "2021-07-01",
  "T's own time is in the side panel",
);
await tTime.fill("2021-06-01");
await tTime.press("Enter");
await settle();
check((await legendTitle()).includes("vs 2021-06-01"), "and can be changed");

check(errors.length === 0, `no console errors${errors.length ? `: ${errors.slice(0, 3).join(" | ")}` : ""}`);
await browser.close();
process.exit(failed ? 1 : 0);
