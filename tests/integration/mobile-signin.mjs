// Run with Playwright and its Chromium browser installed:
// node tests/integration/mobile-signin.mjs
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const web = fileURLToPath(new URL("../../web/", import.meta.url));
const types = { ".js": "application/javascript", ".css": "text/css", ".html": "text/html",
  ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json" };
let apiReads = 0, staleResponse;
const json = (res, code, body) => {
  res.writeHead(code, { "Content-Type": "application/json" }); res.end(JSON.stringify(body));
};
const server = http.createServer(async (req, res) => {
  const pathname = new URL(req.url, "http://localhost").pathname;
  if (pathname === "/api/auth/state") return json(res, 200, { user: null });
  if (pathname === "/api/auth/login") return json(res, 200, { user: "test-user", role: "viewer" });
  if (pathname === "/api/test-stale") { staleResponse = res; return; }
  if (pathname.startsWith("/api/")) { apiReads++; return json(res, 401, { error: "not signed in" }); }
  try {
    const target = path.resolve(web, "." + (pathname === "/" ? "/index.html" : pathname));
    if (!target.startsWith(web)) { res.writeHead(404); res.end(); return; }
    const body = await fs.readFile(target);
    res.writeHead(200, { "Content-Type": types[path.extname(target)] || "application/octet-stream" });
    res.end(body);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${server.address().port}`;
let browser;
try {
  browser = await chromium.launch({ headless: true });
  for (const width of [390, 430]) {
    const context = await browser.newContext({ viewport: { width, height: 844 }, isMobile: true, hasTouch: true });
    const page = await context.newPage();
    await page.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await page.goto(base);
    await page.locator("#lg_pass").waitFor({ state: "visible" });
    await page.locator("#lg_user").fill("test-user");
    await page.locator("#lg_pass").pressSequentially("partial-password", { delay: 30 });
    await page.evaluate(() => { window.testPassword = document.querySelector("#lg_pass"); });
    const beforeResume = apiReads;
    await page.setViewportSize({ width, height: 460 });
    await page.evaluate(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      window.testRefreshResult = await window.refresh(true);
    });
    // The hidden page cannot refresh, even if resume and keyboard events arrive.
    assert.equal(apiReads, beforeResume);
    await page.evaluate(() => Promise.all([1, 2, 3].map(() => window.api("/api/overview", { keep: true }).catch(() => {}))));
    assert.deepEqual(await page.evaluate(() => ({ same: window.testPassword === document.querySelector("#lg_pass"),
      password: document.querySelector("#lg_pass").value, focused: document.activeElement.id,
      refreshed: window.testRefreshResult })),
    { same: true, password: "partial-password", focused: "lg_pass", refreshed: false });

    // An expired authenticated session stops its loops and offers one form.
    await page.evaluate(() => { ME = "expired-user"; ROLE = "admin";
      startLoop(); startUpdateChecks(); startOperationChecks(); });
    await page.getByText("Session expired — sign in again", { exact: true }).waitFor();
    await page.locator("#lg_user").fill("test-user");
    await page.locator("#lg_pass").fill("another-password");
    await page.evaluate(() => { window.testPassword = document.querySelector("#lg_pass"); });
    const afterExpiry = apiReads;
    await page.waitForTimeout(3800); // Includes the old delayed image check and job poll.
    assert.equal(apiReads, afterExpiry);
    assert.equal(await page.evaluate(() => window.testPassword === document.querySelector("#lg_pass")), true);
    assert.equal(await page.locator("#lg_pass").inputValue(), "another-password");

    // A response already in flight must not revoke a successful new login.
    await page.evaluate(() => { window.pendingOld = window.api("/api/test-stale", { keep: true }).catch(() => {});
      afterAuth = async () => {}; }); // Isolate the auth transition from dashboard data fixtures.
    const deadline = Date.now() + 5000;
    while (!staleResponse && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10));
    assert.ok(staleResponse, "the delayed old request reached the server");
    await page.locator("#lg_go").click();
    await page.locator("#gate").waitFor({ state: "hidden" });
    json(staleResponse, 401, { error: "not signed in" }); staleResponse = null;
    await page.evaluate(() => window.pendingOld);
    assert.equal(await page.evaluate(() => ME), "test-user");
    assert.equal(await page.locator("#gate").isVisible(), false);
    await context.close();
    console.log(`Mobile sign-in regression passed at ${width}px`);
  }
} finally {
  if (staleResponse) staleResponse.destroy();
  await browser?.close();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
