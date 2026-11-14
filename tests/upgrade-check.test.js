"use strict";

/* The upgrade check on the page: each upgradable row's verdict, the review's
   findings and tick, the pair running now, and its Updates insight
   (homestead_upgrade_check). Unknown is "Not checked", never a pass (#620). */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const H = require("../web/js/insights.js");

function page() {
  const ctx = { console, Date, Map, Set, Promise, document: { addEventListener() {} }, STATE: { data: {} },
    $: () => null, $$: () => [], icon: () => "", safeHref: String,
    esc: v => String(v ?? "").replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`) };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/views-cluster.js", "utf8"), ctx);
  return name => vm.runInContext(name, ctx);
}
const finding = (level, title) => ({ level, title, detail: `${title}, in detail`, source: "https://example.com/source" });
const check = (verdict, findings) => ({ to: "v1.37.1+k3s1", verdict, findings, checked: "2026-10-23" });
const row = c => ({ id: "cluster", name: "k3s", installed: "v1.36.4+k3s1", newest: "v1.37.1+k3s1", next: "v1.37.1+k3s1",
  behind: true, how: "suc", ...c });

test("an upgradable row says the verdict and its top finding", () => {
  const card = page()("componentsCard");
  const blocked = card({ components: [row({ check: check("blocked", [finding("info", "Fine"), finding("block", "KubeVirt 1.9 is not tested on Kubernetes 1.37")]) })] });
  assert.match(blocked, /ui-chip bad[^"]*">Check first</);
  assert.match(blocked, /KubeVirt 1\.9 is not tested on Kubernetes 1\.37/);
  assert.match(card({ components: [row({ check: check("clear", [finding("info", "Fine")]) })] }), /ui-chip ok[^"]*">No known breaks</);
});

test("a problem already true and left as it is reads as no new problems, with its own level", () => {
  const f = finding("already", "KubeVirt 1.9 on Kubernetes 1.37 is already outside its support matrix; this upgrade does not change that");
  const unchanged = { ...check("unchanged", [f]), to: "v1.37.2+k3s1" };
  const html = page()("componentsCard")({ components: [row({ check: unchanged })] });
  assert.match(html, /ui-chip ok[^"]*">No new problems</);
  assert.match(html, /Nothing in Homestead(?:'|&#39;)s table gets worse with v1\.37\.2\./);
  assert.doesNotMatch(html, /Worth checking|Read first/);
  const section = page()("componentCheckSection")(unchanged);
  assert.match(section, /ui-chip neutral[^"]*">Already true</);
  assert.doesNotMatch(section, /cmp_check_ack/);
  const mixed = page()("componentsCard")({ components: [row({ check: check("check", [f, finding("warn", "Something new")]) })] });
  assert.match(mixed, /Something new/);
  assert.doesNotMatch(mixed, /gets worse/);
});

test("a check with nothing checked reads as not checked, never as a pass", () => {
  const html = page()("componentsCard")({ components: [row({ check: check("unchecked", []) })] });
  assert.match(html, />Not checked</);
  assert.doesNotMatch(html, /No known breaks/);
});

test("manifest installs offer explicit management even when their version is current", () => {
  const card = page()("componentsCard");
  const c = row({id:'cdi',name:'CDI',how:'manual',next:'',behind:false,can_manage:true});
  const html = card({components:[c]});
  assert.match(html, /Enable upgrades/);
  assert.match(html, /componentManageOperator/);
  assert.doesNotMatch(card({components:[{...c,can_manage:false}]}), /Enable upgrades/);
});

test("the review lists every finding with its source, and asks for a tick only when blocked", () => {
  const section = page()("componentCheckSection");
  const blocked = section(check("blocked", [finding("block", "Outside"), finding("info", "Fine")]));
  assert.match(blocked, /Outside/);
  assert.match(blocked, /Fine/);
  assert.match(blocked, /href="https:\/\/example\.com\/source"/);
  assert.match(blocked, /id="cmp_check_ack"/);
  assert.match(blocked, /2026-10-23/);
  assert.doesNotMatch(section(check("check", [finding("warn", "Read first")])), /cmp_check_ack/);
  assert.equal(section(undefined), "", "no check, no section");
});

test("the card shows the pair running now", () => {
  const html = page()("componentsCard")({ components: [row({})], pairs: [
    { id: "kubevirt-kubernetes", status: "outside", title: "KubeVirt 1.9 on Kubernetes 1.37", detail: "d", source: "https://example.com/m" }] });
  assert.match(html, /KubeVirt 1\.9 on Kubernetes 1\.37/);
  assert.match(html, /ui-chip warn[^"]*">Outside its support matrix</);
});

test("the Updates insight lists a pair outside the matrix, or past Kubernetes' end of life, and nothing else", () => {
  const pair = (status, title) => ({ id: "kubevirt-kubernetes", status, title, detail: "d" });
  const rows = H.pairRows({ pairs: [pair("outside", "A"), pair("eol", "B"), pair("supported", "C"), pair("unchecked", "D")] });
  assert.deepEqual(rows.map(r => [r.title, r.severity]), [["A", "medium"], ["B", "low"]]);
  assert.deepEqual(H.pairRows(null), []);
});
