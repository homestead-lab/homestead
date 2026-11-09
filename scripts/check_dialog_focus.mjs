// Demo-only browser check (#486): a dialog takes focus when it opens, keeps
// Tab inside it, gives focus back when it closes, and its ✕ is a named button.
import assert from "node:assert/strict";
import { chromium } from "playwright";
const base = process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(`${base}/networking?demo=1&tab=vips`, { waitUntil: "networkidle" });
  const inDialog = () => page.evaluate(() => !!document.activeElement?.closest(".modalbox"));

  // Open a dialog from a button the keyboard reached.
  const opener = page.getByRole("button", { name: /Add virtual IP/ }).first();
  await opener.focus();
  const openerId = await opener.evaluate(el => { el.dataset.focusCheck = "opener"; return el.outerHTML.slice(0, 60); });
  await opener.press("Enter");
  await page.locator('#modal:not(.hidden) [role="dialog"][aria-modal="true"][aria-labelledby~="mtitle"]').waitFor();
  assert.equal(await inDialog(), true, `focus moves into the dialog opened by ${openerId}`);

  // Tab and Shift+Tab stay inside.
  for (let i = 0; i < 25; i++) { await page.keyboard.press("Tab"); assert.equal(await inDialog(), true, `Tab ${i + 1} stays in the dialog`); }
  for (let i = 0; i < 25; i++) { await page.keyboard.press("Shift+Tab"); assert.equal(await inDialog(), true, `Shift+Tab ${i + 1} stays in the dialog`); }

  // The ✕ is a button named Close, and closing gives focus back.
  const close = page.getByRole("button", { name: "Close", exact: true }).first();
  assert.equal(await close.evaluate(el => el.tagName), "BUTTON");
  await page.keyboard.press("Escape");
  await page.locator("#modal.hidden").waitFor({ state: "attached" });
  assert.equal(await page.evaluate(() => document.activeElement?.dataset?.focusCheck), "opener", "focus returns to the opener");

  // Toasts are announced; an error stays until it is dismissed or the
  // person acts again, and never takes a click meant for what is under it.
  assert.equal(await page.locator("#toast").getAttribute("aria-live"), "polite");
  await page.evaluate(() => toast("Could not save: timed out", "bad"));
  await page.waitForTimeout(4600);
  const error = page.locator('#toast [role="alert"]');
  assert.equal(await error.count(), 1, "the error is still there after the usual 4.2 s");
  await error.getByRole("button", { name: "Dismiss" }).click();
  assert.equal(await error.count(), 0);
  await page.evaluate(() => toast("Could not save: timed out", "bad"));
  const text = await error.locator("span").boundingBox();
  assert.equal(await page.evaluate(({ x, y }) => !!document.elementFromPoint(x, y)?.closest("#toast"), { x: text.x + text.width / 2, y: text.y + text.height / 2 }), false,
    "a click on the toast's text reaches what is under it");
  await page.mouse.click(10, 300);
  assert.equal(await error.count(), 0, "the next click elsewhere clears the error");
  assert.deepEqual(errors, []);

  // ask() and askText() fit their box: the button bar reached 4px past each
  // side of it, so every confirm had a horizontal scrollbar (#833).
  for (const [width, height] of [[1280, 900], [390, 844]]) {
    const sized = await browser.newPage({ viewport: { width, height } });
    const sizedErrors = [];
    sized.on("pageerror", error => sizedErrors.push(error.message));
    await sized.goto(`${base}/?demo=1`, { waitUntil: "networkidle" });
    for (const open of ['ask("Discard your changes to Add a host?", { title: "Unsaved changes", ok: "Discard changes", cancel: "Keep editing", danger: true })',
      'askText("Name the snapshot", "nightly", { title: "Take a snapshot" })']) {
      await sized.evaluate(`void ${open}`);   // not awaited: ask() resolves only once answered
      const box = sized.locator(".askbox");
      await box.waitFor();
      const { scroll, client } = await box.evaluate(el => ({ scroll: el.scrollWidth, client: el.clientWidth }));
      assert.ok(scroll <= client, `${open.split("(")[0]}() at ${width}px scrolls sideways: ${scroll} > ${client}`);
      await sized.keyboard.press("Escape");
      await box.waitFor({ state: "detached" });
    }
    assert.deepEqual(sizedErrors, [], `no page errors at ${width}px`);
    await sized.close();
  }
  console.log("Dialog focus: moves in, Tab stays inside, returns to the opener; Close is a button; error toasts stay until dismissed or the next action, and let clicks through; ask() fits its box at desktop and phone width");
} finally { await browser.close(); }
