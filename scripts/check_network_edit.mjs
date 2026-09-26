// Local demo-only browser regression. No real workload or snapshot mutations.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { chromium } from "playwright";
await mkdir("release-assets/network-edit-checks", {recursive: true});
const browser = await chromium.launch({headless: true});
try {
  const page = await browser.newPage({viewport: {width: 1280, height: 1000}});
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.goto("http://127.0.0.1:4174/?demo=1", {waitUntil: "networkidle"});
  await page.evaluate(() => {
    const original = api;
    window.__sent = [];
    api = async (url, init) => {
      if (url === "/api/lh/snapshot-progress?volume=pvc-demo") return {known: true, active: true, percent: 51, errors: []};
      if (url === "/api/network/services") { window.__sent.push(JSON.parse(init.body)); return {ok: true, message: "Service updated"}; }
      const result = await original(url, init);
      if (url === "/api/network") {
        result.shared_vip = {ip: "192.0.2.108"};
        result.registered_vips = [{ip: "192.0.2.108", free: true, used_by: [], label: "Default apps"}, {ip: "192.0.2.109", free: true, used_by: [], label: "Chosen apps"}];
        result.workloads.push({namespace: "lab", name: "new-vm", kind: "VirtualMachine", ports: []});
        const svc = result.services.find(s => s.name === "frigate");
        Object.assign(svc, {uid: "svc-uid", resource_version: "7", type: "LoadBalancer", requested_ips: ["192.0.2.109"], targets: ["frigate"], ports: [{name: "web", port: 5000, target_port: 5001, protocol: "TCP"}]});
      }
      return result;
    };
    STATE.platform = {...STATE.platform, distribution: "k3s", load_balancer: "kube-vip", servicelb: true};
    return wlEdit("lab", "frigate");
  });
  await page.locator("#e_workload_name").fill("unsaved-name");
  await page.getByRole("button", {name: "Configure default / selected VIP", exact: true}).click();
  assert.equal(await page.locator("#net_existing").inputValue(), "frigate");
  assert.equal(await page.locator(".np-target").inputValue(), "5001");
  await page.locator("#net_mode").selectOption("shared");
  await page.getByRole("button", {name: "Review plan", exact: true}).click();
  await page.getByRole("button", {name: "Update service", exact: true}).waitFor();
  // Changed choices cannot submit the old review.
  await page.locator("#net_mode").selectOption("manual");
  assert.equal(await page.getByRole("button", {name: "Update service", exact: true}).count(), 0);
  await page.locator("#net_lb_pick").selectOption("192.0.2.108");
  for (const width of [1280, 390, 320]) {
    await page.setViewportSize({width, height: 1000});
    const bounds = await page.locator("#mbody").evaluate(e => ({w: e.clientWidth, s: e.scrollWidth}));
    assert.ok(bounds.s <= bounds.w + 1, `VIP editor ${width}: overflow`);
    await page.screenshot({path: `release-assets/network-edit-checks/service-${width}.png`});
  }
  await page.getByRole("button", {name: "Review plan", exact: true}).click();
  await page.getByRole("button", {name: "Update service", exact: true}).click();
  await page.locator("#e_workload_name").waitFor();
  assert.equal(await page.locator("#e_workload_name").inputValue(), "unsaved-name");
  const saved = await page.evaluate(() => window.__sent[0]);
  assert.equal(saved.update, true); assert.equal(saved.uid, "svc-uid");
  assert.equal(saved.ports[0].name, "web", "VIP-only updates preserve named Ingress port references");
  assert.equal(saved.ports[0].target_port, "5001"); assert.equal(saved.vip, "192.0.2.108");
  await page.evaluate(() => { closeModal(); return vmNew(); });
  await page.locator("#v_service").selectOption("manual");
  await page.locator("#vsvc_lb_pick").selectOption("192.0.2.109");
  assert.equal(await page.locator("#v_service_pick").isVisible(), true);
  await page.locator("#v_name").fill("new-vm");
  await page.locator("#v_pass").fill("demo-only-password");
  await page.getByRole("button", {name: "Create VM", exact: true}).click();
  await page.locator("#net_workload").waitFor();
  assert.equal(await page.locator("#net_workload").inputValue(), "lab/new-vm/VirtualMachine");
  assert.equal(await page.locator("#net_lb_ip").inputValue(), "192.0.2.109");
  await page.evaluate(() => {closeModal(); return vmEdit("lab", "new-vm");});
  await page.locator("#mbody .seg button").filter({hasText: /^Network/}).click();
  await page.getByRole("button", {name: "Configure default / selected VIP", exact: true}).click();
  assert.equal(await page.locator("#net_workload").inputValue(), "lab/new-vm/VirtualMachine");
  await page.getByRole("button", {name: "Cancel", exact: true}).click();
  await page.locator("#ve_mem").waitFor({state: "attached"});
  await page.evaluate(() => {closeModal(); return lhSnaps("pvc-demo", "Media");});
  await page.getByRole("progressbar", {name: "Longhorn volume cleanup"}).waitFor();
  assert.equal(await page.getByRole("progressbar", {name: "Longhorn volume cleanup"}).getAttribute("aria-valuenow"), "51");
  await page.screenshot({path: "release-assets/network-edit-checks/snapshot-progress.png"});
  await page.evaluate(() => closeModal());
  assert.deepEqual(errors, []);
  console.log("Container VIP edit preserves unsaved fields; selected VM VIP setup; live snapshot purge progress; mobile bounds passed");
} finally {await browser.close();}
