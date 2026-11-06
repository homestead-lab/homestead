// Demo-only browser check (#906): the page shell is on screen while its scripts
// still load, so a person can tap it before boot. Every /js/ request is held;
// the shell is tapped (the bell, which opens Notifications on a phone) and its
// hidden buttons are clicked; nothing may throw. Then the scripts load, the page
// boots, and the same tap opens the Notifications dialog. In the live demo the
// banner demo.js adds on DOMContentLoaded is there.
import assert from "node:assert/strict";
import { chromium } from "playwright";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";

const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
  // The live demo's flag, as build_demo_site.py injects it, so demo.js adds its banner.
  await context.addInitScript(() => { window.HOMESTEAD_DEMO = true; });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const held = [];
  let release;
  const released = new Promise(resolve => { release = resolve; });
  await page.route(/\/js\/[^/?]+\.js/, async route => { held.push(route.request().url()); await released; await route.continue(); });

  await page.goto(`${base}/?demo=1`, { waitUntil: "commit" });
  const bell = page.locator("#bell > summary");
  await bell.waitFor({ state: "visible", timeout: 30000 });
  assert.ok(held.length > 0, "the scripts are still loading");
  await bell.tap();
  await page.evaluate(() => { for (const id of ["jobClear", "mback"]) document.getElementById(id).click(); });
  await page.waitForTimeout(300);
  assert.deepEqual(errors, [], "a tap before boot throws nothing");
  assert.ok(await page.locator("#modal").evaluate(el => el.classList.contains("hidden")), "no dialog opens before boot");
  await page.evaluate(() => document.getElementById("bell").removeAttribute("open"));

  release();
  await page.waitForFunction(() => document.querySelector("#views")?.children.length > 0, null, { timeout: 60000 });
  await page.waitForLoadState("networkidle");
  assert.equal(await page.locator("#demoBanner").count(), 1, "demo.js's DOMContentLoaded banner is there");
  await bell.tap();
  await page.locator("#modal:not(.hidden) #mtitle", { hasText: "Notifications" }).waitFor({ timeout: 10000 });
  assert.deepEqual(errors, [], "no page errors");
  console.log("Shell before boot passed: a tap before the scripts load does nothing, after boot it opens Notifications");
} finally {
  await browser.close();
}
