"use strict";
// The Edit review names the values stored in the app's Secret that a save
// deletes (secret_removed from /api/edit/preview), asks for a tick before
// saving them away, and says when it could not check - never "none". A
// removed container's line and the review stop saying "data are kept" when
// that container had stored values.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

const CAPACITY = { blocked: false, requires_confirmation: true, candidates: [], warnings: [], additional: 1 };
function world(review) {
  const log = [], fields = { "#editCapacityConfirm": { checked: false }, "#editRemovedConfirm": { checked: false },
    "#editGo": { disabled: false, textContent: "Save", onclick: "orig" }, "#e_removed_containers": { innerHTML: "" },
    "#mbody": { insertAdjacentHTML() {} } };
  const c = { window: {}, console, URLSearchParams, Map, document: { addEventListener() {}, getElementById: id => fields["#" + id] || null },
    $: s => fields[s], $$: () => [], esc: s => String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"),
    childModal: (title, body) => log.push(["childModal", title, body]), toast: (text, tone) => log.push(["toast", text, tone]),
    closeModal() {}, refresh() {}, setTimeout() {},
    api: async (path, options) => {
      log.push(["api", path, JSON.parse(options.body)]);
      return path === "/api/edit/preview" ? { capacity_token: "t", managed: null, capacity: CAPACITY, ...review } : { ok: true, name: "tunnel" };
    } };
  c.window = c; vm.createContext(c); require("./helpers/core-text")(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => c.esc(c.jsArg(s)); c.tip = () => ""; c.icon = () => "";
  for (const f of ["web/js/ui.js", "web/js/gitops.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  vm.runInContext(require("./helpers/workloads-source")(), c);
  vm.runInContext(require("./helpers/lifecycle-source")(), c);
  const shown = () => log.filter(([kind]) => kind === "childModal").at(-1)[2].replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
  const saves = () => log.filter(([kind, path]) => kind === "api" && path === "/api/edit").length;
  return { c, fields, log, shown, saves };
}
const body = (extra = {}) => ({ ns: "lab", name: "tunnel", containers: [{ original_name: "cloudflared", name: "cloudflared" }], ...extra });

test("the review names the stored values a save deletes and says they cannot be restored", async () => {
  const w = world({ secret_removed: [{ container: "cloudflared", name: "OLD_TOKEN" }, { container: "cloudflared", name: "API_TOKEN" }] });
  await w.c.editReview(body());
  assert.match(w.shown(), /Saving deletes 2 values stored in this app's Secret\. Homestead cannot restore them: OLD_TOKEN, API_TOKEN\./);
  assert.match(w.shown(), /Delete 2 stored values/);
});

test("a save that deletes stored values waits for its own tick", async () => {
  const w = world({ secret_removed: [{ container: "cloudflared", name: "OLD_TOKEN" }] });
  await w.c.editReview(body());
  w.fields["#editCapacityConfirm"].checked = true;
  await w.c.confirmEdit();
  assert.equal(w.saves(), 0, "not without the tick");
  assert.ok(w.log.some(([kind, text]) => kind === "toast" && /accept their warnings/.test(text)));
  w.fields["#editRemovedConfirm"].checked = true;
  await w.c.confirmEdit();
  assert.equal(w.saves(), 1);
});

test("values of several containers are named with their container", async () => {
  const w = world({ secret_removed: [{ container: "cloudflared", name: "OLD_TOKEN" }, { container: "worker", name: "DB_PASSWORD" }] });
  await w.c.editReview(body({ remove_containers: ["worker"] }));
  assert.match(w.shown(), /OLD_TOKEN \(cloudflared\), DB_PASSWORD \(worker\)/);
});

test("an unread Secret says it could not check, never none, and the save waits for a new review", async () => {
  const w = world({ secret_removed: null });
  await w.c.editReview(body());
  assert.match(w.shown(), /Homestead could not read this app's Secret\. Review again to see which stored values the save deletes\./);
  assert.match(w.shown(), /Review changes again/);
  assert.doesNotMatch(w.shown(), /Save changes|Delete \d/);
  w.fields["#editCapacityConfirm"].checked = true;
  await w.c.confirmEdit();
  assert.equal(w.saves(), 0, "nothing is saved that the review could not check");
  const previews = () => w.log.filter(([kind, path]) => kind === "api" && path === "/api/edit/preview").length;
  await w.c.editReviewAgain();
  assert.equal(previews(), 2, "the button runs the review again");
});

test("the storage-copy review names the values, and saves only with their tick", async () => {
  const removed = [{ container: "cloudflared", name: "OLD_TOKEN" }];
  const w = world({ secret_removed: removed, capacity: { ...CAPACITY, copy_helper: { ...CAPACITY } } });
  await w.c.editReview(body({ containers: [{ original_name: "cloudflared", name: "cloudflared",
    volumes: [{ path: "/data", source: "new-data", copy_from: { claim: "old-data" } }] }] }));
  assert.match(w.shown(), /Saving deletes 1 value stored in this app's Secret\. Homestead cannot restore it: OLD_TOKEN\./);
  assert.match(w.shown(), /Delete 1 stored value/);
  assert.match(w.shown(), /Start data move/);
  w.fields["#editCapacityConfirm"].checked = true;
  await w.c.confirmEdit();
  assert.equal(w.saves(), 0, "not without the tick");
  w.fields["#editRemovedConfirm"].checked = true;
  await w.c.confirmEdit();
  assert.equal(w.saves(), 1, "the tick is there, so the data move can be saved");
});

test("an unread Secret in the storage-copy review asks for the review again", async () => {
  const w = world({ secret_removed: null, capacity: { ...CAPACITY, copy_helper: { ...CAPACITY } } });
  await w.c.editReview(body());
  assert.match(w.shown(), /Review again to see which stored values the save deletes/);
  assert.match(w.shown(), /Review changes again/);
  assert.doesNotMatch(w.shown(), /Start data move/);
});

test("past four names the rest are counted, and kept or unnamed values read right", async () => {
  const names = ["A_TOKEN", "B_TOKEN", "C_TOKEN", "D_TOKEN", "E_TOKEN", "F_TOKEN"];
  const w = world({ secret_removed: [...names.map(name => ({ container: "cloudflared", name })),
    { container: "cloudflared", name: "RENAMED_TOKEN", value_kept: true }] });
  await w.c.editReview(body());
  assert.match(w.shown(), /Saving deletes 6 values stored in this app's Secret\. Homestead cannot restore them: A_TOKEN, B_TOKEN, C_TOKEN, D_TOKEN and 2 more\./);
  assert.doesNotMatch(w.shown(), /RENAMED_TOKEN/, "a value kept under another name is not lost");
  const five = world({ secret_removed: names.slice(0, 5).map(name => ({ container: "cloudflared", name })) });
  await five.c.editReview(body());
  assert.match(five.shown(), /Homestead cannot restore them: A_TOKEN, B_TOKEN, C_TOKEN, D_TOKEN and 1 more\./);
  const four = world({ secret_removed: names.slice(0, 4).map(name => ({ container: "cloudflared", name })) });
  await four.c.editReview(body());
  assert.match(four.shown(), /Homestead cannot restore them: A_TOKEN, B_TOKEN, C_TOKEN, D_TOKEN\./);
  assert.match(w.shown(), /Delete 6 stored values/);
  const leftover = world({ secret_removed: [{ container: null, name: "cloudflared.LEFTOVER_TOKEN" }, { container: "worker", name: "DB_PASSWORD" }] });
  await leftover.c.editReview(body());
  assert.match(leftover.shown(), /cloudflared\.LEFTOVER_TOKEN, DB_PASSWORD \(worker\)/);
});

test("a value kept under another name needs no tick", async () => {
  const w = world({ secret_removed: [{ container: "cloudflared", name: "TUNNEL_TOKEN", value_kept: true }] });
  await w.c.editReview(body());
  assert.doesNotMatch(w.shown(), /stored value|Secret/);
  w.fields["#editCapacityConfirm"].checked = true;
  await w.c.confirmEdit();
  assert.equal(w.saves(), 1);
});

test("a save that deletes nothing says nothing about stored values", async () => {
  for (const review of [{ secret_removed: [] }, {}]) {
    const w = world(review);
    await w.c.editReview(body());
    assert.doesNotMatch(w.shown(), /stored value|Secret/);
    w.fields["#editCapacityConfirm"].checked = true;
    await w.c.confirmEdit();
    assert.equal(w.saves(), 1);
  }
});

test("removing a container with stored values does not say its data are kept", async () => {
  const changes = { added: [], removed: ["worker"] };
  const lossy = world({ secret_removed: [{ container: "worker", name: "DB_PASSWORD" }], capacity: { ...CAPACITY, container_changes: changes } });
  await lossy.c.editReview(body({ remove_containers: ["worker"] }));
  assert.match(lossy.shown(), /Containers removed: worker\. The app restarts\. Volumes are kept\./);
  assert.doesNotMatch(lossy.shown(), /data are kept/);
  const unread = world({ secret_removed: null, capacity: { ...CAPACITY, container_changes: changes } });
  await unread.c.editReview(body({ remove_containers: ["worker"] }));
  assert.match(unread.shown(), /The app restarts\. Volumes are kept\./, "it cannot say the data are kept");
  const plain = world({ secret_removed: [], capacity: { ...CAPACITY, container_changes: changes } });
  await plain.c.editReview(body({ remove_containers: ["worker"] }));
  assert.match(plain.shown(), /Volumes and data are kept\./);
});

test("the removed-container line counts the container's stored values", () => {
  const w = world({});
  vm.runInContext(`EDIT_REMOVED_PANELS.set(1, { name: "worker", original: "worker", panels: [{ dataset: { secretEnv: '["DB_PASSWORD"]' } }] });
    EDIT_REMOVED_PANELS.set(2, { name: "cache", original: "cache", panels: [{ dataset: {} }] });
    EDIT_REMOVED_PANELS.set(3, { name: "queue", original: "queue", panels: [{ dataset: { secretEnv: '["A_PASSWORD","B_TOKEN"]' } }] });
    containerRemovedPaint();`, w.c);
  const text = w.fields["#e_removed_containers"].innerHTML.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
  assert.match(text, /Removed when saved: worker\. Volumes are kept\. 1 value stored in the app's Secret is deleted\./);
  assert.match(text, /Removed when saved: cache\. Volumes and data are kept\./);
  assert.match(text, /Removed when saved: queue\. Volumes are kept\. 2 values stored in the app's Secret are deleted\./);
});
