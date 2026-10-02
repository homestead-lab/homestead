// Exercise the real editor against the read-only demo. No cluster is contacted.
// node tests/integration/container-edit-steps.mjs
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const web = fileURLToPath(new URL("../../web/", import.meta.url));
const output = process.env.EDIT_SCREENSHOTS || "release-assets/container-edit";
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
const titles = ["Basics", "Hardware and access", "Environment values", "Storage", "Where it runs", "Address"];
let browser;
try {
  await fs.mkdir(output, { recursive: true });
  browser = await chromium.launch({ headless: true });
  for (const width of [1440, 390]) for (const theme of ["dark", "light"]) for (const multi of [false, true]) {
    const context = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 1000 }, isMobile: width === 390, hasTouch: width === 390 });
    const page = await context.newPage();
    await page.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await page.goto(`${base}/?demo=1`);
    await page.waitForFunction(() => typeof wlEdit === "function" && ME && document.querySelector("#gate").classList.contains("hidden"));
    const name = multi ? "home-assistant" : "frigate";
    await page.evaluate(async ({ name, theme }) => {
      document.documentElement.dataset.theme = theme;
      const original = window.api, fixture = await original(`/api/workload?ns=lab&name=${name}`);
      fixture.containers[0].env = { EXISTING: "keep-me" };
      fixture.seed_configs = [{ init_container: "seed", config_map: "fixture-config", key: "config.yaml", value: "fixture: true" }];
      window.testOriginalNames = fixture.containers.map(c => c.original_name);
      window.api = (url, opts) => url.startsWith("/api/workload?") ? Promise.resolve(fixture) : original(url, opts);
      window.editReview = async body => { window.testSaved = body; };
      await wlEdit("lab", name, true);
    }, { name, theme });
    assert.deepEqual(await page.locator("#e_steps .stepper-chip").allTextContents(), titles.map((title, index) => `${index + 1}${title}`));
    const pane = async id => page.locator(`#${id}`).evaluate(el => +el.closest(".stepper-pane").dataset.i);
    for (const [id, expected] of [["e_image_0", 0], ["e_cpu_0", 1], ["e_env_0", 2], ["e_seed_0", 2], ["e_vols_0", 3]]) {
      assert.equal(await page.locator(`#${id}`).count(), 1);
      assert.equal(await pane(id), expected);
    }
    await page.locator("#e_container_name_0").fill("primary-renamed");
    await page.locator("#e_image_0").fill("example/app:next");
    await page.evaluate(() => { window.testImageInput = document.querySelector("#e_image_0"); });
    await page.locator("#e_steps [data-next]").click();
    await page.locator("#e_cpu_0").fill("75m");
    await page.locator("#e_mem_0").fill("384Mi");
    await page.locator("#e_mem_limit_0").fill("2Gi");
    await page.locator("#e_pv_0_caps").fill("SYS_TIME");
    await page.locator("#e_ports_0 .ep-host").fill("8099");
    assert.equal(await page.locator("#e_ports_note").isVisible(), true);
    await page.locator("#e_steps [data-next]").click();
    await page.locator("#e_env_0 .ev").fill("changed-primary");
    await page.locator("#e_seed_0").fill("fixture: changed");
    if (multi) {
      await page.locator('.edit-container[data-section="environment"][data-index="1"] > summary').click();
      await page.locator("#e_env_1 .ev").fill("changed-sidecar");
    }
    assert.equal(await page.locator("#e_env_0").evaluate(el => el.previousElementSibling.textContent.includes("not exposed")), true);
    await page.locator("#e_steps [data-next]").click();
    await page.locator("#e_vols_0 .vro").check();
    for (let index = 0; index < titles.length; index++) {
      await page.evaluate(index => stepGo("e_steps", index), index);
      assert.equal(await page.locator("#e_steps > .stepper-pane:not([hidden])").count(), 1);
      assert.equal(await page.locator("#e_save").isVisible(), true);
      assert.equal(await page.locator("#mbody").evaluate(el => el.scrollWidth <= el.clientWidth + 1), true);
      assert.equal(await page.locator(`.stepper-chip[data-i="${index}"]`).evaluate(el => {
        const r = el.getBoundingClientRect(), parent = el.parentElement.getBoundingClientRect();
        const header = document.querySelector(".modalhead").getBoundingClientRect();
        return r.left >= parent.left - 1 && r.right <= parent.right + 1 && r.top >= header.bottom - 1;
      }), true);
      if (!multi) await page.screenshot({ path: `${output}/${theme}-${width}-${index}.png` });
    }
    await page.evaluate(() => stepGo("e_steps", 0));
    assert.equal(await page.evaluate(() => window.testImageInput === document.querySelector("#e_image_0")), true);
    assert.equal(await page.locator("#e_image_0").inputValue(), "example/app:next");
    await page.locator("#e_save").click();
    const saved = await page.evaluate(() => window.testSaved);
    assert.equal(saved.containers.length, multi ? 2 : 1);
    assert.deepEqual(saved.containers.map(c => c.original_name), await page.evaluate(() => window.testOriginalNames));
    assert.equal(saved.containers[0].name, "primary-renamed");
    assert.equal(saved.containers[0].image, "example/app:next");
    assert.equal(saved.containers[0].cpu, "75m");
    assert.equal(saved.containers[0].memory, "384Mi");
    assert.equal(saved.containers[0].memory_limit, "2Gi");
    assert.equal(saved.containers[0].env.EXISTING, "changed-primary");
    assert.equal(saved.containers[0].env.APP_TOKEN, undefined);
    assert.equal(saved.containers[0].ports[0].host, 8099);
    assert.deepEqual(saved.containers[0].privileges.cap_add, ["SYS_TIME"]);
    assert.equal(saved.containers[0].volumes[0].read_only, true);
    assert.equal(saved.seed_configs[0].value, "fixture: changed");
    if (multi) assert.equal(saved.containers[1].env.LOG_LEVEL, "changed-sidecar");
    console.log(`Editor passed: ${width}px ${theme}, ${multi ? "two containers" : "one container"}`);
    await context.close();
  }
} finally {
  await browser?.close();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
