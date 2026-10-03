// The Portal links widget lines every group's links up in the same columns:
// a group with fewer links than columns keeps them at the column width
// rather than stretching them across the row.
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4173',out=process.env.PORTAL_COLUMN_SCREENSHOTS || 'release-assets/portal-columns';
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
const links=[['Plex','Media'],['Radarr','Media'],['Sonarr','Media'],['Home Assistant','Home'],['Frigate','Home'],['n8n','Tools']]
 .map(([title,section],i)=>({id:`l${i}`,title,section,url:`https://example.com/${i}`}));
try{for(const width of [1440,1100,390]){
 const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`${base}/?demo=1`);await page.locator('[data-widget="nodes"]').waitFor();
 await page.evaluate(links=>{
  clearInterval(window.__loopTimer);const original=window.api;
  window.api=async(path,opts)=>{
   if(path==='/api/auth/preferences/dashboard')return {layout:{version:1,items:[{id:'portal',width:12,height:0}]},revision:'one'};
   if(path==='/api/portal')return {links};
   return original(path,opts);
  };Dashboard.invalidate();
 },links);
 await page.evaluate(()=>viewDash());
 await page.locator('#dashboardPortal .portal-tile').nth(links.length-1).waitFor();
 const groups=await page.locator('#dashboardPortal .portal-list').evaluateAll(lists=>lists.map(list=>
  [...list.querySelectorAll('.portal-tile')].map(tile=>Math.round(tile.getBoundingClientRect().width))));
 assert.deepEqual(groups.map(g=>g.length),[3,2,1],'three groups as given');
 const widths=groups.flat();
 assert.ok(Math.max(...widths)-Math.min(...widths)<=1,`at ${width}px every link has one width: ${JSON.stringify(groups)}`);
 await page.locator('[data-widget="portal"]').screenshot({path:`${out}/portal-${width}.png`});
 assert.deepEqual(errors,[]);await page.close();console.log(`Portal link columns passed at ${width}px`);
}}finally{await browser.close();}
