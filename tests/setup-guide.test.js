"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

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

test("LAN setup precedes workloads, distinguishes configuration from connectivity and links to networking", () => {
  const { ctx } = fixture();
  const order = vm.runInContext("SETUP_CHAPTERS.flatMap(([, ids]) => ids)", ctx);
  assert.ok(order.indexOf("lan") < order.indexOf("starter"));
  assert.equal(ctx.setupStatusLabel("lan", "done"), "Configuration found");
  assert.equal(ctx.setupStatus("lan", { lan: { done: false } }, ["lan"]), "skipped");
  assert.match(vm.runInContext("SETUP_CHECKS.lan", ctx), /does not test/);
  assert.match(vm.runInContext("SETUP_STEPS.lan.actions()[0].run", ctx), /section: 'lan'/);
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
  assert.deepEqual(opened,["network","hardware","settings"]);
  assert.match(vm.runInContext("SETUP_CHECKS.lan",ctx),/not the connection/);
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

function storageRecipes(ctx, s, classes = [], inventory = null) {
  ctx.recipeInput = { s, classes, inventory };
  return JSON.parse(vm.runInContext("JSON.stringify(setupStorageRecipes(recipeInput.s, recipeInput.classes, recipeInput.inventory))", ctx));
}
function storageClass(overrides = {}) {
  return { name: "existing", provisioner: "driver.longhorn.io", replicas: "2", engine: "v1", migratable: false,
    encrypted: false, reclaim: "Retain", expandable: true, disk_tags: [], node_tags: [], parameters: {}, ...overrides };
}
function disks(tags = ["ssd", "hdd"]) {
  return { nodes: Object.fromEntries(["a", "b", "c"].map(name => [name, [{ longhorn: [{ scheduling: true, ready: true, type: "filesystem", tags }] }]])) };
}
test("storage guide offers five editable safe recipes without changing a current default", () => {
  const { ctx } = fixture();
  const recipes = storageRecipes(ctx, { target: 3, default: "vm-default" }, [], disks());
  assert.deepEqual(recipes.map(r => [r.id, r.replicas]), [["r1", 1], ["r2", 2], ["r3", 3], ["ssd", 3], ["hdd", 3]]);
  for (const r of recipes) {
    assert.equal(r.default, false); assert.equal(r.copies, "hosts"); assert.equal(r.reclaim_policy, "Retain");
    assert.equal(r.engine, "v1"); assert.equal(r.expandable, true); assert.equal(r.migratable, false);
  }
  assert.deepEqual(recipes.filter(r => r.recommended).map(r => r.id), ["r3"]);
  assert.deepEqual(storageRecipes(ctx, { target: 2 }).filter(r => r.default).map(r => r.id), ["r2"]);
});
test("storage recipe names avoid immutable classes and reuse only matching settings", () => {
  const { ctx } = fixture();
  const classes = [storageClass({ name: "longhorn-r2", migratable: true }), storageClass({ name: "longhorn-r2-containers", reclaim: "Delete" })];
  let r = storageRecipes(ctx, { target: 2 }, classes)[1];
  assert.equal(r.name, "longhorn-r2-containers-2"); assert.equal(r.existing, null);
  for (const override of [{ internal: true }, { made_for: "images" }, { encrypted: true }, { engine: "v2" },
    { provisioner: "another-driver" }, { expandable: false }, { disk_tags: ["ssd"] }, { node_tags: ["rack"] },
    { parameters: { replicaSoftAntiAffinity: "enabled" } }]) {
    assert.equal(storageRecipes(ctx, { target: 2 }, [storageClass(override)])[1].existing, null);
  }
  r = storageRecipes(ctx, { target: 2, default: "existing" }, [storageClass({ default: true })])[1];
  assert.equal(r.name, "existing"); assert.equal(r.existing.default, true);
  const preferred = storageRecipes(ctx, { target: 2 }, [storageClass({ name: "other" }), storageClass({ default: true })]);
  assert.equal(preferred[1].name, "existing");
  assert.equal(preferred.some(recipe => recipe.default), false, "inventory still protects the default when the setup check is unavailable");
  assert.equal(storageRecipes(ctx, {})[0].recommended, false, "an unavailable recommendation is not invented");
});
test("SSD and HDD presets adapt to eligible V1 hosts and retain missing selectors", () => {
  const { ctx } = fixture(), inv = disks(["ssd"]);
  inv.nodes.b[0].longhorn[0].failed = true;
  inv.nodes.c[0].longhorn[0].type = "block";
  const recipes = storageRecipes(ctx, { target: 3 }, [], inv);
  assert.equal(recipes[2].hosts, 1); assert.equal(recipes[2].replicas, 3);
  assert.equal(recipes[3].hosts, 1); assert.equal(recipes[3].replicas, 1);
  assert.equal(recipes[4].hosts, 0); assert.deepEqual(recipes[4].disk_tags, ["hdd"]);
  assert.equal(storageRecipes(ctx, { target: 3 })[3].hosts, null);
  assert.equal(storageRecipes(ctx, { target: 3 })[3].replicas, 3);
});

/* The guide as it renders: the real shared components and welcome.js. */
function rendered() {
  const ctx = { URLSearchParams, location: { search: "", protocol: "https:", hostname: "cluster.example.com", host: "cluster.example.com" },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} }, sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    STATE: { data: {}, platform: {} }, $: () => null, $$: () => [], go: () => {}, matchMedia: () => ({ matches: false }), navigator: {},
    document: { addEventListener() {}, querySelectorAll: () => [] }, icon: () => "", HomesteadRouter: { urlFor: () => "/setup" } };
  ctx.window = ctx;
  vm.createContext(ctx);
  require("./helpers/load-ui")(ctx);
  ctx.esc = s => String(s ?? "").replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
  ctx.jsArg = s => JSON.stringify(String(s ?? ""));
  ctx.tip = text => `<span class="tip" tabindex="0" data-tip="${ctx.esc(text)}">?</span>`;
  vm.runInContext(fs.readFileSync("web/js/welcome.js", "utf8"), ctx);
  const state = { steps: {}, skips: {}, admin: true };
  const ids = vm.runInContext("setupVisible", ctx)(state).flatMap(([, list]) => list);
  const status = Object.fromEntries(ids.map(id => [id, "todo"]));
  return { ctx, ids, html: id => vm.runInContext("setupStepHtml", ctx)(id, state, status, ids) };
}
/* The buttons of a step's own row, left to right, as their class lists. */
const rowButtons = html => {
  const row = between(html, /<div class="ui-actions(?![\s\S]*<div class="ui-actions)/, null);   // the last one
  return [...row.matchAll(/<button\b[^>]*class="([^"]*)"/g)].map(m => m[1]);
};

test("every setup step ends its row with its main action, and has one primary at most", () => {
  const { ids, html } = rendered();
  for (const id of ids) {
    const buttons = rowButtons(html(id)).filter(c => /\bbtn\b/.test(c));
    const primaries = buttons.filter(c => /\bpri\b/.test(c));
    assert.ok(primaries.length <= 1, `${id} has ${primaries.length} primary buttons`);
    if (primaries.length) assert.match(buttons.at(-1), /\bpri\b/, `${id}: the primary is last`);
  }
});

test("a step doesn't repeat that Next leaves it unchecked; the intro says it", () => {
  const { ids, html } = rendered();
  for (const id of ids.filter(id => id !== "intro")) assert.doesNotMatch(html(id), /Next does not mark/, id);
  assert.match(html("intro"), /without marking the current one complete/);
});

test("the intro offers Don't show again only in the page header", () => {
  const { html } = rendered();
  assert.doesNotMatch(html("intro"), /Don.t show/);
});

test("the Users step opens Settings, Users and access", () => {
  const { ctx } = rendered();
  const run = vm.runInContext("SETUP_STEPS", ctx).people.actions({})[0].run;
  assert.match(run, /settingsTab\('access'\)/);
  assert.match(run, /go\('settings'\)/);
});
