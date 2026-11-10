// Move and Copy dialogs while a start is being sent (#1317), in a real browser: a start refused after its dialog was closed
// reads no plan, and a waiting line left under a child dialog is replaced by the plan when that dialog closes.
import assert from "node:assert/strict";
import { chromium } from "playwright";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";
const browser = await chromium.launch({ headless: true });
try {
  const open = async () => {
    const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
    const errors = []; page.on("pageerror", e => errors.push(e.message));
    await page.goto(`${base}/?demo=1`, { waitUntil: "networkidle" });
    await page.evaluate(() => {
      window.__loopTimer?.stop(); window.__plans = 0; window.__release = null;
      const real = window.api;
      window.api = (path, options) => {
        if (path === "/api/move/plan") { window.__plans++; return Promise.resolve({ ok: true, storage_class: "fast", storage_classes: ["fast"], claims: [], total_gb: 0, joined: true, will_run: true }); }
        if (path === "/api/move/start") return new Promise((resolve, reject) => { window.__release = { resolve, reject }; });   // a send that stays open
        return real(path, options);
      };
      window.ask = async () => true;
      moveReview("dr-b", "container", "web");
    });
    await page.waitForFunction(() => /Ready to move/.test(document.querySelector("#mv_plan")?.textContent || ""));
    await page.evaluate(() => { window.__sending = moveStart("dr-b", "container", "web"); });
    await page.waitForFunction(() => window.__release);
    return { page, errors };
  };
  const refuse = page => page.evaluate(async () => { window.__release.reject(new Error("refused")); await window.__sending; });

  let { page, errors } = await open();
  await page.evaluate(() => closeModal());
  await refuse(page);      // the refused start has settled, and any plan request it made has been sent
  assert.equal(await page.evaluate(() => window.__plans), 1, "a refusal after the dialog was closed reads no plan");
  assert.deepEqual(errors, []);

  ({ page, errors } = await open());
  await page.evaluate(() => movePlan("dr-b", "container", "web"));
  assert.match(await page.locator("#mv_plan").innerText(), /Waiting while the transfer is sent/);
  await page.evaluate(() => childModal("Backup storage · dr-b", "<div>child</div>"));
  await refuse(page);
  await page.evaluate(() => modalBack());
  await page.waitForFunction(() => /Ready to move/.test(document.querySelector("#mv_plan")?.textContent || ""));
  assert.doesNotMatch(await page.locator("#mv_plan").innerText(), /Waiting/);
  assert.deepEqual(errors, []);
  console.log("Move dialog: a refused start leaves no stale plan request or waiting line");
} finally { await browser.close(); }
