import assert from "node:assert/strict";
import {chromium} from "playwright";
import {mkdir} from "node:fs/promises";
const base=process.env.HOMESTEAD_URL || "http://127.0.0.1:4176",out=process.env.NODE_HEALTH_SCREENSHOTS || "release-assets/node-health-review";
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
try{for(const theme of ["dark","light"]){
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage(),errors=[];page.on("pageerror",e=>errors.push(e.message));
 await page.goto(`${base}/?demo=1`);await page.locator('[data-widget="nodes"]').waitFor();
 await page.evaluate(async theme=>{
   SET.theme=theme;applySettings();const original=window.api,seed=structuredClone(STATE.data.ov.nodes[0]);
   const nodes=["backup-node","media-server","nas-01","pi-cluster-1","web-server"].map((name,i)=>({...structuredClone(seed),name,cpu_pct:i===4?null:8+i*9,mem_pct:29+i*8,fs_pct:40+i*8,temps:{...seed.temps,cpu_c:42+i,max_c:47+i,disks:i?[]:[{name:"sda",health:{state:"attention",summary:"2 reallocated sectors"}}]},disk_issues:i?[]:[{disk:"sda",severity:"degraded",reason:"2 reallocated sectors"}]}));
   window.api=async(path,opts)=>{const answer=await original(path,opts);if(path==="/api/nodes")return nodes;if(path==="/api/overview")return {...answer,nodes,nodes_total:5,health:"degraded",health_issues:[{kind:"Disk",name:"backup-node/sda",severity:"degraded",reason:"2 reallocated sectors"}],health_summary:"Disk backup-node/sda: 2 reallocated sectors"};return answer;};
   await viewDash();
 },theme);
 await page.locator('.clusteralert button').click();await page.locator('#clusterHealth').waitFor();
 await page.waitForFunction(()=>document.querySelector('#clusterHealth')?.textContent.includes('2 reallocated sectors'));
 assert.equal(new URL(page.url()).searchParams.get('section'),'health');assert.equal(await page.locator('#clusterHealth').evaluate(el=>el===document.activeElement),true);
 await page.evaluate(()=>go('dash'));await page.getByRole('button',{name:'Edit dashboard',exact:true}).click();
 await page.getByRole('button',{name:'Settings for Node health',exact:true}).click();
 assert.equal(await page.getByLabel('Widget width',{exact:true}).locator('option').count(),4);
 await page.getByLabel('Widget width',{exact:true}).selectOption('6');
 await page.getByLabel('Node health display',{exact:true}).selectOption('detailed');
 assert.equal(await page.locator('[data-widget="nodes"] .node-comparison').count(),1);
 await page.getByRole('button',{name:'Save layout',exact:true}).click();await page.getByRole('button',{name:'Edit dashboard',exact:true}).waitFor();
 assert.equal(await page.evaluate(async()=> (await api('/api/auth/preferences/dashboard')).layout.items.find(x=>x.id==='nodes').display),'detailed');
 await page.getByRole('button',{name:'Edit dashboard',exact:true}).click();await page.getByRole('button',{name:'Settings for Node health',exact:true}).click();
 await page.getByLabel('Node health display',{exact:true}).selectOption('compact');await page.getByRole('button',{name:'Save layout',exact:true}).click();await page.getByRole('button',{name:'Edit dashboard',exact:true}).waitFor();
 await page.addStyleTag({content:"#toast,#bottombar,header.top{visibility:hidden!important}"});
 const widget=page.locator('[data-widget="nodes"]');assert.equal(await widget.locator('.node-compact-host').count(),5);
 assert.equal(await widget.locator('.node-compact-warning').textContent(),'sda: 2 reallocated sectors');
 const bounds=await widget.locator('.node-compact-host').evaluateAll(els=>els.map(e=>({x:e.getBoundingClientRect().x,y:e.getBoundingClientRect().y})));
 assert.ok(bounds[1].x>bounds[0].x && Math.abs(bounds[1].y-bounds[0].y)<1,'half-width widget packs hosts beside one another');
 assert.equal(await widget.locator('.node-compact-host').last().locator('.node-compact-metric b').first().textContent(),'—','missing metrics are not zero');
 for(const width of [1440,390,320]){await page.setViewportSize({width,height:1000});await widget.scrollIntoViewIfNeeded();await widget.screenshot({path:`${out}/compact-${theme}-${width}.png`});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);assert.equal(await widget.evaluate(el=>el.scrollWidth<=el.clientWidth+1),true);assert.equal(await widget.locator('.node-compact-host').evaluateAll(els=>els.every(el=>[...el.querySelectorAll('*')].every(child=>child.getBoundingClientRect().right<=el.getBoundingClientRect().right+1))),true,'each metric fits its host column');}
 assert.deepEqual(errors,[]);console.log(`Node health layout, settings and Review passed (${theme})`);await context.close();
}}finally{await browser.close();}
