"use strict";
// #741 part 2: the alert webhook card and dialog (Settings > Connections), with
// the outage webhook's own Format control.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function load() {
  const actions = [], sent = [], fields = {}, toasts = [];
  const ctx = { STATE: { data: {} }, esc: v => String(v ?? "").replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`),
    jsArg: v => JSON.stringify(v), console };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  Object.assign(ctx, { fmtAgo: s => `${Math.round(s)}s ago`, tip: t => `<i>${t}</i>`,
    serviceRow: (title, pill, detail, bar) => `<row>${title}|${pill}|${detail}|${bar}</row>`,
    actionBar: rows => { const kept = rows.filter(Boolean); actions.push(...kept); return kept.map(r => `[${r.label}]`).join(""); },
    $: sel => fields[sel] || null, toast: (text, tone) => toasts.push([text, tone]), closeModal: () => {},
    modal: (title, html) => { ctx.shown = { title, html }; } });
  ctx.api = async (path, init) => { sent.push({ path, body: init?.body ? JSON.parse(init.body) : null }); return { ok: true, detail: "ok" }; };
  vm.runInContext(fs.readFileSync("web/js/uptime.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/views-stats.js", "utf8"), ctx);
  return { ctx, actions, sent, fields, toasts };
}

const hook = { webhook: "https://ntfy.example.com/(hidden)", format: "ntfy", min_severity: "degraded", lan: false,
  last: { at: 990, ok: false, error: "HTTP 502" } };

test("the card says where alerts go, and only an admin changes it", () => {
  const { ctx, actions } = load();
  const html = ctx.alertHookCardHtml(hook, 1000);
  assert.match(html, /Alert webhook\|<span class="ui-chip ok">on/);
  assert.match(html, /ntfy.*https:\/\/ntfy\.example\.com\/\(hidden\)/);
  assert.match(html, /Warnings and critical/);
  assert.match(html, /HTTP 502/);
  assert.deepEqual(actions.map(a => [a.label, a.need]), [["Edit", "admin"]]);
  const off = load();
  assert.match(off.ctx.alertHookCardHtml({ webhook: "", format: "generic", min_severity: "degraded" }, 1000), /<span class="ui-chip neutral">off/);
  assert.deepEqual(off.actions.map(a => [a.label, a.need]), [["Set up", "admin"]]);
});

test("the dialog uses the outage webhook's Format control", () => {
  const { ctx } = load();
  ctx.STATE.data.alertHook = hook;
  ctx.alertHookConfigure();
  assert.match(ctx.shown.html, /<select id="ah_format" aria-label="Webhook format">/);
  assert.match(ctx.shown.html, /<option value="ntfy" selected>ntfy<\/option>/);
  assert.match(ctx.shown.html, /<option value="generic">Generic JSON<\/option>/);
  assert.match(ctx.webhookFormatSelect("oa_format", "gotify"), /<select id="oa_format" aria-label="Webhook format">.*<option value="gotify" selected>Gotify/);
  assert.match(ctx.shown.html, /id="ah_lan"/);
  assert.match(ctx.shown.html, /alertHookTest\(\)/);
  assert.match(ctx.shown.html, /<option value="degraded" selected>/);
});

test("Save sends address, format, severity and the LAN allowance; Turn off sends none", async () => {
  const { ctx, sent, fields } = load();
  Object.assign(fields, { "#ah_url": { value: " https://gotify.example.com/message?token=(hidden) " }, "#ah_format": { value: "gotify" },
    "#ah_least": { value: "critical" }, "#ah_lan": { checked: true } });
  await ctx.alertHookSave();
  assert.deepEqual(sent.at(-1), { path: "/api/alerts/webhook", body: { webhook: "https://gotify.example.com/message?token=(hidden)",
    format: "gotify", min_severity: "critical", lan: true } });
  await ctx.alertHookOff();
  assert.deepEqual(sent.at(-1), { path: "/api/alerts/webhook", body: { webhook: "" } });
});

test("a test goes in the format chosen, saved or not", async () => {
  const { ctx, sent, fields, toasts } = load();
  Object.assign(fields, { "#ah_url": { value: "https://ntfy.example.com/homestead" }, "#ah_format": { value: "ntfy" } });
  await ctx.alertHookTest();
  assert.deepEqual(sent.at(-1), { path: "/api/alerts/webhook/test", body: { webhook: "https://ntfy.example.com/homestead", format: "ntfy" } });
  assert.deepEqual(toasts.at(-1), ["ok", "ok"]);
});

test("the card sits under Connections, and the demo answers its calls", () => {
  const settings = fs.readFileSync("web/js/views-settings.js", "utf8");
  assert.match(settings, /settingsCard\(``, \{tab:`connections`, id:`alertHookCard`\}\)/);
  assert.match(settings, /connections: \[[^\]]*alertHookPaint/);
  const demo = fs.readFileSync("web/js/demo.js", "utf8");
  assert.match(demo, /"\/api\/alerts\/webhook":/);
  assert.match(demo, /"\/api\/alerts\/webhook\/test":/);
  assert.match(fs.readFileSync("scripts/audit_dialogs.mjs", "utf8"), /alertHookConfigure\(\)/);
});
