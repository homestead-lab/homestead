"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const LH = {
  groups: ["critical", "default", "media", "spare"], tasks: { snapshot: "Snapshot" },
  volumes: [{ name: "pvc-a", pvc: "frigate-config" }, { name: "pvc-b", pvc: "scratch-test" },
    { name: "pvc-c", pvc: "homeassistant-config" }],
  group_rows: [{ name: "critical", volumes: ["pvc-a"], jobs: [] },
    { name: "default", volumes: ["pvc-a", "pvc-b"], jobs: [] },
    { name: "media", volumes: ["pvc-c"], jobs: [] },
    { name: "spare", volumes: [], jobs: [] }],
};

/* Opens the job dialog and returns what it shows. */
function openJobDialog(lh = LH) {
  const els = {};
  const el = id => (els[id] ||= { value: "", hidden: false, textContent: "" });
  const ctx = { URL, document: { addEventListener() {} }, STATE: { data: { lh } },
    esc: s => String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    tip: () => "",
    $: sel => el(sel), closeModal() {} };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/cron.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/views-protect.js", "utf8"), ctx);
  let html = "";
  ctx.modal = (title, body) => { html = body; };
  ctx.scheduleChanged = () => {};
  ctx.lhJob();
  return { html, ctx, el };
}

/* The checkbox row of one group in the dialog. */
const groupRow = (html, g) => html.split('<label class="switch"').find(r => r.includes(`value="${g}"`));

test("each group in the job dialog shows how many volumes it holds, with their names on hover", () => {
  const { html } = openJobDialog();
  assert.match(groupRow(html, "default"), /2 volumes/);
  assert.match(groupRow(html, "default"), /data-tip="frigate-config, scratch-test"/);
  assert.match(groupRow(html, "media"), /1 volume\b/);
  assert.match(groupRow(html, "media"), /data-tip="homeassistant-config"/);
  assert.match(groupRow(html, "spare"), /no volumes/);
});

test("the job dialog says default holds volumes with no other group, not all volumes", () => {
  const { html } = openJobDialog();
  assert.doesNotMatch(html, /all volumes/);
  assert.match(groupRow(html, "default"), /no other group/);
  assert.doesNotMatch(groupRow(html, "media"), /no other group/);
});

test("a group typed into the job dialog says it has no volumes yet", () => {
  const { html, ctx, el } = openJobDialog();
  assert.match(html, /id="lj_newgroup"[^>]*oninput="lhJobNewGroup\(\)"/);
  el("#lj_newgroup").value = " nightly ";
  ctx.lhJobNewGroup();
  assert.equal(el("#lj_newgroup_note").textContent, "new · no volumes yet");
  el("#lj_newgroup").value = "media";
  ctx.lhJobNewGroup();
  assert.equal(el("#lj_newgroup_note").textContent, "");
});
