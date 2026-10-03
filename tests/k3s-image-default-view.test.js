"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const IMAGE = "https://cloud-images.ubuntu.com/minimal/releases/resolute/release-20260827/ubuntu-26.04-minimal-cloudimg-amd64.img";
function setup(images = [], harvester = false) {
  const fields = {"#mbody": {innerHTML: ""}};
  for (const [name, value] of Object.entries({image: "url", name: "demo", setup: "homestead", servers: "1",
    agents: "2", cores: "2", mem: "4Gi", disk: "40", pass: "long-password", net: "default/lan", sc: "longhorn", ip: "192.0.2.60,192.0.2.61,192.0.2.62"}))
    fields["#k_" + name] = {value};
  fields["#k_kubevirt"] = {checked: false};
  const ctx = {console, Map, document: {addEventListener() {}}, STATE: {data: {}},
    $: key => fields[key], $$: () => [], esc: String, tip: () => "", modal() {},
    api: async () => ({images, harvester}), vmLanNetworks: () => [{name: "default/lan"}],
    vmAddressFields: () => "", vmReadAddress: () => ({prefix: 24})};
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(fs.readFileSync("web/js/views-vms.js", "utf8"), ctx);
  ctx.k3sCountChanged = () => {};
  return {ctx, fields};
}
test("new clusters select the released minimal image even with cached Ubuntu images", async () => {
  for (const harvester of [false, true]) {
    for (const images of [[], [
      {namespace: "images", name: "old", display: "Ubuntu 24.04 Noble", storage_class: "longhorn"},
      {namespace: "images", name: "other", display: "Ubuntu custom", storage_class: "longhorn"}]]) {
      const t = setup(images, harvester); await t.ctx.k3sCluster();
      const select = t.fields["#mbody"].innerHTML.match(/<select id="k_image">([\s\S]*?)<\/select>/)[1];
      const selected = [...select.matchAll(/<option[^>]*selected[^>]*>/g)].map(match => match[0]);
      assert.deepEqual(selected, ['<option value="url" selected>']);
      assert.match(select, /Ubuntu 26\.04\.1 LTS minimal cloud image/);
      assert.equal(t.ctx.k3sBody().image_url, IMAGE);
      assert.equal(t.ctx.k3sBody().image_id, "");
    }
  }
});
test("choosing a cached image sends its identity instead of the default download", async () => {
  const t = setup([{namespace: "images", name: "custom", display: "Ubuntu custom", storage_class: "longhorn"}], true);
  await t.ctx.k3sCluster();
  assert.match(t.fields["#mbody"].innerHTML, /value="image:images\/custom"/);
  t.fields["#k_image"].value = "image:images/custom";
  assert.equal(t.ctx.k3sBody().image_id, "images/custom");
  assert.equal(t.ctx.k3sBody().image_url, "");
});
