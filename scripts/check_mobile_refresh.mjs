import assert from "node:assert/strict";

// Exercise both trusted Android touch events and the cancellation cases.
export async function checkMobileRefresh(page, context, installed) {
  const chromium = context.browser().browserType().name() === "chromium";
  await page.evaluate(() => {
    window.__refreshCalls = 0;
    window.__originalRefreshView = VIEWS.dash[2];
    VIEWS.dash[2] = async () => { window.__refreshCalls++; await window.__originalRefreshView(); };
    clearInterval(window.__loopTimer);
  });
  const calls = () => page.evaluate(() => window.__refreshCalls);
  const idle = () => page.waitForFunction(() => !STATE.busy && !document.querySelector("#refresh").disabled);
  const pull = async (distance = 110, options = {}) => {
    await page.evaluate(({ distance, options }) => {
      const target = options.selector ? document.querySelector(options.selector) : document.querySelector("#views .phead");
      const touch = (y, x = 80, id = 1) => ({ identifier: id, target, clientX: x, clientY: y });
      // Safari supplies Touch objects for real input but does not expose the
      // Touch constructor. A fixture uses the same fields in either engine.
      const dispatch = (type, touches, changedTouches = touches) => {
        const event = new Event(type, { bubbles: true, cancelable: true });
        Object.defineProperties(event, { touches: { value: touches }, targetTouches: { value: touches }, changedTouches: { value: changedTouches } });
        return target.dispatchEvent(event);
      };
      dispatch("touchstart", [touch(180)]);
      dispatch("touchmove", options.multi ? [touch(180 + distance), touch(180 + distance, 100, 2)]
        : [touch(180 + distance, 80 + (options.dx || 0))]);
      if (options.dirtyMidway) STATE.settingsDirty = new Set(["app:site"]);
      if (options.navigateMidway) window.NAV_TOKEN++;
      dispatch(options.cancel ? "touchcancel" : "touchend", [], [touch(180 + distance)]);
    }, { distance, options });
    if (!options.skipIdle) await idle();
  };
  await page.locator("#refresh").click(); await idle();
  assert.equal(await calls(), 1, "the mobile button refreshes the page");
  await pull();
  assert.equal(await calls(), installed ? 2 : 1, "only the installed app handles pull-to-refresh");
  if (!installed) {
    await page.evaluate(() => { VIEWS.dash[2] = window.__originalRefreshView; });
    return;
  }
  let count = await calls();
  for (const options of [{ cancel: true }, { dx: 160 }, { multi: true }, { dirtyMidway: true }, { navigateMidway: true }]) {
    await pull(110, options);
    assert.equal(await calls(), count, `cancelled gesture does not refresh: ${JSON.stringify(options)}`);
    await page.evaluate(() => { STATE.settingsDirty = new Set(); });
  }
  await pull(45); assert.equal(await calls(), count, "a short pull does not refresh");
  await page.locator(".main").evaluate(el => el.scrollTop = 200);
  await pull(); assert.equal(await calls(), count, "pulling while scrolled does not refresh");
  await page.evaluate(() => scrollPageTop());

  for (const state of ["dirty", "modal", "ask", "editor", "terminal", "form", "busy", "drawer"]) {
    await page.evaluate(state => {
      if (state === "dirty") STATE.settingsDirty = new Set(["app:site"]);
      if (state === "busy") STATE.busy = true;
      if (state === "drawer") document.body.classList.add("navopen");
      if (state === "modal") document.querySelector("#modal").classList.remove("hidden");
      if (state === "form") STATE.view = "deploy";
      if (["editor", "terminal", "ask"].includes(state)) {
        const el = document.createElement("div"); el.id = "refreshGuardFixture";
        el.className = { editor: "monaco-editor", terminal: "xterm", ask: "askdlg" }[state];
        el.textContent = "Open editor"; document.querySelector("#views").append(el);
      }
    }, state);
    // Busy is expected to remain set until the existing refresh finishes.
    await page.evaluate(() => window.manualRefresh());
    await page.evaluate(() => STATE.busy = false);
    if (state !== "busy") await pull();
    assert.equal(await calls(), count, `${state} blocks manual and gesture refresh`);
    await page.evaluate(() => {
      STATE.view = "dash"; STATE.settingsDirty = new Set();
      document.querySelector("#modal").classList.add("hidden");
      document.body.classList.remove("navopen"); document.querySelector("#refreshGuardFixture")?.remove();
    });
  }
  await page.evaluate(() => {
    const el = document.createElement("div"); el.id = "refreshNestedFixture";
    el.style.cssText = "height:60px;overflow:auto"; el.innerHTML = '<div style="height:300px">Scrollable list</div>';
    document.querySelector("#views").prepend(el);
  });
  await pull(110, { selector: "#refreshNestedFixture div" });
  assert.equal(await calls(), count, "nested scrolling keeps its gestures");
  await page.locator("#refreshNestedFixture").evaluate(el => el.remove());
  await pull(110, { selector: "#views button" });
  assert.equal(await calls(), count, "controls keep their gestures");

  // These are actual Chromium input events, including its scroll negotiation.
  const client = chromium ? await context.newCDPSession(page) : null;
  await page.evaluate(() => { document.activeElement.blur(); scrollPageTop(); document.querySelector("#toast").replaceChildren(); });
  const top = await page.locator("#views").boundingBox();
  const point = { x: top.x + 6, y: top.y + 6 };
  for (const theme of ["dark", "light"]) {
    await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
    if (client) {
      await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [point] });
      for (const distance of [15, 45, 90, 110]) {
        await client.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: point.x, y: point.y + distance }] });
      }
      await page.locator("#pullRefresh").filter({ hasText: "Release to refresh" }).waitFor();
      await page.screenshot({ path: `release-assets/pages/mobile-pwa/pull-refresh-${theme}.png` });
      await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    } else await pull();
    await idle();
    assert.equal(await calls(), ++count, `${client ? "trusted Android input" : "WebKit gesture fixture"} refreshes once on release`);
    assert.equal(await page.evaluate(() => scrollY), 0, "the surrounding page remains locked");
  }
  await client?.detach();
  await page.evaluate(() => {
    window.__pendingCalls = 0;
    VIEWS.dash[2] = () => { window.__pendingCalls++; return new Promise(resolve => window.__finishRefresh = resolve); };
    void manualRefresh();
  });
  assert.equal(await page.locator("#refresh").isDisabled(), true, "refresh stays disabled while a request is pending");
  await page.evaluate(() => manualRefresh());
  await pull(110, { cancel: true, skipIdle: true });
  assert.equal(await page.evaluate(() => window.__pendingCalls), 1, "a pending request cannot start a second refresh");
  await page.evaluate(() => window.__finishRefresh()); await idle();
  await page.evaluate(() => {
    VIEWS.dash[2] = async () => { throw new Error("Refresh failed for this test"); };
  });
  assert.equal(await page.evaluate(() => manualRefresh()), false, "a failed refresh reports failure");
  assert.equal(await page.locator("#pullRefresh").isVisible(), false, "failure is never announced as updated");
  assert.match(await page.locator("#toast").innerText(), /Refresh failed for this test/);
  await page.evaluate(() => { VIEWS.dash[2] = window.__originalRefreshView; document.querySelector("#toast").replaceChildren(); });
  for (const width of [375, 360]) {
    await page.setViewportSize({ width, height: 839 });
    // The setup guide is another top-bar control that must fit with refresh.
    await page.locator("#setupbtn").evaluate(el => el.classList.remove("hidden"));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
    const refresh = await page.locator("#refresh").boundingBox();
    assert.ok(refresh.x >= 0 && refresh.x + refresh.width <= width);
    await page.screenshot({ path: `release-assets/pages/mobile-pwa/refresh-button-${width}.png` });
  }
  await page.setViewportSize({ width: 412, height: 839 });

  // A rejected discard keeps both the URL and the settings edits intact.
  await page.evaluate(() => { STATE.settingsDirty = new Set(["app:site"]); void reloadApp(); });
  await page.locator('.askdlg [data-a="no"]').click();
  assert.equal(await page.evaluate(() => STATE.settingsDirty.size), 1);
  await page.evaluate(() => STATE.settingsDirty = new Set());
  await page.evaluate(() => { STATE.view = "deploy"; void reloadApp(); });
  await page.locator('.askdlg [data-a="no"]').click();
  assert.equal(await page.evaluate(() => STATE.view), "deploy", "cancelling reload preserves the form page");
  await page.evaluate(() => STATE.view = "dash");
  await page.locator("#bottombar [data-more]").click();
  await Promise.all([page.waitForEvent("load"), page.locator("#reloadApp").click()]);
  await page.locator("#views .phead").waitFor({state:"attached"});
  assert.equal(await page.evaluate(() => document.documentElement.dataset.display), "standalone", "Reload app loads the installed shell again");
  console.log(`${chromium ? "Chromium/Android" : "WebKit/iOS"} refresh button, gesture, guards, failure and reload checks passed`);
}
