// Real guide and editor against local, in-memory API fixtures; no cluster writes.
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const web = path.resolve(fileURLToPath(new URL("../../web/", import.meta.url)));
const output = process.env.STORAGE_SCREENSHOTS || "release-assets/setup-storage";
const types = { ".js": "application/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml" };
const server = http.createServer(async (req, res) => {
  try {
    const pathname = new URL(req.url, "http://localhost").pathname;
    const target = path.resolve(web, "." + (pathname === "/" ? "/index.html" : pathname));
    if (!target.startsWith(web + path.sep)) throw new Error("outside fixture");
    const body = await fs.readFile(target);
    res.writeHead(200, { "Content-Type": types[path.extname(target)] || "application/octet-stream" });
    res.end(body);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${server.address().port}`;
let browser;
try {
  await fs.mkdir(output, { recursive: true });
  browser = await chromium.launch({ headless: true });
  for (const width of [1440, 390]) for (const theme of ["dark", "light"]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 }, isMobile: width === 390, hasTouch: width === 390 });
    const page = await context.newPage(), errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await page.goto(`${base}/?demo=1`);
    await page.waitForFunction(() => typeof viewSetup === "function" && ME && document.querySelector("#gate").classList.contains("hidden"));
    await page.evaluate(async theme => {
      document.documentElement.dataset.theme = theme;
      const original = window.api;
      const safe = { provisioner: "driver.longhorn.io", replicas: "1", engine: "v1", migratable: false, encrypted: false,
        reclaim: "Retain", expandable: true, disk_tags: [], node_tags: [], parameters: {} };
      window.storageFixture = { calls: [], failCreate: false, failClasses: false, failDisks: false,
        classes: [{ ...safe, name: "longhorn-r1" }, { ...safe, name: "longhorn-r2", replicas: "2", migratable: true, default: true }],
        disks: { nodes: { one: [{ longhorn: [{ id: "data", scheduling: true, ready: true, type: "filesystem", tags: ["ssd"] }] }] },
          disk_tags: ["ssd"], all_node_tags: [], node_tags: {} } };
      window.api = async (url, opts = {}) => {
        const f = window.storageFixture;
        if (url === "/api/setup") {
          const result = await original(url, opts), current = f.classes.find(c => c.default);
          result.steps.storage = { applies: true, done: current?.replicas === "1", default: current?.name || "", copies: +(current?.replicas || 0),
            provisioner: current?.provisioner, nodes: 1, target: 1, candidates: [] };
          return result;
        }
        if (url === "/api/disks") { if (f.failDisks) throw new Error("disk inventory unavailable"); return structuredClone(f.disks); }
        if (url === "/api/storage/classes" && opts.method !== "POST") {
          if (f.failClasses) throw new Error("class inventory unavailable"); return structuredClone(f.classes);
        }
        if (url === "/api/storage/classes" && opts.method === "POST") {
          const payload = JSON.parse(opts.body); f.calls.push({ url, payload });
          if (f.failCreate) throw new Error("fixture create failed");
          if (payload.default) f.classes.forEach(c => c.default = false);
          f.classes.push({ ...safe, ...payload, replicas: String(payload.replicas), reclaim: payload.reclaim_policy });
          return { message: "Class created" };
        }
        if (url === "/api/storage/classes/default") {
          const payload = JSON.parse(opts.body); f.calls.push({ url, payload });
          f.classes.forEach(c => c.default = c.name === payload.name); return { message: "Default changed" };
        }
        return original(url, opts);
      };
      STATE.view = "setup";
      history.replaceState(null, "", "/setup?demo=1&step=storage");
      await viewSetup();
    }, theme);
    const recipe = id => page.locator(`[data-storage-recipe="${id}"]`);
    await page.waitForSelector('[data-storage-recipe="r3"]');
    assert.equal(await page.locator("[data-storage-recipe]").count(), 5);
    assert.match(await recipe("hdd").textContent(), /No schedulable V1 disk has the hdd tag/);
    assert.match(await recipe("r2").textContent(), /longhorn-r2-containers/);
    assert.match(await recipe("r3").textContent(), /Fewer than 3/);
    assert.equal(await page.evaluate(() => storageFixture.calls.length), 0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: `${output}/${theme}-${width}-guide.png`, fullPage: true });

    // Existing class reuse changes the default only after the explicit action.
    await recipe("r1").getByRole("button", { name: "Make default" }).click();
    await recipe("r1").getByText("Current default").waitFor();
    assert.equal(await page.evaluate(() => storageFixture.calls[0].payload.name), "longhorn-r1");
    await recipe("r2").getByRole("button", { name: "Review and create" }).click();
    await page.waitForFunction(() => !document.querySelector("#sc_go").disabled);
    assert.equal(await page.locator("#sc_name").inputValue(), "longhorn-r2-containers");
    assert.equal(await page.locator("#sc_reps").inputValue(), "2");
    assert.equal(await page.locator("#sc_copies").inputValue(), "hosts", "preset placement survives the one-host auto-default");
    assert.equal(await page.locator("#sc_default").isChecked(), false);
    assert.equal(await page.locator("#sc_migratable").isChecked(), false);
    await page.locator("#sc_name").fill("my-container-storage");
    await page.locator("#sc_reps").fill("3");
    await page.locator("#sc_copies").selectOption("disks");
    await page.evaluate(() => storageFixture.failCreate = true);
    await page.locator("#sc_go").click();
    await page.waitForFunction(() => !document.querySelector("#sc_go").disabled);
    assert.equal(await page.locator("#sc_name").inputValue(), "my-container-storage");
    assert.equal(await page.locator("#sc_reps").inputValue(), "3");
    assert.equal(await page.evaluate(() => storageFixture.calls.at(-1).payload.copies), "disks");
    await page.evaluate(() => storageFixture.failCreate = false);
    await page.locator("#sc_reps").fill("2");
    await page.locator("#sc_copies").selectOption("hosts");
    await page.locator("#sc_default").check();
    await page.locator("#sc_go").click();
    await recipe("r2").getByText("Current default").waitFor();
    assert.match(await recipe("r2").textContent(), /my-container-storage/);
    const payload = await page.evaluate(() => storageFixture.calls.at(-1).payload);
    assert.deepEqual(payload, { name: "my-container-storage", replicas: 2, reclaim_policy: "Retain", copies: "hosts", expandable: true,
      migratable: false, engine: "v1", default: true, disk_tags: [], node_tags: [] });

    // Missing tags remain selected; users can remove or retain the selector.
    await recipe("hdd").getByRole("button", { name: "Review and create" }).click();
    await page.waitForFunction(() => !document.querySelector("#sc_go").disabled);
    const hdd = page.locator('#sc_disktags input[value="hdd"]');
    assert.equal(await hdd.isChecked(), true);
    assert.match(await page.locator("#sc_reach").textContent(), /would not schedule/);
    await hdd.locator("..").click();
    assert.equal(await hdd.isChecked(), false);
    assert.match(await page.locator("#sc_reach").textContent(), /1 host/);
    await hdd.locator("..").click();
    assert.equal(await hdd.isChecked(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
    await page.locator("#modal").screenshot({ path: `${output}/${theme}-${width}-editor.png` });
    await page.locator("#sc_go").click();
    await recipe("hdd").getByRole("button", { name: "Make default" }).waitFor();
    assert.deepEqual(await page.evaluate(() => storageFixture.calls.at(-1).payload.disk_tags), ["hdd"]);

    // Failed reads show retry/unknown placement, never invented existing classes.
    await page.evaluate(async () => { storageFixture.failClasses = true; await setupStorageLoad(true); });
    assert.match(await page.locator("#setupStorageSuggestions").textContent(), /class inventory unavailable/);
    assert.equal(await page.locator("[data-storage-recipe]").count(), 0);
    await page.evaluate(async () => {
      storageFixture.failClasses = false; storageFixture.failDisks = true;
      STATE.data.disks = null; await setupStorageLoad(true);
    });
    assert.match(await recipe("ssd").textContent(), /could not be checked/);
    await recipe("ssd").getByRole("button", { name: "Review and create" }).click();
    await page.waitForFunction(() => !document.querySelector("#sc_go").disabled);
    assert.equal(await page.locator('#sc_disktags input[value="ssd"]').isChecked(), true);
    assert.match(await page.locator("#sc_reach").textContent(), /Could not read disk availability/);
    await page.evaluate(() => closeModal());
    const before = await page.evaluate(() => storageFixture.calls.length);
    await page.evaluate(async () => {
      storageFixture.failDisks = false; storageFixture.classes.forEach(c => c.default = false); await viewSetup();
    });
    await recipe("r1").getByRole("button", { name: "Make default" }).waitFor();
    await recipe("r3").getByRole("button", { name: "Review and create" }).click();
    await page.waitForFunction(() => !document.querySelector("#sc_go").disabled);
    assert.equal(await page.locator("#sc_default").isChecked(), false, "only the suggested replica count opts into default for review");
    await page.evaluate(() => closeModal());
    await page.getByRole("button", { name: "Custom storage class", exact: true }).click();
    await page.waitForFunction(() => !document.querySelector("#sc_go").disabled);
    assert.equal(await page.locator("#sc_name").inputValue(), "");
    assert.equal(await page.locator("#sc_copies").inputValue(), "disks", "the custom form keeps its one-host default");
    await page.evaluate(() => closeModal());
    // A delayed inventory read must not replace the next step.
    await page.evaluate(async () => {
      const original = window.api;
      let release;
      window.api = async (url, opts) => url === "/api/storage/classes" ? new Promise(resolve => release = resolve) : original(url, opts);
      const loading = setupStorageLoad(true);
      setupOpen("disks");
      release(structuredClone(storageFixture.classes));
      await loading;
      window.api = original;
    });
    assert.equal(await page.locator('.setup-card[data-step="disks"]').count(), 1);
    assert.equal(await page.locator("#setupStorageSuggestions").count(), 0);
    assert.equal(await page.evaluate(() => storageFixture.calls.length), before);
    assert.deepEqual(errors, []);
    console.log(`Guided storage passed: ${width}px ${theme}`);
    await context.close();
  }
} finally {
  await browser?.close();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
