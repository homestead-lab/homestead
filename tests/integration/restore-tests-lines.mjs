// Data Protection's Restore tests table keeps each line of a cell on a line
// of its own at every width: a failed test's message sits under its time,
// and "Not tested" under the restores (#542).
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4173',out=process.env.RESTORE_LINES_SCREENSHOTS || 'release-assets/restore-lines';
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
try{for(const width of [1440,390]){
 const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`${base}/data-protection/restore-tests?demo=1`);await page.locator('.restore-table tbody tr').first().waitFor();
 // For each div straight inside a cell, everything before it in the cell ends above it.
 const lines=await page.locator('.restore-table td[data-label]>div').evaluateAll(divs=>divs.map(div=>{
  const range=document.createRange();range.setStart(div.parentElement,0);range.setEndBefore(div);
  const before=range.getBoundingClientRect(),top=div.getBoundingClientRect().top;
  return {cell:div.parentElement.dataset.label,text:div.textContent.trim().slice(0,40),gap:Math.round(top-before.bottom),empty:!range.toString().trim()};
 }).filter(l=>!l.empty));
 assert.ok(lines.some(l=>l.cell==='Last test'),'the demo has a failed test with its message');
 assert.ok(lines.some(l=>l.cell==='Restores'),'the demo has a restore with a "Not tested" line');
 for(const l of lines)assert.ok(l.gap>=-1,`at ${width}px "${l.text}" starts on a line of its own in ${l.cell}: ${JSON.stringify(l)}`);
 await page.locator('.restore-table').screenshot({path:`${out}/restore-tests-${width}.png`});
 assert.deepEqual(errors,[]);await page.close();console.log(`Restore test lines passed at ${width}px`);
}}finally{await browser.close();}
