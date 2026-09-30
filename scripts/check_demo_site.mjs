// The live demo, checked the way GitHub Pages serves it: under /homestead/,
// with 404.html for any path it has no file for.
//
//   python scripts/build_demo_site.py && node scripts/check_demo_site.mjs
//
// Every page in the sidebar is opened, and a deep link loaded directly. A file
// of the site that does not load, or a script error, fails the check; an API
// path the demo has no answer for is listed, so it can be filled in.
import { chromium } from "playwright";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";

const SITE = process.env.DEMO_SITE || "_site";
const BASE = process.env.DEMO_BASE || "/homestead/";
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
  ".png": "image/png", ".jpg": "image/jpeg", ".webmanifest": "application/manifest+json" };

const server = createServer(async (req, res) => {
  const path = decodeURIComponent(new URL(req.url, "http://x").pathname);
  const inside = path.startsWith(BASE) ? path.slice(BASE.length) : null;
  const file = inside === null ? null : normalize(join(SITE, inside || "index.html"));
  try {
    if (!file || !file.startsWith(normalize(SITE))) throw new Error("outside");
    const body = await readFile(file);
    res.writeHead(200, { "Content-Type": TYPES[extname(file)] || "application/octet-stream" }).end(body);
  } catch {
    // As Pages does: 404.html, with a 404 status.
    res.writeHead(404, { "Content-Type": "text/html" }).end(await readFile(join(SITE, "404.html")));
  }
});
await new Promise(done => server.listen(0, "127.0.0.1", done));
const origin = `http://127.0.0.1:${server.address().port}`;

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const broken = [], errors = [], missing = new Set();
page.on("pageerror", error => errors.push(error.message.split("\n")[0]));
page.on("response", response => {
  const url = new URL(response.url());
  // The deep link itself is answered by 404.html; anything else is a file that is not there.
  if (url.origin === origin && response.status() >= 400 && response.request().resourceType() !== "document") {
    broken.push(`${response.status()} ${url.pathname}`);
  }
});
await page.exposeFunction("__demoMissing", path => missing.add(path));
await page.addInitScript(() => {
  const wrap = () => {
    const inner = window.fetch;
    window.fetch = async (input, init) => {
      const response = await inner(input, init);
      if (response.status === 404) {
        const path = new URL(typeof input === "string" ? input : input.url, location.origin).pathname;
        if (path.startsWith("/api/")) window.__demoMissing(`${init?.method || "GET"} ${path}`);
      }
      return response;
    };
  };
  // After demo.js has put its own fetch in place.
  document.addEventListener("DOMContentLoaded", wrap);
});

const failures = [];
try {
  await page.goto(origin + BASE, { waitUntil: "networkidle" });
  await page.locator("#views .phead").waitFor({ timeout: 20000 });
  if (!await page.locator("#demoBanner").count()) failures.push("the demo banner is not shown");
  const views = await page.locator("#nav a[data-view]").evaluateAll(links => links.map(a => a.dataset.view));
  for (const view of views) {
    try {
      await page.locator(`#nav a[data-view="${view}"]`).click();
      await page.locator("#views .phead").waitFor({ timeout: 15000 });
      await page.waitForTimeout(600);
      if (!new URL(page.url()).pathname.startsWith(BASE)) failures.push(`${view}: left the site for ${page.url()}`);
    } catch (error) {
      failures.push(`${view}: ${error.message.split("\n")[0]}`);
    }
  }
  // A deep link, as someone would follow it from the README.
  await page.goto(origin + BASE + "nodes", { waitUntil: "networkidle" });
  await page.locator("#views .phead").waitFor({ timeout: 20000 });
  const active = await page.locator("#nav a.on").getAttribute("data-view");
  if (active !== "nodes") failures.push(`the deep link ${BASE}nodes opened ${active}, not nodes`);
} catch (error) {
  failures.push(error.message.split("\n")[0]);
} finally {
  await browser.close();
  server.close();
}

if (missing.size) console.log(`API paths the demo has no answer for (${missing.size}):\n  ${[...missing].sort().join("\n  ")}`);
failures.push(...[...new Set(broken)].map(line => `not found: ${line}`), ...[...new Set(errors)].map(line => `script error: ${line}`));
if (failures.length) {
  console.error(`demo site check failed:\n  ${failures.join("\n  ")}`);
  process.exit(1);
}
console.log(`demo site ok: every page opens under ${BASE}`);
