"use strict";
// #742 part 2: Reconcile now. An admin reviews and asks Flux to apply an app again; the dialog and
// the app's details follow Flux taking the request.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

const row = (name, extra = {}) => ({ id: `Kustomization flux-system/${name}`, kind: "Kustomization", namespace: "flux-system", name,
  ready: true, suspended: false, revision: "main@246b3c6", tried: "", since: "", interval: "10m", message: "Applied", requested: "", handled: "", ...extra });
const report = (rows, apps = { "lab/web": "Kustomization flux-system/apps", "lab/db": "Kustomization flux-system/apps",
  "lab/held": "Kustomization flux-system/paused" }) => ({ summary: "", apps, objects: rows });
const AT = "2026-10-26T09:00:00.000000Z";

function setup(answers) {
  const calls = [], toasts = [], dialogs = [], state = { open: false, title: "" };
  const c = { window: { remoteRow: w => !!w.remote }, console, STATE: { data: {} },
    $: s => s === "#mtitle" ? { textContent: state.title }
      : s === "#mbody" ? { querySelector: q => state.open && dialogs.at(-1)?.html.includes(q.slice(1, -1)) ? {} : null } : null,
    esc: s => String(s).replaceAll("<", "&lt;").replaceAll(">", "&gt;"), copyText: async () => true,
    fmtAgo: s => `${s} s ago`, jsq: s => `'${s}'`, jsArg: s => JSON.stringify(String(s)),
    toast: (text, tone) => toasts.push({ text, tone }),
    modal: (title, html) => { state.open = true; state.title = title; dialogs.push({ title, html }); },
    modalIsOpen: () => state.open,
    api: async (path, init) => {
      calls.push({ path, init });
      const next = answers.shift();
      if (next instanceof Error) throw next;
      return next;
    } };
  vm.createContext(c); require("./helpers/load-ui")(c);
  vm.runInContext(fs.readFileSync("web/js/gitops.js", "utf8"), c);
  c.window.GITOPS_FOLLOW.wait = async () => {};
  const posts = () => calls.filter(call => call.init?.method === "POST");
  return { c, calls, toasts, dialogs, state, posts };
}

test("Reconcile now is offered to admins, only for an app Flux applies and is not paused", async () => {
  const t = setup([report([row("apps"), row("paused", { suspended: true })])]);
  await t.c.window.gitopsLoad();
  const item = t.c.window.gitopsReconcileItem({ ns: "lab", name: "web" });
  assert.equal(item.label, "Reconcile now");
  assert.equal(item.need, "admin");
  assert.match(item.run, /gitopsReconcile\('lab','web'\)/);
  assert.equal(t.c.window.gitopsReconcileItem({ ns: "lab", name: "plain" }), null);
  assert.equal(t.c.window.gitopsReconcileItem({ ns: "lab", name: "held" }), null);
  assert.equal(t.c.window.gitopsReconcileItem({ ns: "lab", name: "web", remote: true }), null);
});

test("The review names what Flux applies and every app in it, and sends nothing by itself", async () => {
  const t = setup([report([row("apps", { ready: false, message: "kustomize build <failed>" })])]);
  await t.c.window.gitopsLoad();
  t.c.window.gitopsReconcile("lab", "web");
  const { title, html } = t.dialogs.at(-1);
  assert.equal(title, "Reconcile now · web");
  assert.match(html, /Kustomization flux-system\/apps/);
  assert.match(html, /Apps it applies · 2/);
  assert.match(html, /lab\/db/);
  assert.match(html, /kustomize build &lt;failed&gt;/);
  assert.match(html, /gitopsReconcileGo\(this,(&quot;|")lab(&quot;|"),(&quot;|")web(&quot;|")\)/);
  assert.match(html, />Reconcile now</);
  assert.equal(t.posts().length, 0);
});

test("Reconcile now posts the app, then the dialog follows the status until Flux has handled it", async () => {
  const t = setup([report([row("apps")]), { ok: true, id: "Kustomization flux-system/apps", requested: AT },
    report([row("apps", { requested: AT, handled: "" })]),
    report([row("apps", { requested: AT, handled: AT, revision: "main@1234567" })])]);
  await t.c.window.gitopsLoad();
  t.c.window.gitopsReconcile("lab", "web");
  const button = { disabled: false };
  await t.c.window.gitopsReconcileGo(button, "lab", "web");
  assert.equal(button.disabled, true);
  const [post] = t.posts();
  assert.equal(post.path, "/api/gitops/reconcile");
  assert.deepEqual(JSON.parse(post.init.body), { ns: "lab", name: "web" });
  assert.equal(t.calls.filter(call => call.path === "/api/gitops").length, 3);
  assert.ok(t.dialogs.some(d => /Waiting for Flux/.test(d.html)));
  assert.match(t.dialogs.at(-1).html, /Flux applied Kustomization flux-system\/apps/);
  assert.match(t.dialogs.at(-1).html, /main@1234567/);
});

test("A request Flux handled but could not apply says so with Flux's message", async () => {
  const t = setup([report([row("apps")]), { ok: true, id: "Kustomization flux-system/apps", requested: AT },
    report([row("apps", { requested: AT, handled: AT, ready: false, message: "kustomize build failed" })])]);
  await t.c.window.gitopsLoad();
  t.c.window.gitopsReconcile("lab", "web");
  await t.c.window.gitopsReconcileGo({}, "lab", "web");
  assert.match(t.dialogs.at(-1).html, /not ready/);
  assert.match(t.dialogs.at(-1).html, /kustomize build failed/);
});

test("With the dialog closed, the result comes as a message and the dialog stays shut", async () => {
  const t = setup([report([row("apps")]), { ok: true, id: "Kustomization flux-system/apps", requested: AT },
    report([row("apps", { requested: AT, handled: AT, revision: "main@1234567" })])]);
  await t.c.window.gitopsLoad();
  t.c.window.gitopsReconcile("lab", "web");
  t.state.open = false;
  const shown = t.dialogs.length;
  await t.c.window.gitopsReconcileGo({}, "lab", "web");
  assert.equal(t.dialogs.length, shown);
  assert.deepEqual(t.toasts.at(-1), { text: "Flux applied Kustomization flux-system/apps: main@1234567", tone: "ok" });
});

test("A refused request shows the server's reason and the button works again", async () => {
  const t = setup([report([row("apps")]), new Error("Kustomization flux-system/apps is suspended")]);
  await t.c.window.gitopsLoad();
  const button = { disabled: false };
  await t.c.window.gitopsReconcileGo(button, "lab", "web");
  assert.deepEqual(t.toasts.at(-1), { text: "Kustomization flux-system/apps is suspended", tone: "bad" });
  assert.equal(button.disabled, false);
});

test("Flux not taking the request in time is said, not hidden", async () => {
  const t = setup([report([row("apps")]), { ok: true, id: "Kustomization flux-system/apps", requested: AT },
    ...Array.from({ length: 40 }, () => report([row("apps", { requested: AT, handled: "" })]))]);
  await t.c.window.gitopsLoad();
  t.c.window.gitopsReconcile("lab", "web");
  await t.c.window.gitopsReconcileGo({}, "lab", "web");
  assert.match(t.dialogs.at(-1).html, /has not taken the request yet/);
});

test("The details say a request is waiting for Flux until it is handled", async () => {
  const t = setup([report([row("apps", { requested: new Date(Date.now() - 20000).toISOString(), handled: "" })])]);
  await t.c.window.gitopsLoad();
  assert.match(t.c.window.gitopsDetail({ ns: "lab", name: "web" }), /Reconcile asked .* waiting for Flux/);
  const done = setup([report([row("apps", { requested: "x", handled: "x" })])]);
  await done.c.window.gitopsLoad();
  assert.doesNotMatch(done.c.window.gitopsDetail({ ns: "lab", name: "web" }), /waiting for Flux/);
});

// Review fixes: a second request, a request someone else made, an unread status, a status still Unknown.
test("A second request takes over from the first: one result, and no false 'not taken' message", async () => {
  const AT2 = "2026-10-26T09:01:00.000000Z", done = row("apps", { requested: AT2, handled: AT2 });
  const t = setup([report([row("apps")]), { ok: true, id: "Kustomization flux-system/apps", requested: AT },
    report([row("apps", { requested: AT, handled: "" })]),
    { ok: true, id: "Kustomization flux-system/apps", requested: AT2 }, report([done]),
    ...Array.from({ length: 40 }, () => report([done]))]);
  await t.c.window.gitopsLoad();
  t.c.window.gitopsReconcile("lab", "web");
  let second;
  t.c.window.GITOPS_FOLLOW.wait = async () => { second ??= t.c.window.gitopsReconcileGo({}, "lab", "web"); await second; };
  await t.c.window.gitopsReconcileGo({}, "lab", "web");
  const ends = [...t.dialogs.map(d => d.html), ...t.toasts.map(m => m.text)].filter(text => /Flux applied|not taken|could not be read/.test(text));
  assert.equal(ends.length, 1, ends.join("\n"));
  assert.match(ends[0], /Flux applied/);
});

test("A later request handled by Flux (flux reconcile, say) also counts as taken", async () => {
  const t = setup([report([row("apps")]), { ok: true, id: "Kustomization flux-system/apps", requested: AT },
    report([row("apps", { requested: "2026-10-26T10:00:05.123456789+01:00", handled: "2026-10-26T10:00:05.123456789+01:00" })])]);
  await t.c.window.gitopsLoad();
  t.c.window.gitopsReconcile("lab", "web");
  await t.c.window.gitopsReconcileGo({}, "lab", "web");
  assert.match(t.dialogs.at(-1).html, /Flux applied/);
});

test("A status that cannot be read is said, and completion is left unverified", async () => {
  const t = setup([report([row("apps")]), { ok: true, id: "Kustomization flux-system/apps", requested: AT },
    ...Array.from({ length: 40 }, () => new Error("Kubernetes said 500"))]);
  await t.c.window.gitopsLoad();
  t.c.window.gitopsReconcile("lab", "web");
  await t.c.window.gitopsReconcileGo({}, "lab", "web");
  assert.match(t.dialogs.at(-1).html, /could not be read: Kubernetes said 500/);
  assert.doesNotMatch(t.dialogs.at(-1).html, /Flux applied/);
});

test("Handled but still Unknown is followed until Flux says Ready or not", async () => {
  const t = setup([report([row("apps")]), { ok: true, id: "Kustomization flux-system/apps", requested: AT },
    report([row("apps", { requested: AT, handled: AT, ready: null })]),
    report([row("apps", { requested: AT, handled: AT, ready: true })])]);
  await t.c.window.gitopsLoad();
  t.c.window.gitopsReconcile("lab", "web");
  await t.c.window.gitopsReconcileGo({}, "lab", "web");
  assert.equal(t.calls.filter(call => call.path === "/api/gitops").length, 3);
  assert.match(t.dialogs.at(-1).html, /Flux applied/);
});

test("The review says Flux applies the revision it last fetched, with the count of apps in view", async () => {
  const t = setup([report([row("apps")])]);
  await t.c.window.gitopsLoad();
  t.c.window.gitopsReconcile("lab", "web");
  const html = t.dialogs.at(-1).html;
  assert.match(html, /revision it last fetched/);
  assert.doesNotMatch(html, /from git now/);
  assert.match(html, /<dt>(?:<svg[^>]*><use[^>]*\/><\/svg>)?Apps<\/dt><dd>2<\/dd>/);
});
