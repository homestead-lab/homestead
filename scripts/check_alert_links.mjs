// Demo-only browser check: the links alerts carry open their dialog straight
// away - an image update's list, and the "host is back" alert's Balance hosts
// on its containers tab with the demo's suggested moves (#1020).
import assert from "node:assert/strict";
import { chromium } from "playwright";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));

  await page.goto(`${base}/containers?panel=rebalance&demo=1`, { waitUntil: "networkidle" });
  await page.locator("#modal:not(.hidden) #mtitle").filter({ hasText: "Balance hosts" }).waitFor();
  const tab = page.locator('#modal [role="tab"][aria-selected="true"]');
  assert.equal((await tab.innerText()).trim().split("\n")[0], "Containers");
  const moves = page.locator("#balContainers");
  await moves.filter({ hasText: "frigate" }).waitFor();
  assert.match(await moves.innerText(), /home-assistant/);
  await page.evaluate(() => closeModal());

  await page.goto(`${base}/containers?panel=updates&demo=1`, { waitUntil: "networkidle" });
  await page.locator("#modal:not(.hidden) #mtitle").filter({ hasText: "Image updates" }).waitFor();
  assert.deepEqual(errors, []);
  console.log("Alert links: panel=rebalance opens Balance hosts on Containers with the demo moves; panel=updates opens Image updates");
} finally {
  await browser.close();
}
