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
        result.registered_vips = [{ ip: "192.0.2.108", label: "Workload VIP", default: true, free: true, used_by: [] }];
        result.workloads.unshift({ namespace: "lab", name: "guest", kind: "VirtualMachine", ports: [] });
      }
      return result;
    };
    STATE.platform = { distribution: "k3s", load_balancer: "kube-vip", servicelb: true };
    go("network");
  });
  await page.getByText("Networking roles", { exact: true }).waitFor();
  await page.getByRole("button", { name: "Expose workload" }).click();
  await page.locator("#net_workload").waitFor();
  assert.equal(await page.locator("#net_workload").inputValue(), "lab/guest/VirtualMachine");
  assert.equal(await page.locator("#net_mode").inputValue(), "shared");
  await page.locator("#net_mode").selectOption("manual");
  await page.locator("#net_vip").fill("192.0.2.109");
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
