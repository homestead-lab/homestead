// Exercise the real editor against the read-only demo. No cluster is contacted.
// node tests/integration/container-edit-steps.mjs
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const web = fileURLToPath(new URL("../../web/", import.meta.url));
const output = process.env.DIALOG_STANDARD_SCREENSHOTS || "release-assets/dialog-standard";
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
  await fs.mkdir(output, {recursive:true});
  browser = await chromium.launch({headless:true});
  for (const width of [1440,390]) for (const theme of ["dark","light"]) {
    const context = await browser.newContext({viewport:{width,height:900}}), page=await context.newPage();
    const errors=[];page.on("pageerror",error=>errors.push(error.message));
    await page.route("**/*", route=>route.request().url().startsWith(base)?route.continue():route.abort());
    await page.goto(`${base}/?demo=1`);
    await page.waitForFunction(()=>typeof jobsDialog==="function" && ME && document.querySelector("#gate").classList.contains("hidden"));
    await page.evaluate(theme=>{
      document.documentElement.dataset.theme=theme; clearTimeout(window.__operationTimer);
      window.dismissed=[];
      const original=window.api;
      window.api=async (url,opts)=>{if(url==="/api/operations/dismiss"){dismissed.push(JSON.parse(opts.body).id);return {};} return original(url,opts);};
      STATE.data.operations=[
        {id:"failed",title:"Move media",status:"failed",message:"Copy stopped. Source volume retained.",dismissible:true},
        {id:"active",title:"Update immich",status:"running",message:"Waiting for the new pod",progress:null},
        {id:"complete",kind:"self-data-handoff",title:"Move Homestead data",status:"succeeded",progress:100,dismissible:true},
        {id:"recovered",kind:"self-data-handoff",title:"Recovered data move",status:"cancelled",dismissible:true},
        {id:"receipt",title:"Storage recovery",status:"succeeded",progress:100,dismissible:false}
      ]; renderOperations(); jobsDialog();
    },theme);
    assert.match(await page.locator('.dialog-master-content').innerText(),/Move media/);
    await page.locator('.dialog-master-nav button').filter({hasText:'Update immich'}).click();
    assert.equal(await page.locator('.dialog-master-content [role="progressbar"]').count(),0);
    assert.equal(await page.locator('.dialog-master-content details').count(),0,'Log and Dismiss are buttons, not a collapse');
    const detail=page.locator('.dialog-master-nav details');await detail.locator('summary').click();
    await page.evaluate(()=>renderOperations());
    assert.equal(await detail.getAttribute('open'),'');
    assert.match(await page.locator('.dialog-master-content').innerText(),/Update immich/);
    await detail.locator('summary').click();await page.evaluate(()=>renderOperations());
    assert.equal(await detail.getAttribute('open'),null);
    await page.evaluate(()=>{STATE.operationsStale=true;renderOperations();});
    assert.equal(await page.getByText('Connection lost',{exact:true}).isVisible(),true);
    assert.equal(await page.locator('#mbody').evaluate(el=>el.scrollWidth<=el.clientWidth+1),true);
    await page.screenshot({path:`${output}/jobs-${theme}-${width}.png`});
    await page.locator('#jobsClearCompleted').click();
    assert.deepEqual(await page.evaluate(()=>dismissed),['failed','complete','recovered']);
    assert.deepEqual(await page.evaluate(()=>STATE.data.operations.map(x=>x.id)),['active','receipt']);
    assert.equal(await page.locator('#jobsClearCompleted').isVisible(),false);
    await page.evaluate(()=>{STATE.data.operations=[];renderOperations();});
    assert.equal(await page.getByText('No jobs.',{exact:true}).isVisible(),true);
    await page.evaluate(()=>modal("Updating containers", batchUpdateMarkup(
      [{ns:"lab",name:"plex"},{ns:"lab",name:"immich"}],
      {[rolloutKey({ns:"lab",name:"plex"})]:{phase:"ready",ready:1,desired:1},[rolloutKey({ns:"lab",name:"immich"})]:{phase:"failed",pods:[{blocked:"Image could not be pulled"}]}}, [],false,true)));
    assert.equal(await page.getByText('Image could not be pulled',{exact:false}).isVisible(),true);
    assert.equal(await page.locator('#mbody .pill').filter({hasText:'queue stopped'}).isVisible(),true);
    assert.equal(await page.locator('#mbody details').getAttribute('open'),null);
    assert.equal(await page.getByRole('button',{name:'Close queue',exact:true}).isVisible(),true);
    assert.equal(await page.getByText('Closing stops the remaining queue. Updates already started continue in Jobs.',{exact:false}).isVisible(),true);
    await page.screenshot({path:`${output}/updates-${theme}-${width}.png`});
    await page.evaluate(()=>modal('Field disclosure',stepper('test',[
      {title:'Basics',html:'<input id="first" value="unchanged">'},
      {title:'Advanced',html:UI.more('Optional settings','<input id="hiddenField" required>')}
    ],UI.button('Review changes',''),{always:true})));
    await page.evaluate(()=>document.querySelector('#hiddenField').checkValidity());
    assert.equal(await page.locator('#hiddenField').isVisible(),true);
    assert.equal(await page.locator('#hiddenField').evaluate(el=>el===document.activeElement),true);
    if(width>640){await page.locator('#test-tab-1').focus();await page.keyboard.press('Home');}
    else await page.locator('#test .dialog-section-picker select').selectOption('0');
    assert.equal(await page.locator('#first').inputValue(),'unchanged');
    assert.equal(await page.locator('#first').isVisible(),true);
    // A migrated footer must retain its visibility gate and original Cancel action.
    await page.evaluate(async()=>{closeModal();await viewResources();await resOpen('lab','sample');});
    assert.equal(await page.locator('#res_save_row').isVisible(),false);
    await page.evaluate(()=>resEdit());
    assert.equal(await page.locator('#res_save_row').isVisible(),true);
    assert.equal(await page.locator('#res_yaml').getAttribute('readonly'),null);
    assert.equal(await page.locator('#mbody').evaluate(el=>el.scrollWidth<=el.clientWidth+1),true);
    await page.locator('#res_save_row').getByRole('button',{name:'Cancel',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#res_save_row')?.hidden);
    // Externally managed guides use the same desktop/phone navigation renderer.
    await page.evaluate(()=>{
      closeModal();setupCloudflareStage(0);modal('Cloudflare guide',setupCloudflareHtml({steps:{}}));
      window.setupCloudflareOpen=value=>{window.guideSelection=value;};
    });
    if(width>640) await page.locator('#mbody .stepper-chip').nth(2).click();
    else await page.locator('#cloudflare-guide_section').selectOption('2');
    assert.equal(await page.evaluate(()=>guideSelection),2);
    assert.deepEqual(errors,[]);
    console.log(`Dialog standard passed: ${width}px ${theme}`);await context.close();
  }
} finally {await browser?.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
