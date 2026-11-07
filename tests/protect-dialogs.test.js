"use strict";
// Data Protection's dialogs take the shared parts (#501): a lead, facts, at
// most one notice, explanations in UI.more, and a title that names its object.
const seen = require("./helpers/seen");
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");
const src = fs.readFileSync("web/js/views-protect.js", "utf8");

const PLAN = { backup: "backup-7f3a91", source_volume: "pvc-demo-frigate", minimum_size_gb: 20,
  suggested_name: "frigate-config-restore", storage_class: "longhorn-r2", storage_classes: ["longhorn-r2"] };
async function restore(label, plan = PLAN) {
  const titles = [], body = { innerHTML: "" };
  const ctx = { document: { addEventListener() {} }, STATE: { data: {} }, setTimeout: () => 0, clearTimeout() {},
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    modal: title => titles.push(title), $: id => (id === "#mbody" ? body : id === "#mtitle" ? { set textContent(t) { titles.push(t); } } : null),
    tip: t => `<span class="tip" data-tip="${t}">?</span>`, icon: () => "", jsArg: s => JSON.stringify(String(s)),
    api: async path => (path.startsWith("/api/namespaces") ? ["lab"] : plan), defaultNamespace: () => "lab" };
  ctx.jsq = s => ctx.esc(ctx.jsArg(s)); ctx.window = ctx;
  vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(src, ctx);
  await ctx.lhRestore(PLAN.backup, label);
  return { html: body.innerHTML, titles };
}

const LATE_WHY = "longhorn-late makes a volume only once an app mounts it, and a restore has to finish before anything mounts it";
test("Restore backup says why a class is not offered", async () => {
  const { html } = await restore("frigate-config", { ...PLAN, refused_classes: { "longhorn-late": LATE_WHY } });
  assert.match(html, /<select id="lr_sc"[\s\S]*longhorn-r2[\s\S]*<\/select>/);
  assert.doesNotMatch(between(html, '<select id="lr_sc"', "</select>"), /longhorn-late/);
  assert.ok(html.includes(`${LATE_WHY}; choose another storage class`), "the reason and the ask are in the dialog");
});

test("Restore backup does not ask for another class when there is none", async () => {
  const { html } = await restore("frigate-config", { ...PLAN, storage_classes: [], refused_classes: { "longhorn-late": LATE_WHY } });
  assert.ok(html.includes(LATE_WHY), "the reason is in the dialog");
  assert.doesNotMatch(html, /choose another storage class/);
  assert.match(html, /no other class to choose/);
});

test("Restore backup says nothing about classes when every class can take it", async () => {
  const { html } = await restore("frigate-config");
  assert.doesNotMatch(html, /choose another storage class/);
});

/* Restore into an app: the review, and the plan asked for again with the chosen class. */
const INTO = { app: { namespace: "lab", name: "frigate", kind: "Deployment", replicas: 1 }, claim: "frigate-config",
  mount: { volume: "config", containers: [] }, backup: "backup-7f3a91", created: "", new_claim: "frigate-config-restore",
  storage_class: "longhorn-r2", storage_classes: ["longhorn-r2"], refused_classes: {}, size_gb: 20, minimum_size_gb: 20,
  check: "ready", keep_days: 7, managed: {}, notices: [], blockers: [], digest: "d", ready: true };
async function into(answer) {
  const asked = [], posted = [], body = { innerHTML: "" };
  const ctx = { document: { addEventListener() {} }, STATE: { data: {} }, setTimeout: () => 0, clearTimeout() {},
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    modal() {}, $: id => (id === "#mbody" ? body : id === "#ri_go" ? { disabled: false } : null), tip: t => t, icon: () => "",
    jsArg: s => JSON.stringify(String(s)), fmtWhen: s => s, toast() {}, closeModal() {},
    api: async (path, opts) => { if (opts) { posted.push(JSON.parse(opts.body)); return {}; } asked.push(path); return answer(path); } };
  ctx.jsq = s => ctx.esc(ctx.jsArg(s)); ctx.window = ctx;
  vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(src, ctx);
  return { ctx, asked, posted, body };
}
const onLate = path => path.includes("storage_class=longhorn-r2") ? INTO
  : { ...INTO, storage_class: "longhorn-late", refused_classes: { "longhorn-late": LATE_WHY },
    blockers: [`${LATE_WHY}; choose another storage class`], ready: false };

test("Restore into app on a class that cannot take it says why and offers the others", async () => {
  const { ctx, body } = await into(onLate);
  await ctx.lhRestoreInto("backup-7f3a91");
  assert.ok(body.innerHTML.includes(LATE_WHY), "the reason is in the dialog");
  assert.match(body.innerHTML, /This cannot start/);
  const select = between(body.innerHTML, '<select id="ri_sc"', "</select>");
  assert.match(select, /longhorn-r2/);
  assert.doesNotMatch(select, /longhorn-late/);
});

test("Choosing another class asks the review again and starts on that class", async () => {
  const { ctx, asked, posted, body } = await into(onLate);
  await ctx.lhRestoreInto("backup-7f3a91");
  await ctx.lhRestoreInto("backup-7f3a91", "longhorn-r2");
  assert.match(asked.at(-1), /storage_class=longhorn-r2/);
  assert.doesNotMatch(body.innerHTML, /This cannot start/);
  await ctx.restoreIntoStart();
  assert.equal(posted[0].storage_class, "longhorn-r2");
});

test("Restore into app on a class on neither list offers the others, as its blocker asks", async () => {
  const blocker = "local-path is not a storage class a restore can use; choose another storage class";
  const { ctx, body } = await into(() => ({ ...INTO, storage_class: "local-path", blockers: [blocker], ready: false }));
  await ctx.lhRestoreInto("backup-7f3a91");
  assert.ok(seen(body.innerHTML).includes(seen(blocker).replace(/; no other/, ". No other")), body.innerHTML);
  const select = between(body.innerHTML, '<select id="ri_sc"', "</select>");
  assert.match(select, /<option value="" selected disabled>Choose a class/);
  assert.match(select, /longhorn-r2/);
  assert.doesNotMatch(select, /local-path/);
});

test("Restore into app with no class left to offer shows the review's words and no empty choice", async () => {
  const blocker = `${LATE_WHY}; no other class can take it, so make one first`;
  const { ctx, body } = await into(() => ({ ...INTO, storage_class: "longhorn-late", storage_classes: [],
    refused_classes: { "longhorn-late": LATE_WHY }, blockers: [blocker], ready: false }));
  await ctx.lhRestoreInto("backup-7f3a91");
  assert.ok(seen(body.innerHTML).includes(seen(blocker).replace(/; no other/, ". No other")), body.innerHTML);
  assert.doesNotMatch(body.innerHTML, /id="ri_sc"/);
});

test("Restore into app shows no class choice when the app's own class can take it", async () => {
  const { ctx, asked, posted, body } = await into(() => INTO);
  await ctx.lhRestoreInto("backup-7f3a91");
  assert.doesNotMatch(body.innerHTML, /id="ri_sc"/);
  assert.doesNotMatch(asked[0], /storage_class/);
  await ctx.restoreIntoStart();
  assert.equal("storage_class" in posted[0], false);
});

test("Restore backup names what it restores, from the Backups row", async () => {
  const { titles } = await restore("frigate-config");
  assert.ok(titles.every(t => t === "Restore backup · frigate-config"), titles.join(" | "));
});

test("Restore backup opens with a lead and lists the backup as facts, with no notice boxes", async () => {
  const { html } = await restore("frigate-config");
  assert.match(html, /^\s*<p class="ui-lead">/);
  assert.match(html, /<dl class="ui-facts">[\s\S]*Backup[\s\S]*Source volume[\s\S]*Original capacity/);
  assert.doesNotMatch(html, /class="drow"/);
  assert.doesNotMatch(html, /class="note/);
});

test("the destination name is checked at its field, not in a box", async () => {
  const { html } = await restore("frigate-config");
  assert.match(html, /<input id="lr_name"[^>]*>[\s\S]{0,200}id="lr_check"/);
  assert.match(html, /id="lr_check" class="ui-help"/);
  const check = between(src, "window.lhRestoreCheck", "\n};\n");
  assert.doesNotMatch(check, /note (bad|good)/);
  assert.match(check, /ui-field-error/);
});

const body = name => between(src, `window.${name} =`, "\n};\n");

test("Protection plans and Protect open with a lead, not a muted paragraph", () => {
  for (const name of ["lhPlans", "lhAssign"]) {
    assert.match(body(name), /UI\.lead\(/, name);
    assert.doesNotMatch(body(name), /<p class="muted small">/, name);
  }
});

test("Snapshots says once how to read it, and its actions sit in a Close footer", () => {
  const snaps = body("lhSnaps");
  assert.match(snaps, /UI\.lead\("Oldest first\. Restore or browse any snapshot or backup\."\)/);
  assert.doesNotMatch(snaps, /Checkpoint ancestry can branch/);
  assert.doesNotMatch(src, /No active Longhorn purge reported/);
  assert.match(snaps, /UI\.actions\([\s\S]*Take snapshot now[\s\S]*UI\.cancel\("Close"\)/);
  assert.doesNotMatch(snaps, /<div class="row" style="margin-bottom:16px">/);
});

test("Backups has one notice when the volume is gone, and a Close footer", () => {
  const backups = body("lhBackupList");
  assert.match(backups, /UI\.callout\("warn", "The volume is gone"/);
  assert.match(backups, /UI\.lead\(/, "it opens with one sentence, like the other dialogs");
  assert.doesNotMatch(backups, /class="note/);
  assert.match(backups, /UI\.cancel\("Close"\)/);
});

test("Backup target opens with a lead and keeps its explanations in UI.more", () => {
  const target = body("lhTarget");
  assert.match(target, /UI\.lead\(/);
  assert.doesNotMatch(target, /class="note/);
  assert.equal((target.match(/UI\.more\(/g) || []).length, 2);
});

test("warning and error boxes in Data Protection only go down", () => {
  assert.ok((src.match(/class="note (warn|bad)/g) || []).length <= 2, "use UI.callout for a notice");
});

test("New storage class says nothing about migration until Live-migratable is ticked", () => {
  const storage = require("./helpers/storage-source")();
  assert.match(storage, /<div id="sc_hint" hidden><\/div>/);
  assert.match(storage, /Live-migratable · for VM disks \$\{tip\("Migratable is for VM live migration/);
  const hint = between(storage, "window.storageClassHint", "\n};\n");
  assert.match(hint, /hint\.hidden = !on/);
  assert.match(hint, /UI\.callout\("warn"/);
});
