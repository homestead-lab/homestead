"use strict";
// The recurring-job editor names the generation of the job it opened, and "" for a
// new one: a job changed or made elsewhere meanwhile is refused, not saved over
// (#621). The generation moves only when the settings do, not when the job runs.
// Without one (an older server), the server saves as before.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

const src = fs.readFileSync("web/js/views-protect.js", "utf8");

function load() {
  const sent = [], toasts = [];
  const form = { "#lj_name": "daily", "#lj_task": "snapshot", "#lj_cron": "0 3 * * *",
                 "#lj_retain": "5", "#lj_conc": "1", "#lj_newgroup": "" };
  const c = { window: {}, sent, toasts, LJ_GENERATION: null, STATE: { data: { lh: { groups: ["default"], tasks: { snapshot: "Snapshot" } } } },
    esc: s => String(s ?? ""), tip: () => "", modal() {}, closeModal() {}, resetPaint() {}, viewProtect() {}, fieldError() {},
    scheduleBuilder: () => "", scheduleChanged() {}, groupCount: () => "", lhTaskChanged() {},
    $: sel => ({ value: form[sel] ?? "", hidden: false }),
    $$: () => [{ checked: true, value: "default" }],
    toast(...args) { toasts.push(args); },
    api: async (path, opts = {}) => { sent.push({ path, body: JSON.parse(opts.body) }); return { ok: true }; } };
  vm.createContext(c);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), c);
  vm.runInContext(between(src, "window.lhJob = ", "window.lhJobNewGroup"), c);
  vm.runInContext(between(src, "const KEEPS", "window.lhTaskChanged"), c);
  vm.runInContext(between(src, "window.lhJobSave", "window.lhJobDel"), c);
  return c;
}

const listed = { name: "daily", generation: "3", task: "snapshot", cron: "0 2 * * *", retain: 7, concurrency: 1, groups: ["default"] };

test("editing a job names the generation it opened", async () => {
  const c = load();
  c.window.lhJob(listed);
  await c.window.lhJobSave();
  assert.equal(c.sent[0].body.generation, "3");
});

test("a new job names no generation yet, so one of the same name is not replaced", async () => {
  const c = load();
  c.window.lhJob();
  await c.window.lhJobSave();
  assert.equal(c.sent[0].body.generation, "");
});

test("a job listed by an older server, without a generation, is saved as before", async () => {
  const c = load();
  const { generation, ...old } = listed;
  c.window.lhJob(old);
  await c.window.lhJobSave();
  assert.equal("generation" in c.sent[0].body, false);
});
