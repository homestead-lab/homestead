"use strict";
// The Deploy form's tier (#431), in Placement since #844: a choice with normal selected, and collect()
// sends it. The App Store's "Configure & deploy" opens this same form.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function load(fields = {}) {
  const stub = value => ({ value, checked: false, options: [], classList: { add() {}, remove() {} }, dataset: {}, style: {} });
  const c = { window: {}, console, URLSearchParams, Map, Set, document: { addEventListener() {} },
    $: sel => (sel in fields ? fields[sel] : stub("")), $$: () => [], esc: s => String(s ?? "").replaceAll("<", "&lt;"),
    STATE: { data: {} }, tip: t => t,
    selectedHardware: () => [], readPrivileges: () => ({}), readVolumeRows: () => [], containerLanRead: () => null,
    monitoringFieldValue: () => "", readEditedContainer: () => ({}) };
  vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  for (const f of ["web/js/ui.js", "web/js/app-form.js", "web/js/views-workloads.js", "web/js/views-lifecycle.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  return c;
}

const tierField = (c, priority) => c.appFormPlacement("d", {}, c.deployPlacementApp({ priority }), [], []);

test("the Deploy form offers the tier with normal chosen, and says what each does", () => {
  const html = tierField(load(), "");
  assert.match(html, /id="d_priority"/);
  assert.match(html, /<option value="normal" selected>Normal<\/option>/);
  for (const t of ["critical", "low"]) assert.match(html, new RegExp(`<option value="${t}" >`));
  assert.match(html, /When the cluster is short of room/);
  assert.match(html, /Fits in turn, as every app does by default/);
});

test("a tier already chosen stays chosen", () => {
  assert.match(tierField(load(), "critical"), /<option value="critical" selected>/);
});

test("collect sends the tier chosen, and normal when the field is missing", () => {
  const picked = load({ "#d_priority": { value: "low" } });
  assert.equal(vm.runInContext("collect()", picked).priority, "low");
  assert.equal(vm.runInContext("collect()", load()).priority, "normal");
});

test("a tier the form does not know is shown as normal, never critical", () => {
  for (const odd of ["group", "system", "bogus", "Critical", 5]) {
    const html = tierField(load(), odd);
    assert.match(html, /<option value="normal" selected>/, String(odd));
    assert.doesNotMatch(html, /<option value="critical" selected>/, String(odd));
    assert.match(html, /Fits in turn, as every app does by default/, String(odd));
  }
});
