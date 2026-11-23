// An app's logo is a reference the page fetches (#1211), so every place that draws an app must have it
// loaded, not only the pages paint() draws. Real Chromium on the demo: Frigate's logo answers (an SVG).
// The Containers list, its details panel (drawn by renderDetails, not paint), and a host's "If it goes
// down" review (a dialog body written in place) each show the picture, not the initials, and still do
// after two refreshes.
import assert from "node:assert/strict";
import { chromium } from "playwright";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";
const browser = await chromium.launch({ headless: true });
try {
  const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.goto(`${base}/containers?demo=1`, { waitUntil: "networkidle" });
  await page.locator(".wl-row, .wcard").first().waitFor();
  await page.evaluate(() => { window.__loopTimer?.stop(); });
  // Each Frigate avatar in a part of the page: an <img> that has decoded, with its initials hidden.
  const shown = where => page.evaluate(where => [...document.querySelectorAll(`${where} .appav`)]
    .filter(av => (av.closest(".wl-row, .wcard, .impact-row, .details-head, .details-panel") || av.parentElement)?.textContent.includes("frigate"))
    .map(av => { const img = av.querySelector("img"); return { img: !!img && img.complete && img.naturalWidth > 0, initials: av.querySelector(".avfallback")?.hidden === false }; }), where);
  const pictured = async (where, what) => {
    await page.waitForFunction(where => [...document.querySelectorAll(`${where} .appav img`)]
      .some(img => img.complete && img.naturalWidth > 0 && img.previousElementSibling?.hidden), where, { timeout: 5000 }).catch(() => {});
    const avatars = await shown(where);
    assert.ok(avatars.length && avatars.every(a => a.img && !a.initials), `${what} shows Frigate's logo: ${JSON.stringify(avatars)}`);
  };
  await pictured("#views", "the Containers list");

  await page.locator(`#views .wl-open[aria-label="Open details for frigate"]`).first().click();
  const panel = ".details-panel";
  await page.locator(`${panel} .appav`).first().waitFor();
  await pictured(panel, "the details panel");
  await page.evaluate(async () => { await refresh(true); await refresh(true); });
  await pictured(panel, "the details panel after two refreshes");
  await page.evaluate(() => window.closeDetails?.());

  await page.evaluate(() => nodeIfDown("harvester-node2"));
  await page.locator("#mbody .impact-row").first().waitFor();
  await pictured("#mbody", "the If it goes down review");
  await page.evaluate(async () => { await refresh(true); await refresh(true); });
  await pictured("#mbody", "the If it goes down review after two refreshes");
  assert.deepEqual(errors, [], "no page errors");
  console.log("logos shown: Containers list, details panel, If it goes down review, each after two refreshes");
} finally {
  await browser.close();
}
