"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function setup() {
  const elements = Object.fromEntries(Object.entries({
    va_start: { value: "192.0.2.108" }, va_end: { value: "192.0.2.110" },
    va_kind: { value: "single" }, va_label: { value: "Shared apps" },
    va_default: { checked: false }, va_confirm: { checked: true },
    va_result: { textContent: "" }, va_save: { disabled: false },
  }).map(([k, v]) => [`#${k}`, v]));
  const requests = [], notices = [];
  const ctx = { console, STATE: { data: { ov: {} }, platform: { load_balancer: "kube-vip" } },
    $: id => elements[id], esc: value => String(value).replaceAll("<", "&lt;"),
    nodeAddressesOnly: () => false, modal: (title, html) => { ctx.html = html; },
    closeModal: () => { ctx.closed = true; }, toast: (...args) => notices.push(args),
    api: async (url, opts) => {
      requests.push({ url, body: JSON.parse(opts.body) });
      return url.endsWith("/add") ? { added: ["192.0.2.108"], skipped: [], detail: "1 address added" } : { ok: true };
    },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => (ctx.esc || String)(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/views-network.js", "utf8"), ctx);
  ctx.viewNetworking = async () => { ctx.refreshed = true; };
  return { ctx, elements, requests, notices };
}

test("node addresses are excluded from every VIP source and stale selections", async () => {
  const { ctx } = setup();
  ctx.document = {addEventListener() {}};
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/views-workloads.js", "utf8"), ctx);
  const ip = "192.0.2.109";
  const choices = await ctx.vipChoices({
    node_ips: [ip], registered_vips: [{ip, free:true}], available_vips:[ip],
    shared_vip:{ip}, vips:[{ip, services:3, listeners:[]}],
  });
  assert.equal(choices.shared, "");
  assert.equal(choices.free.length, 0);
  assert.equal(choices.own.length, 0);
  assert.equal(choices.used.length, 0);
  assert.doesNotMatch(ctx.vipPicker("test", ip, choices), /192\.0\.2\.109/);
  assert.doesNotMatch(ctx.networkVipCards(ip, choices), /192\.0\.2\.109/);
});

test("only unclassed node-address Services are treated as node access", () => {
  const {ctx} = setup();
  const data={node_ips:["192.0.2.109"], services:[{namespace:"lab",name:"speedtest-vip"}]};
  const row={type:"LoadBalancer",external_ips:["192.0.2.109"]};
  assert.equal(ctx.networkIsNodeAccess(row,data),true);
  assert.equal(ctx.networkIsNodeAccess({...row,lb_class:"kube-vip.io/kube-vip-class"},data),false);
  assert.equal(ctx.networkIsNodeAccess({...row,external_ips:[]},data),false);
  assert.equal(ctx.networkAdditionalName(data,"lab","speedtest"),"speedtest-vip-2");
});

test("VIP cards separate identity, usage and actions; default cannot be removed", () => {
  const { ctx } = setup();
  const html = ctx.networkVipCard({ ip: "192.0.2.108", free: false, label: "<media>", used_by: ["lab/media"] }, { shared_vip: { ip: "192.0.2.108" } });
  assert.match(html, /vip-card-heading/);
  assert.match(html, /Default workload VIP/);
  assert.match(html, /Use this VIP/);
  assert.match(html, /&lt;media>/);
  assert.doesNotMatch(html, /vipRemove|vipDefault/);
  const blocked = ctx.networkVipCard({ ip: "192.0.2.1", free: true }, { node_ips: ["192.0.2.1"] });
  assert.doesNotMatch(blocked, /networkExpose|vipDefault/);
});

test("add form explains activation, DHCP and default semantics", () => {
  const { ctx } = setup();
  ctx.vipAdd();
  for (const text of ["outside your router", "saving it alone", "will not move", "One VIP", "A range of VIPs"]) assert.ok(ctx.html.includes(text), text);
  ctx.STATE.platform.load_balancer = "metallb";
  ctx.vipAdd(); assert.match(ctx.html, /does not configure MetalLB/);
  ctx.STATE.platform.load_balancer = "servicelb";
  ctx.vipAdd(); assert.match(ctx.html, /id="va_default" type="checkbox" disabled/);
});

test("adding requires address ownership confirmation", async () => {
  const { ctx, elements, requests } = setup();
  elements["#va_confirm"].checked = false;
  await ctx.vipAddGo();
  assert.equal(requests.length, 0);
  assert.match(elements["#va_result"].textContent, /confirm/);
});

test("single add ignores hidden range end and does not change default implicitly", async () => {
  const { ctx, requests } = setup();
  await ctx.vipAddGo();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body.end, "");
  assert.equal(ctx.closed, true);
});

test("explicit default uses first requested address after range save", async () => {
  const { ctx, elements, requests } = setup();
  elements["#va_kind"].value = "range";
  elements["#va_default"].checked = true;
  await ctx.vipAddGo();
  assert.equal(requests[0].body.end, "192.0.2.110");
  assert.equal(requests[1].url, "/api/network/vips/default");
  assert.equal(requests[1].body.ip, "192.0.2.108");
  assert.equal(ctx.STATE.data.ov.lb_ip, "192.0.2.108");
});

test("failed default retains successful reservation and explains recovery", async () => {
  const { ctx, elements } = setup();
  elements["#va_default"].checked = true;
  const original = ctx.api;
  ctx.api = (url, opts) => url.endsWith("/default") ? Promise.reject(new Error("Provider unavailable")) : original(url, opts);
  await ctx.vipAddGo();
  assert.match(elements["#va_result"].textContent, /Saved addresses remain available/);
  assert.equal(ctx.closed, undefined);
  assert.equal(ctx.refreshed, true);
  assert.equal(elements["#va_save"].disabled, false);
});

test("partial range additions stay visible and never silently choose another default", async () => {
  const { ctx, elements, requests } = setup();
  elements["#va_default"].checked = true;
  elements["#va_kind"].value = "range";
  const original = ctx.api;
  ctx.api = (url, opts) => url.endsWith("/add") ? Promise.resolve({ added: ["192.0.2.109"], skipped: ["192.0.2.108 is unavailable"], detail: "1 added; 1 skipped" }) : original(url, opts);
  await ctx.vipAddGo();
  assert.equal(requests[0].body.ip, "192.0.2.108");
  assert.match(elements["#va_result"].textContent, /skipped/);
  assert.equal(ctx.closed, undefined);
});
