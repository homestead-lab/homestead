// Read-only visual regression against demo data, never a live cluster.
// Start: python -m http.server 4173 --bind 127.0.0.1 --directory web
// Run: node scripts/check_volume_layout.mjs
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { chromium } from "playwright";

await mkdir("release-assets/layout-checks", { recursive: true });
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(() => localStorage.setItem("homestead.settings",
    JSON.stringify({ theme: "dark", bg: "soft", motion: "off", refresh: 60 })));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("http://127.0.0.1:4173/?demo=1", { waitUntil: "networkidle" });
  await page.locator('#nav a[data-view="storage"]').click();
  await page.locator(".voltable tbody tr").first().waitFor();
  await page.evaluate(() => document.fonts.ready);
  await page.addStyleTag({ content: "#jobTray{display:none!important}" });
  // Include the reported long name and a multi-line health reason.
  await page.locator(".volname b").first().evaluate(el => { el.textContent = "binhex-minecraftserver-appdata"; });
  for (const width of [1440, 1200, 1081, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    const layout = await page.evaluate(() => ({
      width: window.innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
      rows: [...document.querySelectorAll(".voltable tbody tr")].map(row => [...row.cells].map(cell => {
        const r = cell.getBoundingClientRect();
        return { top: r.top, bottom: r.bottom, display: getComputedStyle(cell).display };
      }))
    }));
    assert.ok(layout.rows.length >= 5);
    assert.ok(layout.scrollWidth <= width + 1, `${width}px viewport overflows to ${layout.scrollWidth}px`);
    if (width > 1080) for (const cells of layout.rows) {
      assert.ok(cells.every(c => c.display === "table-cell"), "desktop cells must retain table formatting");
      assert.ok(cells.every(c => Math.abs(c.top - cells[0].top) < 1 && Math.abs(c.bottom - cells[0].bottom) < 1),
        `misaligned row at ${width}px: ${JSON.stringify(cells)}`);
    }
    if ([1440, 390].includes(width)) await page.screenshot({ path: `release-assets/layout-checks/volume-layout-${width}.png`, fullPage: true });
    console.log(`Volume layout ${width}px: aligned, no horizontal page overflow`);
  }
  assert.equal(await page.locator(".volusage").filter({ hasText: "27.8 GiB Longhorn footprint" }).count(), 1);
  assert.ok(await page.locator(".volusage").filter({ hasText: "Filesystem usage unavailable" }).count() > 0);
  assert.deepEqual(errors, []);
} finally {
  await browser.close();
}
