// A snapshot opens in the file manager beside its live volume (#545): the
// snapshot pane offers no write action, and a file restores into the live
// folder after a review of what it replaces.
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {openAuditPage} from '../../scripts/audit_ready.mjs';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4173',out=process.env.SNAPSHOT_RESTORE_SCREENSHOTS || 'release-assets/snapshot-restore';
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
try{for(const width of [1440,390])for(const theme of ['dark','light']){
 const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await openAuditPage(page, `${base}/?demo=1`);
 await page.evaluate(t=>{document.documentElement.dataset.theme=t;window.__demoSnapshotV2=false;window.__demoSnapshotWaiting=false;go('storage');return snapshotFiles('pvc-demo-frigate','daily-snapshot');},theme);
 await page.getByRole('button',{name:'Prepare browser'}).click();
 const title=page.locator('#fm_pane0 .fm-snaptitle');await title.waitFor({timeout:15000});
 assert.match(await title.innerText(),/Snapshot · daily-snapshot · /,'the snapshot pane names the snapshot and when it was taken');
 await page.locator('#fm_pane0 .fm-row[data-name="config.yml"]').waitFor();
 assert.equal(await page.locator('#fm_pane0 [aria-label="New folder"], #fm_pane0 [aria-label="Upload files"]').count(),0,'the snapshot pane has no write tools');
 assert.equal(await page.locator('#fm_pane1 [aria-label="New folder"]').count(),1,'the live pane keeps its write tools');
 if(width<700)await page.locator('#fm_tab0').click();
 await page.locator('#fm_pane0 .fm-row[data-name="config.yml"] .fm-name').click();
 const actions=await page.locator('#fm_actions').innerText();
 for(const absent of ['Edit','Rename','Permissions','Delete','Move'])assert.doesNotMatch(actions,new RegExp(`\\b${absent}\\b`),`no ${absent} on the snapshot`);
 await page.getByRole('button',{name:'Restore to frigate-config'}).click();
 const panel=page.locator('#fm_panel');await panel.locator('.fm-restore-list b',{hasText:'Replaces'}).waitFor();
 const review=await panel.innerText();
 assert.match(review,/config\.yml/);assert.match(review,/frigate is running and using this volume/);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'the page never scrolls sideways');
 await page.screenshot({path:`${out}/review-${width}-${theme}.png`});
 await panel.getByRole('button',{name:'Restore 1 entry'}).click();
 await page.locator('#toast .tst',{hasText:'Restored 1 entry into frigate-config'}).waitFor();
 assert.equal(await panel.isHidden(),true,'the review closes once the restore runs');
 await page.locator('#fm .fm-foot button',{hasText:'Close'}).click();
 assert.deepEqual(errors,[]);await page.close();console.log(`Snapshot restore passed at ${width}px, ${theme}`);
}}finally{await browser.close();}
