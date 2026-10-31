import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4176',out=process.env.NOTIFICATION_SCREENSHOTS || 'release-assets/combined-architecture';
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
try{for(const width of [1440,390,320])for(const theme of ['dark','light']){
 const page=await browser.newPage({viewport:{width,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`${base}/?demo=1`);await page.locator('[data-widget="nodes"]').waitFor();
 // The check owns its job and alert for the whole run, not just until the next poll (#892): the demo answers every
 // jobs read from window.__demoOps, and every alerts read (the poll, the Alerts dialog) is answered here, as the demo has no hook for it.
 // The image update check, which first runs 3.5 s after sign-in and adds the bell's container-update rows, runs now instead.
 await page.evaluate(async theme=>{clearInterval(window.__loopTimer);SET.theme=theme;SET.motion='off';applySettings();
  clearTimeout(window.__imageUpdateStart);clearInterval(window.__imageUpdateLoop);await loadImageUpdates(false,true);await PWA_ALERTS.request;
  const alerts={active:[{key:'disk',title:'Drive needs attention',body:'2 reallocated sectors',severity:'degraded'}]},real=window.api;
  window.api=(path,...rest)=>String(path).split('?')[0]==='/api/alerts'?Promise.resolve(structuredClone(alerts)):real(path,...rest);
  PWA_ALERTS.report=structuredClone(alerts);STATE.data.operations=window.__demoOps=[{id:'update',title:'Update media server',status:'running',message:'Downloading the replacement container image',progress:42}];paintBell()},theme);
 // A tick of everything the bell shows, as lands mid-check on a loaded machine (#892): the jobs and alerts polls and the
 // image update check. The bell still holds the check's job and alert, and no row moves, so no tap lands on another row.
 const tick=await page.evaluate(async()=>{const rows=()=>[...document.querySelectorAll('#bellPop button')].map(b=>b.textContent.trim().replace(/\s+/g,' '));const before=rows();
  await Promise.all([refreshOperations(true),refreshPwaAlerts(true),loadImageUpdates(false,true)]);paintBell();
  return {before,after:rows(),jobs:STATE.data.operations.map(x=>x.id),alerts:PWA_ALERTS.report.active.map(x=>x.key)};});
 assert.deepEqual({jobs:tick.jobs,alerts:tick.alerts},{jobs:['update'],alerts:['disk']});assert.ok(tick.before.some(r=>/container update/.test(r)),'the image check ran before the tick');assert.deepEqual(tick.after,tick.before,'a tick moves no row in the bell');
 const bell=page.locator('#bell>summary');await bell.click();
 if(width<=900){
  const content=page.locator('#notificationsDialogList');await content.waitFor();assert.equal(await page.locator('.actionmenu-portal .bellpop').count(),0);
  assert.match(await page.locator('#mtitle').textContent(),/Notifications/);
  const bounds=await page.locator('.modalbox').boundingBox();assert.ok(bounds.width>=width-32,'notifications use the phone width');
  await page.waitForFunction(()=>document.querySelector('#notificationsDialogList>button.warn')?.getBoundingClientRect().height>=43.9);
  await page.evaluate(()=>paintBell());assert.equal(await content.count(),1);
  await page.locator('.modalbox').screenshot({path:`${out}/notifications-${theme}-${width}.png`});
  await content.getByRole('button',{name:'1 active alert',exact:true}).click();await page.locator('#pwaAlertsDialog').getByText('Drive needs attention').waitFor();assert.equal(await content.count(),0);
  await page.locator('.ui-actions').getByRole('button',{name:'Close',exact:true}).click();await bell.click();await content.waitFor();
  await content.getByRole('button',{name:'All jobs · 1',exact:true}).click();await page.locator('#jobsDialogList article.jobitem[data-operation="update"]').waitFor();assert.equal(await content.count(),0);
  await page.evaluate(()=>closeModal());await bell.click();await content.waitFor();await page.keyboard.press('Escape');await page.locator('#modal').waitFor({state:'hidden'});
 }else{await page.locator('.actionmenu-portal .bellpop').waitFor();assert.equal(await page.locator('#notificationsDialogList').count(),0);await page.locator('.bellpop').getByRole('button',{name:'1 active alert',exact:true}).click();await page.locator('#pwaAlertsDialog').getByText('Drive needs attention').waitFor();}
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);assert.deepEqual(errors,[]);console.log(`Notifications navigation passed (${width}, ${theme})`);await page.close();
}}finally{await browser.close();}
