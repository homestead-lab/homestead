// Check default public-demo health and retain incident UI coverage in CI.
import { chromium } from "playwright";
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";
await mkdir("release-assets/scenarios", { recursive: true });
const browser = await chromium.launch({ headless: true });
try {
  for (const width of [1440, 768, 390]) for (const scenario of ["healthy", "incidents", "critical"]) {
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
    await page.evaluate(() => go("workloads"));
    await page.locator("#views h2").filter({ hasText: /^Containers$/ }).waitFor();
    await page.waitForFunction(() => STATE.data.imageUpdates?.checked_at);
    assert.equal(await page.locator("#views .phead .pill.crit").count(), scenario === "healthy" ? 0 : 1);
    assert.equal(await page.getByRole("img", { name: /^Registry check unavailable:/ }).count(), scenario === "healthy" ? 0 : 1);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: `release-assets/scenarios/${scenario}-containers-${width}.png`, fullPage: true });
    await page.evaluate(() => go("vms"));
    await page.locator("#views h2").filter({ hasText: /^Virtual machines$/ }).waitFor();
    await page.waitForFunction(() => STATE.data.vms?.length);
    assert.equal(await page.locator("#views").getByText("ErrorUnschedulable", { exact: true }).count(), scenario === "healthy" ? 0 : 1);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: `release-assets/scenarios/${scenario}-vms-${width}.png`, fullPage: true });
    await page.evaluate(() => vmOpen("lab", "ubuntu-test"));
    await page.locator("#mbody .vm-facts").waitFor();
    assert.equal(await page.locator("#mbody .note.bad").count(), scenario === "healthy" ? 0 : 1);
    assert.equal(await page.locator("#mbody").getByText("ErrorUnschedulable", { exact: true }).count(), scenario === "healthy" ? 0 : 1);
    if (scenario === "healthy") {
      await page.locator("#mbody").getByText("Stopped", { exact: true }).waitFor();
      await page.locator("#mbody .vm-facts").getByText("4Gi", { exact: true }).waitFor();
    }
    await page.screenshot({ path: `release-assets/scenarios/${scenario}-vm-detail-${width}.png`, fullPage: true });
    await page.evaluate(() => closeModal());
    if (scenario === "healthy") {
      await page.evaluate(() => { go("network"); networkTab("ip"); });
      await page.locator(".ipam-table").waitFor();
      await page.evaluate(() => {
        const rows = STATE.data.ipam.subnets[0].rows;
        rows[0].name = "Conference room display and meeting controller";
        rows[1].unifi = { ...rows[1].unifi, name: "distribution-switch-with-a-long-device-name.example.com" };
        rows[1].name = "";
        renderIpam();
      });
      for (const selector of [".ipam-name b", ".ipam-name .ipam-unifi-name", ".ipam-name .ipam-line.dim"]) {
        assert.equal(await page.locator(selector).evaluateAll(elements => elements.every(el =>
          el.scrollWidth <= el.clientWidth + 1 && getComputedStyle(el).textOverflow !== "ellipsis")), true, `${selector} names remain readable at ${width}px`);
      }
      if (width > 560) assert.equal(await page.locator(".ipam-row").first().evaluate(row =>
        row.querySelector(".ip-name").offsetWidth > row.querySelector(".ip-addr").offsetWidth), true);
      else assert.equal(await page.locator(".ipam-row").evaluateAll(rows => rows.every(row => {
        const name = row.querySelector(".ip-name").getBoundingClientRect();
        const seen = row.querySelector(".ip-seen").getBoundingClientRect();
        return name.right <= seen.left;
      })), true, "names do not overlap status badges on phones");
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.screenshot({ path: `release-assets/scenarios/ipam-${width}.png`, fullPage: true });
    }
    assert.deepEqual(errors, []);
    console.log(`Demo ${scenario} passed at ${width}px`);
    await page.close();
  }
} finally { await browser.close(); }
