"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
function setup() {
  const fields = {"#vh": {}, "#vh_boot_output": {value: "console"}, "#vh_firmware": {value: "bios"},
    "#vh_secure": {checked: false}, "#vh_serial": {checked: true}, "#vh_notes": {innerHTML: ""}};
  const ctx = {console, Intl, Set, $: key => fields[key], $$: () => [], esc: String, tip: () => "", toast(){}};
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/vm-hardware.js", "utf8"), ctx);
  return {ctx, fields};
}
test("GPU output switches BIOS to UEFI, clears Secure Boot, and submits consistent display settings", () => {
  const {ctx, fields} = setup();
  fields["#vh_secure"].checked = true;
  fields["#vh_boot_output"].value = "gpu";
  ctx.vmBootOutputChanged();
  const h = vm.runInContext("vmHardwareValues()", ctx);
  assert.equal(h.boot_output, "gpu"); assert.equal(h.graphics, false);
  assert.equal(h.firmware, "uefi"); assert.equal(h.secure_boot, false);
  assert.match(fields["#vh_notes"].innerHTML, /monitor.*GPU.*bootloader/);
  fields["#vh_boot_output"].value = "console";
  ctx.vmBootOutputChanged();
  assert.equal(vm.runInContext("vmHardwareValues().graphics", ctx), true);
  assert.equal(fields["#vh_firmware"].value, "uefi");
});
test("serial output enables its console and presets update the output selector", () => {
  const {ctx, fields} = setup();
  fields["#vh_serial"].checked = false; fields["#vh_boot_output"].value = "serial";
  ctx.vmBootOutputChanged(); assert.equal(fields["#vh_serial"].checked, true);
  vm.runInContext('vhSet(vmPresetSettings("linux"))', ctx);
  assert.equal(fields["#vh_boot_output"].value, "console");
  vm.runInContext('vhSet(vmPresetSettings("headless"))', ctx);
  assert.equal(fields["#vh_boot_output"].value, "serial");
});
test("opening the form preserves its current output and changes serialize for review", () => {
  const {ctx, fields} = setup();
  const original = vm.runInContext("vmHardwareValues()", ctx);
  assert.equal(ctx.vmHardwareChanges(original), null);
  const html = ctx.vmHardwareFields(original);
  assert.match(html, /Primary boot output/);
  assert.match(html, /Passed-through GPU/);
  fields["#vh_boot_output"].value = "gpu"; ctx.vmBootOutputChanged();
  const changes = ctx.vmHardwareChanges(original);
  assert.equal(changes.boot_output, "gpu"); assert.equal(changes.graphics, false);
  assert.equal(changes.firmware, "uefi");
});
