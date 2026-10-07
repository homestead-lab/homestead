const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const vm = require("vm");

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const ctx = { console, esc, window: {}, appAvatar: (name, icon) => `<span class="av">${esc(name)}|${esc(icon)}</span>` };
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("web/js/logos.js", "utf8"), ctx);

test("tiles escape names from the catalogue and mark the image match", () => {
  const html = vm.runInContext(`logoTilesHtml([{ name: '<img src=x onerror=alert(1)>', icon: 'https://example.com/a.png', match: 'image' },
    { name: 'Other', icon: 'https://example.com/b.png', match: 'name' }], 0, 'logoPick')`, ctx);
  assert.ok(!html.includes("<img src=x"), "a name is text, never markup");
  assert.match(html, /&lt;img src=x/);
  assert.match(html, /aria-selected="true"[^]*image match/);
  assert.match(html, /logoPick\(1\)/);
});

test("nothing found says what to do next", () => {
  assert.match(vm.runInContext("logoTilesHtml([], -1, 'logoPick')", ctx), /paste a URL/);
});

test("only apps that can carry a logo and have none are counted", () => {
  const can = ctx.window.logoCanHave;
  assert.equal(can({ name: "a" }), true);
  for (const extra of [{ icon: "x" }, { has_logo: true }, { logo_skipped: true }, { platform: true }, { self: true },
    { homestead: "self" }, { managed_smb: true }, { managed_nfs: true }, { site: { handle: "b" } }])
    assert.equal(can({ name: "a", ...extra }), false, JSON.stringify(extra));
});
