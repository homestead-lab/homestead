// Named passthrough devices and retained host inventory, without contacting a cluster.
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const web = fileURLToPath(new URL("../../web/", import.meta.url));
const output = process.env.PASSTHROUGH_SCREENSHOTS || "release-assets/passthrough";
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
  await fs.mkdir(output, {recursive: true});
  browser = await chromium.launch({headless: true});
  for (const width of [1440, 375]) for (const theme of ["dark", "light"]) {
    const context = await browser.newContext({viewport: {width, height: 1000}});
    const page = await context.newPage(), errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await page.goto(`${base}/?demo=1`);
    await page.waitForFunction(() => typeof nodeDevicesPaint === "function" && ME && document.querySelector("#gate").classList.contains("hidden"));
    await page.evaluate(async theme => {
      document.documentElement.dataset.theme = theme;
      window.deviceReads = 0;
      const original = window.api;
      window.api = async (url, opts) => { if (url === "/api/passthrough/inspect") deviceReads++; return original(url, opts); };
      await go("nodes", {params: {node: "harvester-node1"}});
      nodeSectionGo("hardware");
    }, theme);
    await page.getByRole("button", {name: "Look at its devices", exact: true}).click();
    await page.locator('#nodeDevices').getByText('IOMMU group 12 · 2 devices', {exact: true}).waitFor();
    let text = await page.locator('#nodeDevices').innerText();
    assert.match(text, /GeForce RTX 2080/); assert.match(text, /TU104 HD Audio/);
    assert.match(text, /2 devices move together/); assert.match(text, /carries this host's network/);
    assert.equal(await page.evaluate(() => deviceReads), 1);
    const downloadEvent = page.waitForEvent('download');
    await page.locator('#nodeDevices').getByRole('button', {name: 'Capture vBIOS', exact: true}).first().click();
    const download = await downloadEvent;
    assert.equal(download.suggestedFilename(), 'demo-gpu-vbios.rom');
    const captured = await fs.readFile(await download.path());
    assert.deepEqual([...captured.subarray(0, 2)], [0x55, 0xaa]);
    const saved = await page.evaluate(() => PT.facts[ptKey('harvester-node1')]);
    await page.screenshot({path: `${output}/groups-${width}-${theme}.png`, fullPage: true});
    await page.evaluate(async () => { await go('dash'); await go('nodes', {params: {node: 'harvester-node1'}}); nodeSectionGo('hardware'); });
    await page.locator('#nodeDevices').getByText('IOMMU group 12 · 2 devices', {exact: true}).waitFor();
    assert.equal(await page.evaluate(() => deviceReads), 1, 'returning does not run another host helper');
    assert.equal(await page.getByRole('button', {name: 'Look at its devices', exact: true}).count(), 0);
    await page.goto(`${base}/?demo=1`);
    await page.waitForFunction(() => typeof nodeDevicesPaint === 'function' && ME && document.querySelector('#gate').classList.contains('hidden'));
    await page.evaluate(async ({saved, theme}) => {
      document.documentElement.dataset.theme = theme;
      const original = window.api;
      window.api = async (url, opts) => url.startsWith('/api/passthrough/inventory?') ? {facts: saved} : original(url, opts);
      await go('nodes', {params: {node: 'harvester-node1'}}); nodeSectionGo('hardware');
    }, {saved, theme});
    await page.locator('#nodeDevices').getByText('IOMMU group 12 · 2 devices', {exact: true}).waitFor();
    assert.match(await page.locator('#nodeDevices').innerText(), /Last inspected/);
    assert.equal(await page.locator('#nodeDevices').getByRole('button', {name: 'Refresh devices'}).count(), 1);
    const hostOverflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
    assert.equal(hostOverflow, false, 'host inventory fits the viewport');
    await page.evaluate(async () => { await vmNew(); stepGo('v_steps', 4); });
    assert.match(await page.locator('#pd_pick option').first().innerText(), /GeForce RTX 2080.*group 12/);
    await page.getByRole('button', {name: 'Add device', exact: true}).click();
    assert.match(await page.locator('#pd_adds').innerText(), /GeForce RTX 2080/);
    const changes = await page.evaluate(() => vmDevicesChanges());
    assert.equal(changes.add[0].resource, 'homestead.io/pci-10de-1e87');
    await page.evaluate(() => { stepGo('v_steps', 3); stepGo('v_steps', 4); });
    assert.equal(await page.locator('#pd_adds .pd-add').count(), 1, 'wizard back keeps device selections');
    await page.screenshot({path: `${output}/vm-new-${width}-${theme}.png`});
    let overflow = await page.evaluate(() => document.querySelector('#mbody').scrollWidth > document.querySelector('#mbody').clientWidth + 1);
    assert.equal(overflow, false, 'VM device card fits the dialog');
    await page.evaluate(async () => { closeModal(); await vmEdit('default', 'home-assistant-os'); });
    if(await page.locator('#ve_section').isVisible()) await page.locator('#ve_section').selectOption('hardware');
    else await page.locator('#ve-tab-hardware').click();
    await page.locator('#vh details[data-sec="devices"] summary').click();
    await page.locator('#vh_boot_output').selectOption('gpu');
    assert.equal(await page.locator('#vh_firmware').inputValue(), 'uefi');
    const hardware = await page.evaluate(() => vmHardwareChanges(window.__vhBase));
    assert.equal(hardware.boot_output, 'gpu'); assert.equal(hardware.graphics, false);
    await page.screenshot({path: `${output}/boot-output-${width}-${theme}.png`});
    assert.equal(await page.evaluate(() => document.querySelector('#mbody').scrollWidth > document.querySelector('#mbody').clientWidth + 1), false,
      'boot output selector fits the dialog');
    if(await page.locator('#ve_section').isVisible()) await page.locator('#ve_section').selectOption('devices');
    else await page.locator('#ve-tab-devices').click();
    await page.locator('#pd_pick').waitFor({state: 'visible'});
    assert.match(await page.locator('#pd_pick option').first().innerText(), /GeForce RTX 2080/);
    await page.getByRole('button', {name: 'Add device', exact: true}).click();
    await page.screenshot({path: `${output}/vm-edit-${width}-${theme}.png`});
    assert.equal(await page.locator('#pd_adds .pd-add').count(), 1);
    assert.deepEqual(errors, []);
    console.log(`${width}px ${theme}: groups, retained inventory, reload and VM device labels passed`);
    await context.close();
  }
} finally {
  if (browser) await browser.close();
  await new Promise(resolve => server.close(resolve));
}
