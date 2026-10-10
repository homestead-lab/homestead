const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const vm = require("vm");

// Only the removal: the rest of auth.js needs a page.
const src = fs.readFileSync("web/js/auth.js", "utf8");
const body = src.slice(src.indexOf("async function userRemove"), src.indexOf("window.delUser"));

function load(answer) {
  const calls = { toasts: [], dialog: 0, settings: 0, asked: [] };
  const ctx = {
    STATE: { view: "settings" }, window: {},
    ask: async (msg, opts) => { calls.asked.push([msg, opts]); return true; },
    api: async () => { if (answer instanceof Error) throw answer; return answer; },
    toast: (m, k) => calls.toasts.push([m, k]),
    manageUsers: () => { calls.dialog++; }, resetPaint: () => {}, viewSettings: () => { calls.settings++; },
  };
  vm.createContext(ctx);
  vm.runInContext(body + ";this.userRemove = userRemove;", ctx);
  return { ctx, calls };
}

test("removing from the card stays on the card and redraws it", async () => {
  const { ctx, calls } = load({ ok: true });
  await ctx.userRemove("alex", "card");
  assert.equal(calls.dialog, 0, "the old Users dialog does not open");
  assert.equal(calls.settings, 1, "the card is drawn again without the user");
  assert.deepEqual(calls.toasts, [["alex removed", "ok"]]);
  assert.equal(calls.asked[0][1].ok, "Remove", "the button says what it does");
});

test("a user already removed elsewhere is not an error, and the row goes", async () => {
  const { ctx, calls } = load(new Error("no such user"));
  await ctx.userRemove("alex", "card");
  assert.deepEqual(calls.toasts, [["alex had already been removed", "ok"]]);
  assert.equal(calls.settings, 1);
});

test("any other failure still redraws, so the list matches the server", async () => {
  const { ctx, calls } = load(new Error("cannot delete the only administrator"));
  await ctx.userRemove("alex", "dialog");
  assert.deepEqual(calls.toasts, [["cannot delete the only administrator", "bad"]]);
  assert.equal(calls.dialog, 1, "the dialog it came from is redrawn");
});
