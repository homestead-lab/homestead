// Cluster Health keeps its last results while it checks again, and a tooltip
// does not stay on screen once its element is gone (#834).
//   CHECK_BROWSER=firefox runs it in Firefox, which sends no focusout for a
//   removed element. Chromium does, so there the check holds back focusout
//   from a removed element to stand in for it (CI installs Chromium only).
import assert from "node:assert/strict";
import {chromium, firefox} from "playwright";
import {mkdir} from "node:fs/promises";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";
const out = process.env.HEALTH_TOOLTIP_SCREENSHOTS || "";
const kind = process.env.CHECK_BROWSER === "firefox" ? "firefox" : "chromium";
if (out) await mkdir(out, {recursive: true});
const browser = await (kind === "firefox" ? firefox : chromium).launch({headless: true});
const watchdog = setTimeout(() => { console.error("health-keeps-results: no result after 180 s"); process.exit(1); }, 180000);
const shot = async (page, name) => { if (out) await page.screenshot({path: `${out}/${name}.png`}); };

// The shared tooltip as the reader sees it, and whether its element is still on the page.
const tip = page => page.evaluate(() => {
  const bubble = document.querySelector("#uiTooltip");
  return {visible: !!bubble && !bubble.hidden, text: bubble?.textContent || "",
    ownerGone: typeof tooltipOwner !== "undefined" && !!tooltipOwner && !tooltipOwner.isConnected};
});

try {
  const context = await browser.newContext({viewport: {width: 1440, height: 900}});
  await context.addInitScript(() => localStorage.setItem("homestead.settings", JSON.stringify({motion: "off", refresh: 5})));
  const page = await context.newPage(), errors = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.goto(`${base}/?demo=1&demo-scenario=incidents`);
  await page.waitForSelector(".dashboard-widget");
  // Only the refreshes this check asks for. Every read takes a second, as on a
  // real cluster; the clock can be moved on, so the 30 s Health cache runs out.
  await page.evaluate(() => {
    window.__loopTimer?.stop(); window.stopAlertChecks?.();
    const answer = window.fetch, now = Date.now.bind(Date);
    window.__skew = 0; Date.now = () => now() + window.__skew;
    window.fetch = async (input, init) => {
      if (/\/api\//.test(typeof input === "string" ? input : input.url)) await new Promise(r => setTimeout(r, 1000));
      return answer(input, init);
    };
  });
  // (b) The Dashboard's Host health: a host's name opens its page, and the
  // tooltip of the name it clicked goes with the old page.
  // The button by its place: showing the tooltip moves its title to data-tip.
  const hostName = page.locator(".node-compact-heading button").first();
  await hostName.waitFor();
  const name = (await hostName.textContent()).trim();
  if (kind === "chromium") await page.evaluate(() => {
    // Firefox sends no focusout when the focused name is removed: nor does this page now.
    window.__holdFocusout = e => { if (e.target.closest?.(".node-compact-heading")) e.stopImmediatePropagation(); };
    window.addEventListener("focusout", window.__holdFocusout, true);
  });
  await hostName.hover();
  assert.equal((await tip(page)).text, name, "hovering the host's name shows its tooltip");
  await hostName.click();
  await page.waitForFunction(n => STATE.view === "nodes" && !document.querySelector(".node-compact-heading")
    && document.querySelector("#views .phead")?.textContent.includes(n), name, {timeout: 30000});
  await page.waitForTimeout(300);
  await shot(page, `${kind}-host-name-clicked`);
  let state = await tip(page);
  assert.ok(!state.visible, `the host's page opens with no tooltip left from the dashboard: ${JSON.stringify(state)}`);
  await page.evaluate(() => refresh(true));
  state = await tip(page);
  assert.ok(!state.visible, `nor after a refresh: ${JSON.stringify(state)}`);
  await page.evaluate(() => window.__holdFocusout && window.removeEventListener("focusout", window.__holdFocusout, true));

  // (a) Cluster Health: the first results, then refreshes with the cache run out.
  await page.evaluate(() => go("cluster"));
  await page.waitForSelector('[data-insight="full"] .insight-summary', {timeout: 30000});
  await page.evaluate(() => {
    window.__placeholder = 0;
    // After every change to the page: is the card showing the placeholder now?
    new MutationObserver(() => {
      if (/Loading checks/.test(document.querySelector('[data-insight="full"]')?.textContent || "")) window.__placeholder++;
    }).observe(document.querySelector("#views"), {childList: true, subtree: true, characterData: true});
    window.__row = document.querySelector('[data-insight="full"] .ui-insight-row');
  });
  assert.ok(await page.evaluate(() => !!window.__row), "the incidents demo has Health rows");
  // Unchanged results: the refresh keeps the rows as they are.
  await page.evaluate(() => refresh(true));
  assert.ok(await page.evaluate(() => window.__row === document.querySelector('[data-insight="full"] .ui-insight-row')),
    "a refresh with unchanged results keeps the same row elements");
  const texts = [];
  for (let i = 0; i < 2; i++) {
    await page.evaluate(() => { window.__skew += 31000; });
    const done = page.evaluate(() => refresh(true));
    for (let t = 0; t < 12; t++) {
      texts.push(await page.evaluate(() => document.querySelector('[data-insight="full"]')?.textContent.trim().slice(0, 40) || "(gone)"));
      // Half way: the page has painted, and Health's own reads are still out.
      if (t === 6 && i === 0) await shot(page, `${kind}-health-rechecking`);
      await page.waitForTimeout(250);
    }
    await done;
    await page.waitForFunction(() => !document.querySelector("#views.refreshing"));
  }
  const seen = await page.evaluate(() => window.__placeholder);
  assert.equal(seen, 0, `the Health card never went back to "Loading checks…" (${seen} times)`);
  assert.ok(texts.every(t => !/Loading checks/.test(t)), `sampled while checking again: ${JSON.stringify(texts)}`);

  // (b) A refresh that rebuilds the element under a resting pointer hides its tooltip.
  const ack = page.locator('[data-insight="full"] [data-tip]').first();
  await ack.hover();
  assert.ok((await tip(page)).visible, "hovering Acknowledge shows its tooltip");
  await page.evaluate(() => refresh(true));
  state = await tip(page);
  assert.ok(!state.ownerGone, `an unchanged refresh keeps the button, so its tooltip may stay: ${JSON.stringify(state)}`);
  // Another account's view, say: Health starts over, and the rows are rebuilt.
  // Checked as soon as refresh() resolves, before Health's own reads answer:
  // paint() itself must drop the tooltip, with no pointer event and no wait.
  await page.evaluate(() => { HealthInsights.reset(); return refresh(true); });
  await shot(page, `${kind}-health-rebuilt-under-pointer`);
  state = await tip(page);
  assert.ok(!state.visible, `the tooltip of a rebuilt button is gone by the end of the refresh: ${JSON.stringify(state)}`);
  assert.deepEqual(errors, []);
  console.log(`health-keeps-results (${kind}): ok`);
} finally {
  clearTimeout(watchdog);
  await browser.close();
}
