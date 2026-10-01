// Check default public-demo health and retain incident UI coverage in CI.
import { chromium } from "playwright";
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";
await mkdir("release-assets/scenarios", { recursive: true });
const browser = await chromium.launch({ headless: true });
try {
  for (const width of [1440, 390]) for (const scenario of ["healthy", "incidents", "critical"]) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(`${base}/?demo=1${scenario === "healthy" ? "" : `&demo-scenario=${scenario}`}`, { waitUntil: "networkidle" });
    await page.locator("#views .phead").waitFor();
    await page.waitForFunction(() => document.querySelector("#healthPill")?.textContent?.trim());
    const expected = scenario === "incidents" ? "DEGRADED" : scenario.toUpperCase();
    assert.equal((await page.locator("#healthPill").innerText()).trim(), expected);
    assert.equal(await page.locator("#views .clusteralert").count(), scenario === "healthy" ? 0 : 1);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: `release-assets/scenarios/${scenario}-${width}.png`, fullPage: true });
    assert.deepEqual(errors, []);
    console.log(`Demo ${scenario} passed at ${width}px`);
    await page.close();
  }
} finally { await browser.close(); }
