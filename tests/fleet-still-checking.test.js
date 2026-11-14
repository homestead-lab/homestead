"use strict";

/* A linked cluster this Homestead is still asking for its summary and has never
   heard from (the server sends checking: true, answered: false) reads "still
   checking" with a neutral dot, not red "not answering", in the cluster menu
   and on the Fleet page; the move/copy picker lists it, disabled. The last
   answer wins: one that answered and is down still reads "not answering",
   also while it is asked again (#1083). */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

function setup() {
  const modals = [];
  const ctx = { console, Promise, JSON, STATE: { view: "settings", data: {} },
    document: { addEventListener() {} }, $: () => null, $$: () => [],
    esc: s => String(s ?? "").replaceAll("<", "&lt;").replaceAll('"', "&quot;"), icon: () => "",
    localStorage: { getItem: () => null, setItem() {} }, location: { pathname: "/", search: "" },
    toast() {}, closeModal() {}, modal: (title, body) => modals.push({ title, body }), api: async () => ({}) };
  ctx.window = ctx; vm.createContext(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/fleet.js", "utf8"), ctx);
  return { ctx, modals };
}

// As /api/fleet sends them (server/homestead_fleet.py, summary()).
const VIEW = { self: "a", linked: true, members: [
  { id: "a", handle: "home", name: "Home", self: true, url: "http://192.0.2.10:8088", version: "2.8.1", reachable: true, error: "", compatible: true, checking: false, answered: true },
  { id: "b", handle: "branch", name: "Branch", url: "http://192.0.2.11:8088", version: "2.8.1", reachable: true, error: "", compatible: true, checking: false, answered: true },
  { id: "c", handle: "lab", name: "Lab", url: "http://192.0.2.12:8088", version: "", reachable: false, error: "still checking", compatible: true, checking: true, answered: false },
  { id: "d", handle: "dr", name: "DR", url: "http://192.0.2.13:8088", version: "", reachable: false, error: "no answer from http://192.0.2.13:8088", compatible: true, checking: false, answered: true },
  // Its last check failed and it is being asked again: _pending() keeps that answer.
  { id: "e", handle: "shed", name: "Shed", url: "http://192.0.2.14:8088", version: "", reachable: false, error: "no answer from http://192.0.2.14:8088", compatible: true, checking: true, answered: true }] };

// The markup of one member: each is a button (menu, picker) or a list item (Fleet page).
const part = (html, name) => {
  const found = html.split(/(?=<button\b|<li>)/).find(p => p.includes(`<b>${name}</b>`));
  assert.ok(found, `${name} is listed`);
  return found;
};
const disabled = html => /^<button type="button" class="btn" disabled/.test(html);
const dotOf = html => (html.match(/class="fleet-dot ?([a-z]*)"/) || [])[1];

test("the cluster menu shows a member being checked as still checking, with a neutral dot", () => {
  const t = setup();
  t.ctx.FLEET.view = VIEW;
  const html = t.ctx.fleetMenuHtml();
  const lab = part(html, "Lab");
  assert.match(lab, /still checking/);
  assert.doesNotMatch(lab, /not responding/);
  assert.equal(dotOf(lab), "checking", "neutral, not red");
  const dr = part(html, "DR");
  assert.match(dr, /not responding/);
  assert.equal(dotOf(dr), "bad");
});

test("the Fleet page shows a member being checked as still checking, with a neutral dot", () => {
  const t = setup();
  const html = t.ctx.fleetMembersHtml(VIEW);
  const lab = part(html, "Lab");
  assert.match(lab, /·\s*still checking<\/span>/, "said in the line, not only in its tooltip");
  assert.doesNotMatch(lab, /not responding/);
  assert.equal(dotOf(lab), "checking");
  const dr = part(html, "DR");
  assert.match(dr, /not responding/);
  assert.equal(dotOf(dr), "bad");
});

test("the move/copy picker lists a member being checked, disabled, as still checking", () => {
  const t = setup();
  t.ctx.FLEET.view = VIEW;
  t.ctx.moveToCluster("Deployment", "web");
  const body = t.modals[0].body;
  const lab = part(body, "Lab");
  assert.ok(lab, "still listed");
  assert.ok(disabled(lab), "nothing is sent to a cluster that hasn't answered yet");
  assert.match(lab, /still checking/);
  assert.equal(dotOf(lab), "checking");
  const dr = part(body, "DR");
  assert.ok(disabled(dr));
  assert.match(dr, /not responding/);
  assert.equal(dotOf(dr), "bad");
  assert.ok(!disabled(part(body, "Branch")));
});

test("a member being rechecked that answered last time keeps its version and stays pickable", () => {
  // _pending() keeps the last known state while a recheck runs: reachable wins.
  const t = setup();
  const again = { ...VIEW.members[1], checking: true };
  t.ctx.FLEET.view = { ...VIEW, members: [VIEW.members[0], again] };
  assert.match(part(t.ctx.fleetMenuHtml(), "Branch"), /v2\.8\.1/);
  assert.equal(dotOf(part(t.ctx.fleetMembersHtml(t.ctx.FLEET.view), "Branch")), "ok");
  t.ctx.moveToCluster("Deployment", "web");
  assert.ok(!disabled(part(t.modals[0].body, "Branch")));
});

test("the demo has a linked cluster that is still being checked", () => {
  const src = fs.readFileSync("web/js/demo.js", "utf8");
  const sites = between(src, "const demoSites = [", "const siteTag");
  assert.match(sites, /reachable: false[^}]*checking: true, answered: false[^}]*error: "still checking"/);
});

test("a member whose last check failed reads not answering while it is asked again, in all three places", () => {
  const t = setup();
  t.ctx.FLEET.view = VIEW;
  for (const shed of [part(t.ctx.fleetMenuHtml(), "Shed"), part(t.ctx.fleetMembersHtml(VIEW), "Shed")]) {
    assert.match(shed, /not responding/);
    assert.doesNotMatch(shed, /still checking/);
    assert.equal(dotOf(shed), "bad");
  }
  t.ctx.moveToCluster("Deployment", "web");
  const shed = part(t.modals[0].body, "Shed");
  assert.ok(disabled(shed));
  assert.match(shed, /not responding/);
  assert.equal(dotOf(shed), "bad");
});

test("the demo's view of all clusters names the lab as waiting, as the server does", () => {
  const src = fs.readFileSync("web/js/demo.js", "utf8");
  const flow = between(src, '"/api/fleet/all/flow"', "\n");
  assert.match(flow, /\{id:"d4ab1e",name:"Lab",error:"has not answered yet",state:"waiting"\}/);
  assert.match(flow, /\{id:"c3beef",name:"DR site",error:"not answering",state:"not answering"\}/, "the DR site reads like the server's, lowercase, with its state");
  const t = setup();
  assert.match(t.ctx.fleetMissingSentence({ name: "Lab", state: "waiting", error: "has not answered yet" }), /^Lab has not responded yet/);
});
