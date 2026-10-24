// A live dashboard can keep requests open after it is ready to use. Wait for
// the signed-in page and its initial data, rather than for every request to end.
export async function openAuditPage(page, url) {
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.locator("#gate.hidden").waitFor({ state: "attached" });
  await page.locator("#views .phead").waitFor({ state: "visible" });
  await page.waitForFunction(() => typeof pageLoading === "function" && !pageLoading(window.NAV_TOKEN));
  await page.evaluate(() => document.fonts.ready);
}
