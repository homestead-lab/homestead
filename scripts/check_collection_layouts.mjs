// Layout and interaction regressions using fictional public-demo data.
// No workload operations are sent: routing is checked with a recording stub.
import {chromium} from 'playwright';
import assert from 'node:assert/strict';
import {mkdir} from 'node:fs/promises';
const base=process.env.HOMESTEAD_URL || 'http://127.0.0.1:4173';
const output='release-assets/pages/collection-layouts';
await mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true});
try {
  for(const theme of ['dark','light']) for(const width of [1440,390,320]) {
    const page=await browser.newPage({viewport:{width,height:1000},colorScheme:theme}),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.addInitScript(theme=>localStorage.setItem('homestead.settings',JSON.stringify({theme,motion:'off',refresh:60})),theme);
    await page.goto(`${base}/?demo=1`,{waitUntil:'networkidle'});
    await page.locator('.node-comparison').waitFor();
    await page.evaluate(async()=>{
      await document.fonts.ready;
      const original=window.api;
      const nodes=structuredClone(await original('/api/nodes'));
      const fourth=structuredClone(nodes[2]);
      Object.assign(fourth,{name:'harvester-node4',cpu_pct:12.6,mem_pct:25.6,mem_used_gb:4,pods:24,pods_sys:20,pods_wl:4,vms:2,workloads:['media-server','download-manager'],
        addresses:{InternalIP:'192.0.2.212'},duties:{},disks:[{device:'nvme0n1',size_gb:500,root_fs:true,system:true,role:'system'}]});
      window.__nodesFixture=[...nodes,fourth];
      window.__workloadsFixture=null;
      window.api=async(path,options)=>{
        if(path==='/api/nodes')return structuredClone(window.__nodesFixture);
        if(path==='/api/workloads' && window.__workloadsFixture)return structuredClone(window.__workloadsFixture);
        const result=await original(path,options);
        if(path==='/api/overview')Object.assign(result,{nodes:structuredClone(window.__nodesFixture),nodes_total:window.__nodesFixture.length});
        return result;
      };
    });
    await page.evaluate(()=>go('nodes'));
    await page.waitForFunction(()=>document.querySelectorAll('.comparison-select').length===4);
    const matrix=page.locator('.comparison-table').first();
    assert.equal(await matrix.locator('thead th').count(),5);
    await matrix.locator('.comparison-select').nth(3).click();
    assert.equal(await matrix.locator('.comparison-select').nth(3).getAttribute('aria-pressed'),'true');
    assert.match(await page.locator('.node-comparison-detail').first().textContent(),/harvester-node4/);
    assert.equal(await page.evaluate(()=>document.activeElement?.getAttribute('aria-label')),'Select harvester-node4');
    await page.evaluate(()=>viewNodes());
    assert.equal(await matrix.locator('.comparison-select').nth(3).getAttribute('aria-pressed'),'true');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    for(const row of await matrix.locator('tbody tr').all()) {
      if(!(await row.isVisible()))continue;
      const bounds=await row.locator('td').evaluateAll(cells=>cells.map(c=>c.getBoundingClientRect()));
      assert.ok(bounds.every(b=>Math.abs(b.y-bounds[0].y)<1),'metrics align across all four hosts');
    }
    await page.screenshot({path:`${output}/nodes-${width}-${theme}.png`,fullPage:true});
    // Preserve uptime and pod graphics, including filesystem-bar alignment.
    assert.equal(await matrix.locator('.uptime-strip').count(),4);
    assert.equal(await matrix.locator('.podgrid').count(),4);
    if(width>560) for(const row of await matrix.locator('tbody tr').all()) {
      const bars=await row.locator('.comparison-disk .meter').evaluateAll(els=>els.map(e=>e.getBoundingClientRect().y));
      if(bars.length>1)assert.ok(bars.every(y=>Math.abs(y-bars[0])<1),'storage bars align across nodes');
    }
    await page.evaluate(()=>{
      window.__fourNodes=structuredClone(window.__nodesFixture);
      window.__nodesFixture=Array.from({length:10},(_,i)=>({...structuredClone(window.__fourNodes[i%4]),name:`harvester-node${i+1}`}));
      return viewNodes();
    });
    assert.equal(await page.locator('.comparison-table').count(),1);
    assert.equal(await page.locator('.comparison-table thead th').count(),11);
    const scroll=page.locator('.comparison-scroll');
    assert.equal(await scroll.evaluate(e=>e.scrollWidth>e.clientWidth),true,'ten nodes scroll inside the comparison');
    await scroll.evaluate(e=>{e.scrollLeft=e.scrollWidth});
    const labelLeft=await matrix.locator('thead th').first().evaluate(e=>e.getBoundingClientRect().left);
    assert.ok(Math.abs(labelLeft-(await scroll.boundingBox()).x)<2,'metric column stays pinned');
    await matrix.locator('.comparison-select').nth(9).click();
    assert.ok(await scroll.evaluate(e=>e.scrollLeft>0),'selecting a distant host retains horizontal position');
    await page.evaluate(()=>viewNodes());
    assert.equal(await matrix.locator('.comparison-select').nth(9).getAttribute('aria-pressed'),'true');
    assert.ok(await scroll.evaluate(e=>e.scrollLeft>0),'refresh retains horizontal position');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`${output}/nodes-ten-${width}-${theme}.png`,fullPage:true});
    await page.evaluate(()=>{window.__nodesFixture=window.__fourNodes.slice(0,1);return viewNodes()});
    await page.locator('.single-node-summary .nodecard').waitFor();
    assert.equal(await page.locator('.single-node-summary .uptime-strip').count(),1);
    assert.equal(await page.locator('.single-node-summary .podgrid').count(),1);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`${output}/nodes-single-${width}-${theme}.png`,fullPage:true});
    // Identical node names on linked clusters retain independent selection
    // and route their Open node action to the selected cluster.
    await page.evaluate(()=>{
      const a=structuredClone(window.__fourNodes[0]),b=structuredClone(a);
      a.site={id:'primary-example',name:'Primary cluster',self:true};b.site={id:'secondary-example',name:'Secondary cluster',self:false};
      window.__nodesFixture=[a,b];localStorage.setItem('homestead.fleet.mode','all');
      window.__nodeDetailOriginal=window.nodeDetail;
      window.nodeDetail=name=>{window.__nodeRoute={name,target:FLEET.target}};
      return viewNodes();
    });
    await page.locator('.comparison-select').nth(1).click();
    const nodeAction=width<=560?page.locator('.node-comparison-detail[data-cluster="secondary-example"]'):page.locator('.comparison-table tbody tr').last().locator('td[data-cluster="secondary-example"]');
    await nodeAction.getByRole('button',{name:'Open node',exact:true}).click();
    assert.equal(await page.evaluate(()=>window.__nodeRoute.target),'secondary-example');
    await page.evaluate(()=>{window.nodeDetail=window.__nodeDetailOriginal;FLEET.target='';localStorage.removeItem('homestead.fleet.mode');window.__nodesFixture=window.__fourNodes;return go('dash')});
    await page.waitForFunction(()=>document.querySelectorAll('.comparison-select').length===4);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);

    await page.evaluate(()=>go('workloads'));
    await page.locator('.wl-row').first().waitFor();
    const toggle=page.locator('.collection-disclosure').first();
    const key=await page.locator('.wl-row').first().getAttribute('data-row-key');
    await toggle.focus();await page.keyboard.press('Enter');
    assert.equal(await toggle.getAttribute('aria-expanded'),'true');
    assert.equal(await page.locator('.wl-detail-row').first().isVisible(),true);
    await page.evaluate(()=>renderWorkloads());
    assert.equal(await toggle.getAttribute('aria-expanded'),'true','refresh retains expansion');
    for(const order of [{col:0,dir:-1},{col:3,dir:-1},{col:3,dir:1}]) {
      await page.evaluate(order=>saveSort('containers',order),order);
      assert.equal(await page.locator('.wl-row').evaluateAll(rows=>rows.every(row=>row.nextElementSibling?.dataset.detailFor===row.dataset.rowKey)),true,'sort keeps each detail beside its parent');
    }
    assert.equal(await page.locator('.collection-disclosure[aria-expanded=true]').count(),1);
    assert.equal(await page.locator('.collection-disclosure[aria-expanded=true]').evaluate(b=>b.closest('tr').dataset.rowKey),key);
    await page.evaluate(()=>{localStorage.removeItem('homestead.sort.containers');renderWorkloads()});
    if(width<=560) {
      const shortRows=await page.locator('.wl-row:not(.wl-off)').evaluateAll(rows=>rows.filter(r=>!r.querySelector('.tag.warn')).map(r=>r.getBoundingClientRect().height));
      assert.ok(shortRows.every(h=>h<=64),`ordinary mobile rows remain compact: ${shortRows.join(', ')}`);
      assert.equal(await page.locator('.wl-row').first().locator('.wl-row-meta').isVisible(),false);
      assert.equal(await page.locator('.wl-detail-row').first().locator('.wl-detail-actions').isVisible(),true);
    }
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:`${output}/containers-${width}-${theme}.png`,fullPage:true});
    await page.locator('.collection-disclosure[aria-expanded=true]').click();
    assert.equal(await page.locator('.wl-detail-row').first().isVisible(),false);

    // Same namespace/name on two clusters must expand and route independently.
    await page.evaluate(()=>{
      localStorage.setItem('homestead.fleet.mode','all');
      const a=structuredClone(STATE.data.wl[0]),b=structuredClone(a);
      a.group='';b.group='';a.site={id:'primary-example',name:'Primary cluster',self:true};b.site={id:'secondary-example',name:'Secondary cluster',self:false};
      a.name=b.name='example-container';
      b.images=['registry.example.com/containers/example-with-a-long-image-reference:stable'];
      window.__workloadsFixture=[a,b];
      window.__routedCalls=[];
      window.wlLogs=(...args)=>window.__routedCalls.push({args,target:FLEET.target});
      return viewWorkloads();
    });
    await page.waitForFunction(()=>document.querySelectorAll('.wl-row').length===2);
    const remote=page.locator('.wl-row[data-cluster="secondary-example"]');
    await remote.locator('.collection-disclosure').click();
    assert.equal(await page.locator('.wl-detail-row[data-cluster="primary-example"]').isVisible(),false);
    const actions=width<=560?page.locator('.wl-detail-row[data-cluster="secondary-example"] .wl-detail-actions'):remote.locator('.wl-actions');
    await actions.getByRole('button',{name:'Logs example-container',exact:true}).click();
    assert.equal(await page.evaluate(()=>window.__routedCalls.at(-1).target),'secondary-example');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    // Space, keyboard focus, and full labels remain usable for long names.
    await remote.locator('.collection-disclosure').focus();await page.keyboard.press('Space');
    assert.equal(await remote.locator('.collection-disclosure').getAttribute('aria-expanded'),'false');
    await page.evaluate(()=>{
      window.__workloadsFixture[0].name='a-container-with-a-long-descriptive-name';
      window.__workloadsFixture[0].desired=0;window.__workloadsFixture[0].ready=0;
      window.__workloadsFixture[1].pods=[{name:'example-pod',unplaced:'Insufficient memory on eligible hosts'}];
      window.__workloadsFixture[1].ready=0;
      return viewWorkloads();
    });
    await page.locator('.wl-off').getByText('Stopped',{exact:true}).waitFor();
    await page.locator('.wl-row').getByText('Blocked',{exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    assert.deepEqual(errors,[]);
    console.log(`Collection layouts and interactions passed: ${width}px ${theme}`);
    await page.close();
  }
} finally {await browser.close()}
