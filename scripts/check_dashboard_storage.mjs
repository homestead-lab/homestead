import {chromium,devices} from "playwright";
import assert from "node:assert/strict";
import {mkdir} from "node:fs/promises";
const base=process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";
await mkdir("release-assets/pages/dashboard-storage",{recursive:true});
const browser=await chromium.launch({headless:true});
try {
  const context=await browser.newContext({...devices["Pixel 7"]});
  const page=await context.newPage(),errors=[];
  page.on("pageerror",e=>errors.push(e.message));
  await page.goto(`${base}/?demo=1`,{waitUntil:"networkidle"});
  await page.locator(".nodecard").first().waitFor();
  await page.evaluate(()=>{
    const original=window.fetch;
    window.fetch=async(input,init)=>{
      const response=await original(input,init);
      if(new URL(typeof input==="string"?input:input.url,location.origin).pathname!=="/api/overview") return response;
      const data=await response.json();
      Object.assign(data.nodes[0],{fs_cap_gb:100,fs_used_gb:40,fs_pct:40,disks:[{
        device:"nvme0n1",size_gb:500,root_fs:true,system:true,role:"longhorn",lh_size_gb:70,
        lh_filesystems:[{capacity_gb:100,used_gb:40,data_gb:10,available_gb:60,reserved_gb:30,on_root:true}],
      }]});
      data.top_cpu=Array.from({length:5},(_,i)=>({name:`worker-${i+1}-with-a-long-descriptive-name`,
        ns:"example-apps",nodes:["node-a","node-b"],cpu:1.25-i*.2}));
      data.top_mem=data.top_cpu.map((w,i)=>({...w,mem_mb:2048-i*200}));
      return new Response(JSON.stringify(data),{status:200,headers:{"Content-Type":"application/json"}});
    };
  });
  for(const size of [{width:1440,height:1000},{width:412,height:839},{width:360,height:640}]) {
    await page.setViewportSize(size);
    await page.evaluate(()=>go("dash"));
    await page.locator(".consumer-row").first().waitFor();
    const card=page.locator(".nodecard").first();
    assert.match(await card.innerText(),/40%\s+40\/100 GB/);
    assert.match(await card.innerText(),/Filesystem use · 500 GB disk/);
    assert.match(await card.locator(".meter.split").getAttribute("data-tip"),/Longhorn allowance left 30 GB/);
    const widths=await card.locator(".meter.split>span").evaluateAll(spans=>spans.map(s=>Number.parseFloat(s.style.width)));
    assert.deepEqual(widths,[30,10,30]);
    for(const group of await card.locator(".badgegroup").all()) {
      const caption=await group.locator(".badgecap").boundingBox();
      const values=await group.locator(".badge-values").boundingBox();
      assert.ok(values.y>=caption.y+caption.height,"node tags sit below their heading");
      assert.ok(Math.abs(values.x-caption.x)<1,"wrapped tags align with their heading");
      for(const tag of await group.locator(".tag").all()) {
        const bounds=await tag.boundingBox();
        assert.ok(bounds.x+bounds.width<=values.x+values.width+1,"node tags fit their group");
      }
    }
    for(const ranking of await page.locator(".consumer-card").all()) {
      assert.equal(await ranking.locator("tbody .consumer-row").count(),5);
      assert.ok((await ranking.boundingBox()).height<=320,"five ranked workloads fit a compact card");
      for(const row of await ranking.locator("tbody .consumer-row").all())
        assert.ok((await row.boundingBox()).height<=54,"ranked workload remains a short row");
    }
    assert.match(await page.locator(".consumer-value").first().innerText(),/125%/);
    assert.match(await page.locator(".consumer-meta").first().getAttribute("title"),/example-apps · node-a, node-b/);
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),"dashboard fits the viewport");
    await page.locator(".consumer-grid").screenshot({path:`release-assets/pages/dashboard-storage/rankings-${size.width}.png`});
    await card.screenshot({path:`release-assets/pages/dashboard-storage/host-${size.width}.png`});
    console.log(`Disk capacity and compact rankings passed at ${size.width}px`);
    if(size.width<900) {
      await page.evaluate(()=>go("settings"));
      await page.locator(".settings-layout").waitFor();
      await page.evaluate(()=>settingsGo("you"));
      const back=page.getByRole("button",{name:"‹ Back to Settings",exact:true});
      await back.waitFor();
      await page.screenshot({path:`release-assets/pages/dashboard-storage/settings-${size.width}.png`});
      await back.click();
      assert.equal(await page.locator(".settings-layout").getAttribute("data-open"),"0");
    }
  }
  assert.deepEqual(errors,[]);
  await context.close();
} finally {await browser.close();}
