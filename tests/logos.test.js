const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const vm = require("vm");

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const ctx = { console, esc, window: {}, appAvatar: (name, icon) => `<span class="av">${esc(name)}|${esc(icon)}</span>` };
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync("web/js/logos.js", "utf8"), ctx);

test("tiles escape names from the catalogue and mark the image match", () => {
  const html = vm.runInContext(`logoTilesHtml([{ name: '<img src=x onerror=alert(1)>', icon: 'https://example.com/a.png', match: 'image' },
    { name: 'Other', icon: 'https://example.com/b.png', match: 'name' }], 0, 'logoPick')`, ctx);
  assert.ok(!html.includes("<img src=x"), "a name is text, never markup");
  assert.match(html, /&lt;img src=x/);
  assert.match(html, /aria-selected="true"[^]*image match/);
  assert.match(html, /logoPick\(1\)/);
});

test("a logo tile is one Tab stop: its match chip is not focusable inside the button", () => {
  const html = vm.runInContext(`logoTilesHtml([{ name: 'A', icon: 'https://example.com/a.png', match: 'chart' },
    { name: 'B', icon: 'https://example.com/b.png', match: 'image' }, { name: 'C', os: 'ubuntu', icon: '/x.svg', match: 'its-os' }], 0, 'logoPick')`, ctx);
  const tiles = html.split("<button").slice(1);
  assert.equal(tiles.length, 3);
  for (const tile of tiles) {
    assert.doesNotMatch(tile, /tabindex=/, "nothing inside the tile takes focus");
    assert.match(tile, /class="ui-chip ok slim"/, "the match chip is still there");
  }
  assert.match(tiles[0], /title="The logo its own Helm chart names"/, "the tile carries the tip");
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

test("while the app store is still loading, the logo fixup says so and offers Try again", async () => {
  const body = { innerHTML: "" };
  Object.assign(ctx, {
    modal() {}, icon: () => "", $: selector => (selector === "#mbody" ? body : null),
    api: async () => { throw Object.assign(new Error("the App Store catalogue is still loading"), { status: 503, body: { loading: true } }); },
  });
  await vm.runInContext("window.logoFixup()", ctx);
  assert.match(body.innerHTML, /still loading/);
  assert.match(body.innerHTML, /Try again/);
  assert.match(body.innerHTML, /logoFixup\(\)/);
});
