// The Longhorn V2 upgrade dialog (#909), in a real browser: after the settings review and Back (twice, as the ‹ button does), the upgrade
// dialog is as it was, and its Start still sends - once. Also that a double click on Start sends once.
import assert from "node:assert/strict";
import { chromium } from "playwright";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";
const browser = await chromium.launch({ headless: true });
try {
  const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.goto(`${base}/?demo=1`, { waitUntil: "networkidle" });
  await page.evaluate(() => {
    window.__loopTimer?.stop();
    window.__starts = 0; const real = window.api;
    window.api = (path, options) => { if (path === "/api/cluster/components/upgrade") window.__starts++; return real(path, options); };
    window.__demoV2UpgradeState = "ready"; lhV2Upgrade("v1.13.0");
  });
  await page.locator("#lhV2UpgradeBackup").waitFor();
  await page.locator("#lhV2UpgradeBackup").check();
  await page.evaluate(async () => { lhV2UpgradeSettings(true); await lhV2UpgradeSettingsReview(true); });
  await page.locator("#lhV2SettingsAck").waitFor();
  await page.evaluate(() => { modalBack(); modalBack(); });
  await page.locator("#lhV2UpgradeBody").waitFor();
  assert.equal(await page.locator("#lhV2UpgradeBackup").isChecked(), true, "the tick was kept");
  await page.locator('button[onclick="lhV2UpgradeStart()"]').dblclick();
  await page.waitForTimeout(300);
  assert.equal(await page.evaluate(() => window.__starts), 1, "Start sent once");
  assert.deepEqual(errors, []);
  console.log("Longhorn V2 upgrade: Start still sends after the settings review and Back, once");
} finally { await browser.close(); }
