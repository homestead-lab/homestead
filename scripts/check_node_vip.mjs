// Demo-only: reproduce ServiceLB node access without changing a real cluster.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { chromium } from "playwright";
await mkdir("release-assets/vip-access-checks", {recursive:true});
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage({viewport:{width:1280,height:1100}});
  const errors=[]; page.on("pageerror", e=>errors.push(e.message));
  await page.goto("http://127.0.0.1:4174/?demo=1", {waitUntil:"networkidle"});
  await page.evaluate(async () => {
    const original=api;
    api=async (url,init) => {
      const result=await original(url,init);
      if(url==="/api/network") {
        result.node_ips=["192.0.2.109"];
        result.shared_vip={ip:"192.0.2.108"};
        result.registered_vips=[
          {ip:"192.0.2.108",label:"Workload VIP",free:true},
          {ip:"192.0.2.13",label:"Spare VIP",free:true},
          {ip:"192.0.2.109",free:true}];
        result.available_vips=["192.0.2.109"];
        result.vips=[{ip:"192.0.2.109",services:3,listeners:[{port:3000,protocol:"TCP"}]}];
        result.services=[{namespace:"lab",name:"speedtest",type:"LoadBalancer",uid:"existing",resource_version:"7",
          lb_class:"",requested_ips:[],external_ips:["192.0.2.109"],targets:["speedtest"],
          ports:[{name:"web",port:3000,target_port:3000,protocol:"TCP"}]}];
        result.workloads=[{namespace:"lab",name:"speedtest",kind:"Deployment",ports:[{port:3000}]}];
      }
      return result;
    };
    STATE.platform={...STATE.platform,distribution:"k3s",load_balancer:"kube-vip",servicelb:true};
    await networkManage("lab","speedtest");
  });
  assert.match(await page.locator("#net_current_access").innerText(),/192.0.2.109:3000/);
  assert.match(await page.locator("#net_current_access").innerText(),/not a VIP/);
  assert.equal(await page.locator("input[name=net_access][value=nodes]").isChecked(),true);
  assert.equal(await page.locator("#net_lb_ip").inputValue(),"");
  assert.equal(await page.locator('input[name=net_address][value="192.0.2.109"]').count(),0);
  await page.locator("input[name=net_access][value=shared]").check();
  const cfg=await page.evaluate(()=>networkConfig());
  assert.equal(cfg.update,false); assert.equal(cfg.name,"speedtest-vip"); assert.equal(cfg.uid,"");
  assert.equal(cfg.vip_mode,"shared"); assert.equal(cfg.ports[0].name,"web");
  assert.match(await page.locator("#net_change_note").innerText(),/nothing is removed/);
  await page.getByRole("button",{name:"Review changes",exact:true}).click();
  await page.getByRole("button",{name:"Apply changes",exact:true}).waitFor();
  await page.locator("input[name=net_access][value=manual]").check();
  assert.equal(await page.getByRole("button",{name:"Apply changes",exact:true}).count(),0);
  await page.locator('input[name=net_address][value="192.0.2.13"]').check();
  assert.equal(await page.evaluate(()=>networkConfig().vip),"192.0.2.13");
  for(const theme of ["dark","light"]) {
    await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
    for(const width of [1280,390,320]) {
      await page.setViewportSize({width,height:1100});
      const bounds=await page.locator("#mbody").evaluate(e=>({w:e.clientWidth,s:e.scrollWidth}));
      assert.ok(bounds.s<=bounds.w+1,theme+" "+width+" overflow");
      await page.screenshot({path:`release-assets/vip-access-checks/${theme}-${width}.png`});
    }
  }
  await page.locator("input[name=net_access][value=nodes]").check();
  assert.equal(await page.evaluate(()=>networkConfig().update),true);
  assert.equal(await page.evaluate(()=>networkConfig().uid),"existing");
  assert.deepEqual(errors,[]);
  console.log("Node access is not a VIP; safe additional connection; review invalidation; dark/light/mobile passed");
} finally {await browser.close();}
