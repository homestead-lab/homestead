// Deterministic demo-only review; never contacts a cluster.
import assert from "node:assert/strict";
import {mkdir} from "node:fs/promises";
import {chromium} from "playwright";
await mkdir("release-assets/import-admission-checks",{recursive:true});
const browser=await chromium.launch({headless:true});
try {
  const page=await browser.newPage({viewport:{width:1280,height:1100}});
  const errors=[]; page.on("pageerror",e=>errors.push(e.message));
  await page.goto("http://127.0.0.1:4175/?demo=1",{waitUntil:"networkidle"});
  await page.evaluate(async()=>{
    const original=api; window.__importSent=[];
    api=async(url,init)=>{
      if(url==="/api/import") window.__importSent.push(JSON.parse(init.body));
      return original(url,init);
    };
    modal("Import container","<p>Import form retained below the review.</p>");
    await importReview({name:"media-server",image:"example/media-server:1",memory_limit:"1Gi",
      volumes:[{name:"media-server-appdata",create:true,access_mode:"ReadWriteOnce",storage_class:"longhorn-r2"},
        {name:"existing-shared-media",create:false,access_mode:"ReadWriteMany",storage_class:"longhorn-rwx"}]});
  });
  await page.locator("#importConfirm").waitFor();
  assert.match(await page.locator("#mbody").innerText(),/Copy files/);
  assert.match(await page.locator("#mbody").innerText(),/Imported application/);
  for(const theme of ["dark","light"]){
    await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);
    for(const width of [1280,390,320]){
      await page.setViewportSize({width,height:1100});
      const box=await page.locator("#mbody").evaluate(el=>({w:el.clientWidth,s:el.scrollWidth}));
      assert.ok(box.s<=box.w+1,`${theme}/${width} horizontal overflow`);
      await page.evaluate(()=>document.querySelector("#toast").replaceChildren());
      await page.screenshot({path:`release-assets/import-admission-checks/${theme}-${width}.png`});
      await page.locator("#importConfirm").scrollIntoViewIfNeeded();
      await page.screenshot({path:`release-assets/import-admission-checks/${theme}-${width}-approval.png`});
    }
  }
  await page.locator("#importGo").click();
  assert.equal(await page.evaluate(()=>window.__importSent.length),0);
  await page.locator("#importConfirm").check();
  await page.locator("#importGo").click();
  assert.equal(await page.evaluate(()=>window.__importSent[0].capacity_token),"demo-import-review");
  assert.deepEqual(errors,[]);
  console.log("Import review desktop/mobile dark/light and signed submit checks passed");
} finally {await browser.close();}
