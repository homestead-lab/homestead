// Exercise the browser's real version-header handling while an HA rollout has
// only one new replica. API responses are fixtures; no cluster is contacted.
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const web=fileURLToPath(new URL("../../web/",import.meta.url));
const types={".js":"application/javascript",".css":"text/css",".html":"text/html",".svg":"image/svg+xml"};
const server=http.createServer(async(req,res)=>{
  try {
    const url=new URL(req.url,"http://localhost");
    const target=path.resolve(web,"."+(url.pathname==="/"?"/index.html":url.pathname));
    if(!target.startsWith(web))throw new Error("outside fixture");
    const body=await fs.readFile(target);
    res.writeHead(200,{"Content-Type":types[path.extname(target)]||"application/octet-stream"});res.end(body);
  }catch {res.writeHead(404);res.end();}
});
await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
const base=`http://127.0.0.1:${server.address().port}`;
const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r;});return{resolve,promise};};
async function within(promise) {
  let timer;
  try {return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error("HA update did not reach the second-replica wait")),10000);})]);}
  finally {clearTimeout(timer);}
}
let browser;
try {
  browser=await chromium.launch({headless:true});
  for(const pair of ["local","linked"]) {
    const context=await browser.newContext(),page=await context.newPage();
    const second=deferred(),release=deferred(),applied=[],errors=[];
    let polls=0,navigations=0;
    page.on("pageerror",e=>errors.push(e.message));
    page.on("dialog",dialog=>dialog.accept());
    await page.addInitScript(()=>{window.__fixtureNativeFetch=window.fetch.bind(window);});
    const demo=await fs.readFile(new URL('../../web/js/demo.js',import.meta.url),'utf8');
    await page.route('**/js/demo.js*',route=>route.fulfill({contentType:'application/javascript',body:demo+`
      (()=>{const demoFetch=window.fetch;window.fetch=(url,opts)=>
        window.__fixtureRollouts && typeof url==='string' && url.startsWith('/api/image-updates/')
          ? window.__fixtureNativeFetch(url,opts) : demoFetch(url,opts);})();
    `}));
    await page.goto(`${base}/?demo=1`);
    await page.waitForFunction(()=>typeof reviewImageActions==="function"&&document.querySelector("#gate").classList.contains("hidden")&&FLEET.view?.self);
    const here=await page.evaluate(()=>FLEET.view.self),there="b2c0de";
    page.on("request",req=>{if(req.isNavigationRequest()&&req.frame()===page.mainFrame())navigations++;});
    await page.route("**/api/image-updates/**",async route=>{
      const req=route.request(),url=new URL(req.url()),cluster=req.headers()["x-homestead-cluster"];
      const body=req.postDataJSON();
      // The review also reads "What's in this update" for each copy. Not a
      // rollout request: answer it as unavailable, from a server that has not
      // changed version, so it neither blocks nor announces anything.
      if(url.pathname.endsWith("/notes")) {
        await route.fulfill({status:200,contentType:"application/json",
          body:JSON.stringify({ns:body.ns,name:body.name,images:[],error:"not read in this test"})});
        return;
      }
      const identity={uid:cluster,generation:2,desired:pair===(cluster===here?"local":"linked")?2:1};
      let data;
      if(url.pathname.endsWith("/preview")) {
        data={capacity:{blocked:false,warnings:[],candidates:[]},capacity_token:`review-${cluster}`,
          images:[{container:"homestead",before:"example/app:1",after:"example/app:2"}]};
      }else if(url.pathname.endsWith("/apply")) {
        assert.equal(body.capacity_token,`review-${cluster}`);applied.push(cluster);
        data={...identity,phase:"starting",ready:0};
      }else if(url.pathname.endsWith("/progress")) {
        if(identity.desired===2) {
          polls++;
          if(polls===2){second.resolve();await release.promise;}
        }
        const waiting=identity.desired===2&&polls===1;
        data={...identity,phase:waiting?"progressing":"ready",ready:waiting?1:identity.desired};
      }else throw new Error("Unexpected rollout request");
      // Even if a remote version header reaches the browser, it must not
      // announce this page's update. The local first pod really has changed.
      const headers=cluster!==here||applied.includes(here)?{"X-Homestead-Version":"9.9.9"}:{};
      await route.fulfill({status:200,contentType:"application/json",headers,body:JSON.stringify(data)});
    });
    try {
      await page.evaluate(({there})=>{
        window.__fixtureRollouts=true;
        FLEET.target=there; // A previous click on a linked row must not reroute the local rollout.
        return reviewImageActions([{ns:'lab',name:'homestead',part:'self'},
          {ns:'lab',name:'homestead',part:'self',cluster:there,clusterName:'Branch'}]);
      },{there});
      await page.locator('#imageCapacityApprove').check();
      await page.locator('#imageCapacityApply').click();
      await within(second.promise);
      assert.equal(navigations,0,`${pair} pair: one new replica must not reload the page`);
      assert.deepEqual(applied,pair==='linked'?[there]:[there,here],"linked work finishes before the local rollout starts");
      assert.equal(await page.locator('#imageQueue').count(),1);
      const reload=page.waitForEvent('request',{predicate:req=>req.isNavigationRequest()&&req.frame()===page.mainFrame(),timeout:10000});
      release.resolve();await reload;
      assert.deepEqual(applied,[there,here]); assert.equal(polls,2);
      assert.equal(navigations,1,"reload happens once, after both clusters finish");
      assert.deepEqual(errors,[]);
      console.log(`HA update (${pair} pair): both copies ready, linked cluster first, one reload at completion.`);
    }catch(error) {
      console.error({pair,applied,polls,errors,version:await page.evaluate(()=>({pending:window.__homesteadQueuedVersion,
        updated:window.__homesteadUpdated,marked:sessionStorage.getItem('homestead.selfUpdate'),
        hold:!!window.homesteadUpdateQueueActive,bar:document.querySelector('.homestead-updated')?.innerText})),
        queue:await page.locator('#imageQueue').innerText().catch(()=>"unavailable")});
      throw error;
    }finally {release.resolve();await context.close();}
  }
}finally {await browser?.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
