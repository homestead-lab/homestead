// Real browser editor flow, against the local demo only; no cluster mutations.
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const web = fileURLToPath(new URL("../../web/", import.meta.url));
const output = process.env.FIREWALL_SCREENSHOTS || "release-assets/firewall";
const types = {".js":"application/javascript", ".css":"text/css", ".html":"text/html", ".svg":"image/svg+xml"};
const server = http.createServer(async (req, res) => {
  try {
    const pathname = new URL(req.url, "http://localhost").pathname;
    const target = path.resolve(web, "." + (pathname === "/" ? "/index.html" : pathname));
    if (!target.startsWith(web)) throw new Error("outside fixture");
    const body = await fs.readFile(target);
    res.writeHead(200, {"Content-Type":types[path.extname(target)] || "application/octet-stream"}); res.end(body);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${server.address().port}`;
let browser;
try {
  await fs.mkdir(output, {recursive:true});
  browser = await chromium.launch({headless:true});
  for (const width of [1440, 375]) {
    const context = await browser.newContext({viewport:{width,height:1000}});
    const page = await context.newPage(), errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await page.goto(`${base}/?demo=1`);
    await page.waitForFunction(() => typeof firewallEdit === "function" && ME && document.querySelector("#gate").classList.contains("hidden"));
    await page.evaluate(async () => { go("network"); networkTab("firewall"); await viewFirewall(); });
    await page.getByRole("button", {name:"＋ New policy",exact:true}).click();
    await page.locator("#fw_name").fill("homestead-fw-browser-test");
    await page.locator("#fw_preset").selectOption("web");
    assert.equal(await page.locator("#fw_apply").isDisabled(), true);
    await page.locator("#fw_review_button").click();
    await page.waitForFunction(() => !document.getElementById("fw_apply").disabled);
    let cfg = await page.evaluate(() => FIREWALL_REVIEW);
    assert.equal(cfg.ingress, "restricted");
    assert.equal(cfg.egress, "unchanged");
    assert.equal(cfg.ingress_rules[0].ports, "80, 443");
    await page.locator(".fw-ports").fill("8080");
    assert.equal(await page.locator("#fw_apply").isDisabled(), true);
    await page.locator("#fw_preset").selectOption("isolate");
    await page.locator("#fw_review_button").click();
    await page.waitForFunction(() => !document.getElementById("fw_apply").disabled);
    cfg = await page.evaluate(() => FIREWALL_REVIEW);
    assert.equal(cfg.allow_dns, true);
    assert.equal(cfg.egress, "restricted");
    assert.deepEqual(cfg.ingress_rules, []);
    await page.screenshot({path:`${output}/review-${width}.png`,fullPage:true});
    assert.equal(await page.evaluate(() => document.getElementById("mbody").scrollWidth > document.getElementById("mbody").clientWidth + 1), false);
    await page.locator("#fw_apply").click();
    await page.waitForFunction(() => document.getElementById("modal").classList.contains("hidden"));
    await page.waitForFunction(() => STATE.data.firewall.policies.some(p => p.name === "homestead-fw-browser-test"));
    await page.screenshot({path:`${output}/policies-${width}.png`,fullPage:true});
    const ns = await page.evaluate(() => STATE.data.firewall.policies.find(p => p.name === "homestead-fw-browser-test").namespace);
    await page.evaluate(async ns => { await firewallEdit(ns, "homestead-fw-browser-test"); }, ns);
    assert.equal(await page.locator("#fw_target").isDisabled(), true);
    assert.equal(await page.locator("#fw_name").isDisabled(), true);
    assert.equal(await page.locator("#fw_dns").isChecked(), true);
    // A late preview must not re-enable Apply after the form changed.
    await page.evaluate(() => {
      window.originalFirewallPost = firewallPost;
      window.originalApi = api;
      window.api = async (url, options) => {
        if (url.endsWith("/firewall/preview")) await new Promise(resolve => { window.finishFirewallPreview = resolve; });
        return originalApi(url, options);
      };
      firewallReview();
    });
    await page.waitForFunction(() => typeof finishFirewallPreview === "function");
    await page.locator("#fw_dns").uncheck();
    await page.evaluate(() => finishFirewallPreview());
    await page.waitForTimeout(100);
    assert.equal(await page.locator("#fw_apply").isDisabled(), true);
    // A delayed save must not close a different editor opened in the meantime.
    await page.evaluate(async () => {
      api = originalApi;
      await firewallReview();
      window.api = async (url, options) => {
        if (url.endsWith("/firewall/save")) await new Promise(resolve => { window.finishFirewallSave = resolve; });
        return originalApi(url, options);
      };
      window.pendingFirewallSave = firewallSave();
      closeModal();
      await firewallEdit();
    });
    await page.locator("#fw_name").fill("homestead-fw-next-editor");
    await page.evaluate(async () => { finishFirewallSave(); await pendingFirewallSave; api = originalApi; });
    assert.equal(await page.locator("#fw_name").inputValue(), "homestead-fw-next-editor");
    assert.equal(await page.locator("#fw_editor").isVisible(), true);
    // A delayed delete must not repaint Firewall after leaving Networking.
    await page.evaluate(ns => {
      closeModal(); firewallRemove(ns, "homestead-fw-browser-test");
      window.api = async (url, options) => {
        if (url.endsWith("/firewall/delete")) await new Promise(resolve => { window.finishFirewallDelete = resolve; });
        return originalApi(url, options);
      };
      window.pendingFirewallDelete = firewallDeleteConfirmed();
      closeModal(); go("workloads");
    }, ns);
    await page.waitForFunction(() => STATE.view === "workloads");
    await page.evaluate(async () => { finishFirewallDelete(); await pendingFirewallDelete; api = originalApi; });
    assert.equal(await page.getByRole("button", {name:"＋ New policy",exact:true}).count(), 0);
    assert.equal(await page.evaluate(() => STATE.view), "workloads");
    await page.evaluate(async () => { go("network"); networkTab("firewall"); await viewFirewall(); });
    await page.waitForFunction(() => !STATE.data.firewall.policies.some(p => p.name === "homestead-fw-browser-test"));
    assert.deepEqual(errors, []);
    await context.close();
  }
  console.log(`Firewall create/review/edit/remove passed at 1440px and 375px. Screenshots: ${output}`);
} finally {
  if (browser) await browser.close();
  await new Promise(resolve => server.close(resolve));
}
