// Add and remove different containers in each pod, without contacting a cluster.
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const web = fileURLToPath(new URL("../../web/", import.meta.url));
const output = process.env.CONTAINER_SET_SCREENSHOTS || "release-assets/container-set";
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
  for (const width of [1440, 375]) for (const theme of ["dark", "light"]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 } });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await page.goto(`${base}/?demo=1`);
    await page.waitForFunction(() => typeof wlEdit === "function" && ME && document.querySelector("#gate").classList.contains("hidden"));
    await page.evaluate(async theme => {
      document.documentElement.dataset.theme = theme;
      window.containerRequests = [];
      const original = window.api;
      window.api = async (url, opts) => {
        if (opts) containerRequests.push({ url, body: JSON.parse(opts.body || "{}") });
        const result = await original(url, opts);
        if (url === "/api/edit/preview") {
          const body = JSON.parse(opts.body);
          result.capacity.container_changes = { added: body.containers.filter(c => c.new).map(c => c.name), removed: body.remove_containers };
        }
        return result;
      };
      await wlEdit("lab", "home-assistant", true);
    }, theme);
    const originals = await page.locator('#e_basics_containers .edit-container').evaluateAll(cards => cards.map(card => ({ index: +card.dataset.index, name: card.dataset.originalName })));
    const copies = await page.locator('#e_rep').inputValue();
    await page.getByRole('button', { name: 'Add container', exact: false }).click();
    const added = page.locator('#e_basics_containers .edit-container[data-new="1"]');
    const index = +(await added.getAttribute('data-index'));
    await page.locator(`#e_container_name_${index}`).fill('metrics');
    await page.locator(`#e_image_${index}`).fill('example/metrics:1');
    for (const step of [1, 2, 3]) {
      await page.evaluate(step => stepGo('e_steps', step), step);
      await page.locator(`#e_containers .stepper-pane:not([hidden]) .edit-container[data-index="${index}"] > summary`).click();
    }
    await page.evaluate(index => {
      document.querySelector(`#e_mem_${index}`).value = '256Mi';
      document.querySelector(`#e_env_${index} .ek`).value = 'MODE';
      document.querySelector(`#e_env_${index} .ev`).value = 'metrics';
      editAddVol(index, { kind: 'existing', source: 'frigate-config', path: '/shared', read_only: true });
    }, index);
    await page.evaluate(() => stepGo('e_steps', 0));
    await page.screenshot({ path: `${output}/edit-added-${width}-${theme}.png` });
    await page.evaluate(index => containerRemove('edit', index), originals[1].index);
    await page.getByRole('button', { name: 'Undo removal', exact: true }).click();
    assert.equal(await page.locator(`#e_basics_containers .edit-container[data-index="${originals[1].index}"]`).count(), 1);
    await page.evaluate(index => containerRemove('edit', index), originals[1].index);
    for (const section of ['basics', 'hardware', 'environment', 'storage']) {
      assert.equal(await page.locator(`#e_${section}_containers .edit-container[data-index="${originals[1].index}"]`).count(), 0);
      assert.equal(await page.locator(`#e_${section}_containers .edit-container[data-index="${index}"]`).count(), 1);
    }
    assert.equal(await page.locator('#e_rep').inputValue(), copies, 'adding containers does not add pod replicas');
    await page.locator('#e_save').click();
    await page.waitForFunction(() => document.querySelector('#mtitle').textContent === 'Review workload changes');
    assert.match(await page.locator('#mbody').innerText(), /Containers added: metrics/);
    assert.match(await page.locator('#mbody').innerText(), /persistent volumes and data are kept/);
    const edited = await page.evaluate(() => containerRequests.find(r => r.url === '/api/edit/preview').body);
    assert.deepEqual(edited.remove_containers, [originals[1].name]);
    const helper = edited.containers.find(c => c.new);
    assert.equal(helper.name, 'metrics'); assert.equal(helper.memory, '256Mi');
    assert.equal(helper.env.MODE, 'metrics'); assert.equal(helper.volumes[0].read_only, true);
    assert.ok(!edited.containers.some(c => c.original_name === originals[1].name));
    assert.ok(!(await page.evaluate(() => containerRequests)).some(r => r.url === '/api/edit'), 'nothing saved before confirmation');
    await page.evaluate(() => {
      const original = window.api;
      window.appliedContainerEdit = null;
      window.api = async (url, opts) => {
        if (url === '/api/edit') {
          appliedContainerEdit = JSON.parse(opts.body);
          containerRequests.push({url,body:appliedContainerEdit});
          throw new Error('Service lab/home-assistant already exists');
        }
        const result = await original(url, opts);
        if (url.startsWith('/api/workload?') && appliedContainerEdit) {
          return {...result,containers:appliedContainerEdit.containers.map(c=>({...c,new:false,original_name:c.name}))};
        }
        return result;
      };
    });
    await page.locator('#editCapacityConfirm').check();
    await page.getByRole('button', {name:'Save reviewed changes',exact:true}).click();
    await page.getByRole('button', {name:'Reload saved workload',exact:true}).waitFor();
    assert.equal(await page.locator('#editGo').isDisabled(),true);
    assert.match(await page.locator('#mbody').innerText(), /Some changes may already be saved/);
    assert.match(await page.locator('#mbody').innerText(), /does not need a new name/);
    const beforeRetry = await page.evaluate(()=>containerRequests.length);
    await page.evaluate(()=>editReview({ns:'lab',name:'home-assistant',containers:[{new:true,name:'metrics'}]}));
    assert.equal(await page.evaluate(()=>containerRequests.length),beforeRetry,'stale additions cannot be reviewed again');
    await page.screenshot({path:`${output}/edit-uncertain-${width}-${theme}.png`});
    await page.getByRole('button',{name:'Reload saved workload',exact:true}).click();
    await page.locator('#e_workload_name').waitFor();
    assert.equal(await page.locator('#e_workload_name').inputValue(),'home-assistant');
    assert.equal(await page.locator('#e_basics_containers .edit-container[data-new="1"]').count(),0);
    const savedNames = await page.locator('#e_basics_containers .edit-container').evaluateAll(cards=>cards.map(c=>c.dataset.originalName));
    assert.ok(savedNames.includes('metrics'),'saved sidecar is loaded as an existing container');
    await page.evaluate(()=>{appliedContainerEdit=null;});
    await page.evaluate(async () => { closeModal(); await wlEdit('lab', 'frigate', true); });
    assert.equal(await page.locator('#e_basics_containers [data-container-remove]').isDisabled(), true, 'last container stays');
    await page.evaluate(async () => { closeModal(); await go('deploy'); });
    await page.locator('#d_container_name').fill('main');
    await page.locator('#d_workload_name').fill('multi-app');
    await page.locator('#d_image').fill('example/main:1');
    await page.locator('#d_add_container button').first().click();
    const extra = page.locator('#d_extra_containers .edit-container');
    const newIndex = +(await extra.getAttribute('data-index'));
    await page.locator(`#e_container_name_${newIndex}`).fill('helper');
    await page.locator(`#e_image_${newIndex}`).fill('example/helper:1');
    await page.locator(`.qty[data-target="e_mem_${newIndex}"] .qty-text`).fill('192Mi');
    await page.locator(`#e_env_${newIndex} .ek`).fill('MODE');
    await page.locator(`#e_env_${newIndex} .ev`).fill('shared-pod');
    const created = await page.evaluate(() => collect());
    assert.equal(created.replicas, 1);
    assert.equal(created.additional_containers.length, 1);
    assert.equal(created.additional_containers[0].name, 'helper');
    assert.equal(created.additional_containers[0].memory, '192Mi');
    assert.equal(created.additional_containers[0].env.MODE, 'shared-pod');
    await page.screenshot({ path: `${output}/new-added-${width}-${theme}.png`, fullPage: true });
    await page.evaluate(()=>UI.selectSection('containerDeploy','basics'));
    await page.locator('#d_remove_primary').click();
    await page.waitForFunction(() => document.querySelector('#d_container_name').value === 'helper');
    assert.equal((await page.evaluate(() => collect())).additional_containers.length, 0);
    assert.equal(await page.locator('#d_remove_primary').isDisabled(), true);
    await page.locator('#d_add_container button').first().click();
    await page.locator('#d_extra_containers .edit-container').getByRole('button', { name: 'Remove container', exact: true }).click();
    assert.equal((await page.evaluate(() => collect())).additional_containers.length, 0);
    assert.equal(await page.locator('#d_container_name').inputValue(), 'helper');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false);
    assert.deepEqual(errors, []);
    console.log(`Container additions/removals passed at ${width}px/${theme}`);
    await context.close();
  }
} finally {
  await browser?.close();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
