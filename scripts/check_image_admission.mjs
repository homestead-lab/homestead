// Demo-only browser regression; never reaches a live cluster.
import assert from "node:assert/strict";
import {mkdir} from "node:fs/promises";
import {chromium} from "playwright";
await mkdir("release-assets/image-admission-checks",{recursive:true});
const browser=await chromium.launch({headless:true});
try {
  const page=await browser.newPage({viewport:{width:1280,height:1100}});
  const errors=[]; page.on("pageerror",e=>errors.push(e.message));
  await page.goto("http://127.0.0.1:4174/?demo=1",{waitUntil:"networkidle"});
  await page.evaluate(async()=>{
    const original=api;
    window.__imageSent=[];
    window.__failImage="";
    api=async (url,init)=>{
      if(url==="/api/image-updates/apply" || url==="/api/image-updates/rollback"){
        const body=JSON.parse(init.body); window.__imageSent.push(body);
        return {uid:body.name,generation:9,phase:"progressing",ready:0,desired:1};
      }
      if(url.startsWith("/api/image-updates/progress")){
        const name=new URL(url,location.origin).searchParams.get("name");
        return {uid:name,generation:9,phase:window.__failImage===name ? "failed" : "ready",ready:1,desired:1};
      }
      return original(url,init);
    };
    return imageUpdateReview("lab","frigate");
  });
  await page.locator("#imageCapacityApprove").waitFor();
  assert.equal(await page.locator("#imageCapacityApply").isDisabled(),true);
  assert.match(await page.locator("#mbody").innerText(),/Recreate/);
  for(const theme of ["dark","light"]){
    await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
    for(const width of [1280,390,320]){
      await page.setViewportSize({width,height:1100});
      const box=await page.locator("#mbody").evaluate(el=>({w:el.clientWidth,s:el.scrollWidth}));
      assert.ok(box.s<=box.w+1,theme+" "+width+" overflow");
      await page.screenshot({path:`release-assets/image-admission-checks/${theme}-${width}.png`});
      await page.locator("#imageCapacityApprove").scrollIntoViewIfNeeded();
      await page.screenshot({path:`release-assets/image-admission-checks/${theme}-${width}-approval.png`});
    }
  }
  await page.locator("#imageCapacityApprove").check();
  await page.locator("#imageCapacityApply").click();
  await page.getByText("finished",{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>window.__imageSent[0].capacity_token),"demo-image-review");
  await page.evaluate(()=>{closeModal(); return imageRollback("lab","frigate");});
  await page.locator("#imageCapacityApprove").check();
  await page.locator("#imageCapacityApply").click();
  await page.getByText("finished",{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>window.__imageSent[1].action),"rollback");
  await page.evaluate(async()=>{
    closeModal(); window.__imageSent=[]; window.__failImage="frigate";
    return reviewImageActions([{ns:"lab",name:"homestead"},{ns:"lab",name:"frigate"},{ns:"lab",name:"plex"}]);
  });
  await page.locator("#imageCapacityApprove").check();
  await page.locator("#imageCapacityApply").click();
  await page.getByText("queue stopped",{exact:true}).waitFor();
  assert.deepEqual(await page.evaluate(()=>window.__imageSent.map(x=>x.name)),["frigate"]);
  assert.equal(await page.getByText("not started",{exact:true}).count(),2);
  await page.screenshot({path:"release-assets/image-admission-checks/stopped-queue.png"});
  await page.evaluate(async()=>{
    closeModal();
    const original=api;
    api=(url,init)=>url==="/api/operations/cancel-plan"
      ? Promise.resolve({image_review:{ns:"lab",name:"frigate"},can:false})
      : original(url,init);
    return cancelOperation("demo-image-job");
  });
  await page.locator("#imageCapacityApprove").waitFor();
  assert.equal(await page.locator("#imageCapacityApply").innerText(),"Start rollback");
  assert.equal(await page.evaluate(()=>window.__imageSent.length),1,"opening job recovery never mutates");
  await page.evaluate(()=>closeModal());
  assert.deepEqual(errors,[]);
  console.log("Image/rollback review, signed submit, stopped queue and dark/light/mobile checks passed");
} finally {await browser.close();}
