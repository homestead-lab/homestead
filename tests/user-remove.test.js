const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const vm = require("vm");
const between = require("./helpers/between");

// Only the removal: the rest of auth.js needs a page.
const src = fs.readFileSync("web/js/auth.js", "utf8");
const body = between(src, "async function userRemove", "window.userRemove = userRemove");

function load(answer) {
  const calls = { toasts: [], settings: 0, asked: [] };
  const ctx = {
    STATE: { view: "settings" }, window: {},
    ask: async (msg, opts) => { calls.asked.push([msg, opts]); return true; },
    api: async () => { if (answer instanceof Error) throw answer; return answer; },
    toast: (m, k) => calls.toasts.push([m, k]),
    resetPaint: () => {}, viewSettings: () => { calls.settings++; },
  };
  vm.createContext(ctx);
  vm.runInContext(body + ";this.userRemove = userRemove;", ctx);
  return { ctx, calls };
}

test("removing from the card stays on the card and redraws it", async () => {
  const { ctx, calls } = load({ ok: true });
  await ctx.userRemove("alex", "card");
  assert.equal(calls.settings, 1, "the card is drawn again without the user");
  assert.deepEqual(calls.toasts, [["alex removed", "ok"]]);
  assert.equal(calls.asked[0][1].ok, "Remove user", "the button says what it does");
});

test("a user already removed elsewhere is not an error, and the row goes", async () => {
  const { ctx, calls } = load(new Error("no such user"));
  await ctx.userRemove("alex", "card");
  assert.deepEqual(calls.toasts, [["alex had already been removed", "ok"]]);
  assert.equal(calls.settings, 1);
});

test("any other failure still redraws, so the list matches the server", async () => {
  const { ctx, calls } = load(new Error("cannot delete the only administrator"));
  await ctx.userRemove("alex");
  assert.deepEqual(calls.toasts, [["cannot delete the only administrator", "bad"]]);
  assert.equal(calls.settings, 1, "Settings, Users and access is redrawn");
});

/* Users are managed in Settings, Users and access, and the setup guide's
   Users step opens it (#465); the old dialog has nothing left that opens it. */
test("the old Users dialog is gone", () => {
  for (const name of ["manageUsers", "addUser", "setRole", "delUser"])
    assert.doesNotMatch(src, new RegExp(`window\\.${name}\\s*=`), `${name} is removed`);
  assert.doesNotMatch(body, /"dialog"/, "userRemove has no dialog path");
});
