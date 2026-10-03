import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4176',out=process.env.ARCHITECTURE_SCREENSHOTS || 'release-assets/combined-architecture';
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
try{for(const theme of ['dark','light']){
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],requests=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`${base}/?demo=1`);await page.locator('[data-widget="nodes"]').waitFor();
 await page.evaluate(async theme=>{
  clearInterval(window.__loopTimer);SET.theme=theme;SET.motion='off';applySettings();await fleetLoad();
  window.archRequests=[];const original=window.fetch;window.fetch=(input,opts)=>{archRequests.push(typeof input==='string'?input:input.url);return original(input,opts)};
  setFleetMode('all');await go('flow');
 },theme);
 await page.locator('.arch2').first().waitFor();
 assert.equal(await page.locator('.architecture-cluster').count(),2);
 const panels=await page.locator('.architecture-cluster').evaluateAll(els=>els.map(el=>el.getBoundingClientRect().toJSON()));assert.ok(panels[1].top>=panels[0].bottom,'cluster diagrams stack vertically');
 assert.ok(await page.evaluate(()=>archRequests.some(p=>p.includes('/api/fleet/all/flow'))));
 assert.match(await page.locator('#views').textContent(),/Architecture is incomplete/);
 assert.match(await page.locator('#views').textContent(),/DR site/);
 for(const id of ['a1f00d','b2c0de'])assert.ok(await page.locator(`.a2wl[data-cluster="${id}"]`).count()>0);
 const check=await page.evaluate(()=>{
  const graph=STATE.data.flow,ids=[...document.querySelectorAll('.arch2 [id]')].map(el=>el.id);
  const sameCluster=()=>STATE.data.alinks.every(([a,b])=>a.split('/')[0].slice(2)===b.split('/')[0].slice(2));
  const scopes=graph.vips.map(v=>({id:v.id,related:[...archRelated(v.id)],prefix:v.id.split('/')[0].slice(2)}));
  return {unique:ids.length===new Set(ids).size,sameCluster:sameCluster(),isolated:scopes.every(s=>s.related.every(id=>id.split('/')[0].slice(2)===s.prefix)),links:STATE.data.alinks.length};
 });
 assert.equal(check.unique,true);assert.equal(check.sameCluster,true);assert.equal(check.isolated,true);assert.ok(check.links>0);
 await page.evaluate(()=>{window.moveWorkload=(name,ns)=>{window.archMoved={name,ns,target:FLEET.target}}});
 const remote=page.locator('.a2wl[data-cluster="b2c0de"]').filter({has:page.locator('[title="Move to another host"]')}).first();await remote.hover();await remote.locator('.a2act').click();assert.equal(await page.evaluate(()=>archMoved.target),'b2c0de');
 assert.equal(await page.locator('.architecture-cluster').first().locator('.dimmed').count(),0,'other clusters remain readable while tracing');
 await page.mouse.move(0,0);await page.evaluate(()=>archHighlight(null));
 for(const width of [1440,390,320]){await page.setViewportSize({width,height:1000});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);await page.screenshot({path:`${out}/architecture-${theme}-${width}.png`,fullPage:true});}
 await page.evaluate(async()=>{setFleetMode('one');FLEET.target='';await viewFlow()});
 assert.equal(await page.locator('.arch2 [data-cluster]').count(),0);assert.equal(await page.evaluate(()=>STATE.data.flow.workloads.some(w=>w.id.includes('a1f00d/'))),false);
 assert.deepEqual(errors,[]);console.log(`Combined Architecture and cluster routing passed (${theme})`);await page.close();
}}finally{await browser.close();}
