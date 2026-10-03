import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4176',out=process.env.DEPLOY_FORM_SCREENSHOTS || 'release-assets/deploy-form';
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
try{for(const width of [1440,390,320])for(const theme of ['dark','light']){
 const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`${base}/?demo=1`);await page.locator('[data-widget="compute"]').waitFor();
 await page.evaluate(async theme=>{clearInterval(window.__loopTimer);SET.theme=theme;applySettings();await go('deploy');},theme);
 const select=async key=>width>900?page.getByRole('tab',{name:({basics:'Basics',hardware:'Hardware and access',environment:'Environment values',storage:'Storage',address:'Address',containers:'Additional containers',summary:'Summary'})[key],exact:true}).click():page.locator('#containerDeploy_section').selectOption(key);
 await page.locator('#d_container_name').fill('web');await page.locator('#d_workload_name').fill('web-stack');await page.locator('#d_image').fill('nginx:alpine');
 assert.equal(await page.locator('#containerDeploy>.stepper-foot').count(),1);
 assert.equal(await page.getByRole('button',{name:'Review deployment',exact:true}).isVisible(),false);
 await page.getByRole('button',{name:'Next',exact:true}).click();assert.equal(await page.locator('#d_cpu').isVisible(),true);await page.locator('#d_cpu').fill('125m');
 await select('environment');await page.getByRole('button',{name:'＋ add variable',exact:true}).click();await page.locator('#d_env .ek').last().fill('MODE');await page.locator('#d_env .ev').last().fill('production');
 await select('address');await page.locator('#d_net').selectOption('internal');
 await select('storage');assert.equal(await page.getByText('Choose a storage type',{exact:true}).isVisible(),true);
 await select('basics');assert.equal(await page.locator('#d_image').inputValue(),'nginx:alpine');
 await page.locator('#d_add_container button').first().click();assert.equal(await page.locator('#d_extra_containers').isVisible(),true);
 const extra=page.locator('#d_extra_containers .edit-container'),index=await extra.getAttribute('data-index');await page.locator(`#e_image_${index}`).fill('busybox:stable');
 const before=await page.evaluate(()=>JSON.parse(JSON.stringify(collect())));
 assert.equal(before.cpu,'125m');assert.equal(before.env.MODE,'production');assert.equal(before.additional_containers.length,1);
 for(const key of ['basics','hardware','environment','storage','address','containers','summary']){
  await select(key);
  assert.equal(await page.locator('#containerDeploy>.stepper-pane:visible').count(),1);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,`${key} fits ${width}`);
  const fields=await page.locator('#containerDeploy [id]').evaluateAll(els=>els.map(el=>el.id));assert.equal(new Set(fields).size,fields.length,'fields are mounted once');
  if(['basics','storage','summary'].includes(key))await page.screenshot({path:`${out}/${key}-${theme}-${width}.png`,fullPage:true});
 }
 assert.equal(await page.getByRole('button',{name:'Next',exact:true}).isVisible(),false);
 assert.deepEqual(await page.evaluate(()=>JSON.parse(JSON.stringify(collect()))),before,'navigation preserves every configuration field');
 await page.getByRole('button',{name:'Review deployment',exact:true}).click();await page.locator('#deployGo').waitFor();
 assert.equal(await page.locator('#deployGo').isEnabled(),true);
 await page.evaluate(()=>closeModal());
 await select('basics');await page.locator('#d_image').fill('');await select('summary');await page.getByRole('button',{name:'Review deployment',exact:true}).click();
 assert.equal(await page.locator('#d_image').isVisible(),true,'invalid fields are revealed');assert.equal(await page.locator('#d_image').evaluate(el=>el===document.activeElement),true);
 await page.locator('#d_image').fill('nginx:alpine');await page.locator('#d_target_mode').selectOption('existing');
 assert.equal(await page.locator('#d_join_note').isVisible(),true);assert.deepEqual((await page.evaluate(()=>collect())).additional_containers,[]);
 await select('summary');await page.getByRole('button',{name:'Review deployment',exact:true}).click();await page.locator('#deployConfirm').waitFor();
 assert.equal(await page.locator('#deployGo').isDisabled(),true,'joining still requires a rollout acknowledgement');
 await page.locator('#deployConfirm').check();
 if(await page.locator('#deployCapacityConfirm').count()){assert.equal(await page.locator('#deployGo').isDisabled(),true,'capacity warnings need their own acknowledgement');await page.locator('#deployCapacityConfirm').check();}
 assert.equal(await page.locator('#deployGo').isEnabled(),true);
 await page.evaluate(()=>closeModal());
 await page.getByRole('button',{name:'Cancel',exact:true}).click();await page.waitForFunction(()=>STATE.view==='workloads');
 assert.deepEqual(errors,[]);console.log(`Deploy sections passed (${width}px ${theme})`);await page.close();
}}finally{await browser.close();}
