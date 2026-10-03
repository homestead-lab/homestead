import assert from "node:assert/strict";
import {chromium} from "playwright";
import {mkdir} from "node:fs/promises";
const base=process.env.HOMESTEAD_URL || "http://127.0.0.1:4176",out=process.env.ALERT_SCREENSHOTS || "release-assets/alert-review";await mkdir(out,{recursive:true});
const browser=await chromium.launch({headless:true});
try{for(const width of [1440,390,320])for(const theme of ["dark","light"]){
 const context=await browser.newContext({viewport:{width,height:1000}}),page=await context.newPage(),errors=[];page.on("pageerror",e=>errors.push(e.message));
 await page.goto(`${base}/?demo=1&demo-scenario=incidents`);await page.waitForSelector(".dashboard-widget");
 await page.evaluate(async theme=>{SET.theme=theme;applySettings();await pwaAlertsDialog();},theme);
 await page.waitForSelector("#pwaAlertsDialog .ui-insight-row");assert.equal(await page.locator('#pwaAlertsDialog .ui-insight-row').count(),2);
 await page.getByRole('button',{name:'Acknowledge',exact:true}).first().click();await page.waitForFunction(()=>document.querySelector('#pwaAlertsDialog details')?.textContent.includes('24 reallocated'));
 assert.equal(await page.locator('#pwaAlertsDialog > .ui-insight-list .ui-insight-row').count(),1);
 await page.locator('#pwaAlertsDialog summary').click();await page.waitForTimeout(180);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
 await page.locator('.modalbox').screenshot({path:`${out}/acknowledged-${theme}-${width}.png`});
 await page.getByRole('button',{name:'Undo',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('#pwaAlertsDialog > .ui-insight-list .ui-insight-row').length===2);
 await page.getByRole('button',{name:'Close',exact:true}).click();await page.evaluate(()=>{settingsTab('device');go('settings');});
 await page.getByRole('button',{name:'Review alerts',exact:true}).waitFor();await page.getByRole('button',{name:'Review alerts',exact:true}).click();
 await page.waitForSelector('#pwaAlertsDialog .ui-insight-row');await page.evaluate(()=>{stopAuthenticatedWork();});
 assert.equal(await page.evaluate(()=>PWA_ALERTS.report),null,'signout clears account alerts');
 assert.deepEqual(errors,[]);console.log(`Alert acknowledgement passed ${width}px ${theme}`);await context.close();
}}finally{await browser.close();}
