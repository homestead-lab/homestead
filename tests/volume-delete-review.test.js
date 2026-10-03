"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const target = {namespace: "lab", pvc_name: "prime-old-scratch", name: "pvc-retained"};
function plan(incomplete = false) {
  return {namespace: "lab", name: target.pvc_name, uid: "lh-uid", orphan: true,
    phase: "PVC already absent", inventory_complete: !incomplete, blocked: incomplete,
    blocking_reasons: incomplete ? ["Impact inventory is incomplete"] : [],
    warnings: incomplete ? ["Longhorn attachment inventory unavailable (HTTP 429)"] : [],
    consumers: [], longhorn: {name: target.name, state: "detached", attached_node: "", replicas: 2},
    pv: {name: target.name, uid: "pv-uid", reclaim_policy: "Retain"},
    actions: {detach: {complete: !incomplete}, delete_claim: {enabled: false}, delete_data: {enabled: !incomplete}}};
}
function review(api) {
  const elements = {};
  let html = "", selection = null;
  const body = {};
  Object.defineProperty(body, "innerHTML", {get: () => html, set: value => {
    html = value;
    elements["#volumeDeleteReview"] = value.includes('id="volumeDeleteReview"') ? {} : null;
    elements["#vd_confirm"] = value.includes('id="vd_confirm"') ? {value: ""} : null;
    elements["#vd_go"] = value.includes('id="vd_go"') ? {disabled: true} : null;
    selection = null;
  }});
  elements["#mbody"] = body;
  const context = {console, api, esc: String, jsArg: s => JSON.stringify(String(s ?? "")),
    jsq: JSON.stringify, $: selector => elements[selector], toast: () => {},
    document: {querySelector: () => selection}, modal: (_, content) => {body.innerHTML = content;}};
  context.window = context;
  vm.createContext(context);
  require("./helpers/load-ui")(context);
  vm.runInContext(fs.readFileSync("web/js/views-storage.js", "utf8"), context);
  return {context, elements, html: () => html, replace: value => {body.innerHTML = value;},
    confirm: () => {selection = {value: "delete_data"}; elements["#vd_confirm"].value = target.pvc_name; context.volumeDeleteGate();}};
}

test("incomplete inventory reports detached but unverified and keeps deletion blocked", async () => {
  const r = review(async () => plan(true));
  await r.context.volumeDelete(target);
  assert.match(r.html(), /Longhorn reports detached; safety checks are incomplete/);
  assert.match(r.html(), /No workload references found in the available inventory/);
  assert.doesNotMatch(r.html(), /Stop and unmount this claim first/);
  assert.match(r.html(), /Refresh review/);
  r.confirm();
  assert.equal(r.elements["#vd_go"].disabled, true);
});

test("refresh reads the exact backing volume again and requires a fresh confirmation", async () => {
  const calls = [];
  let resolve;
  const r = review((path, init) => {
    calls.push({path, init});
    return calls.length === 1 ? Promise.resolve(plan(true)) : new Promise(done => {resolve = done;});
  });
  await r.context.volumeDelete(target);
  r.confirm();
  const pending = r.context.volumeDeleteRefresh();
  assert.equal(r.context.__volumeDeletePlan, null);
  assert.equal(r.elements["#vd_confirm"], null);
  assert.equal(calls[1].path, calls[0].path);
  assert.match(calls[1].path, /ns=lab&name=prime-old-scratch&volume=pvc-retained$/);
  assert.ok(calls.every(call => !call.init));
  resolve(plan());
  await pending;
  assert.equal(r.elements["#vd_confirm"].value, "");
  assert.equal(r.elements["#vd_go"].disabled, true);
  r.confirm();
  assert.equal(r.elements["#vd_go"].disabled, false);
});

test("a failed refresh discards the previous plan and offers another read", async () => {
  let fail = false;
  const r = review(async () => {if (fail) throw new Error("Unavailable"); return plan();});
  await r.context.volumeDelete(target);
  r.confirm();
  fail = true;
  await r.context.volumeDeleteRefresh();
  assert.equal(r.context.__volumeDeletePlan, null);
  assert.match(r.html(), /Impact check failed/);
  assert.match(r.html(), /Refresh review/);
  assert.doesNotMatch(r.html(), /volumeDeleteNow|vd_confirm/);
});

test("late success and failure cannot overwrite another dialog or a newer review", async () => {
  for (const fail of [false, true]) {
    const pendingCalls = [];
    const r = review(() => new Promise((resolve, reject) => pendingCalls.push({resolve, reject})));
    const old = r.context.volumeDelete(target);
    const newer = r.context.volumeDelete({...target, name: "another-volume"});
    if (fail) pendingCalls[0].reject(new Error("Unavailable")); else pendingCalls[0].resolve(plan());
    await old;
    assert.match(r.html(), /checking mounts/);
    assert.equal(r.context.__volumeDeletePlan, null);
    r.replace("Another dialog");
    if (fail) pendingCalls[1].reject(new Error("Unavailable")); else pendingCalls[1].resolve(plan());
    await newer;
    assert.equal(r.html(), "Another dialog");
    assert.equal(r.context.__volumeDeletePlan, null);
  }
});

test("an attached volume still asks the user to stop and unmount it", async () => {
  const p = plan(true);
  p.longhorn.state = "attached";
  p.longhorn.attached_node = "k3s-2";
  const r = review(async () => p);
  await r.context.volumeDelete(target);
  assert.match(r.html(), /Stop and unmount this claim first/);
  assert.doesNotMatch(r.html(), /Longhorn reports detached/);
});
