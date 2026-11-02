// UI.table folds on a phone the way a stack table does (#506): the first cell
// is the card's title, short values sit two to a line with their label above,
// a long value takes the whole line, and an empty cell shows no lone label.
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4173',out=process.env.UI_TABLE_SCREENSHOTS || 'release-assets/ui-table';
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
try{for(const width of [390,1440]){
 const page=await browser.newPage({viewport:{width,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`${base}/?demo=1`);await page.locator('[data-widget="nodes"]').waitFor();
 await page.evaluate(()=>{
  window.__loopTimer?.stop();
  const host=document.createElement('div');host.id='uiTableCheck';host.style.cssText='padding:16px';
  host.innerHTML=UI.table([{label:'Host'},{label:'State'},{label:'Memory'},{label:'Note'},{label:'Pool'}],[
   ['host-a','Ready','12 GiB','Its last restart was asked for by the scheduled update window',''],
   ['host-b','Cordoned','8 GiB','Kept out of scheduling until it is checked by hand','fast']]);
  document.querySelector('main, #main, body').prepend(host);enhanceActions(host);
 });
 const cells=await page.locator('#uiTableCheck tbody tr:first-child td').evaluateAll(tds=>tds.map(td=>{
  const r=td.getBoundingClientRect();return {label:td.dataset.label,top:Math.round(r.top),left:Math.round(r.left),width:Math.round(r.width),shown:getComputedStyle(td).display!=='none'&&r.height>0};
 }));
 const by=Object.fromEntries(cells.map(c=>[c.label,c])),row=await page.locator('#uiTableCheck tbody tr:first-child').evaluate(tr=>Math.round(tr.getBoundingClientRect().width));
 if(width===390){
  assert.equal(by.State.top,by.Memory.top,`short values share a line: ${JSON.stringify(cells)}`);
  assert.ok(by.Memory.left>by.State.left,'Memory sits beside State');
  assert.ok(by.Note.width>row*0.8,`a long value takes the whole line: ${JSON.stringify(by.Note)} of ${row}`);
  assert.equal(by.Pool.shown,false,'an empty cell is not shown with a lone label');
 }else{
  assert.equal(new Set(cells.map(c=>c.top)).size,1,'on a desktop the row is one line of cells');
 }
 await page.locator('#uiTableCheck').screenshot({path:`${out}/ui-table-${width}.png`});
 assert.deepEqual(errors,[]);await page.close();console.log(`UI.table phone layout passed at ${width}px`);
}}finally{await browser.close();}
