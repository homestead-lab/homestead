"use strict";
// The server checks that a save deleting values stored in the app's Secret
// carries the review's own token for that list (confirm_secret_removed). The
// page sends it once the tick is set, and shows the server's refusal (the
// values changed since the review) with a way out: a new review.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

const CAPACITY = { blocked: false, requires_confirmation: true, candidates: [], warnings: [], additional: 1 };
function world(review, refuse = null) {
  const log = [], notices = [], fields = { "#editCapacityConfirm": { checked: false }, "#editRemovedConfirm": { checked: false },
    "#editGo": { disabled: false, textContent: "Save", onclick: "orig" }, "#e_removed_containers": { innerHTML: "" },
    "#mbody": { insertAdjacentHTML: (where, html) => notices.push(html) } };
  const c = { window: {}, console, URLSearchParams, Map, document: { addEventListener() {}, getElementById: id => fields["#" + id] || null },
    $: s => fields[s], $$: () => [], esc: s => String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"),
    childModal: (title, body) => log.push(["childModal", title, body]), toast: (text, tone) => log.push(["toast", text, tone]),
    closeModal() {}, refresh() {}, setTimeout() {},
    api: async (path, options) => {
      log.push(["api", path, JSON.parse(options.body)]);
      if (path === "/api/edit/preview") return { capacity_token: "t", managed: null, capacity: CAPACITY, ...review };
      if (refuse) throw Object.assign(new Error(refuse.error), { status: 409, body: refuse });
      return { ok: true, name: "tunnel" };
    } };
  c.window = c; vm.createContext(c); require("./helpers/core-text")(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => c.esc(c.jsArg(s)); c.tip = () => ""; c.icon = () => "";
  for (const f of ["web/js/ui.js", "web/js/gitops.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  vm.runInContext(require("./helpers/workloads-source")(), c);
  vm.runInContext(require("./helpers/lifecycle-source")(), c);
  const sent = () => log.filter(([kind, path]) => kind === "api" && path === "/api/edit").map(([, , body]) => body);
  const previews = () => log.filter(([kind, path]) => kind === "api" && path === "/api/edit/preview").length;
  return { c, fields, log, notices, sent, previews };
}
const body = () => ({ ns: "lab", name: "tunnel", containers: [{ original_name: "cloudflared", name: "cloudflared" }] });
const LOST = [{ key: "cloudflared.OLD_TOKEN", container: "cloudflared", name: "OLD_TOKEN", value_kept: false }];

test("a ticked deletion sends the review's token", async () => {
  const w = world({ secret_removed: LOST, secret_removed_token: "removal-1" });
  await w.c.editReview(body());
  w.fields["#editCapacityConfirm"].checked = true;
  w.fields["#editRemovedConfirm"].checked = true;
  await w.c.confirmEdit();
  assert.equal(w.sent().length, 1);
  assert.equal(w.sent()[0].confirm_secret_removed, "removal-1");
});

test("a save that loses no value sends a token only when the review gave one", async () => {
  const kept = world({ secret_removed: [{ ...LOST[0], value_kept: true }], secret_removed_token: "removal-2" });
  await kept.c.editReview(body());
  kept.fields["#editCapacityConfirm"].checked = true;
  await kept.c.confirmEdit();
  assert.equal(kept.sent()[0].confirm_secret_removed, "removal-2", "a renamed variable's old key goes too");
  const none = world({ secret_removed: [] });
  await none.c.editReview(body());
  none.fields["#editCapacityConfirm"].checked = true;
  await none.c.confirmEdit();
  assert.equal(none.sent().length, 1);
  assert.ok(!("confirm_secret_removed" in none.sent()[0]));
});

test("the server's refusal is shown with a new review as the way out, and nothing counts as maybe saved", async () => {
  const error = "The values stored in this app's Secret changed since the review. Nothing was saved. Review the changes again to see which values the save deletes.";
  const w = world({ secret_removed: LOST, secret_removed_token: "removal-1" }, { error, refused: "secret_removed", reason: "changed" });
  await w.c.editReview(body());
  w.fields["#editCapacityConfirm"].checked = true;
  w.fields["#editRemovedConfirm"].checked = true;
  await w.c.confirmEdit();
  const button = w.fields["#editGo"];
  assert.equal(button.textContent, "Review changes again");
  assert.equal(button.disabled, false);
  assert.equal(w.notices.length, 1);
  assert.match(w.notices[0], /changed since the review/);
  assert.doesNotMatch(w.notices.join(" "), /Reload saved settings|may already be saved/);
  button.onclick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(w.previews(), 2, "the button runs the review again");
  assert.ok(!w.log.some(([kind, text]) => kind === "toast" && /Reload the app/.test(text)));
});

test("any other failure still counts as maybe saved", async () => {
  const w = world({ secret_removed: [] }, { error: "The app changed after the review" });
  await w.c.editReview(body());
  w.fields["#editCapacityConfirm"].checked = true;
  await w.c.confirmEdit();
  assert.equal(w.fields["#editGo"].textContent, "Check before saving again");
  assert.match(w.notices.join(" "), /Reload saved settings/);
});
