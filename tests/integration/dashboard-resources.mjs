import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4176',out=process.env.RESOURCE_WIDGET_SCREENSHOTS || 'release-assets/node-health-review';
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
try{for(const theme of ['dark','light']){
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`${base}/?demo=1`);await page.locator('[data-widget="nodes"]').waitFor();
 await page.evaluate(async theme=>{
  clearInterval(window.__loopTimer);SET.theme=theme;applySettings();const original=window.api;
  window.resourceFixture={containers:Array.from({length:12},(_,i)=>({name:['bazarr','homarr','nginx-proxy','overseerr','pihole','plex','prowlarr','qbittorrent','radarr','sonarr','tdarr','watchtower'][i],ns:'apps',desired:i===10?0:1,ready:i===9?0:1,cpu:i===8?null:i*.023,mem_mb:i===8?null:60+i*38})),vms:Array.from({length:8},(_,i)=>({name:`vm-${i+1}`,ns:'lab',status:i===2?'Stopped':i===3?'Starting':'Running',usage:i===4?null:{cpu_pct:i*3.4,mem:1024**3*(i+1)}}))};
  window.api=(path,opts)=>path==='/api/workloads'?Promise.resolve([...resourceFixture.containers,{name:'platform-helper',platform:true}]):path==='/api/vms'?Promise.resolve(resourceFixture.vms):original(path,opts);
 },theme);
 await page.getByRole('button',{name:'Edit dashboard',exact:true}).click();
 for(const [id,title] of [['containers','Containers'],['vms','Virtual machines']]){
  await page.getByRole('button',{name:`Add ${title}`,exact:true}).click();
  await page.getByLabel('Widget width',{exact:true}).selectOption('4');await page.getByLabel('Widget height',{exact:true}).selectOption('240');
 }
 await page.getByRole('button',{name:'Save layout',exact:true}).click();await page.getByRole('button',{name:'Edit dashboard',exact:true}).waitFor();
 await page.waitForFunction(()=>document.querySelectorAll('[data-widget="containers"] tbody tr').length===12);
 await page.addStyleTag({content:'#toast,#bottombar,header.top{visibility:hidden!important}'});
 for(const [id,count] of [['containers',12],['vms',8]]){
  const card=page.locator(`[data-widget="${id}"]`),scroll=card.locator('.dashboard-resource-scroll');
  assert.equal(await card.locator('tbody tr').count(),count);assert.ok((await card.boundingBox()).height<=260,'short cards stay shallow');
  assert.equal(await scroll.evaluate(el=>el.scrollHeight>el.clientHeight),true,'rows scroll within short cards');
  await scroll.evaluate(el=>el.scrollTop=45);await page.evaluate(()=>HealthInsights.load(true));assert.equal(await scroll.evaluate(el=>el.scrollTop),45,'refresh keeps reading position');
  await scroll.evaluate(el=>el.scrollTop=0);await card.screenshot({path:`${out}/${id}-${theme}-narrow.png`});
 }
 assert.equal(await page.locator('[data-widget="containers"] tr').filter({hasText:'radarr'}).locator('td').nth(2).textContent(),'—');
 assert.equal(await page.locator('[data-widget="containers"] tr').filter({hasText:'tdarr'}).locator('td').nth(2).textContent(),'—');
 // Width and height are saved to the account, including after reopening the editor.
 await page.getByRole('button',{name:'Edit dashboard',exact:true}).click();
 for(const [id,title] of [['containers','Containers'],['vms','Virtual machines']]){
  await page.getByRole('button',{name:`Settings for ${title}`,exact:true}).click();assert.equal(await page.getByLabel('Widget height',{exact:true}).inputValue(),'240');await page.getByLabel('Widget width',{exact:true}).selectOption('12');
 }
 await page.getByRole('button',{name:'Save layout',exact:true}).click();await page.getByRole('button',{name:'Edit dashboard',exact:true}).waitFor();
 for(const width of [1440,390,320]){
  await page.setViewportSize({width,height:1000});
  for(const id of ['containers','vms']){const card=page.locator(`[data-widget="${id}"]`);await card.screenshot({path:`${out}/${id}-${theme}-${width}.png`});assert.equal(await card.evaluate(el=>el.scrollWidth<=el.clientWidth+1),true);assert.equal(await card.locator('table').evaluateAll(els=>els.every(el=>el.scrollWidth<=el.clientWidth+1)),true);}
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
  if(width===1440){const tables=await page.locator('[data-widget="containers"] table').evaluateAll(els=>els.map(el=>({x:el.getBoundingClientRect().x,y:el.getBoundingClientRect().y})));assert.ok(tables[1].x>tables[0].x && Math.abs(tables[1].y-tables[0].y)<1,'wide lists use parallel columns');}
 }
 await page.locator('[data-widget="vms"]').getByRole('button',{name:'View all virtual machines',exact:true}).click();await page.waitForFunction(()=>STATE.view==='vms');
 assert.deepEqual(errors,[]);console.log(`Compact container/VM widgets passed (${theme})`);await page.close();
}}finally{await browser.close();}
