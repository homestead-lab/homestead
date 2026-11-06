// Demo-only browser check: the links alerts carry open their dialog straight
// away - an image update's list, a log alert's list, and the "host is back"
// alert's Balance hosts on its containers tab with the demo's suggested moves
// (#1020). A viewer sees that plan read-only: no click, Space or Enter on a
// switch or tick changes it (#1084).
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

  // As a viewer: every switch and tick is disabled, and nothing a person does
  // to one changes the plan. A disabled checkbox must take no click, no click
  // on its label and no key: only a real browser shows that.
  await page.evaluate(() => { ROLE = "viewer"; paintWho(); return balanceHosts(); });
  await page.locator("#modal:not(.hidden) #mtitle").filter({ hasText: "Balance hosts" }).waitFor();
  await moves.filter({ hasText: "frigate" }).waitFor();
  await page.locator("#balVolumes #balFollow").waitFor({ state: "attached" });
  await page.locator("#balReview .ui-facts, #balReview .ui-lead").first().waitFor({ state: "attached" });
  const line = page.locator("#modal").getByText("Operators can change and apply this plan.", { exact: true });
  assert.equal(await line.count(), 1, "the viewer's line shows once");
  const state = () => page.evaluate(() => JSON.stringify({
    ticks: [...document.querySelectorAll("#modal input[type=checkbox]")].map(i => [i.id, i.checked, i.disabled]),
    plan: ["containers", "volumes"].map(k => [BALANCE[k].on, [...BALANCE[k].exclude], BALANCE[k].follow, !!BALANCE[k].plan]),
  }));
  const before = await state();
  const boxes = JSON.parse(before).ticks;
  assert.ok(boxes.length >= 5, `switches and ticks drawn: ${boxes.length}`);
  assert.ok(boxes.every(([, , disabled]) => disabled), `all disabled for a viewer: ${before}`);
  for (const [pane, tab] of [["#balContainers", "containers"], ["#balVolumes", "volumes"], ["#balReview", "review"]]) {
    await page.evaluate(key => UI.selectSection("balance", key), tab);
    const inputs = page.locator(`${pane} input[type=checkbox]`);
    for (let i = 0; i < await inputs.count(); i++) {
      const input = inputs.nth(i);
      await input.click({ force: true });
      await input.locator("xpath=ancestor::label[1]").click({ force: true, position: { x: 4, y: 4 } });
      await input.focus();
      await page.keyboard.press("Space");
      await page.keyboard.press("Enter");
      assert.equal(await input.evaluate(el => el === document.activeElement), false, "a disabled tick takes no focus");
    }
    await page.waitForTimeout(150);
    assert.equal(await state(), before, `nothing changed in ${tab}`);
    if (tab === "review") {
      // The footer's Start balancing shows on the last step only, so this is where its absence means something.
      assert.equal(await page.locator("#modal [data-finish]").evaluate(el => el.hidden), false, "on the last step");
      assert.equal(await page.locator("#balanceGo").isVisible(), false, "a viewer has no Start balancing");
    }
  }
  assert.equal(await page.locator("#balVolumes label.switch").first().evaluate(el => getComputedStyle(el).cursor), "not-allowed",
    "a disabled switch does not look clickable");
  await page.evaluate(() => { closeModal(); ROLE = "admin"; paintWho(); });

  await page.goto(`${base}/containers?panel=logalerts&demo=1`, { waitUntil: "networkidle" });
  await page.locator("#modal:not(.hidden) #mtitle").filter({ hasText: "Log alerts" }).waitFor();
  await page.locator("#la_list").filter({ hasText: "Front door camera errors" }).waitFor();
  await page.evaluate(() => closeModal());

  await page.goto(`${base}/containers?panel=updates&demo=1`, { waitUntil: "networkidle" });
  await page.locator("#modal:not(.hidden) #mtitle").filter({ hasText: "Image updates" }).waitFor();
  assert.deepEqual(errors, []);
  console.log("Alert links: panel=rebalance opens Balance hosts on Containers with the demo moves, read-only for a viewer; panel=logalerts opens Log alerts; panel=updates opens Image updates");
} finally {
  await browser.close();
}
