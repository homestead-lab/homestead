"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

function fixture({ base = "/", search = "" } = {}) {
  const fields = {}, timers = new Map();
  let nextTimer = 0, rejectRequest, response = { user: null }, logout = { ok: true, status: 200 };
  const element = () => ({ value: "", disabled: false, addEventListener() {},
    focus() { ctx.document.activeElement = this; },
    classList: { hidden: true, contains() { return this.hidden; },
      add() { this.hidden = true; }, remove() { this.hidden = false; } } });
  fields["#gate"] = element(); fields["#whoami"] = element();
  fields["#gatebox"] = element();
  fields["#gatebox"].contains = el => Object.entries(fields).some(([key, value]) => key.startsWith("#lg_") && value === el);
  Object.defineProperty(fields["#gatebox"], "innerHTML", { set(html) {
    ctx.gateHtml = html;
    for (const key of Object.keys(fields)) if (key.startsWith("#lg_")) delete fields[key];
    for (const [, id] of html.matchAll(/id="(lg_\w+)"/g)) fields["#" + id] = element();
  } });
  const ctx = { console, document: { activeElement: null, body: { dataset: {} } },
    $: s => fields[s], $$: () => [], esc: s => String(s),
    localStorage: { getItem: () => null, setItem() {} },
    fetch: async (url, options) => {
      ctx.requests.push([url, options && options.body ? JSON.parse(options.body) : null]);
      if (url !== "/api/auth/logout") return { ok: !response.error, status: response.error ? 403 : 200, json: async () => response };
      if (logout instanceof Error) throw logout;
      return { ...logout, json: async () => ({}) };
    },
    toasts: [], toast: (text, kind) => ctx.toasts.push([text, kind]), HOMESTEAD_BASE: base, URLSearchParams,
    api: () => new Promise((_, reject) => { rejectRequest = reject; }),
    setTimeout: fn => { timers.set(++nextTimer, fn); return nextTimer; },
    setInterval: fn => { timers.set(++nextTimer, fn); return nextTimer; },
    clearTimeout: id => timers.delete(id), clearInterval: id => timers.delete(id),
    navigations: [], location: { pathname: base + "apps", search, replace: url => ctx.navigations.push(url) } };
  ctx.window = ctx; ctx.requests = [];
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/router.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/auth.js", "utf8").replace(/^boot\(\);$/m, ""), ctx);
  ctx.afterAuth = async () => { ctx.afterAuthCalls = (ctx.afterAuthCalls || 0) + 1; };
  return { ctx, fields, timers, run: code => vm.runInContext(code, ctx),
    fail: () => rejectRequest(new Error("not signed in")), rejection: () => rejectRequest,
    respond: value => { response = value; }, logoutAnswers: value => { logout = value; } };
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
  // The two loops are UI.poll handles (.stop()); the other two are plain timers.
  for (const name of ["__loopTimer", "__imageUpdateLoop"]) {
    const id = t.ctx.setInterval(() => {});
    t.ctx[name] = { id, stop() { t.timers.delete(id); } };
  }
  for (const name of ["__imageUpdateStart", "__operationTimer"]) t.ctx[name] = t.ctx.setInterval(() => {});
  const first = t.ctx.api("/api/overview").catch(() => {}), rejectFirst = t.rejection();
  const second = t.ctx.api("/api/operations").catch(() => {}), rejectSecond = t.rejection();
  rejectFirst(new Error("not signed in")); await first;
  const password = t.fields["#lg_pass"]; password.value = "still typing";
  rejectSecond(new Error("not signed in")); await second;
  assert.equal(t.fields["#lg_pass"], password);
  assert.equal(password.value, "still typing");
  for (const name of ["__loopTimer", "__imageUpdateLoop"]) assert.equal(t.timers.has(t.ctx[name].id), false);
  for (const name of ["__imageUpdateStart", "__operationTimer"]) assert.equal(t.timers.has(t.ctx[name]), false);
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
  t.run(between(auth, "async function afterAuth()", "/* ---------------- API keys"));
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
  const ctx = { STATE: { view: "dash", busy: false }, VIEWS: { dash: ["", async () => calls++, true] },
    $: () => ({ classList: { contains: () => false } }), toast() {} };
  vm.createContext(ctx);
  const app = fs.readFileSync("web/js/app.js", "utf8");
  vm.runInContext(between(app, "async function refresh(force)", "window.refresh = refresh;"), ctx);
  assert.equal(await ctx.refresh(true), false);
  assert.equal(calls, 0);
});

test("signing out loads a fresh page, so nothing from the session is left for the next person", async () => {
  const t = fixture(); t.run('ME = "admin"; ROLE = "admin";');
  t.ctx.pwaForgetDevice = async () => {};
  await t.ctx.doLogout();
  assert.deepEqual(t.ctx.navigations, ["/"]);
  assert.equal(t.run("ME"), null);
});

test("someone else signing in after a session expired gets a fresh page, not the last one's", async () => {
  const t = fixture(); t.run('ME = "old"; ROLE = "admin";');
  const expired = t.ctx.api("/api/overview").catch(() => {}); t.fail(); await expired;
  t.fields["#lg_user"].value = "new"; t.fields["#lg_pass"].value = "new password";
  t.respond({ user: "new", role: "viewer" });
  await t.ctx.doLogin();
  assert.deepEqual(t.ctx.navigations, ["/"]);
  assert.equal(t.ctx.afterAuthCalls, undefined);
});

test("the same person signing back in after a session expired carries on where they were", async () => {
  const t = fixture(); t.run('ME = "admin"; ROLE = "admin";');
  const expired = t.ctx.api("/api/overview").catch(() => {}); t.fail(); await expired;
  t.fields["#lg_user"].value = "admin"; t.fields["#lg_pass"].value = "password";
  t.respond({ user: "admin", role: "admin" });
  await t.ctx.doLogin();
  assert.deepEqual(t.ctx.navigations, []);
  assert.equal(t.ctx.afterAuthCalls, 1);
});

test("signing out stays in the app's own place: the demo under its base, and with ?demo=1", async () => {
  const pages = fixture({ base: "/homestead/" });
  pages.ctx.pwaForgetDevice = async () => {};
  await pages.ctx.doLogout();
  assert.deepEqual(pages.ctx.navigations, ["/homestead/"]);
  const screenshots = fixture({ search: "?demo=1&demo-scenario=busy" });
  screenshots.ctx.pwaForgetDevice = async () => {};
  await screenshots.ctx.doLogout();
  assert.deepEqual(screenshots.ctx.navigations, ["/?demo=1&demo-scenario=busy"]);
});

test("a sign-out the server did not take says so and stays signed in, rather than reloading straight back in", async () => {
  for (const answer of [new Error("Failed to fetch"), { ok: false, status: 502 }]) {
    const t = fixture(); t.run('ME = "admin"; ROLE = "admin";');
    t.ctx.pwaForgetDevice = async () => {};
    t.logoutAnswers(answer);
    await t.ctx.doLogout();
    assert.deepEqual(t.ctx.navigations, []);
    assert.equal(t.run("ME"), "admin");
    assert.equal(t.ctx.toasts.length, 1);
    assert.equal(t.ctx.toasts[0][1], "bad");
  }
});

test("a session that had already ended still signs out", async () => {
  const t = fixture(); t.run('ME = "admin"; ROLE = "admin";');
  t.ctx.pwaForgetDevice = async () => {};
  t.logoutAnswers({ ok: false, status: 401 });
  await t.ctx.doLogout();
  assert.deepEqual(t.ctx.navigations, ["/"]);
});

test("the same name signing back in with another role gets a fresh page", async () => {
  // A role change ends that user's sessions: this is how a demotion arrives.
  const t = fixture(); t.run('ME = "alice"; ROLE = "admin";');
  const expired = t.ctx.api("/api/overview").catch(() => {}); t.fail(); await expired;
  t.fields["#lg_user"].value = "alice"; t.fields["#lg_pass"].value = "password";
  t.respond({ user: "alice", role: "viewer" });
  await t.ctx.doLogin();
  assert.deepEqual(t.ctx.navigations, ["/"]);
  assert.equal(t.ctx.afterAuthCalls, undefined);
});

test("the sign-in form opens in a browser that blocks site storage (#618)", () => {
  const t = fixture();
  const refuse = () => { throw Object.assign(new Error("The operation is insecure."), { name: "SecurityError" }); };
  t.ctx.localStorage = { getItem: refuse, setItem: refuse, removeItem: refuse };
  t.ctx.loginForm();
  assert.ok(t.fields["#lg_go"], "the Sign in button is drawn");
});

test("first-admin setup asks for the one-time setup code and sends it (#737)", async () => {
  const t = fixture(); t.ctx.loginForm(null, true);
  assert.ok(t.fields["#lg_code"], "the setup form has a setup code field");
  t.respond({ user: "robin" });
  t.fields["#lg_user"].value = "robin"; t.fields["#lg_pass"].value = t.fields["#lg_pass2"].value = "a-long-password";
  t.fields["#lg_code"].value = " k7qf-2m9x-h4 ";
  await t.ctx.doSetup();
  assert.deepEqual(t.ctx.requests, [["/api/auth/setup", { username: "robin", password: "a-long-password", setup_code: "k7qf-2m9x-h4", remember: false }]]);
});

test("setup without a code asks for it before sending anything (#737)", async () => {
  const t = fixture(); t.ctx.loginForm(null, true);
  t.fields["#lg_user"].value = "robin"; t.fields["#lg_pass"].value = t.fields["#lg_pass2"].value = "a-long-password";
  await t.ctx.doSetup();
  assert.deepEqual(t.ctx.requests, []);
  assert.ok(t.fields["#lg_code"], "the form is shown again");
});

test("sign-in has no setup code field", () => {
  const t = fixture(); t.ctx.loginForm();
  assert.equal(t.fields["#lg_code"], undefined);
});

test("the setup code hint names the namespace the server named, once it has", async () => {
  const t = fixture(); t.ctx.loginForm("", true);
  assert.match(t.ctx.gateHtml, /kubectl -n lab get secret homestead-setup/);
  assert.match(t.ctx.gateHtml, /Helm: use the release's namespace/);
  t.fields["#lg_user"].value = "robin"; t.fields["#lg_pass"].value = t.fields["#lg_pass2"].value = "a-long-password";
  t.fields["#lg_code"].value = "ABCD-EFGH-JK";
  t.respond({ error: "That setup code is not right. It is in the Secret homes/homestead-setup; show it with: "
    + "kubectl -n homes get secret homestead-setup -o jsonpath='{.data.code}' | base64 -d" });
  await t.ctx.doSetup();
  const hint = between(t.ctx.gateHtml, 'id="lg_code_hint"', "</details>");
  assert.match(hint, /kubectl -n homes get secret homestead-setup/);
  assert.doesNotMatch(hint, /-n lab\b/);
  assert.doesNotMatch(hint, /Helm: use the release's namespace/);
});
