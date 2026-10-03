// Exercise launch placement and import-log scrolling without dispatching workloads.
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
const base = process.env.HOMESTEAD_URL || 'http://127.0.0.1:4173';
await mkdir('release-assets/pages/launch-import', { recursive: true });
const browser = await chromium.launch({headless:true});
try {
  for (const width of [1440,390]) {
    const page = await browser.newPage({viewport:{width,height:900}}), errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(`${base}/?demo=1`,{waitUntil:'networkidle'});
    await page.locator('#views .phead').waitFor({state:"attached"});
    await page.evaluate(()=>{
      const original=window.api;
      window.__launchFixture={pinned:null,preferred:null,resident:null};
      window.api=async(path,options)=>{
        if(path.includes('/api/operations/log')) return window.__importLogFixture;
        const result=await original(path,options);
        const plan=path.includes('/start-plan')?result:path==='/api/vm/power/preview'?result.capacity:null;
        if(plan){
          plan.placement=window.__launchFixture;
          plan.candidates=[{name:'host-a',eligible:true,metrics_available:true,used_gb:6,capacity_gb:16,projected_gb:8,projected_percent:50,reservations_known:true,reserved_gb:4,allocatable_gb:15,reserved_cpu_percent:20,projected_pods:1},
            {name:'host-b',eligible:!plan.placement.pinned,metrics_available:true,used_gb:4,capacity_gb:16,projected_gb:6,projected_percent:37.5,reservations_known:true,reserved_gb:3,allocatable_gb:15,reserved_cpu_percent:10,projected_pods:1,reasons:['Pinned to host-a']}];
        }
        return result;
      };
    });
    for (const mode of ['automatic','pinned','preferred','resident']) {
      await page.evaluate(async mode=>{
        closeModal(); window.__launchFixture={pinned:null,preferred:null,resident:null};
        if(mode!=='automatic')window.__launchFixture[mode]='host-a';
        await vmPower('lab','ubuntu-2404',mode==='resident'?'unpause':'start');
      },mode);
      const expected={automatic:'Host selected at launch',pinned:'Required host: host-a',preferred:'Preferred host: host-a',resident:'Resumes on host-a'}[mode];
      await page.getByText(expected,{exact:true}).waitFor();
      assert.equal(await page.getByText('Chosen',{exact:true}).count(),0);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await page.screenshot({path:`release-assets/pages/launch-import/vm-${mode}-${width}.png`,fullPage:true});
    }
    await page.evaluate(async()=>{closeModal();window.__launchFixture={pinned:null,preferred:'host-a',resident:null};await wlScale('lab','example-app',1)});
    await page.getByText('Preferred host: host-a',{exact:true}).waitFor();
    await page.screenshot({path:`release-assets/pages/launch-import/container-${width}.png`,fullPage:true});
    await page.evaluate(async()=>{
      closeModal();clearInterval(window.__logTimer);
      const at=new Date().toISOString();
      window.__importLogFixture={id:'copy-fixture',status:'running',progress:20,message:'Copying disk',
        history:Array.from({length:45},(_,i)=>({t:at,s:'running',p:i,m:`Copy step ${i}`})),
        sources:[{title:'Disk 1 · example-disk · copy',pod:'copy-pod',kind:'disk-copy',
          progress:{percent:20,bytes:20000000000,total_bytes:100000000000,bytes_per_second:100000000,eta_seconds:800},
          text:Array.from({length:30},(_,i)=>`${i}.0% · ${i}.0 / 100.0 GB · 100.0 MB/s · ETA 13m 20s`).join('\n')},
          {title:'Storage',text:Array.from({length:30},(_,i)=>`Storage event ${i}`).join('\n')}]};
      await operationLog('copy-fixture');
    });
    await page.getByText('Copy step 44',{exact:true}).waitFor();
    const bottom=()=>page.locator('.oplog-steps').evaluate(el=>el.scrollTop+el.clientHeight>=el.scrollHeight-1);
    assert.equal(await bottom(),true,'latest step is visible on first paint');
    await page.waitForTimeout(3200);
    assert.equal(await bottom(),true,'refresh keeps latest step visible');
    assert.ok(await page.locator('.oplog-copy').evaluate(el=>el.clientHeight<=120));
    assert.match(await page.locator('.oplog-copy-progress').innerText(),/20\.0%.*20\.0 \/ 100\.0 GB.*100\.0 MB\/s.*ETA 13m 20s/s);
    await page.locator('#oplogFollow').uncheck();
    await page.evaluate(()=>{
      document.querySelector('.oplog-steps').scrollTop=30;
      document.querySelector('.oplog-copy').scrollTop=40;
      document.querySelector('.modalbox').scrollTop=20;
    });
    await page.waitForTimeout(3200);
    assert.equal(await page.locator('.oplog-steps').evaluate(el=>el.scrollTop),30);
    assert.equal(await page.locator('.oplog-copy').evaluate(el=>el.scrollTop),40);
    assert.equal(await page.locator('.modalbox').evaluate(el=>el.scrollTop),20);
    await page.locator('#oplogFollow').check();
    assert.equal(await bottom(),true,'turning follow back on immediately shows the latest step');
    await page.screenshot({path:`release-assets/pages/launch-import/import-log-${width}.png`,fullPage:true});
    assert.deepEqual(errors,[]);
    console.log(`Launch placement and import log passed at ${width}px`);
    await page.close();
  }
}finally{await browser.close()}
