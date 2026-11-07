// #741: the outage webhook's Format choice and Send a test (admins only).
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const vm = require("vm");

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const fields = {};
const ctx = {
  console, esc, jsArg: v => JSON.stringify(v), window: {}, STATE: { data: {} }, Date, JSON,
  UI: { section: (title, html) => html, fields: (...parts) => parts.join(""), field: (label, html) => `<label>${label}</label>${html}`,
        more: (title, html) => `<details><summary>${title}</summary>${html}</details>`,
        button: (label, onclick, opts = {}) => `<button onclick='${onclick}' ${opts.attrs || ""}>${label}</button>` },
  $: sel => fields[sel] || null,
  sent: [], toast: () => {},
};
ctx.api = async (path, init) => { ctx.sent.push({ path, body: JSON.parse(init.body) }); return { ok: true, detail: "ok" }; };
ctx.globalThis = ctx;
vm.createContext(ctx);
require("./helpers/load-time")(ctx);
vm.runInContext(fs.readFileSync("web/js/uptime.js", "utf8"), ctx);
const run = code => vm.runInContext(code, ctx);

const row = actions => JSON.stringify({ ns: "lab", name: "frigate", outage_actions: actions });

test("the webhook has a Format choice, generic JSON unless saved otherwise", () => {
  const html = run(`outageSectionHtml("app", ${row({ webhook: "https://ntfy.example.com/homestead", format: "ntfy" })})`);
  assert.match(html, /<select id="oa_format"/);
  assert.match(html, /<option value="ntfy" selected>ntfy<\/option>/);
  assert.match(html, /<option value="gotify">Gotify<\/option>/);
  assert.match(run(`outageSectionHtml("app", ${row({ webhook: "https://h.example.com/x" })})`), /<option value="generic" selected>Generic JSON/);
});

test("Send a test and Allow a LAN address are for admins", () => {
  const html = run(`outageSectionHtml("app", ${row({ webhook: "https://h.example.com/x", lan: true })})`);
  assert.match(html, /<button[^>]*outageTest[^>]*data-need="admin"/);
  assert.match(html, /<label class="switch" data-need="admin"[^>]*><input type="checkbox" id="oa_lan" checked>/);
  assert.match(run(`outageSectionHtml("app", ${row({ webhook: "https://h.example.com/x" })})`), /id="oa_lan">/);
});

test("the form keeps the LAN allowance as shown, so an operator's other edit keeps it", () => {
  Object.assign(fields, { "#oa_hook": { checked: true }, "#oa_url": { value: "http://ntfy.example.com/(hidden)" },
                          "#oa_format": { value: "ntfy" }, "#oa_lan": { checked: true } });
  assert.deepEqual(JSON.parse(JSON.stringify(run("outageFromForm()"))),
                   { webhook: "http://ntfy.example.com/(hidden)", format: "ntfy", lan: true });
  fields["#oa_lan"].checked = false;
});

test("the form saves the format, and leaves generic out so old settings compare equal", () => {
  Object.assign(fields, { "#oa_hook": { checked: true }, "#oa_url": { value: " https://gotify.example.com/message?token=(hidden) " },
                          "#oa_format": { value: "gotify" } });
  assert.deepEqual(JSON.parse(JSON.stringify(run("outageFromForm()"))),
                   { webhook: "https://gotify.example.com/message?token=(hidden)", format: "gotify" });
  fields["#oa_format"].value = "generic";
  assert.deepEqual(JSON.parse(JSON.stringify(run("outageFromForm()"))), { webhook: "https://gotify.example.com/message?token=(hidden)" });
});

test("a test is sent in the format chosen, saved or not", async () => {
  Object.assign(fields, { "#oa_url": { value: "https://ntfy.example.com/homestead" }, "#oa_format": { value: "ntfy" } });
  await run(`window.outageTest("app", "lab", "frigate")`);
  assert.equal(ctx.sent.at(-1).path, "/api/monitoring/actions/test");
  assert.equal(ctx.sent.at(-1).body.format, "ntfy");
});
