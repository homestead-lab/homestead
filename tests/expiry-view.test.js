"use strict";

/* Networking > Certificates and keys: what expires and when. A source that
   could not be read says so, never "nothing expiring"; key rows are asked for
   only by an admin. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

const DAY = 86400, NOW = Date.now() / 1000;
const row = (kind, namespace, name, days, state, extra = {}) => ({ kind, namespace, name, not_after: Math.round(NOW + days * DAY),
  days_left: Math.trunc(days), state, note: "", renews: state === "renews", overdue: false, ...extra });
const counts = items => Object.fromEntries(["expired", "critical", "warn", "unknown", "ok", "renews"]
  .map(s => [s, items.filter(r => r.state === s).length]));
const report = (items, extra = {}) => ({ checked_at: Math.round(NOW - 600), items, counts: counts(items), errors: {},
  partial: false, max_secrets: 2000, warn_days: 21, critical_days: 7, ...extra });

function view(answers, role = "viewer") {
  const out = { calls: [] };
  const ctx = { console, STATE: { data: {}, q: "" }, document: { addEventListener() {} }, $: () => null, $$: () => [],
    esc: v => String(v ?? "").replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`), icon: () => "", tip: () => "",
    plural: (n, word, many = word + "s") => `${n} ${Number(n) === 1 ? word : many}`,
    agoText: () => "10 minutes ago", fmtDay: v => new Date(v * 1000).toISOString().slice(0, 10), can: need => need === "viewer" || role === "admin",
    api: async path => { out.calls.push(path); return answers[path]; },
    networkPage: (key, html) => { out.key = key; out.html = html; } };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/views-expiry.js", "utf8"), ctx);
  return { ctx, out };
}
const text = html => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");

test("the section line counts what expires, and never says all is well when it could not look", () => {
  const { ctx } = view({});
  const line = vm.runInContext("expiryLine", ctx);
  assert.equal(line(report([row("tls-secret", "lab", "a", 90, "ok")])), "1 watched, none expiring soon");
  assert.equal(line(report([row("tls-secret", "lab", "a", 5, "critical")])), "1 item expiring within 21 days");
  assert.equal(line({ checked_at: null, items: [], counts: counts([]), errors: {} }), "Not checked yet");
  const failed = line(report([], { errors: { "tls-secrets": "HTTP Error 403: Forbidden" } }));
  assert.match(failed, /could not read/i);
  assert.doesNotMatch(failed, /none|nothing/i);
});

test("a viewer sees certificates only, and the page does not ask for keys", async () => {
  const items = [row("tls-secret", "media", "photos-tls", 5.4, "critical"), row("webhook-ca", "", "virt-api", 3, "renews")];
  const { ctx, out } = view({ "/api/expiry": report(items) });
  await vm.runInContext("viewExpiry()", ctx);
  assert.deepEqual(out.calls, ["/api/expiry"]);
  assert.equal(out.key, "expiry");
  const shown = text(out.html);
  assert.match(shown, /photos-tls/);
  assert.match(shown, /in 5 days/);
  assert.match(shown, /Renews itself/);
  assert.doesNotMatch(shown, /API key/);
});

test("an admin also sees API keys with their owners and the linked-cluster key", async () => {
  const keys = { items: [{ ...row("api-key", "", "backup-script", 11, "warn"), owner: "admin" },
    { ...row("fleet-key", "", "Linked-cluster key", 0, "no-expiry"), not_after: null, days_left: null,
      note: "It has no expiry; it changes when a cluster is unlinked" }] };
  const { ctx, out } = view({ "/api/expiry": report([]), "/api/expiry/keys": keys }, "admin");
  await vm.runInContext("viewExpiry()", ctx);
  assert.deepEqual(out.calls.sort(), ["/api/expiry", "/api/expiry/keys"]);
  const shown = text(out.html);
  assert.match(shown, /backup-script/);
  assert.match(shown, /owner admin/);
  assert.match(shown, /Linked-cluster key/);
  assert.match(shown, /Never/);
});

test("a source that could not be read is named, and the summary does not say nothing expires", async () => {
  const { ctx, out } = view({ "/api/expiry": report([], { errors: { "tls-secrets": "HTTP Error 403: Forbidden" } }) });
  await vm.runInContext("viewExpiry()", ctx);
  const shown = text(out.html);
  assert.match(shown, /Could not read/);
  assert.match(shown, /TLS Secrets/);
  assert.match(shown, /403/);
  assert.doesNotMatch(shown, /nothing expires/i);
});

test("before the first check, and with more Secrets than are read, the page says so", async () => {
  let { ctx, out } = view({ "/api/expiry": { checked_at: null, items: [], counts: counts([]), errors: {}, partial: false,
    warn_days: 21, critical_days: 7 } });
  await vm.runInContext("viewExpiry()", ctx);
  assert.match(text(out.html), /Not checked yet/);
  assert.doesNotMatch(text(out.html), /nothing expires/i);
  ({ ctx, out } = view({ "/api/expiry": report([row("tls-secret", "lab", "a", 90, "ok")], { partial: true }) }));
  await vm.runInContext("viewExpiry()", ctx);
  assert.match(text(out.html), /only the first 2000/i);
});
