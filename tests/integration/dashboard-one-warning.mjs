// The Dashboard shows one warning (#506): a degraded cluster, chart history
// that could not load and a layout that could not refresh are one UI.callout,
// with its icon and role="alert", not a banner and two warning boxes. Saved
// history that fails shows as the Over time card's error state.
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4173',out=process.env.DASH_WARNING_SCREENSHOTS || 'release-assets/dashboard-warning';
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
try{for(const width of [1440,390]){
 const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`${base}/?demo=1&demo-scenario=critical`);await page.locator('[data-widget="nodes"]').waitFor();
 await page.evaluate(async()=>{
  window.__loopTimer?.stop();const original=window.api;
  window.api=async(path,opts)=>{
   if(path==='/api/history'||path.startsWith('/api/history/long'))throw new Error('history store unreachable');
   if(path==='/api/auth/preferences/dashboard')throw new Error('preferences unreachable');
   return original(path,opts);
  };Dashboard.invalidate();await Dashboard.load();await viewDash();await historyPaint({fresh:true});
 });
 const views=page.locator('#views');
 await views.locator('.dash-alert').waitFor();
 assert.equal(await views.locator('.clusteralert, .note.warn').count(),0,'no hand-built banner or warning boxes');
 const alerts=views.locator('.ui-callout.warn, .ui-callout.bad').filter({hasNot:page.locator('#historyCard *')});
 assert.equal(await views.locator('.dash-alert .ui-callout').count(),1,'one callout');
 assert.equal(await alerts.evaluateAll(els=>els.filter(el=>!el.closest('#historyCard')).length),1,'the page shows one warning');
 const callout=views.locator('.dash-alert .ui-callout');
 assert.equal(await callout.getAttribute('role'),'alert');
 assert.equal(await callout.locator('.ui-callout-icon').count(),1);
 const said=await callout.innerText();
 assert.match(said,/Cluster needs attention/);assert.match(said,/history/i);assert.match(said,/layout/i);
 await page.locator('#historyCard .ui-error, #historyCard .history-error').first().waitFor();
 assert.equal(await page.locator('#historyCard .note.warn').count(),0,'the Over time card shows its own error state');
 await page.screenshot({path:`${out}/dashboard-warning-${width}.png`});
 await callout.getByRole('button',{name:'Review'}).click();await page.locator('#clusterHealth').waitFor();
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 assert.deepEqual(errors,[]);await page.close();console.log(`Dashboard one warning passed at ${width}px`);
}}finally{await browser.close();}
