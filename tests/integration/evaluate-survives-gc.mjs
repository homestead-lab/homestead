// A page.evaluate answer survives a garbage collection. Before Chromium 153
// (Playwright 1.63), DevTools held the promise it waited for only weakly: a
// collection after the promise settled, but before DevTools' own callback ran,
// lost the answer, and Playwright 1.55 reported "Execution context was
// destroyed, most likely because of a navigation" with no navigation at all.
// Busy pages hit it now and then - after viewNodes(), viewWorkloads() or
// wlEdit() - so any check could fail at random. Each case forces that
// collection with gc(); on an affected browser every one of them fails.
import assert from "node:assert/strict";
import { chromium } from "playwright";

const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";
const browser = await chromium.launch({ headless: true, args: ["--js-flags=--expose-gc"] });
try {
  const page = await browser.newPage();
  await page.goto(`${base}/?demo=1`);
  await page.locator(".node-compact-grid").waitFor();
  for (let i = 0; i < 10; i++) {
    assert.equal(await page.evaluate(() => {
      const answer = new Promise(resolve => setTimeout(() => resolve("answered"), 0));
      // Queued behind Playwright's own await on the answer.
      queueMicrotask(() => answer.then(() => { gc(); gc(); }));
      return answer;
    }), "answered", "a function's answer survives a collection after it settles");
    assert.equal(await page.evaluate(`(() => {
      const answer = new Promise(resolve => setTimeout(() => resolve(7), 0));
      queueMicrotask(() => answer.then(() => { gc(); gc(); }));
      return answer;
    })()`), 7, "a string expression's answer survives it too");
  }
  // The app's own view, as the checks await it (the app's globals, as in every check on the demo).
  await page.evaluate(() => { const view = viewNodes(); queueMicrotask(() => view.then(() => { gc(); gc(); })); return view; });
  assert.ok(await page.evaluate(() => STATE.data.nodes.length > 0));
  console.log(`page.evaluate answers survive a garbage collection (Chromium ${browser.version()})`);
} finally {
  await browser.close();
}
