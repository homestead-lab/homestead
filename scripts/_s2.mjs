import { chromium } from "playwright";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { join, extname } from "node:path";
const SITE = process.env.SITE, OUT = process.env.OUT;
const T = { ".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml", ".png": "image/png" };
const s = createServer(async (q, r) => { const p = new URL(q.url, "http://x").pathname.replace(/^\/homestead\/?/, "") || "index.html";
  try { const b = await readFile(join(SITE, p)); r.writeHead(200, { "Content-Type": T[extname(p)] || "text/html" }).end(b); } catch { r.writeHead(404, { "Content-Type": "text/html" }).end(await readFile(join(SITE, "404.html"))); } });
await new Promise(d => s.listen(4188, "127.0.0.1", d));
const b = await chromium.launch(); const errs = [];
for (const [w, h, n] of [[1440, 820, "desk"], [390, 760, "phone"]]) {
  for (const path of (process.env.PAGES || "volumes,networking,system/cluster,data-protection").split(",")) {
    const pg = await b.newPage({ viewport: { width: w, height: h } });
    pg.on("pageerror", e => errs.push(path + ": " + e.message));
    await pg.goto("http://127.0.0.1:4188/homestead/" + path, { waitUntil: "networkidle" }); await pg.waitForTimeout(1400);
    await pg.addStyleTag({ content: "#jobTray{display:none!important}" });
    await pg.screenshot({ path: `${OUT}/${n}-${path.replace("/", "-")}.png` });
    if (process.env.OPEN) { const t = pg.locator(".sumtoggle").first(); if (await t.count()) { await t.click(); await pg.waitForTimeout(300); await pg.screenshot({ path: `${OUT}/${n}-${path.replace("/", "-")}-open.png` }); } }
    await pg.close();
  }
}
console.log("errors", JSON.stringify(errs)); await b.close(); s.close();
