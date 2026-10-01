"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function fixture() {
  const fields = {}, timers = new Map();
  let nextTimer = 0, rejectRequest, response = { user: null };
  const element = () => ({ value: "", disabled: false, addEventListener() {},
    focus() { ctx.document.activeElement = this; },
    classList: { hidden: true, contains() { return this.hidden; },
      add() { this.hidden = true; }, remove() { this.hidden = false; } } });
  fields["#gate"] = element(); fields["#whoami"] = element();
  fields["#gatebox"] = element();
  fields["#gatebox"].contains = el => Object.entries(fields).some(([key, value]) => key.startsWith("#lg_") && value === el);
  Object.defineProperty(fields["#gatebox"], "innerHTML", { set(html) {
    for (const key of Object.keys(fields)) if (key.startsWith("#lg_")) delete fields[key];
    for (const [, id] of html.matchAll(/id="(lg_\w+)"/g)) fields["#" + id] = element();
  } });
  const ctx = { console, document: { activeElement: null, body: { dataset: {} } },
    $: s => fields[s], $$: () => [], esc: s => String(s),
    localStorage: { getItem: () => null, setItem() {} },
    fetch: async () => ({ ok: true, json: async () => response }),
    api: () => new Promise((_, reject) => { rejectRequest = reject; }),
    setTimeout: fn => { timers.set(++nextTimer, fn); return nextTimer; },
    setInterval: fn => { timers.set(++nextTimer, fn); return nextTimer; },
    clearTimeout: id => timers.delete(id), clearInterval: id => timers.delete(id) };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/auth.js", "utf8").replace(/^boot\(\);$/m, ""), ctx);
  ctx.afterAuth = async () => {};
  return { ctx, fields, timers, run: code => vm.runInContext(code, ctx),
    fail: () => rejectRequest(new Error("not signed in")), rejection: () => rejectRequest,
    respond: value => { response = value; } };
}

test("unauthenticated background failures preserve the password field and focus", async () => {
  const t = fixture(); t.ctx.loginForm();
  const password = t.fields["#lg_pass"];
  password.value = "partially typed"; password.focus();
  const request = t.ctx.api("/api/overview").catch(() => {}); t.fail(); await request;
  assert.equal(t.fields["#lg_pass"], password);
  assert.equal(password.value, "partially typed");
  assert.equal(t.ctx.document.activeElement, password);
});

test("session expiry stops polls and renders sign-in only once for concurrent failures", async () => {
  const t = fixture(); t.run('ME = "admin"; ROLE = "admin";');
  for (const name of ["__loopTimer", "__imageUpdateLoop", "__imageUpdateStart", "__operationTimer"])
    t.ctx[name] = t.ctx.setInterval(() => {});
  const first = t.ctx.api("/api/overview").catch(() => {}), rejectFirst = t.rejection();
  const second = t.ctx.api("/api/operations").catch(() => {}), rejectSecond = t.rejection();
  rejectFirst(new Error("not signed in")); await first;
  const password = t.fields["#lg_pass"]; password.value = "still typing";
  rejectSecond(new Error("not signed in")); await second;
  assert.equal(t.fields["#lg_pass"], password);
  assert.equal(password.value, "still typing");
  for (const name of ["__loopTimer", "__imageUpdateLoop", "__imageUpdateStart", "__operationTimer"])
    assert.equal(t.timers.has(t.ctx[name]), false);
  assert.equal(t.run("ROLE"), null);
});

test("delayed autofocus never steals focus from a password already being typed", () => {
  const t = fixture(); t.ctx.loginForm();
  t.fields["#lg_pass"].focus();
  for (const fn of t.timers.values()) fn();
  assert.equal(t.ctx.document.activeElement, t.fields["#lg_pass"]);
});

test("session loss during initialization cannot restart background polls", async () => {
  const t = fixture(); t.run('ME = "admin"; ROLE = "admin";');
  let finish, loops = 0;
  t.ctx.loadHealthSettings = () => new Promise(resolve => { finish = resolve; });
  t.ctx.paintWho = () => {};
  t.ctx.startLoop = () => loops++;
  // Restore the real initialization function (the fixture normally stubs it).
  const auth = fs.readFileSync("web/js/auth.js", "utf8");
  t.run(auth.slice(auth.indexOf("async function afterAuth()"), auth.indexOf("window.setRole =")));
  const initializing = t.ctx.afterAuth();
  const failed = t.ctx.api("/api/overview").catch(() => {}); t.fail(); await failed;
  finish(); await initializing;
  assert.equal(loops, 0);
});

test("a late rejection from the old session cannot sign out a fresh login", async () => {
  const t = fixture(); t.run('ME = "old"; ROLE = "admin";');
  const pending = t.ctx.api("/api/operations").catch(() => {});
  t.ctx.loginForm(); t.fields["#lg_user"].value = "new"; t.fields["#lg_pass"].value = "new password";
  t.respond({ user: "new", role: "viewer" });
  await t.ctx.doLogin(); t.fail(); await pending;
  assert.equal(t.run("ME"), "new");
  assert.equal(t.fields["#gate"].classList.contains("hidden"), true);
});

test("a visibility or manual refresh does not load the page behind sign-in", async () => {
  let calls = 0;
  const ctx = { STATE: { view: "dash", busy: false }, VIEWS: { dash: ["", "", async () => calls++, true] },
    $: () => ({ classList: { contains: () => false } }), toast() {} };
  vm.createContext(ctx);
  const app = fs.readFileSync("web/js/app.js", "utf8");
  vm.runInContext(app.slice(app.indexOf("async function refresh(force)"), app.indexOf("window.refresh = refresh;")), ctx);
  assert.equal(await ctx.refresh(true), false);
  assert.equal(calls, 0);
});
