// The dialog audit's whole-dialog screenshot draws no backdrop blur. Chromium
// renders blur in software on CI and in containers, and the Edit dialogs nest
// up to eight blurred layers: under load one screenshot outran Playwright's 30 s
// action timeout (#893). The capture paints the page behind the dialog opaque, so
// the blur only had a flat colour to soften. The app's own styles keep their
// blur, and the audit measures with them: the capture style is gone afterwards.
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright";
import { captureDialog, openAuditPage, withCaptureStyle } from "../../scripts/audit_ready.mjs";

const base = (process.env.HOMESTEAD_URL || "http://127.0.0.1:4173") + "/?demo=1&demo-scenario=incidents";
// Every element matching the selector, with its ::before and ::after, whose backdrop-filter is not none.
// The whole page inside the capture; only the dialog when comparing before and after, since toasts and
// the job tray come and go on their own.
const DIALOG = "#modal, #modal *";
const blurred = (selector) => [...document.querySelectorAll(selector)].flatMap((e) => [null, "::before", "::after"]
  .filter((pseudo) => getComputedStyle(e, pseudo).backdropFilter !== "none")
  .map((pseudo) => `${e.tagName.toLowerCase()}${e.id ? `#${e.id}` : ""}${[...e.classList].slice(0, 2).map((c) => `.${c}`).join("")}${pseudo || ""}`));

const browser = await chromium.launch({ headless: true });
const dir = await mkdtemp(join(tmpdir(), "audit-capture-"));
try {
  for (const [label, width, height, mobile] of [["desktop", 1440, 900, false], ["mobile", 390, 844, true]]) {
    const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: mobile ? 2 : 1,
      isMobile: mobile, hasTouch: mobile, colorScheme: "dark" });
    const page = await context.newPage();
    await page.addInitScript(() => localStorage.setItem("homestead.settings",
      JSON.stringify({ theme: "dark", bg: "soft", blur: 26, motion: "off", refresh: 60 })));
    await openAuditPage(page, base);
    // The heaviest dialog: Edit on its placement step, blurred cards inside the blurred box.
    await page.evaluate(() => go("workloads"));
    await page.locator("#views .phead").waitFor({ state: "attached" });
    await page.evaluate(() => wlEdit("lab", "home-assistant"));
    await page.locator("#modal:not(.hidden) #mbody").waitFor();
    await page.evaluate(() => stepGo("e_steps", 4));

    const real = await page.evaluate(blurred, DIALOG);
    assert.ok(real.some((e) => e.startsWith("div#modal")) && real.some((e) => e.startsWith("div.modalbox")),
      `${label}: the app's dialog keeps its blur outside the capture: ${real.join(", ")}`);
    const during = await withCaptureStyle(page, () => page.evaluate(blurred, "*"));
    assert.deepEqual(during, [], `${label}: the audit's screenshot draws no backdrop blur`);
    assert.deepEqual(await page.evaluate(blurred, DIALOG), real, `${label}: the capture style is removed after the screenshot`);

    // What captureDialog itself shows at the moment it takes the screenshot.
    let atShot;
    const locator = page.locator.bind(page);
    page.locator = (selector, options) => {
      const found = locator(selector, options), screenshot = found.screenshot.bind(found);
      found.screenshot = async (shot) => { atShot = await page.evaluate(blurred, "*"); return screenshot(shot); };
      return found;
    };
    try {
      await captureDialog(page, join(dir, `edit-${label}.png`));
    } finally {
      delete page.locator;
    }
    assert.deepEqual(atShot, [], `${label}: captureDialog takes its screenshot with no backdrop blur`);
    assert.deepEqual(await page.evaluate(blurred, DIALOG), real, `${label}: captureDialog leaves the real styles in place`);
    assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector(".modalbox")).maxHeight !== "none"), true,
      `${label}: the dialog's own height limit is back for the measurements`);
    // A failed screenshot is reported as itself, even when the page went away and the
    // capture style can no longer be removed.
    await assert.rejects(withCaptureStyle(page, async () => { await page.reload(); throw new Error("the screenshot failed"); }),
      /the screenshot failed/, `${label}: the capture's own error is the one reported`);
    await context.close();
  }
  console.log("Audit capture: the whole-dialog screenshot draws no backdrop blur; the app's styles keep theirs.");
} finally {
  await browser.close();
  await rm(dir, { recursive: true, force: true });
}
