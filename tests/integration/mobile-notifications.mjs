import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4176',out=process.env.NOTIFICATION_SCREENSHOTS || 'release-assets/combined-architecture';
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
try{for(const width of [1440,390,320])for(const theme of ['dark','light']){
 const page=await browser.newPage({viewport:{width,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`${base}/?demo=1`);await page.locator('[data-widget="nodes"]').waitFor();
 await page.evaluate(theme=>{clearInterval(window.__loopTimer);SET.theme=theme;SET.motion='off';applySettings();PWA_ALERTS.report={active:[{key:'disk',title:'Drive needs attention',body:'2 reallocated sectors',severity:'degraded'}]};STATE.data.operations=[{id:'update',title:'Update media server',status:'running',message:'Downloading the replacement container image',progress:42}];paintBell()},theme);
 const bell=page.locator('#bell>summary');await bell.click();
 if(width<=900){
  const content=page.locator('#notificationsDialogList');await content.waitFor();assert.equal(await page.locator('.actionmenu-portal .bellpop').count(),0);
  assert.match(await page.locator('#mtitle').textContent(),/Notifications/);
  const bounds=await page.locator('.modalbox').boundingBox();assert.ok(bounds.width>=width-32,'notifications use the phone width');
  await page.waitForFunction(()=>document.querySelector('#notificationsDialogList>button.danger')?.getBoundingClientRect().height>=43.9);
  await page.evaluate(()=>paintBell());assert.equal(await content.count(),1);
  await page.locator('.modalbox').screenshot({path:`${out}/notifications-${theme}-${width}.png`});
  await content.getByRole('button',{name:'1 active alert',exact:true}).click();await page.locator('#pwaAlertsDialog').waitFor();assert.equal(await content.count(),0);
  await page.locator('.ui-actions').getByRole('button',{name:'Close',exact:true}).click();await bell.click();await content.waitFor();
  await content.getByRole('button',{name:'All jobs · 1',exact:true}).click();await page.locator('#jobsDialogList').waitFor();assert.equal(await content.count(),0);
  await page.evaluate(()=>closeModal());await bell.click();await content.waitFor();await page.keyboard.press('Escape');await page.locator('#modal').waitFor({state:'hidden'});
 }else{await page.locator('.actionmenu-portal .bellpop').waitFor();assert.equal(await page.locator('#notificationsDialogList').count(),0);await page.locator('.bellpop').getByRole('button',{name:'1 active alert',exact:true}).click();await page.locator('#pwaAlertsDialog').waitFor();}
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);assert.deepEqual(errors,[]);console.log(`Notifications navigation passed (${width}, ${theme})`);await page.close();
}}finally{await browser.close();}
