import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4176',out=process.env.INSIGHTS_OUTPUT || 'release-assets/health-review';await mkdir(out,{recursive:true});
const browser=await chromium.launch({headless:true});
try {for(const width of [1440,390,320])for(const theme of ['dark','light']){
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`${base}/?demo=1&platform=harvester&demo-scenario=incidents`);await page.waitForSelector('.dashboard-widget');
 await page.evaluate(async theme=>{SET.theme=theme;applySettings();clearInterval(window.__loopTimer);await Dashboard.start();for(const w of Dashboard.defaults())Dashboard.remove(w.id);for(const id of ['health','workloads','backups','updates','jobs'])Dashboard.add(id);await Dashboard.save();},theme);
 await page.setViewportSize({width,height:1000});await page.waitForFunction(()=>document.querySelector('[data-insight="health"]')?.textContent.includes('scheduled'));
 const shot=async name=>{assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,`${name} overflows ${width}`);await page.waitForTimeout(200);
 if(await page.locator('#modal').isVisible()){
   const style=await page.addStyleTag({content:'.modalbox{max-height:none!important;overflow:visible!important}#modal{position:absolute!important;inset:0 0 auto!important;display:block!important}.ui-actions{position:static!important}'});
   await page.locator('.modalbox').screenshot({path:`${out}/${name}-${theme}-${width}.png`});await style.evaluate(el=>el.remove());
 }else await page.screenshot({path:`${out}/${name}-${theme}-${width}.png`,fullPage:true});};
 assert.equal(await page.locator('[data-insight]').count(),5);await shot('dashboard');
 await page.reload();await page.evaluate(theme=>{SET.theme=theme;applySettings();clearInterval(window.__loopTimer);},theme);await page.waitForSelector('[data-insight="health"]');assert.equal(await page.locator('[data-insight]').count(),5,'widgets persist');
 await page.evaluate(()=>go('cluster'));await page.waitForFunction(()=>document.querySelector('[data-insight="full"]')?.textContent.includes('scheduled'));await shot('cluster-health');
 await page.evaluate(async()=>{await imageUpdateCenter();});await page.waitForSelector('#updateStage');assert.equal(await page.locator('#updateStage').locator('xpath=ancestor::*[contains(@class,"ui-actions")]').count(),2);assert.match(await page.locator('#updateStage').textContent(),/Review selected/);
 await shot('image-updates');await page.evaluate(()=>closeModal());
 const state={ns:'lab',name:'immich',phase:'updating',generation:5,observed_generation:5,ready:1,desired:1,updated:0,pull:{state:'pulling',percent:42,total_bytes:1073741824,seconds:31,node:'host-1',image:'immich-server'},pods:[{name:'immich-new',node:'host-1',phase:'Pending',pull:{state:'pulling'}}]};
 await page.evaluate(s=>modal('Updating Immich',rolloutMarkup(s),true),state);assert.equal(await page.locator('#mbody [role="progressbar"]').getAttribute('aria-valuenow'),'42');await shot('image-download');
 await page.evaluate(s=>modal('Updating Immich',rolloutMarkup({...s,updated:1,pull:{state:'pulled'}}),true),state);assert.equal(await page.locator('#mbody [role="progressbar"]').getAttribute('aria-valuenow'),null,'old ready pods do not claim 100%');await shot('readiness');
 await page.evaluate(s=>modal('Updating containers',batchUpdateMarkup([{ns:'lab',name:'immich'},{ns:'lab',name:'plex'}],{[rolloutKey(s)]:s,[rolloutKey({ns:'lab',name:'plex'})]:{phase:'queued'}},[],false,true),true),state);await shot('update-queue');
 await page.evaluate(()=>{closeModal();STATE.data.operations=[{id:'done',title:'Update Immich',status:'succeeded',message:'All replacement pods ready',progress:100},{id:'failed',title:'Backup photos',status:'failed',message:'Backup destination unavailable'},{id:'cancelled',title:'Move Plex',status:'cancelled',message:'Cancelled before changes'}];jobsDialog();document.querySelector('.dialog-master-nav details').open=true;});
 assert.equal(await page.locator('.dialog-master-nav .ui-status-dot.ok').count(),1);assert.equal(await page.locator('.dialog-master-nav .ui-status-dot.bad').count(),1);assert.equal(await page.locator('.dialog-master-nav .ui-status-dot.neutral').count(),1);await shot('jobs');
 assert.deepEqual(errors,[]);console.log(`Insights and updates passed ${width}px ${theme}`);await context.close();
}}finally{await browser.close();}
