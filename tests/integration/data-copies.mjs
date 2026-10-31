// Copies of Homestead's data (#731 item 3), on the demo: the default demo's
// data keeps two copies and says nothing; a one-copy cluster that can keep
// two offers Keep two copies, which opens the move with longhorn-r2 picked;
// a read that failed is said with its reason on the card and in Cluster
// Health (#620).
import assert from "node:assert/strict";
import { chromium } from "playwright";
import { openAuditPage } from "../../scripts/audit_ready.mjs";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";
const browser = await chromium.launch({ headless: true });

async function open(flag) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } }), errors = [];
  page.on("pageerror", e => errors.push(e.message));
  if (flag !== undefined) await page.addInitScript(f => { window.__demoDataCopies = f; }, flag);
  await openAuditPage(page, `${base}/settings?demo=1`);
  await page.evaluate(() => { settingsTab("about"); go("settings"); });
  const card = page.locator("#replicaCard");
  await card.getByText("Its data:").waitFor();
  return { page, card, errors };
}

try {
  let { page, card, errors } = await open();
  assert.equal(await card.getByText("Homestead's data has one copy").count(), 0, "two copies: nothing to say");
  assert.equal(await card.getByText("Could not read Homestead's data copies").count(), 0);
  assert.deepEqual(errors, []);
  await page.close();

  ({ page, card, errors } = await open(true));
  await card.getByText("Homestead's data has one copy; this cluster can now keep two.").waitFor();
  await card.getByRole("button", { name: "Keep two copies" }).click();
  await page.locator("#selfDataClass").waitFor();
  assert.equal(await page.locator("#selfDataClass").inputValue(), "longhorn-r2", "the move starts with longhorn-r2 picked");
  assert.equal((await page.locator("#mtitle").innerText()).trim(), "Move Homestead data");
  assert.deepEqual(errors, []);
  await page.close();

  ({ page, card, errors } = await open("unknown"));
  await card.getByText("Could not read Homestead's data copies: Longhorn's nodes could not be read").waitFor();
  assert.equal(await card.getByRole("button", { name: "Keep two copies" }).count(), 0);
  await page.evaluate(() => HealthInsights.open("health"));
  await page.locator(".ui-insight-list").getByText("Could not read Homestead's data copies").first().waitFor();
  assert.deepEqual(errors, []);
  await page.close();
  console.log("data copies: default, offer and unreadable checked");
} finally {
  await browser.close();
}
