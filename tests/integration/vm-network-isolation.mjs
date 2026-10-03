// VM network isolation controls at desktop and phone widths, without a cluster.
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const web = fileURLToPath(new URL("../../web/", import.meta.url));
const output = process.env.VM_ISOLATION_SCREENSHOTS || "release-assets/vm-isolation";
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
  for (const width of [1440, 375]) {
    const context = await browser.newContext({viewport:{width,height:1000}});
    const page = await context.newPage(), errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/*',route=>route.request().url().startsWith(base)?route.continue():route.abort());
    await page.goto(`${base}/?demo=1`);
    await page.waitForFunction(()=>typeof vmNew==='function' && ME && document.querySelector('#gate').classList.contains('hidden'));
    await page.evaluate(async()=>{await vmNew();stepGo('v_steps',2);});
    await page.locator('#v_isolated').check();
    assert.equal(await page.locator('#v_net').isDisabled(),true);
    assert.equal(await page.locator('#v_nic_model').isDisabled(),true);
    assert.equal(await page.locator('#v_service').isDisabled(),true);
    await page.evaluate(()=>{stepGo('v_steps',3);stepGo('v_steps',2);});
    assert.equal(await page.locator('#v_isolated').isChecked(),true);
    await page.screenshot({path:`${output}/create-${width}.png`});
    assert.equal(await page.evaluate(()=>document.querySelector('#mbody').scrollWidth>document.querySelector('#mbody').clientWidth+1),false);
    await page.evaluate(async()=>{
      window.isolationReviews=[];
      window.vmCreateReview=async(body,network)=>isolationReviews.push({body,network});
      document.querySelector('#v_name').value='offline';document.querySelector('#v_pass').value='test-only-password';
      await doVmCreate();
    });
    const created=await page.evaluate(()=>isolationReviews[0]);
    assert.equal(created.body.isolated,true);assert.equal('network' in created.body,false);
    assert.equal('nic_model' in created.body,false);assert.equal(created.network.serviceMode,'');
    await page.evaluate(async()=>{
      closeModal();
      const original=window.api;
      window.isolatedSaved=false;
      window.api=async(url,options)=>{
        const result=await original(url,options);
        if(url.startsWith('/api/vm?')) return {...result,isolated:isolatedSaved,implicit_network:!isolatedSaved,nics:[]};
        return result;
      };
      window.vmEditReview=async body=>isolationReviews.push({body});
      await vmEdit('default','home-assistant-os');
    });
    await page.locator('#ve_section').isVisible() ? await page.locator('#ve_section').selectOption('network') : await page.locator('#ve-tab-network').click();
    assert.equal(await page.locator('#ve-tab-network').getAttribute('aria-selected'), 'true');
    assert.match(await page.locator('#ve-pane-network').innerText(),/KubeVirt currently adds its default/);
    await page.locator('#ve_isolated').check();
    // The VM editor uses the same controller as other forms: moving between
    // sections preserves the DOM and error focus reveals the correct section.
    await page.evaluate(()=>{window.savedIsolationField=document.querySelector('#ve_isolated');UI.selectSection('ve','general');});
    await page.locator('#ve_desc').fill('Retained across sections');
    await page.evaluate(()=>revealDialogField(document.querySelector('#ve_isolated')));
    assert.equal(await page.locator('#ve_section').inputValue(),'network');
    assert.equal(await page.locator('#ve_isolated').isChecked(),true);
    assert.equal(await page.evaluate(()=>savedIsolationField===document.querySelector('#ve_isolated')),true);
    assert.equal(await page.locator('#ve_desc').inputValue(),'Retained across sections');
    assert.equal(await page.locator('#ve > .ui-actions').count(),1);
    if(width>640){
      await page.locator('#ve-tab-network').focus();await page.keyboard.press('Home');
      assert.equal(await page.locator('#ve-tab-general').getAttribute('tabindex'),'0');
      await page.locator('#ve-tab-network').click();
    }
    const add=page.getByRole('button',{name:'Interface',exact:false});
    assert.equal(await add.isDisabled(),true);
    await page.evaluate(()=>vmAddNic());assert.equal(await page.locator('#ve_nics tr.vn-add').count(),0);
    await page.screenshot({path:`${output}/edit-${width}.png`});
    assert.equal(await page.evaluate(()=>document.querySelector('#mbody').scrollWidth>document.querySelector('#mbody').clientWidth+1),false);
    await page.getByRole('button',{name:'Review changes',exact:true}).click();
    const edited=await page.evaluate(()=>isolationReviews.at(-1).body);
    assert.equal(edited.isolated,true);assert.equal(edited.nics.length,0);assert.equal(edited.add_nics.length,0);
    await page.evaluate(async()=>{closeModal();isolatedSaved=true;await vmEdit('default','home-assistant-os');});
    assert.equal(await page.locator('#ve_isolated').isChecked(),true);
    await page.locator('#ve_section').isVisible() ? await page.locator('#ve_section').selectOption('network') : await page.locator('#ve-tab-network').click();
    assert.equal(await page.locator('#ve-tab-network').getAttribute('aria-selected'), 'true');
    assert.equal(await page.getByRole('button',{name:'Interface',exact:false}).isDisabled(),true);
    await page.locator('#ve_isolated').uncheck();
    assert.equal(await page.getByRole('button',{name:'Interface',exact:false}).isDisabled(),false);
    await page.getByRole('button',{name:'Interface',exact:false}).click();
    assert.equal(await page.locator('#ve_nics tr.vn-add').count(),1);
    assert.deepEqual(errors,[]);
    await context.close();
  }
  console.log('VM isolation controls passed at 1440px and 375px; screenshots: '+output);
} finally {
  if(browser) await browser.close();
  await new Promise(resolve=>server.close(resolve));
}
