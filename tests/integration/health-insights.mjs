import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {mkdir} from 'node:fs/promises';
import {captureDialog} from '../../scripts/audit_ready.mjs';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4176',out=process.env.INSIGHTS_OUTPUT || 'release-assets/health-review';await mkdir(out,{recursive:true});
const trackOperations=page=>page.evaluate(race=>{if(window.__opsInFlight!==undefined)return;const real=window.api;window.__opsInFlight=0;
 window.api=async function(path,...rest){const ops=/^\/api\/operations(\?|$)/.test(String(path));if(ops)window.__opsInFlight++;
  try{const reply=await real.call(this,path,...rest);if(ops&&race)await new Promise(r=>setTimeout(r,600));return reply;}finally{if(ops)window.__opsInFlight--;}};},process.env.INSIGHTS_RACE==='1');
const browser=await chromium.launch({headless:true});
try {for(const width of [1440,390,320])for(const theme of ['dark','light']){
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`${base}/?demo=1&platform=harvester&demo-scenario=incidents`);await trackOperations(page);await page.waitForSelector('.dashboard-widget');
 await page.evaluate(async theme=>{SET.theme=theme;applySettings();window.__loopTimer?.stop();await Dashboard.start();for(const w of Dashboard.defaults())Dashboard.remove(w.id);for(const id of ['health','workloads','backups','updates','jobs'])Dashboard.add(id);await Dashboard.save();},theme);
 await page.setViewportSize({width,height:1000});await page.waitForFunction(()=>document.querySelector('[data-insight="health"]')?.textContent.includes('scheduled'));
 const shot=async name=>{assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,`${name} overflows ${width}`);await page.waitForTimeout(200);
 if(await page.locator('#modal').isVisible()){
   await captureDialog(page,`${out}/${name}-${theme}-${width}.png`);
 }else await page.screenshot({path:`${out}/${name}-${theme}-${width}.png`,fullPage:true});};
 assert.equal(await page.locator('[data-insight]').count(),5);await shot('dashboard');
 await page.reload();await trackOperations(page);await page.evaluate(theme=>{SET.theme=theme;applySettings();window.__loopTimer?.stop();},theme);await page.waitForSelector('[data-insight="health"]');assert.equal(await page.locator('[data-insight]').count(),5,'widgets persist');
 await page.evaluate(()=>go('cluster'));await page.waitForFunction(()=>document.querySelector('[data-insight="full"]')?.textContent.includes('scheduled'));await shot('cluster-health');
 await page.evaluate(async()=>{await imageUpdateCenter();});await page.waitForSelector('#updateStage');assert.equal(await page.locator('#updateStage').locator('xpath=ancestor::*[contains(@class,"ui-actions")]').count(),2);assert.match(await page.locator('#updateStage').textContent(),/Review selected/);
 await shot('image-updates');await page.evaluate(()=>closeModal());
 const state={ns:'lab',name:'immich',phase:'updating',generation:5,observed_generation:5,ready:1,desired:1,updated:0,pull:{state:'pulling',percent:42,total_bytes:1073741824,seconds:31,node:'host-1',image:'immich-server'},pods:[{name:'immich-new',node:'host-1',phase:'Pending',pull:{state:'pulling'}}]};
 await page.evaluate(s=>modal('Updating Immich',rolloutMarkup(s),true),state);assert.equal(await page.locator('#mbody [role="progressbar"]').getAttribute('aria-valuenow'),'42');await shot('image-download');await page.evaluate(s=>{document.querySelector('#mbody details').open=true;paintRollout(document.querySelector('#mbody'),rolloutMarkup(s));},state);assert.equal(await page.locator('#mbody details').getAttribute('open'),'','polling keeps details open');
 await page.evaluate(s=>modal('Updating Immich',rolloutMarkup({...s,updated:1,pull:{state:'pulled'}}),true),state);assert.equal(await page.locator('#mbody [role="progressbar"]').getAttribute('aria-valuenow'),null,'old ready pods do not claim 100%');await shot('readiness');
 await page.evaluate(s=>modal('Updating containers',batchUpdateMarkup([{ns:'lab',name:'immich'},{ns:'lab',name:'plex'}],{[rolloutKey(s)]:s,[rolloutKey({ns:'lab',name:'plex'})]:{phase:'queued'}},[],false,true),true),state);await shot('update-queue');
 // A poll already in flight is not stopped by clearTimeout: its reply would repaint the jobs dialog with the demo's own jobs (#1350).
 // INSIGHTS_RACE=1: a poll with a slow reply is started just before the seeding. INSIGHTS_RACE=gap: a poll timer is pending when it starts.
 if(process.env.INSIGHTS_RACE==='1')await page.evaluate(()=>{window.refreshOperations(true);});
 if(process.env.INSIGHTS_RACE==='gap')await page.evaluate(()=>{window.__operationTimer=setTimeout(window.refreshOperations,150);});
 // One step, so that nothing starts a poll between the wait and the seeding: no new poll (timer cleared), none open (waited for),
 // the reply of the one that was open has painted (a turn of the event loop), and the timer it set is cleared again.
 await page.evaluate(async()=>{closeModal();clearTimeout(window.__operationTimer);
  const give=Date.now()+5000;while(window.__opsInFlight>0&&Date.now()<give)await new Promise(r=>setTimeout(r,10));
  await new Promise(r=>setTimeout(r,0));clearTimeout(window.__operationTimer);
  if(window.__opsInFlight>0)throw new Error('an operations poll is still open');
  STATE.data.operations=[{id:'done',title:'Update Immich',status:'succeeded',message:'All replacement pods ready',progress:100},{id:'failed',title:'Backup photos',status:'failed',message:'Backup destination unavailable'},{id:'cancelled',title:'Move Plex',status:'cancelled',message:'Cancelled before changes'}];jobsDialog();document.querySelector('.dialog-master-nav details').open=true;});
 assert.equal(await page.locator('.dialog-master-nav .ui-status-dot.ok').count(),1);assert.equal(await page.locator('.dialog-master-nav .ui-status-dot.bad').count(),1);assert.equal(await page.locator('.dialog-master-nav .ui-status-dot.neutral').count(),1);await shot('jobs');
 assert.deepEqual(errors,[]);console.log(`Insights and updates passed ${width}px ${theme}`);await context.close();
}}finally{await browser.close();}
