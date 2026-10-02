// Real creation-form checks against the read-only demo; no cluster is contacted.
// node tests/integration/planned-container-volumes.mjs
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const web = fileURLToPath(new URL("../../web/", import.meta.url));
const output = process.env.VOLUME_SCREENSHOTS || "release-assets/planned-volumes";
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
let browser;
try {
  await fs.mkdir(output, { recursive: true });
  browser = await chromium.launch({ headless: true });
  for (const width of [1440, 390]) for (const theme of ["dark", "light"]) for (const kind of ["new-rwo", "new-rwx"]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, isMobile: width === 390, hasTouch: width === 390 });
    const page = await context.newPage(), errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await page.goto(`${base}/?demo=1`);
    await page.waitForFunction(() => typeof viewDeploy === "function" && ME && document.querySelector("#gate").classList.contains("hidden"));
    await page.evaluate(async ({ theme, kind }) => {
      document.documentElement.dataset.theme = theme;
      const original = window.api;
      window.testPreviewCalls = 0;
      window.api = async (url, opts) => {
        if (url.startsWith("/api/deploy/options")) return { ...await original(url, opts),
          storage_classes: ["longhorn-r2", "longhorn-r3"], shared_storage_classes: ["longhorn-r2", "longhorn-r3"],
          storage_class_facts: { "longhorn-r2": { replicas: "2", engine: "v1", expandable: true },
            "longhorn-r3": { replicas: "3", engine: "v1", expandable: true } } };
        if (url === "/api/preview") { window.testPreviewCalls++; window.testPreview = JSON.parse(opts.body); }
        return original(url, opts);
      };
      await viewDeploy({ name: "qbittorrent", image: "example/qbittorrent:1", network_mode: "internal", volumes: [
        { path: "/config", source: "qbittorrent-appdata", kind, size_gb: 55, storage_class: "longhorn-r3", sub_path: "config" },
        { path: "/data", source: "nas-data", kind: "existing", sub_path: "data" },
      ] });
    }, { theme, kind });
    let rows = page.locator("#d_vols .deploy-volume"), owner = rows.nth(0), reused = rows.nth(1);
    await reused.locator(".vk").selectOption("planned");
    assert.match(await reused.locator(".vselect").textContent(), /qbittorrent-appdata.*created with this workload/);
    await reused.locator(".vselect").selectOption("qbittorrent-appdata");
    await reused.locator(".vro").check();
    assert.equal(await reused.locator(".vnew").isVisible(), false);
    assert.match(await reused.locator(".vhelp").textContent(), /created once/);
    let volumes = await page.evaluate(() => collect().volumes);
    assert.deepEqual(volumes.map(v => [v.source, v.kind, v.size_gb, v.storage_class, v.access_mode]),
      Array(2).fill(["qbittorrent-appdata", kind, 55, "longhorn-r3", kind === "new-rwx" ? "ReadWriteMany" : "ReadWriteOnce"]));
    assert.deepEqual(volumes.map(v => [v.path, v.sub_path]), [["/config", "config"], ["/data", "data"]]);
    assert.equal(volumes[1].read_only, true);
    assert.equal(await page.evaluate(() => volumeListIssue(collect().volumes)), "");
    await owner.locator(".vs").fill("qbittorrent-renamed");
    await owner.locator(".vz").fill("7");
    await owner.locator(".vsc").selectOption("longhorn-r2");
    assert.equal(await reused.locator(".vselect").inputValue(), "qbittorrent-renamed");
    volumes = await page.evaluate(() => collect().volumes);
    assert.deepEqual(volumes.map(v => [v.source, v.size_gb, v.storage_class]), Array(2).fill(["qbittorrent-renamed", 7, "longhorn-r2"]));
    await reused.locator(".vselect").selectOption("");
    assert.match(await page.evaluate(() => volumeListIssue(collect().volumes)), /another mapping/);
    await reused.locator(".vselect").selectOption("qbittorrent-renamed");
    await page.evaluate(() => { collect(); renderVols(); });
    assert.equal(await reused.locator(".vk").inputValue(), "planned");
    assert.equal(await reused.locator(".vselect").inputValue(), "qbittorrent-renamed");
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
    if (kind === "new-rwo") await page.locator("#d_vols").screenshot({ path: `${output}/${theme}-${width}.png` });
    await page.evaluate(() => doDeploy());
    assert.equal(await page.evaluate(() => window.testPreviewCalls), 1);
    assert.deepEqual((await page.evaluate(() => window.testPreview.volumes)).map(v => v.source), Array(2).fill("qbittorrent-renamed"));
    await page.evaluate(() => closeModal());
    await owner.locator(".row-remove").click();
    assert.match(await page.evaluate(() => volumeListIssue(collect().volumes)), /another mapping/);
    await page.evaluate(() => doDeploy());
    assert.equal(await page.evaluate(() => window.testPreviewCalls), 1, "an orphaned mapping cannot reach preview");
    // The defining row may follow the reference, as after a form re-render.
    await page.evaluate(kind => addVol("/config", "qbittorrent-renamed", { kind, source: "qbittorrent-renamed", path: "/config", size_gb: 7, storage_class: "longhorn-r2", sub_path: "config" }), kind);
    assert.equal(await page.evaluate(() => volumeListIssue(collect().volumes)), "");
    volumes = await page.evaluate(() => collect().volumes);
    assert.equal(volumes[0].kind, kind);
    assert.equal(volumes[0].size_gb, 7);
    await page.evaluate(() => { collect(); renderVols(); });
    assert.equal(await rows.nth(0).locator(".vk").inputValue(), "planned");
    assert.equal(await page.evaluate(() => volumeListIssue(collect().volumes)), "");
    await rows.nth(1).locator(".vk").selectOption("existing");
    assert.match(await page.evaluate(() => volumeListIssue(collect().volumes)), /another mapping/);
    await rows.nth(1).locator(".vk").selectOption(kind);
    assert.equal(await page.evaluate(() => volumeListIssue(collect().volumes)), "");
    assert.deepEqual(errors, []);
    console.log(`Planned volume passed: ${width}px ${theme}, ${kind}`);
    await context.close();
  }
} finally {
  await browser?.close();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
