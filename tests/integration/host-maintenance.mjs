// Maintenance a person declares, walked through in the demo (#535 part 2):
// review, chip on the card, Extend, End, chip gone.
import assert from "node:assert/strict";
import {mkdir} from "node:fs/promises";
import {chromium} from "playwright";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4176";
const shots = process.env.HOST_MAINTENANCE_SCREENSHOTS;
const browser = await chromium.launch({headless: true});
const HOST = "harvester-node1";
if (shots) await mkdir(shots, {recursive: true});

try {
  // Hosts as cards and as rows: both show a host's status.
  for (const [label, width, layout] of [["desktop", 1440, "cards"], ["phone", 390, "cards"], ["rows", 1440, "rows"]]) {
    const context = await browser.newContext({viewport: {width, height: 1000}});
    const page = await context.newPage(), errors = [];
    await page.addInitScript(chosen => localStorage.setItem("homestead.layout.nodes", chosen), layout);
    page.on("pageerror", e => errors.push(e.message));
    // After the fade-ins: a dialog or a card caught mid-way shows nothing.
    const shot = async name => { if (shots) { await page.waitForTimeout(700); await page.screenshot({path: `${shots}/${name}-${label}.png`}); } };
    await page.goto(`${base}/nodes?demo=1`);
    await page.waitForSelector(layout === "cards" ? ".nodecard" : ".comparison-table");
    await page.evaluate(() => clearInterval(window.__loopTimer));
    assert.equal(await page.locator(".ui-chip", {hasText: /^in maintenance$/i}).count(), 0, "not yet");

    // Host actions offers it.
    await page.evaluate(host => nodeActions(host), HOST);
    await page.waitForSelector('button[onclick*="hostMaintenanceReview"]');
    assert.equal(await page.locator('button[onclick*="hostMaintenanceExtend"]').count(), 0);
    await page.locator('button[onclick*="hostMaintenanceReview"]').click();
    await page.waitForSelector("#mm_reason");
    const go = page.locator("#mm_execute");
    assert.equal(await go.isDisabled(), true, "nothing typed yet");
    await shot("review");

    // A reason with markup, the name, and each thing to acknowledge.
    await page.fill("#mm_reason", "<img src=x onerror=window.__hit=1> swap");
    await page.selectOption("#mm_hours", "2");
    assert.equal(await go.isDisabled(), true, "the name isn't typed");
    await page.fill("#mm_confirm", HOST);
    for (const box of await page.locator("[data-mm-ack]").all()) {
      if (!(await box.isVisible())) await page.evaluate(() => UI.selectSection("hostMaintenance", "drain"));
      await box.check();
    }
    await page.evaluate(() => UI.selectSection("hostMaintenance", "summary"));
    assert.equal(await go.isDisabled(), false);
    await go.click();
    await page.waitForFunction(() => /In maintenance/.test(document.querySelector("#mmProgress")?.innerText || ""), null, {timeout: 15000});
    await shot("started");
    await page.evaluate(() => closeModal());

    // The card and the page say so, as text.
    const hostsPage = () => page.evaluate(async () => { noteNodeUptime(await api("/api/nodes/uptime")); await go("nodes"); });
    await hostsPage();
    await page.waitForFunction(() => [...document.querySelectorAll(".ui-chip")].some(c => /^in maintenance$/i.test(c.innerText.trim())), null, {timeout: 10000});
    assert.equal(await page.evaluate(() => window.__hit), undefined, "markup in a reason never runs");
    if (layout === "cards") {
      // In maintenance says more than cordoned, and the head of a card has room for one of them.
      const chipsOnCard = await page.locator(".nodecard", {hasText: HOST}).first().locator(".nodehead-acts .ui-chip").allInnerTexts();
      assert.ok(chipsOnCard.some(t => /in maintenance/i.test(t)), chipsOnCard.join());
      assert.ok(!chipsOnCard.some(t => /cordoned/i.test(t)), chipsOnCard.join());
    }
    // The dashboard's Host health says so too, in both its displays.
    assert.match(await page.evaluate(() => dashboardNodes(STATE.data.nodes, "compact")), />in maintenance</);
    assert.match(await page.evaluate(() => dashboardNodes(STATE.data.nodes, "detailed")), />in maintenance</);
    await shot("card");
    const uptime = await page.evaluate(async host => {
      const box = document.createElement("div"); box.id = "nodeUptime"; document.body.append(box);
      await nodeUptimePaint(STATE.data.nodes.find(n => n.name === host));
      const out = {text: box.innerText, images: box.querySelectorAll("img").length}; box.remove();
      return out;
    }, HOST);
    assert.match(uptime.text, /In maintenance: <img src=x onerror=window.__hit=1> swap · (1h 5\dm|2h) left/);
    assert.equal(uptime.images, 0);

    // In maintenance: Extend and End instead, and the power actions stay.
    await page.evaluate(host => nodeActions(host), HOST);
    await page.waitForSelector('button[onclick*="hostMaintenanceExtend"]');
    assert.equal(await page.locator('button[onclick*="hostMaintenanceReview"]').count(), 0);
    assert.equal(await page.locator('button[onclick*="nodePowerReview"]').count(), 2, "Restart host and Shut down host are still available");
    await shot("actions");
    await page.locator('button[onclick*="hostMaintenanceExtend"]').click();
    await page.selectOption("#mm_extend_hours", "8");
    await page.locator("#mm_extend_go").click();
    await page.waitForFunction(host => api(`/api/node/maintenance?node=${host}`).then(r => r.declared?.left_s > 7 * 3600), HOST);
    await page.evaluate(() => closeModal());

    // End: chip gone.
    await page.evaluate(host => hostMaintenanceEnd(host), HOST);
    assert.equal(await page.locator("#mm_end_go").isDisabled(), true);
    await page.fill("#mm_end_confirm", HOST);
    await page.locator("#mm_end_go").click();
    await page.waitForFunction(host => api(`/api/node/maintenance?node=${host}`).then(r => r.declared === null), HOST);
    await hostsPage();
    await page.waitForFunction(() => ![...document.querySelectorAll(".ui-chip")].some(c => /^in maintenance$/i.test(c.innerText.trim())), null, {timeout: 10000});
    assert.deepEqual(errors, []);
    await context.close();
  }
  console.log("host maintenance: ok");
} finally {
  await browser.close();
}
