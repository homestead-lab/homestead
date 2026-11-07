// Exercise the real dialog, checkbox, click handler and api() with delayed HTTP
// responses. A long host inspection must never look like an ignored click.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {chromium} from 'playwright';

const base = process.env.HOMESTEAD_URL || 'http://127.0.0.1:4173';
const browser = await chromium.launch({headless:true});
const demo = await fs.readFile(new URL('../../web/js/demo.js', import.meta.url), 'utf8');
const screenshots = process.env.OPERATOR_ENABLE_SCREENSHOTS;
if (screenshots) await fs.mkdir(screenshots, {recursive:true});
try {
  for (const width of [1440, 375]) for (const theme of ['dark', 'light']) {
    const context = await browser.newContext({viewport:{width,height:900}}), page = await context.newPage();
    const errors = [], posts = [], pending = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {window.__operatorNativeFetch = window.fetch.bind(window);});
    await page.route('**/js/demo.js*', route => route.fulfill({contentType:'application/javascript', body:demo + `
      (()=>{const demoFetch=window.fetch;window.fetch=(url,opts)=>
        window.__operatorFixture && typeof url==='string' && url.startsWith('/api/cluster/components')
          ? window.__operatorNativeFetch(url,opts) : demoFetch(url,opts);})();
    `}));
    let managed = false;
    const report = () => ({components:[{id:'kubevirt',name:'KubeVirt',installed:'v1.9.0',newest:'v1.9.1',
      how:managed?'manifests':'manual',can_manage:!managed,behind:true,next:managed?'v1.9.1':''}]});
    await page.route(url => url.pathname.startsWith('/api/cluster/components'), async route => {
      if (route.request().method() === 'POST') {posts.push(route.request().postDataJSON());pending.push(route);}
      else await route.fulfill({contentType:'application/json',body:JSON.stringify(report())});
    });
    try {
      await page.goto(`${base}/?demo=1`, {waitUntil:'load'});
      await page.waitForSelector('.dashboard-widget');
      await page.evaluate(({theme,report}) => {
        window.__loopTimer?.stop(); document.documentElement.dataset.theme = theme;
        window.__operatorFixture = true; STATE.data.components = report;
        componentManageOperator('kubevirt');
      }, {theme,report:report()});
      const button = page.locator('#cmp_manage_go'), ack = page.locator('#cmp_manage_ack');
      assert.equal(await button.isDisabled(), true, 'consent is required');
      await ack.check(); assert.equal(await button.isEnabled(), true);
      await Promise.all([page.waitForRequest('**/api/cluster/components/manage-operator'), button.click()]);
      await page.waitForFunction(() => document.querySelector('#cmp_manage_go').textContent.includes('Checking'));
      assert.equal(await button.isDisabled(), true); assert.equal(await ack.isDisabled(), true);
      assert.match(await page.locator('#cmp_manage_status').innerText(), /Checking the installation and host configuration/);
      if (screenshots) await page.screenshot({path:`${screenshots}/${width}-${theme}-checking.png`});
      await page.evaluate(() => {componentManageOperatorGo('kubevirt');componentManageOperator('kubevirt');});
      assert.deepEqual(posts, [{component:'kubevirt',confirm:true}], 'duplicate clicks cannot submit again');
      await pending.shift().fulfill({status:400,contentType:'application/json',body:JSON.stringify({error:'Host inspection failed: <example>'})});
      await page.waitForFunction(() => document.querySelector('#cmp_manage_status').textContent.includes('Host inspection failed'));
      assert.equal(await button.isEnabled(), true); assert.equal(await ack.isEnabled(), true);
      assert.equal(await page.locator('#cmp_manage_status example').count(), 0, 'errors are escaped');
      assert.match(await page.locator('#cmp_manage_status').innerText(), /Could not enable upgrades/);
      if (screenshots) await page.screenshot({path:`${screenshots}/${width}-${theme}-error.png`});
      await Promise.all([page.waitForRequest('**/api/cluster/components/manage-operator'), button.click()]);
      managed = true;
      await pending.shift().fulfill({contentType:'application/json',body:JSON.stringify({ok:true,detail:'Homestead upgrades enabled for KubeVirt'})});
      await page.waitForFunction(() => document.querySelector('#modal').classList.contains('hidden') && STATE.data.components.components[0].how === 'manifests');
      assert.equal(posts.length, 2, 'one explicit retry follows the refusal');
      // A delayed response must not close or replace a different dialog.
      await page.evaluate(() => {STATE.data.components.components[0].can_manage=true;componentManageOperator('kubevirt');});
      await ack.check();
      await Promise.all([page.waitForRequest('**/api/cluster/components/manage-operator'), button.click()]);
      await page.waitForFunction(() => document.querySelector('#cmp_manage_go').disabled);
      await page.evaluate(() => {closeModal();modal('Another dialog','<p id="operatorOther">Keep this open</p>');});
      await pending.shift().fulfill({contentType:'application/json',body:JSON.stringify({ok:true,detail:'Homestead upgrades enabled for KubeVirt'})});
      await page.waitForFunction(() => !componentManagePending.size);
      assert.equal(await page.locator('#operatorOther').isVisible(), true);
      assert.deepEqual(errors, []);
      console.log(`Operator enable ${width}px ${theme}: consent, progress, duplicate guard, refusal, retry and late result passed.`);
    } finally {for (const route of pending) await route.abort().catch(()=>{});await context.close();}
  }
} finally {await browser.close();}
