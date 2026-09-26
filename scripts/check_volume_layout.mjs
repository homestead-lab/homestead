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
  await page.evaluate(() => lhSnaps("pvc-demo-frigate", "frigate-config"));
  await page.locator(".snapshot-timeline").waitFor();
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
    const timeline = await page.locator(".snapshot-timeline").boundingBox();
    assert.ok(timeline.x >= 0 && timeline.x + timeline.width <= width + 1);
    assert.equal(await page.locator(".snapshot-point.head button").count(), 0);
    if (width !== 320) await page.screenshot({path: `release-assets/layout-checks/snapshots-${width}.png`});
    console.log(`Snapshot timeline ${width}px: contained and live head protected`);
  }
  await page.evaluate(async () => {
    closeModal();
    const original = api;
    api = async path => path.startsWith("/api/volumes/delete-plan") ? {
      orphan: true, namespace: "lab", name: "retained-disk", uid: "demo-lh-uid", phase: "PVC already absent",
      blocked: false, blocking_reasons: [], consumers: [], warnings: [],
      pv: {name: "retained-pv", uid: "demo-pv-uid", reclaim_policy: "Retain"},
      longhorn: {name: "retained-vol", state: "detached", actual_gb: 12, replicas: 2},
      snapshots: {count: 1}, backups: {count: 2},
      actions: {detach: {complete: true}, delete_claim: {enabled: false}, delete_data: {enabled: true}}
    } : original(path);
    try { await volumeDelete({name: "retained-vol", pvc_name: "retained-disk", namespace: "lab"}); }
    finally { api = original; }
  });
  assert.equal(await page.locator('input[value="delete_claim"]').isDisabled(), true);
  assert.equal(await page.locator("#vd_go").isDisabled(), true);
  await page.locator('input[value="delete_data"]').check();
  await page.locator("#vd_confirm").fill("wrong");
  assert.equal(await page.locator("#vd_go").isDisabled(), true);
  await page.locator("#vd_confirm").fill("retained-disk");
  assert.equal(await page.locator("#vd_go").isDisabled(), false);
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
  console.log("Orphan-volume review: no claim-only action, exact-name confirmation required, mobile contained");
  assert.deepEqual(errors, []);
} finally {
  await browser.close();
}
