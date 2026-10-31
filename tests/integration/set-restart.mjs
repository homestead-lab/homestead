// Starting a StatefulSet's stuck pod on another host (#441), in the demo on a
// k3s cluster with one host down: the waiting set offers the button, the review
// keeps Start off until the set's name is typed, and the run follows its job.
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {openAuditPage} from '../../scripts/audit_ready.mjs';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4173',out=process.env.SET_RESTART_SCREENSHOTS || 'release-assets/set-restart';
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
try{for(const width of [1440,390])for(const theme of ['dark','light']){
 const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await openAuditPage(page, `${base}/?demo=1&demo-scenario=critical&platform=k3s`);
 await page.evaluate(()=>go('nodes'));await page.waitForFunction(()=>STATE.data.nodes?.some(n=>n.status==='NotReady'));
 const button=page.getByRole('button',{name:'Start on another host…'});
 await page.evaluate(t=>{document.documentElement.dataset.theme=t;STATE.platform.harvester=false;return nodeIfDown('harvester-node2');},theme);
 await page.locator('.impact-list').first().waitFor();
 assert.equal(await button.count(),0,'a host that is Ready offers no button');
 await page.evaluate(()=>{STATE.platform.harvester=true;return nodeIfDown('harvester-node3');});
 await page.locator('.impact-list').first().waitFor();
 assert.equal(await button.count(),0,'on Harvester there is no button');
 assert.match(await page.locator('#mbody').innerText(),/Harvester restarts these itself/,'Harvester says it restarts these itself');
 await page.evaluate(()=>{STATE.platform.harvester=false;return nodeIfDown('harvester-node3');});
 await button.waitFor();assert.equal(await button.count(),1,'only the waiting StatefulSet on the down host offers the button');
 assert.match(await button.evaluate(b=>{let e=b;while(e&&!/postgres/.test(e.textContent))e=e.parentElement;return e?e.querySelector('strong,b,.name,span')?.textContent+' '+e.textContent:'';}),/postgres/,'the button belongs to the data/postgres row');
 assert.equal(await button.evaluate(b=>b.closest('.impact-list')?.querySelectorAll('button').length&&[...b.closest('.impact-list').children].filter(r=>r.contains(b)&&/paperless|frigate/.test(r.textContent)).length),0,'the button is not in another row');
 await button.click();
 const run=page.locator('#sr_run');await run.waitFor();
 assert.equal(await run.isDisabled(),true,'Start is off until the name is typed');
 await page.locator('#sr_confirm').fill('wrong');assert.equal(await run.isDisabled(),true,'a wrong name keeps Start off');
 await page.locator('#sr_confirm').fill('postgres');assert.equal(await run.isDisabled(),false,'the right name turns Start on');
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'the page never scrolls sideways');
 await page.screenshot({path:`${out}/review-${width}-${theme}.png`});
 await run.click();
 await page.locator('#srProgress').waitFor();
 await page.waitForFunction(()=>{const t=document.querySelector('#srProgress')?.textContent||'';return /postgres-0/.test(t)&&!/Starting…/.test(t);});
 await page.screenshot({path:`${out}/job-${width}-${theme}.png`});
 await page.waitForFunction(()=>/Ready on another host: postgres-0/.test(document.querySelector('#srProgress')?.textContent||''),null,{timeout:30000});
 await page.screenshot({path:`${out}/job-${width}-${theme}.png`});
 assert.deepEqual(errors,[]);await page.close();console.log(`Start on another host passed at ${width}px, ${theme}`);
}}finally{await browser.close();}
