// The sideways setup-form check holds when the form's own focus comes late.
// loginForm() focuses the username 60 ms after it opens, and on a short
// installed screen that scrolls the form back to its top. On a busy runner the
// focus could land between check_mobile_pwa.mjs scrolling "Create account"
// into view and measuring it, so the button was off-screen and the check
// failed now and then. Here the focus waits for the form's first scroll (or
// 1 s, as a busy phone might), so it always comes after the check's scroll
// unless the check waits for it first.
import assert from "node:assert/strict";
import { chromium, devices } from "playwright";
import { checkSetupReach } from "../../scripts/check_setup_reach.mjs";

const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";
const viewport = { width: 844, height: 390 };
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ ...devices["Pixel 7"], viewport });
  // Installed: the form scrolls inside #gate (style.css, display-mode standalone).
  await context.addInitScript(() => {
    window.HOMESTEAD_DEMO = true;
    const original = window.matchMedia.bind(window);
    window.matchMedia = query => {
      const result = original(query);
      if (query === "(display-mode: standalone)") Object.defineProperty(result, "matches", { value: true });
      return result;
    };
  });
  const page = await context.newPage(), errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(`${base}/?demo=1`, { waitUntil: "networkidle" });
  await page.locator("#views .phead").waitFor({ state: "attached" });
  assert.equal(await page.evaluate(() => document.documentElement.dataset.display), "standalone");
  await page.evaluate(() => {
    const open = window.loginForm;
    window.loginForm = (...args) => {
      const later = window.setTimeout;
      window.setTimeout = (fn, ms, ...rest) => {
        if (ms !== 60) return later(fn, ms, ...rest);
        let done = false;
        window.__focusRan = false;
        const run = () => { if (!done) { done = true; fn(...rest); window.__focusRan = true; } };
        document.querySelector("#gate").addEventListener("scroll", run, { once: true });
        return later(run, 1000);
      };
      try { return open(...args); } finally { window.setTimeout = later; }
    };
  });
  for (let i = 0; i < 5; i++) {
    await page.evaluate(() => { window.__focusRan = undefined; });
    await checkSetupReach(page, viewport);
    assert.equal(await page.evaluate(() => typeof window.__focusRan), "boolean",
      "loginForm still focuses the username after 60 ms: if that delay changed, change it here too");
    // What the check measured must be the settled form: once the held focus
    // has run, the button is still in view.
    await page.waitForFunction(() => window.__focusRan);
    assert.equal(await page.evaluate(() => document.activeElement?.id), "lg_user", "the form's own focus has happened");
    const settled = await page.locator("#lg_go").evaluate(el => { const b = el.getBoundingClientRect(); return { top: b.top, bottom: b.bottom }; });
    assert.ok(settled.top >= 0 && settled.bottom <= viewport.height, `the setup button stays in view after the form's focus: ${JSON.stringify(settled)}`);
    assert.ok(await page.locator("#gate").evaluate(el => el.scrollHeight > el.clientHeight), "the form is taller than the screen, so it scrolls");
    await page.evaluate(() => ungate());
  }
  assert.deepEqual(errors, []);
  console.log("The sideways setup button stays in view when the form's focus comes late");
  await context.close();
} finally {
  await browser.close();
}
