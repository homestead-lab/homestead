// IP addresses shows its figures as UI.stats, the way Networking › Overview
// does, with Scan now and Sync UniFi in the page header rather than squeezed
// into a figure (#506).
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4173',out=process.env.IPAM_FIGURES_SCREENSHOTS || 'release-assets/ipam-figures';
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
try{for(const width of [1440,390]){
 const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`${base}/networking?tab=ip&demo=1`);
 const pane=page.locator('.network-pane[data-pane="ip"]');await pane.locator('.ipam-table, table').first().waitFor();
 assert.equal(await pane.locator('.ui-stats .ui-stat').count(),4,'four figures, as UI.stats');
 assert.equal(await pane.locator('.ui-stats .btn').count(),0,'no action buttons inside the figures');
 assert.equal(await pane.locator('.ui-stats [style*="font-size"]').count(),0,'no hand-set figure sizes');
 // On a computer the header's actions are in the top bar (one bar); on a phone, on the page.
 const head=page.locator('#pagebar .page-actions:visible, #views .phead .page-actions:visible').last();
 assert.equal(await head.getByRole('button',{name:'Scan now'}).count(),1,'Scan now is in the page header');
 if(width===390){
  const rows=await head.evaluate(el=>{const tops=new Set([...el.children].filter(c=>c.offsetParent).map(c=>Math.round(c.getBoundingClientRect().top)));return tops.size;});
  assert.ok(rows<=2,`header controls take two lines at most: ${rows}`);
  const pair=await pane.locator('.ui-stats .ui-stat').evaluateAll(cards=>cards.slice(0,2).map(c=>Math.round(c.getBoundingClientRect().top)));
  assert.equal(pair[0],pair[1],'figures sit two to a row on a phone');
 }
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'the page never scrolls sideways');
 await page.screenshot({path:`${out}/ipam-${width}.png`});
 assert.deepEqual(errors,[]);await page.close();console.log(`IP addresses figures passed at ${width}px`);
}}finally{await browser.close();}
