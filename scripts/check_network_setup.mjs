// Demo-only browser regression; no live cluster mutations.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { chromium } from "playwright";

await mkdir("release-assets/network-checks", { recursive: true });
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("http://127.0.0.1:4174/?demo=1", { waitUntil: "networkidle" });
  await page.evaluate(() => {
    const original = api;
    api = async (url, ...args) => {
      const result = await original(url, ...args);
      if (url === "/api/network") {
        result.shared_vip = { ip: "192.0.2.108" };
        result.registered_vips = [
          { ip: "192.0.2.108", label: "Shared applications", default: true, free: false, used_by: ["lab/homestead-vip", "lab/a-very-long-service-name-that-must-wrap-without-hiding-the-address"] },
          { ip: "192.0.2.109", label: "Spare address for a new workload", free: true, used_by: [] },
          { ip: "192.0.2.110", label: "Unavailable", blocked: "Reserved by the cluster management service", free: false, used_by: [] },
        ];
        result.workloads.unshift({ namespace: "lab", name: "guest", kind: "VirtualMachine", ports: [] });
      }
      return result;
    };
    STATE.platform = { distribution: "k3s", load_balancer: "kube-vip", servicelb: true };
    go("network");
  });
  await page.getByText("Networking roles", { exact: true }).waitFor();
  for (const width of [1280, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 1200 });
    const cards = await page.locator(".vip-card").evaluateAll(cards => cards.map(card => {
      const heading = card.querySelector(".vip-card-heading");
      const ip = heading.querySelector("b").getBoundingClientRect(), badge = heading.querySelector(".tag").getBoundingClientRect();
      return { width: card.clientWidth, scroll: card.scrollWidth, overlap: ip.left < badge.right && ip.right > badge.left && ip.top < badge.bottom && ip.bottom > badge.top };
    }));
    for (const card of cards) {
      assert.ok(card.scroll <= card.width + 1, `${width}: VIP card overflow`);
      assert.equal(card.overlap, false, `${width}: address overlaps status`);
    }
    await page.locator(".vip-section").screenshot({ path: `release-assets/network-checks/vip-cards-${width}.png` });
  }
  await page.getByRole("button", { name: "Add VIP", exact: false }).click();
  await page.locator("#va_start").fill("192.0.2.120");
  assert.equal(await page.locator("#va_end_wrap").isVisible(), false);
  await page.locator("#va_kind").selectOption("range");
  assert.equal(await page.locator("#va_end_wrap").isVisible(), true);
  await page.locator("#va_end").fill("192.0.2.122");
  await page.locator("#va_default").check();
  for (const width of [1280, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    const box = await page.locator("#mbody").evaluate(el => ({ width: el.clientWidth, scroll: el.scrollWidth }));
    assert.ok(box.scroll <= box.width + 1, `${width}: add VIP overflow ${JSON.stringify(box)}`);
    await page.locator("#va_save").scrollIntoViewIfNeeded();
    await page.screenshot({ path: `release-assets/network-checks/vip-add-${width}.png` });
  }
  await page.evaluate(() => closeModal());
  await page.locator(".vip-card").nth(1).getByRole("button", { name: "Use this VIP" }).click();
  // Nothing is picked for the address until someone chooses the app.
  await page.getByText("Use 192.0.2.109", {exact:true}).waitFor();
  assert.equal(await page.locator("#net_workload").inputValue(), "");
  assert.equal(await page.locator("#net_rest").isHidden(), true);
  await page.locator("#net_workload").selectOption("lab/guest/VirtualMachine");
  assert.equal(await page.locator("#net_mode").inputValue(), "manual");
  assert.equal(await page.locator("#net_lb_ip").inputValue(), "192.0.2.109");
  assert.equal(await page.evaluate(() => networkConfig().vip), "192.0.2.109");
  await page.evaluate(() => closeModal());
  await page.getByRole("button", { name: "Expose workload" }).click();
  await page.getByText("Connect an app to your network", {exact:true}).waitFor();
  await page.locator("#net_workload").waitFor({state: "attached"});
  assert.equal(await page.locator("#net_workload").inputValue(), "");
  await page.locator("#net_workload").selectOption("lab/guest/VirtualMachine");
  assert.equal(await page.locator("#net_mode").inputValue(), "shared");
  await page.locator("input[name=net_access][value=manual]").check();
  await page.locator('input[name=net_address][value="192.0.2.109"]').check();
  assert.equal(await page.evaluate(() => networkConfig().workload_kind), "VirtualMachine");
  for (const width of [1280, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    const box = await page.locator("#mbody").evaluate(el => ({ width: el.clientWidth, scroll: el.scrollWidth }));
    assert.ok(box.scroll <= box.width + 1, `${width}: modal overflow ${JSON.stringify(box)}`);
  }
  await page.screenshot({ path: "release-assets/network-checks/vm-vip-mobile.png", fullPage: true });
  await page.evaluate(() => {
    closeModal();
    document.body.insertAdjacentHTML("beforeend", '<div id="addonsCard"></div>');
    const original = api;
    api = async (url, ...args) => url === "/api/addons" ? {
      distribution: "k3s", helm_controller: true, kvm: {}, kvm_known: false,
      longhorn: { installed: true }, kubevirt: { installed: true }, kube_vip: { installed: true },
      multus: { installed: true, state: "configuration-error", repairable: true, ready: false,
        detail: "Network attachment API missing", issues: ["multus-1: CrashLoopBackOff"],
        diagnostic_command: "sudo k3s kubectl get ds multus -n kube-system" }
    } : original(url, ...args);
    return addonsPaint();
  });
  await page.getByRole("button", { name: "Repair configuration" }).waitFor();
  assert.ok(await page.getByText("configuration-error", { exact: true }).isVisible());
  await page.screenshot({ path: "release-assets/network-checks/addon-repair-mobile.png", fullPage: true });
  assert.deepEqual(errors, []);
  console.log("Networking defaults, VM exposure, Multus repair and mobile modal checks passed");
} finally { await browser.close(); }
