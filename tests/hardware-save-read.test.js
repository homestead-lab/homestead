"use strict";
// Saving or deleting a hardware feature names the list the form was built on,
// so the server can refuse a save made from a list another admin changed
// since, instead of undoing their change (stale-writes).
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

const IGPU = { id: "igpu", name: "Intel/AMD iGPU", builtin: true };
const CORAL = { id: "coral_usb", name: "Google Coral USB", host_path: "/dev/bus/usb", container_path: "/dev/bus/usb",
  path_type: "Directory", usb_ids: ["1a6e:089a"], label: "hardware/coral-usb", builtin: false };

function page(fields) {
  const posts = [];
  const c = { console, Set, Map, Promise, URLSearchParams, location: { search: "" }, document: { addEventListener() {} },
    STATE: { data: { hardwareFeatures: [IGPU, CORAL] }, platform: {} },
    $: sel => ({ value: fields[sel.replace(/^#/, "")] ?? "", textContent: "" }), $$: () => [], toast() {},
    ask: async () => true, modal() {}, modalBack() {}, icon: () => "", esc: v => String(v ?? ""),
    api: async (path, opts) => { posts.push({ path, body: JSON.parse(opts.body) }); return []; } };
  c.window = c; vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => c.esc(c.jsArg(s));
  for (const f of ["web/js/ui.js", "web/js/views-overview.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  Object.assign(c, { hardwareFeatureSettings: async () => {}, hardwareDef: id => [IGPU, CORAL].find(x => x.id === id) });
  return { c, posts };
}

const FORM = { hf_id: "hailo", hf_name: "Hailo-8", hf_desc: "", hf_host: "/dev/hailo0", hf_container: "/dev/hailo0",
  hf_type: "CharDevice", hf_usb: "" };

test("a saved feature names the custom list the form was built on", async () => {
  const { c, posts } = page(FORM);
  await c.hardwareFeatureSave("");
  assert.equal(posts.length, 1);
  assert.equal(posts[0].path, "/api/hardware/features");
  assert.deepEqual(posts[0].body.features.map(f => f.id), ["coral_usb", "hailo"]);
  assert.deepEqual(posts[0].body.read, [CORAL], "the list as read, built-ins left out");
});

test("a deleted feature names the list it was deleted from", async () => {
  const { c, posts } = page({});
  await c.hardwareFeatureDelete("coral_usb");
  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].body.features, []);
  assert.deepEqual(posts[0].body.read, [CORAL]);
});
