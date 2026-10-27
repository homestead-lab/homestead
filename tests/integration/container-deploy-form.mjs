import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4176',out=process.env.DEPLOY_FORM_SCREENSHOTS || 'release-assets/deploy-form';
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
try{for(const width of [1440,390,320])for(const theme of ['dark','light']){
 const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`${base}/?demo=1`);await page.locator('[data-widget="compute"]').waitFor();
 await page.evaluate(async theme=>{clearInterval(window.__loopTimer);SET.theme=theme;applySettings();await go('deploy');},theme);
 const select=async key=>width>900?page.getByRole('tab',{name:({basics:'Basics',hardware:'Hardware and access',environment:'Environment values',storage:'Storage',placement:'Placement',address:'Address',monitoring:'Monitoring',summary:'Summary'})[key],exact:true}).click():page.locator('#containerDeploy_section').selectOption(key);
 const shown=()=>page.locator('#containerDeploy>.stepper-pane:visible').getAttribute('data-key');
 // (a) The same sections as Edit, in the same order, with Summary last (#544).
 assert.deepEqual(await page.locator('#containerDeploy [role="tab"]').evaluateAll(tabs=>tabs.map(t=>t.firstChild.textContent)),['Basics','Hardware and access','Environment values','Storage','Placement','Address','Monitoring','Summary']);
 // (b) Placement is live (#844): Edit's section, with pod copies and the tier, and nothing greyed.
 const placementTab=page.locator('#containerDeploy-tab-placement');
 assert.equal(await placementTab.getAttribute('aria-disabled'),null);assert.equal(await placementTab.getAttribute('aria-label'),null);assert.doesNotMatch(await placementTab.textContent(),/Set after deploying/);
 assert.equal(await page.locator('#containerDeploy [aria-label$="set after deploying"]').count(),0,'no section is greyed');
 // (c) Next steps through Placement.
 await page.evaluate(()=>UI.selectSection('containerDeploy','storage'));await page.getByRole('button',{name:'Next',exact:true}).click();assert.equal(await shown(),'placement');
 await page.getByRole('button',{name:'Next',exact:true}).click();assert.equal(await shown(),'address');
 await page.getByRole('button',{name:'Back',exact:true}).click();await page.getByRole('button',{name:'Back',exact:true}).click();assert.equal(await shown(),'storage');
 await select('placement');assert.equal(await shown(),'placement');
 for(const id of ['d_rep','d_spread','d_node','d_failover','d_priority'])assert.equal(await page.locator(`#${id}`).isVisible(),true,id);
 assert.equal(await page.locator('#d_failover').inputValue(),'move','a new app still moves when its host fails');
 const host=await page.locator('#d_node option').nth(1).getAttribute('value');await page.locator('#d_node').selectOption(host);await page.locator('#d_spread').selectOption('prefer');
 await page.locator('#d_placement button',{hasText:'Add'}).first().click();const app=await page.locator('#d_with .aff-target option').nth(1).getAttribute('value');await page.locator('#d_with .aff-target').selectOption(app);
 const placed=await page.evaluate(()=>JSON.parse(JSON.stringify(collect())));
 assert.equal(placed.node,host);assert.equal(placed.placement.spread,'prefer');assert.deepEqual(placed.placement.with.map(r=>`${r.ns}/${r.name}`),[app]);
 await page.evaluate(()=>UI.selectSection('containerDeploy','basics'));
 await page.locator('#d_container_name').fill('web');await page.locator('#d_workload_name').fill('web-stack');await page.locator('#d_image').fill('nginx:alpine');
 assert.equal(await page.locator('#containerDeploy>.stepper-foot').count(),1);
 assert.equal(await page.getByRole('button',{name:'Review deployment',exact:true}).isVisible(),false);
 await page.getByRole('button',{name:'Next',exact:true}).click();assert.equal(await page.locator('.qty[data-target="d_cpu"] .qty-text').isVisible(),true);await page.locator('.qty[data-target="d_cpu"] .qty-text').fill('125m');
 await select('environment');await page.getByRole('button',{name:'＋ add variable',exact:true}).click();await page.locator('#d_env .ek').last().fill('MODE');await page.locator('#d_env .ev').last().fill('production');
 await select('address');await page.locator('#d_net').selectOption('internal');
 await select('storage');assert.equal(await page.getByText('Choose a storage type',{exact:true}).isVisible(),true);
 await select('basics');assert.equal(await page.locator('#d_image').inputValue(),'nginx:alpine');
 await page.locator('#d_containers_block button',{hasText:'Add container'}).click();assert.equal(await page.locator('#d_basics_containers').isVisible(),true);
 const extra=page.locator('#d_basics_containers .edit-container'),index=await extra.getAttribute('data-index');await page.locator(`#e_image_${index}`).fill('busybox:stable');
 // (d) The new container has its part in every section that holds containers.
 for(const section of ['basics','hardware','environment','storage','address'])assert.equal(await page.locator(`#d_${section}_containers .edit-container[data-index="${index}"]`).count(),1,section);
 const before=await page.evaluate(()=>JSON.parse(JSON.stringify(collect())));
 assert.equal(before.cpu,'125m');assert.equal(before.env.MODE,'production');assert.equal(before.additional_containers.length,1);
 for(const key of ['basics','hardware','environment','storage','placement','address','monitoring','summary']){
  await select(key);
  assert.equal(await page.locator('#containerDeploy>.stepper-pane:visible').count(),1);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,`${key} fits ${width}`);
  const fields=await page.locator('#containerDeploy [id]').evaluateAll(els=>els.map(el=>el.id));assert.equal(new Set(fields).size,fields.length,'fields are mounted once');
  if(['basics','storage','placement','summary'].includes(key))await page.screenshot({path:`${out}/${key}-${theme}-${width}.png`,fullPage:true});
 }
 assert.equal(await page.getByRole('button',{name:'Next',exact:true}).isVisible(),false);
 assert.deepEqual(await page.evaluate(()=>JSON.parse(JSON.stringify(collect()))),before,'navigation preserves every configuration field');
 await page.getByRole('button',{name:'Review deployment',exact:true}).click();await page.locator('#deployGo').waitFor();
 assert.equal(await page.locator('#deployGo').isEnabled(),true);
 await page.evaluate(()=>closeModal());
 await select('basics');await page.locator('#d_image').fill('');await select('summary');await page.getByRole('button',{name:'Review deployment',exact:true}).click();
 assert.equal(await page.locator('#d_image').isVisible(),true,'invalid fields are revealed');assert.equal(await page.locator('#d_image').evaluate(el=>el===document.activeElement),true);
 await page.locator('#d_image').fill('nginx:alpine');
 // (g) An extra container with no image: Review deployment opens it in Basics, at its image.
 await page.locator(`#e_image_${index}`).fill('');await select('summary');await page.getByRole('button',{name:'Review deployment',exact:true}).click();
 assert.equal(await shown(),'basics');assert.equal(await page.locator(`#e_image_${index}`).isVisible(),true);assert.equal(await page.locator(`#e_image_${index}`).evaluate(el=>el===document.activeElement),true);
 // (e) Removing it takes its part out of every section.
 await page.locator(`#d_basics_containers .edit-container[data-index="${index}"]`).getByRole('button',{name:'Remove container',exact:true}).click();
 assert.equal(await page.locator(`#containerDeploy .edit-container[data-index="${index}"]`).count(),0);assert.deepEqual((await page.evaluate(()=>collect())).additional_containers,[]);
 await page.locator('#d_containers_block button',{hasText:'Add container'}).click();
 assert.equal(await page.locator('#d_basics_containers .edit-container').count(),1);await page.locator('#d_target_mode').selectOption('existing');
 assert.equal(await page.locator('#d_join_note').isVisible(),true);assert.deepEqual((await page.evaluate(()=>collect())).additional_containers,[]);
 // (f) Joining an app adds one container: no Add button and no other container's panels, in any section.
 assert.equal(await page.locator('#d_containers_block button',{hasText:'Add container'}).isVisible(),false);
 assert.equal(await page.locator('#d_containers_join').isVisible(),true);
 for(const key of ['basics','hardware','environment','storage','address']){await select(key);assert.equal(await page.locator('#containerDeploy .stepper-pane:visible .edit-container:visible').count(),0,key);}
 await select('placement');assert.equal(await page.locator('#d_placement_join').isVisible(),true,'a joined app keeps its own placement');assert.equal(await page.locator('#d_placement').isVisible(),false);
 for(const key of ['placement','node','failover'])assert.equal(key in (await page.evaluate(()=>collect())),false,key);
 await select('summary');await page.getByRole('button',{name:'Review deployment',exact:true}).click();await page.locator('#deployConfirm').waitFor();
 assert.equal(await page.locator('#deployGo').isDisabled(),true,'joining still requires a rollout acknowledgement');
 await page.locator('#deployConfirm').check();
 if(await page.locator('#deployCapacityConfirm').count()){assert.equal(await page.locator('#deployGo').isDisabled(),true,'capacity warnings need their own acknowledgement');await page.locator('#deployCapacityConfirm').check();}
 assert.equal(await page.locator('#deployGo').isEnabled(),true);
 await page.evaluate(()=>closeModal());
 await page.getByRole('button',{name:'Cancel',exact:true}).click();await page.waitForFunction(()=>STATE.view==='workloads');
 assert.deepEqual(errors,[]);console.log(`Deploy sections passed (${width}px ${theme})`);await page.close();
}}finally{await browser.close();}
