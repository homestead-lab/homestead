// A live dashboard can keep requests open after it is ready to use. Wait for
// the signed-in page and its initial data, rather than for every request to end.
export async function openAuditPage(page, url) {
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.locator("#gate.hidden").waitFor({ state: "attached" });
  await page.locator("#views .phead").waitFor({ state: "visible" });
  await page.waitForFunction(() => typeof pageLoading === "function" && !pageLoading(window.NAV_TOKEN));
  await page.evaluate(() => document.fonts.ready);
}

// The whole dialog, not only what fits on the screen - with the page behind it
// hidden, so nothing shows through where it runs long. The style is there only
// for the screenshot: the measurements are taken with the real styles.
// No backdrop blur either (#893): behind the dialog there is only the opaque
// fill, and Chromium blurs in software on CI, so a long dialog with nested
// blurred cards took seconds to draw and, under load, outran the 30 s limit.
export const DIALOG_CAPTURE_STYLE = ".modalbox{max-height:none!important;overflow:visible!important}"
  + "#modal{overflow:visible!important;position:absolute!important;inset:0 0 auto 0!important;display:block!important;background:#060607!important}"
  + "body>*:not(#modal){visibility:hidden!important}.ui-actions{position:static!important}"
  + "*,*::before,*::after{backdrop-filter:none!important;-webkit-backdrop-filter:none!important}";

export async function withCaptureStyle(page, run) {
  const style = await page.addStyleTag({ content: DIALOG_CAPTURE_STYLE });
  let done = false;
  try {
    const result = await run();
    done = true;
    return result;
  } finally {
    // After a good capture a style that stays would skew the next dialog's
    // measurements, so that fails; after a failed one, report the failure.
    await style.evaluate((s) => s.remove()).catch((error) => { if (done) throw error; });
  }
}

export function captureDialog(page, path) {
  return withCaptureStyle(page, () => page.locator(".modalbox").screenshot({ path }));
}
