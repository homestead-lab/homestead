// Shared page layouts preserve navigation, save gates and field state. No cluster writes.
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const web = path.resolve(fileURLToPath(new URL("../../web/", import.meta.url)));
const output = process.env.PAGE_STANDARD_SCREENSHOTS || "release-assets/page-standard";
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
  await fs.mkdir(output,{recursive:true});
  browser=await chromium.launch({headless:true});
  for(const width of [1440,390]) for(const theme of ['dark','light']) {
    const context=await browser.newContext({viewport:{width,height:1000}}), page=await context.newPage(), errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',route=>route.request().url().startsWith(base)?route.continue():route.abort());
    await page.goto(`${base}/?demo=1`);
    await page.waitForFunction(()=>typeof viewSettings==='function' && ME && document.querySelector('#gate').classList.contains('hidden'));
    await page.evaluate(async theme=>{
      document.documentElement.dataset.theme=theme;
      const original=window.api;window.settingsWrites=[];let saved;
      window.api=async(url,options={})=>{
        if(url==='/api/settings') {
          if(options.method==='POST'){saved=JSON.parse(options.body);settingsWrites.push(structuredClone(saved));return saved;}
          if(saved)return saved;
        }
        return original(url,options);
      };
      STATE.settingsOpen=false;go('settings');
    },theme);
    await page.locator('.settings-nav [data-tab="monitoring"]').waitFor();
    if(width<900)assert.equal(await page.locator('.settings-main').isVisible(),false);
    await page.locator('.settings-nav [data-tab="monitoring"]').click();
    await page.locator('#set_cpu_warn').waitFor({state:'visible'});
    assert.equal(await page.locator('#settingsSaveBar').isVisible(),false);
    const original=Number(await page.locator('#set_cpu_warn').inputValue());
    await page.locator('#set_cpu_warn').fill(String(original-1));
    assert.equal(await page.locator('#settingsSaveBar').isVisible(),true);
    await page.evaluate(()=>{settingsGo('updates');});
    await page.locator('.askdlg [data-a="no"]').click();
    assert.equal(await page.locator('#set_cpu_warn').inputValue(),String(original-1));
    assert.equal(await page.locator('.settings-grid').getAttribute('data-tab'),'monitoring');
    await page.locator('#settingsSaveBar').getByRole('button',{name:'Save',exact:true}).click();
    await page.waitForFunction(()=>settingsWrites.length===1 && document.querySelector('#settingsSaveBar')?.hidden);
    assert.equal(await page.evaluate(()=>settingsWrites[0].thresholds.cpu.warning),original-1);
    await page.locator('#set_cpu_warn').fill(String(original-2));
    await page.locator('#settingsSaveBar').getByRole('button',{name:'Discard',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#settingsSaveBar')?.hidden);
    assert.equal(await page.locator('#set_cpu_warn').inputValue(),String(original-1));
    if(width<900){
      await page.locator('.settings-back').click();
      await page.locator('.settings-nav [data-tab="updates"]').waitFor({state:'visible'});
      assert.equal(await page.locator('.settings-main').isVisible(),false);
    }
    await page.locator('.settings-nav [data-tab="updates"]').click();
    await page.locator('#settingsHostUpdates .settings-card-head').waitFor({state:'visible'});
    for(const id of ['homesteadUpdateCard','settingsComponents','settingsHostUpdates'])
      assert.equal(await page.locator(`#${id} .settings-card-head`).count(),1);
    assert.equal(await page.locator('#homesteadUpdateCard .ctitle').evaluate(el=>getComputedStyle(el).color),await page.locator('body').evaluate(el=>getComputedStyle(el).color));
    if(width>900){
      await page.locator('.settings-nav [data-tab="updates"]').focus();await page.keyboard.press('Home');
      await page.waitForFunction(()=>document.querySelector('.settings-grid')?.dataset.tab==='homestead' && document.activeElement?.dataset.tab==='homestead');
      await page.evaluate(()=>settingsTab('updates'));
    }
    await page.screenshot({path:`${output}/settings-${theme}-${width}.png`});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
    // Host sections hide panes without recreating their fields/content.
    await page.evaluate(async()=>{go('nodes');});
    await page.waitForFunction(()=>STATE.data.nodes?.length);
    await page.evaluate(()=>nodeDetail('harvester-node1'));
    await page.locator('#nodePage .settings-nav [data-tab="hardware"]').waitFor();
    await page.locator('#nodePage .settings-nav [data-tab="hardware"]').click();
    await page.evaluate(()=>{window.keptNodePane=document.querySelector('#nodePage .node-pane[data-pane="hardware"]');nodeSectionGo('overview');nodeSectionGo('hardware');});
    assert.equal(await page.evaluate(()=>keptNodePane===document.querySelector('#nodePage .node-pane[data-pane="hardware"]')),true);
    if(width<900){await page.locator('#nodePage .settings-back').click();assert.equal(await page.locator('#nodePage .settings-nav').isVisible(),true);}
    // Setup keeps its progress semantics and mobile selector in the shared shell.
    await page.evaluate(()=>go('setup'));
    await page.locator('#setupPage').waitFor();
    if(width<900)await page.locator('#setupSelect').selectOption('appearance');
    else await page.locator('.setup-nav [data-step="appearance"]').click();
    await page.waitForFunction(()=>document.querySelector('#setupStep [data-step="appearance"]'));
    assert.equal(await page.locator('#setupSelect').inputValue(),'appearance');
    assert.deepEqual(errors,[]);
    console.log(`Page standard passed: ${width}px ${theme}`);await context.close();
  }
} finally {await browser?.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
