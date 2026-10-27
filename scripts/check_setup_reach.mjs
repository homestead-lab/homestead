// Sign-in/setup stays reachable at any phone size, held upright or sideways:
// the setup form opens, settles, and its button scrolls into view.
// Used by check_mobile_pwa.mjs and tests/integration/setup-reach-late-focus.mjs.
import assert from "node:assert/strict";

export async function checkSetupReach(page, viewport) {
  await page.evaluate(() => loginForm(null, true));
  await page.locator("#lg_go").waitFor();
  // Upright on a small phone the whole setup form fits: nothing to scroll
  // while it is still settling into place (#737 made it taller).
  if (viewport.height >= 640) assert.equal(await page.locator("#gate").evaluate(el => el.scrollHeight <= el.clientHeight), true, "the setup form fits the screen");
  // The form focuses its first field 60 ms after it opens (loginForm in
  // auth.js), and when the form is taller than the screen that scrolls it back
  // up to that field. Let that happen first: on a busy runner it could otherwise land
  // between the scroll below and the measurement, and carry the button away.
  await page.waitForFunction(() => document.querySelector("#gatebox").contains(document.activeElement));
  await page.locator("#lg_go").scrollIntoViewIfNeeded();
  const login = await page.locator("#lg_go").boundingBox();
  assert.ok(login.y >= 0 && login.y + login.height <= viewport.height,
    `the setup button is in view at ${viewport.width}×${viewport.height}: ${JSON.stringify(login)}`);
  assert.equal(await page.locator("#gate").evaluate(el => el.scrollWidth <= el.clientWidth), true);
}
