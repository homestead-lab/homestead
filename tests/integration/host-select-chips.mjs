// Selecting several of a host's apps (#589): the chips stay chips and light
// up when picked, and Move selected opens one review for them.
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {openAuditPage} from '../../scripts/audit_ready.mjs';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4173',out=process.env.HOST_SELECT_SCREENSHOTS || 'release-assets/host-select';
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
// WCAG relative luminance contrast of two computed CSS colours.
const contrast=(a,b)=>{const lum=c=>{const [r,g,bl]=c.match(/[\d.]+/g).slice(0,3).map(Number).map(v=>{v/=255;return v<=.03928?v/12.92:((v+.055)/1.055)**2.4;});return .2126*r+.7152*g+.0722*bl;};
  const [x,y]=[lum(a),lum(b)].sort((p,q)=>q-p);return (x+.05)/(y+.05);};
try{for(const width of [1440,390])for(const theme of ['dark','light']){
 const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await openAuditPage(page, `${base}/?demo=1`);
 await page.evaluate(t=>{document.documentElement.dataset.theme=t;go('nodes',{params:{node:'harvester-node1'}});},theme);
 await page.getByRole('tab',{name:/Apps and VMs/}).first().click();
 const card=page.locator('#nodeChipsCard');await card.waitFor();
 await card.getByRole('button',{name:'Select',exact:true}).click();
 assert.equal(await card.locator('input[type=checkbox]').count(),0,'select mode keeps the chips, no tick boxes');
 const chip=name=>card.locator('button.tag.movable',{hasText:name});
 await chip('home-assistant').click();await chip('mosquitto').click();
 assert.equal(await chip('home-assistant').getAttribute('aria-pressed'),'true');
 assert.equal(await chip('mosquitto').getAttribute('aria-pressed'),'true');
 assert.equal(await chip('homestead-smb').getAttribute('aria-pressed'),'false');
 assert.equal(await page.evaluate(()=>document.activeElement?.dataset.id),'lab/mosquitto','focus stays on the chip just toggled');
 const look=async name=>chip(name).evaluate(e=>{const s=getComputedStyle(e);return {bg:s.backgroundColor,fg:s.color};});
 const on=await look('home-assistant'),off=await look('homestead-smb');
 assert.notEqual(on.bg,off.bg,'a picked chip lights up');
 assert.ok(contrast(on.bg,on.fg)>=4.5,`a lit chip's text is readable (${contrast(on.bg,on.fg).toFixed(2)}:1)`);
 await chip('home-assistant').hover();
 assert.equal((await look('home-assistant')).bg,on.bg,'hovering a lit chip keeps it lit');
 assert.match(await card.innerText(),/2 selected/);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'the page never scrolls sideways');
 await page.screenshot({path:`${out}/selected-${width}-${theme}.png`});
 await page.keyboard.press('Escape');
 assert.equal(await card.locator('[aria-pressed]').count(),0,'Escape ends select mode');
 assert.equal(await page.evaluate(()=>document.activeElement?.textContent.trim()),'Select','focus lands on Select after Escape');
 await card.getByRole('button',{name:'Select',exact:true}).click();
 await chip('home-assistant').click();await chip('mosquitto').click();
 await card.getByRole('button',{name:'Move selected…'}).click();
 await page.locator('#mbody').getByText('mosquitto').first().waitFor();
 assert.match(await page.locator('#mbody').innerText(),/home-assistant/,'one review for every picked app');
 assert.deepEqual(errors,[]);await page.close();console.log(`Host select chips passed at ${width}px, ${theme}`);
}}finally{await browser.close();}
