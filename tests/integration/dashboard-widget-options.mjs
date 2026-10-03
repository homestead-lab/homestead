import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4176',out=process.env.WIDGET_OPTIONS_SCREENSHOTS || 'release-assets/widget-options';
await mkdir(out,{recursive:true});const browser=await chromium.launch({headless:true});
try{for(const theme of ['dark','light']){
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`${base}/?demo=1`);await page.locator('[data-widget="nodes"]').waitFor();
 await page.evaluate(theme=>{
  clearInterval(window.__loopTimer);SET.theme=theme;applySettings();const original=window.api;
  window.testLayout={layout:{version:1,items:[{id:'compute',width:4,height:0},{id:'containers',width:4,height:240},{id:'portal',width:4,height:240}]},revision:'one'};
  window.api=async(path,opts)=>{
   if(path==='/api/auth/preferences/dashboard'){if(opts?.method==='POST'){testLayout={layout:JSON.parse(opts.body).layout,revision:'two'};}return structuredClone(testLayout);}
   if(path==='/api/workloads')return [{name:'sonarr',group:'Media',ns:'apps',desired:1,ready:1,cpu:.1,mem_mb:80},{name:'radarr',group:'Media',ns:'apps',desired:0,ready:0},{name:'other',group:'Tools',ns:'apps',desired:1,ready:0},{name:'ungrouped',ns:'apps',desired:1,ready:1}];
   if(path==='/api/portal')return {links:[{id:'a',title:'Movies',section:'Media',url:'https://example.com/movies'},{id:'b',title:'Router',section:'Network',url:'https://example.com/router'}]};
   return original(path,opts);
  };Dashboard.invalidate();
 },theme);
 await page.evaluate(()=>viewDash());
 await page.getByRole('button',{name:'Edit dashboard',exact:true}).click();
 await page.getByRole('button',{name:'Settings for Containers',exact:true}).click();
 await page.waitForFunction(()=>document.querySelectorAll('#dashboardGroupOptions [data-group]').length===3);
 assert.equal(await page.evaluate(()=>document.activeElement.classList.contains('dashboard-inspector')),true);
 assert.ok((await page.locator('.dashboard-inspector').boundingBox()).y>=0,'settings scroll into view');
 assert.equal(await page.locator('.dashboard-catalog').evaluate(el=>el.scrollHeight>el.clientHeight && el.clientHeight<=260),true,'picker is a bounded scrollable list');
 await page.getByLabel('All groups',{exact:true}).uncheck();
 assert.equal(await page.locator('[data-widget="containers"] tbody tr').count(),0,'empty means none');
 await page.locator('#dashboardGroupOptions input[data-group="Media"]').check();
 assert.equal(await page.locator('[data-widget="containers"] tbody tr').count(),2);
 await page.getByLabel('Workload status',{exact:true}).selectOption('running');
 assert.equal(await page.locator('[data-widget="containers"] tbody tr').count(),1);
 await page.getByLabel('Widget start column',{exact:true}).selectOption('9');
 let a=await page.locator('[data-widget="compute"]').boundingBox(),b=await page.locator('[data-widget="containers"]').boundingBox();
 assert.ok(Math.abs(a.y-b.y)<1 && b.x>a.x+a.width*1.8,'blank columns persist between cards');
 await page.getByRole('button',{name:'Settings for Portal links',exact:true}).click();
 await page.locator('#dashboardPortal .portal-tile').first().waitFor();
 await page.locator('#dashboardGroupOptions input[data-group="Network"]').uncheck();
 await page.getByLabel('Start a new row',{exact:true}).check();
 assert.equal(await page.locator('#dashboardPortal .portal-tile').count(),1);
 assert.equal(await page.getByLabel('Portal display',{exact:true}).inputValue(),'compact');
 assert.ok((await page.locator('#dashboardPortal .portal-tile').boundingBox()).height<=40);
 await page.getByLabel('Portal display',{exact:true}).selectOption('tiles');
 assert.equal(await page.locator('#dashboardPortal .portal-list').count(),0);
 await page.getByRole('button',{name:'Undo',exact:true}).click();
 assert.equal(await page.getByLabel('Portal display',{exact:true}).inputValue(),'compact');
 await page.getByRole('button',{name:'Settings for Containers',exact:true}).click();
 assert.equal(await page.getByLabel('Workload status',{exact:true}).inputValue(),'running','switching widgets updates the visible selection');
 assert.equal(await page.getByLabel('Start a new row',{exact:true}).isChecked(),false);
 assert.equal(await page.locator('#dashboardGroupOptions input[data-group="Media"]').isChecked(),true);
 assert.ok((await page.locator('.dashboard-library').boundingBox()).y+(await page.locator('.dashboard-library').boundingBox()).height<=1000,'control panel fits viewport');
 await page.screenshot({path:`${out}/editor-${theme}.png`});
 await page.getByRole('button',{name:'Save layout',exact:true}).click();await page.getByRole('button',{name:'Edit dashboard',exact:true}).waitFor();
 await page.evaluate(()=>HealthInsights.load(true));
 assert.equal(await page.locator('[data-widget="containers"] tbody tr').count(),1,'refresh respects saved filters');
 await page.evaluate(async()=>{Dashboard.invalidate();await viewDash();});
 await page.waitForFunction(()=>document.querySelectorAll('[data-widget="containers"] tbody tr').length===1);
 assert.equal(await page.locator('#dashboardPortal .portal-tile').count(),1,'saved sections restore');
 for(const width of [1440,390,320]){
  await page.setViewportSize({width,height:1000});
  await page.evaluate(()=>{document.body.classList.remove('nav-open');document.querySelector('#toast')?.remove();});
  await page.addStyleTag({content:'*,*::before,*::after{transition:none!important}'});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
  if(width<900){const cells=await page.locator('.dashboard-widget').evaluateAll(els=>els.map(el=>({x:el.getBoundingClientRect().x,w:el.getBoundingClientRect().width})));assert.ok(cells.every(c=>c.x===cells[0].x && c.w===cells[0].w),'phone ignores desktop gaps');}
  await page.screenshot({path:`${out}/saved-${theme}-${width}.png`,fullPage:true});
 }
 assert.deepEqual(errors,[]);await page.close();console.log(`Widget options passed (${theme})`);
}}finally{await browser.close();}
