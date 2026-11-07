"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
function setup() {
  const fields = {"#vh": {}, "#vh_boot_output": {value: "console"}, "#vh_firmware": {value: "bios"},
    "#vh_secure": {checked: false}, "#vh_serial": {checked: true}, "#vh_notes": {innerHTML: ""}};
  const ctx = {console, Intl, Set, $: key => fields[key], $$: () => [], esc: String, tip: () => "", toast(){}};
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
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
  assert.match(html, />Display</);
  assert.match(html, /Virtual display \(the console's Screen\)/);
  assert.match(html, /Passed-through GPU/);
  fields["#vh_boot_output"].value = "gpu"; ctx.vmBootOutputChanged();
  const changes = ctx.vmHardwareChanges(original);
  assert.equal(changes.boot_output, "gpu"); assert.equal(changes.graphics, false);
  assert.equal(changes.firmware, "uefi");
});

test("the display and virtual devices are a section of their own beside Disks", () => {
  const {ctx} = setup();
  const original = vm.runInContext("vmHardwareValues()", ctx);
  const split = ctx.vmHardwareFields(original, {}, false, false, true);
  assert.doesNotMatch(split, /id="vh_boot_output"/, "not repeated inside Hardware");
  assert.match(ctx.vmHardwareFields(original), /id="vh_boot_output"/, "still inside Hardware when not split");
  const display = ctx.vmDisplayFields(original);
  assert.match(display, /id="vh_boot_output"/);
  assert.match(display, /id="vh_display_notes"/);
  const edit = require("node:fs").readFileSync("web/js/views-vms.js", "utf8");
  assert.match(edit, /\{key:"display", title:"Display and devices", html:vmDisplayFields\(v\.hardware\)\}/);
  assert.match(edit, /\{ key: "devices", title: `Passthrough · \$\{\(v\.host_devices \|\| \[\]\)\.length\}`, html: devices \}/, "the VM page shows passthrough without Edit");
  assert.match(require("./helpers/lifecycle-source")(), /title: "Display and devices", html: vmDisplayFields\(opts\.hardware_base\)/);
});
