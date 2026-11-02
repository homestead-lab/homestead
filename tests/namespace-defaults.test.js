"use strict";

/* Forms that create something in a namespace start on the install's own
   (defaultNamespace(), from workloadNamespace.name), not on "lab". */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function lifecycle() {
  const shown = {};
  const ctx = { console, URLSearchParams, Map, Date, Promise, encodeURIComponent, Set,
    document: { addEventListener() {} }, STATE: { data: { importNamespaces: ["lab", "home", "media"] } },
    $: () => null, $$: () => [], esc: v => String(v ?? ""), toast() {}, icon: () => "", tip: () => "",
    modal: (title, html) => { shown.title = title; shown.html = html; },
    childModal: (title, html) => { shown.title = title; shown.html = html; }, defaultNamespace: () => "home" };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(require("./helpers/lifecycle-source")(), ctx);
  return { ctx, shown };
}

test("VM disk import starts on the install's namespace", () => {
  const { ctx, shown } = lifecycle();
  ctx.vmDiskImport();
  assert.match(shown.html, /<option value="home" selected>/);
  assert.doesNotMatch(shown.html, /<option value="lab" selected>/);
});

test("moving an app here starts on the install's namespace", () => {
  const { ctx, shown } = lifecycle();
  try { ctx.moveReview("branch", "container", "frigate"); } catch (e) { /* only the form matters here */ }
  assert.match(shown.html || "", /id="mv_ns" value="home"/);
});

test("no form in these files falls back to the literal lab namespace", () => {
  for (const file of [...require("./helpers/lifecycle-source").files(), "web/js/views-vms.js"]) {
    const source = fs.readFileSync(file, "utf8");
    const lab = source.split("\n").map((line, i) => [i + 1, line]).filter(([, line]) => /["']lab["']|ns=lab\b/.test(line));
    assert.deepEqual(lab.map(([n]) => `${file}:${n}`), [], "use defaultNamespace() instead");
  }
});

test("container import starts on, and loads storage for, the install's namespace", async () => {
  const { ctx, shown } = lifecycle(), asked = [];
  Object.assign(ctx, { vipChoices: () => "", hardwareChoices: () => "", privilegeFields: () => "", nodeAddressesOnly: () => [], nodeAddressOption: () => "" });
  ctx.api = async path => { asked.push(path); return path.startsWith("/api/deploy/options") ? { pvcs: [], storage_classes: ["longhorn-r2"] } : []; };
  try { await ctx.importSetup("unraid", "frigate"); } catch (e) { /* only the form matters here */ }
  assert.ok(asked.includes("/api/deploy/options?ns=home"), asked.join(", "));
  assert.match(shown.html || "", /<option value="home" selected>/);
  assert.doesNotMatch(shown.html || "", /<option value="lab" selected>/);
});
