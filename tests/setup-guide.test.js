"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function fixture(demo = false) {
  const saved = new Map();
  const store = { getItem: key => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value), removeItem: key => saved.delete(key) };
  const classes = new Set(["hidden"]);
  const button = { classList: { remove: name => classes.delete(name), toggle: (name, on) => on ? classes.add(name) : classes.delete(name) } };
  const ctx = { URLSearchParams, location: { search: demo ? "?demo=1" : "", protocol: "https:", hostname: "cluster.example.com", host: "cluster.example.com" },
    localStorage: store, sessionStorage: store, STATE: { data: {} }, $: () => button,
    go: () => {}, esc: String, matchMedia: () => ({ matches: false }), navigator: {},
    UI: { chip: label => label }, HomesteadRouter: { urlFor: () => "/setup" } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/welcome.js", "utf8"), ctx);
  return { ctx, button, classes, saved };
}

test("real clusters keep the guide shortcut and pulse until the guide is completed", () => {
  const { ctx, classes, button } = fixture();
  ctx.setupOffer();
  assert.equal(classes.has("hidden"), false);
  assert.equal(classes.has("pulse"), true);
  assert.equal(typeof button.onclick, "function");
  button.onclick();
  assert.equal(classes.has("pulse"), true, "opening alone does not complete the guide");
  ctx.setupOffer({ completed: true });
  assert.equal(classes.has("pulse"), false);
  assert.equal(classes.has("hidden"), false);
});

test("demo offers stop pulsing when reminders are dismissed and stay available", () => {
  const { ctx, classes, button } = fixture(true);
  ctx.setupOffer();
  assert.equal(classes.has("pulse"), true);
  button.onclick();
  assert.equal(classes.has("pulse"), true);
  ctx.setupOffer({ hidden: true });
  assert.equal(classes.has("pulse"), false);
  assert.equal(classes.has("hidden"), false);
});

test("the probe and dashboard shortcut are absent from the guide", () => {
  const { ctx } = fixture();
  assert.equal(vm.runInContext("Object.hasOwn(SETUP_STEPS, 'probe')", ctx), false);
  assert.equal(ctx.setupDashItem, undefined);
  assert.equal(ctx.setupDashLine, undefined);
});

test("a background reminder refresh does not overwrite an open guide's browser facts", async () => {
  const { ctx, classes } = fixture();
  const current = { steps: { appearance: { done: false } }, skips: ["appearance"] };
  ctx.STATE.data.setup = current;
  ctx.api = async () => ({ steps: {}, completed: true });
  await ctx.welcomeCheck();
  assert.equal(ctx.STATE.data.setup, current);
  assert.equal(ctx.STATE.data.setup.steps.appearance.done, false);
  assert.equal(classes.has("pulse"), false);
});

test("a skipped check remains unchecked and a later observed success replaces skip", () => {
  const { ctx } = fixture();
  assert.equal(ctx.setupStatus("disks", { disks: { done: false } }, ["disks"]), "skipped");
  assert.equal(ctx.setupStatus("disks", { disks: { done: true } }, ["disks"]), "done");
});

test("confirmation on this device can be undone without manufacturing cluster health", () => {
  const { ctx } = fixture();
  ctx.localStorage.setItem("homestead.setup.appearance", "1");
  assert.equal(ctx.setupFacts({ steps: { health: { done: false } } }).appearance.done, true);
  ctx.localStorage.setItem("homestead.setup.appearance", "0");
  const facts = ctx.setupFacts({ steps: { health: { done: false } } });
  assert.equal(facts.appearance.done, false);
  assert.equal(facts.health.done, false);
});

test("configured integrations and saved HTTPS addresses do not claim a passed health check", () => {
  const { ctx } = fixture();
  for (const id of ["lan", "smb", "backups", "osupdates", "unifi", "unraid", "homeassistant", "linked", "starter", "console"]) {
    assert.equal(ctx.setupStatusLabel(id, "done"), "Configuration found");
  }
  assert.equal(ctx.setupStatusLabel("https", "done", { here: false }), "Previously checked");
  assert.equal(ctx.setupStatusLabel("health", "done"), "Check passed");
});

test("Cloudflare guide stores only its position, keeps demo separate and rejects invalid positions", () => {
  const { ctx, saved } = fixture(true);
  ctx.setupCloudflareStage(2);
  assert.deepEqual([...saved.entries()], [["homestead.setup.cloudflare.demo", "2"]]);
  assert.equal(ctx.setupCloudflareStage(), 2);
  saved.set("homestead.setup.cloudflare.demo", "99");
  assert.equal(ctx.setupCloudflareStage(), null);
  ctx.setupCloudflareStage(null);
  assert.equal(saved.size, 0);
});

test("SMB and LAN network steps remain admin-only and expose the configuration actions", () => {
  const {ctx} = fixture();
  const state = {admin:true,steps:{lan:{applies:true,done:false},smb:{applies:true,done:false}}};
  const ids = () => ctx.setupVisible(state).flatMap(([,list]) => [...list]);
  assert.ok(ids().includes("lan")); assert.ok(ids().includes("smb"));
  assert.equal(vm.runInContext("SETUP_STEPS.lan.title",ctx), "LAN networks");
  assert.equal(vm.runInContext("SETUP_STEPS.smb.title",ctx), "SMB");
  const opened=[];ctx.go=page=>opened.push(page);ctx.networkTab=tab=>opened.push(tab);ctx.settingsTab=tab=>opened.push(tab);
  vm.runInContext("eval(SETUP_STEPS.lan.actions({})[0].run)",ctx);
  vm.runInContext("eval(SETUP_STEPS.smb.actions({}).find(a=>a.pri).run)",ctx);
  assert.deepEqual(opened,["services","network","hardware","settings"]);
  assert.match(vm.runInContext("SETUP_CHECKS.lan",ctx),/not connectivity/);
  assert.match(vm.runInContext("SETUP_CHECKS.smb",ctx),/not that clients can access/);
  state.admin=false;assert.equal(ids().includes("lan"),false);assert.equal(ids().includes("smb"),false);
});
test("next and browser facts cannot mark SMB or LAN configuration complete", () => {
  const {ctx} = fixture();
  const steps=ctx.setupFacts({admin:true,steps:{lan:{done:false,applies:true},smb:{done:false,applies:true}}});
  for(const id of ["lan","smb"]){
    assert.equal(steps[id].done,false);
    assert.equal(ctx.setupStatus(id,steps,[]),"todo");
    assert.equal(ctx.setupStatus(id,steps,[id]),"skipped");
    assert.equal(ctx.setupStatus(id,{[id]:{done:false,error:"unavailable"}},[]),"attention");
  }
});
