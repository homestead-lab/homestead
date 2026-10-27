// A check that opens Edit straight after signing in keeps its dialog when the
// app is slow to start. The app closes any open dialog when it opens its first
// page (go() -> closeModal), and that comes only after afterAuth's first reads.
// Here /api/platform, one of them, answers 1.5 s late. No cluster is contacted.
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { openAuditPage } from "../../scripts/audit_ready.mjs";

const web = fileURLToPath(new URL("../../web/", import.meta.url));
const types = { ".js": "application/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml" };
const server = http.createServer(async (req, res) => {
  try {
    const pathname = new URL(req.url, "http://localhost").pathname;
    const target = path.resolve(web, "." + (pathname === "/" ? "/index.html" : pathname));
    if (!target.startsWith(web)) throw new Error("outside fixture");
    const body = await fs.readFile(target);
    res.writeHead(200, { "Content-Type": types[path.extname(target)] || "application/octet-stream" }); res.end(body);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${server.address().port}`;


// The page's own reads, slowed in the page: the demo answers /api/* in its
// fetch, so the first fetch the page installs (the demo's) is the one wrapped.
// /api/platform answers `delay` ms late, or, with no delay, once the check
// calls releasePlatform(); window.platformAnswered says it has answered.
function slowPlatform(delay) {
  localStorage.setItem("homestead.settings", JSON.stringify({ theme: "dark", bg: "soft", blur: 26, motion: "off", refresh: 60 }));
  const released = new Promise(resolve => { window.releasePlatform = resolve; });
  let demo = null, current = window.fetch;
  const slowed = async (input, init) => {
    if (new URL(typeof input === "string" ? input : input.url, location.origin).pathname === "/api/platform") {
      await (delay ? new Promise(resolve => setTimeout(resolve, delay)) : released);
      window.platformAnswered = true;
    }
    return demo(input, init);
  };
  Object.defineProperty(window, "fetch", { configurable: true, get: () => current,
    set: next => { if (!demo) { demo = next; current = slowed; } else current = next; } });
}

async function openPage(width, delay) {
  const context = await browser.newContext({ viewport: { width, height: 1000 } });
  await context.addInitScript(slowPlatform, delay);
  const page = await context.newPage(), errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
  return { context, page, errors };
}
const firstPageOpen = page => page.waitForFunction(() => window.NAV_TOKEN > 0 && !pageLoading(window.NAV_TOKEN));
// wlEdit can wait for ever on a read the first page abandoned (#891): fail instead.
const openEdit = page => page.evaluate(() => Promise.race([wlEdit("lab", "home-assistant", true),
  new Promise((_, reject) => setTimeout(() => reject(new Error("wlEdit did not settle within 10 s")), 10000))]));
const editShown = async page => await page.locator("#modal").isVisible() && await page.locator("#e_steps").isVisible();

let browser;
try {
  browser = await chromium.launch({ headless: true });
  for (const width of [1440, 375]) {
    // The race itself, held open: signed in is not started. The gate is hidden
    // while /api/platform is held, Edit opens, and the first page then closes it.
    // If the app stops doing that, this check no longer proves anything.
    let { context, page, errors } = await openPage(width, 0);
    await page.goto(`${base}/?demo=1`);
    await page.waitForFunction(() => typeof wlEdit === "function" && ME && document.querySelector("#gate").classList.contains("hidden"));
    await openEdit(page);
    assert.equal(await editShown(page), true, `Edit opens before the first page at ${width}px`);
    await page.evaluate(() => releasePlatform());
    await firstPageOpen(page);
    assert.equal(await editShown(page), false, `the first page closes an Edit opened before it at ${width}px`);
    assert.deepEqual(errors, []);
    await context.close();

    // The opening the checks share waits for the first page, so a slow start
    // (/api/platform 1.5 s late) cannot close the dialog the check opens next.
    ({ context, page, errors } = await openPage(width, 1500));
    await openAuditPage(page, `${base}/?demo=1`);
    await openEdit(page);
    // Once the slow read has answered, nothing is left to open a page over it.
    await page.waitForFunction(() => window.platformAnswered);
    await firstPageOpen(page);
    assert.equal(await editShown(page), true, `Edit opened after the first page stays open at ${width}px`);
    assert.deepEqual(errors, []);
    await context.close();
  }
  console.log("Edit opened after the first page stays open while the app is slow to start.");
} finally {
  await browser?.close();
  server.close();
}
