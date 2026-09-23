"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const context = { window: {}, console };
vm.createContext(context);
vm.runInContext(fs.readFileSync("web/js/volume-picker.js", "utf8"), context);

const { volumeKind, volumeRowIssue, volumeListIssue } = context.window;

test("stored volumes map onto the picker's storage kinds", () => {
  assert.equal(volumeKind({ kind: "pod" }), "pod");
  assert.equal(volumeKind({ type: "host" }), "host");
  assert.equal(volumeKind({ type: "emptyDir" }), "ephemeral");
  assert.equal(volumeKind({ type: "pvc", create: false }), "existing");
  assert.equal(volumeKind({ type: "pvc", access_mode: "ReadWriteMany" }), "new-rwx");
  assert.equal(volumeKind({}), "new-rwo");
});

test("a storage mapping needs an absolute mount path and a source", () => {
  assert.match(volumeRowIssue({ path: "", kind: "existing", source: "media" }), /mount path/);
  assert.match(volumeRowIssue({ path: "config", kind: "existing", source: "media" }), /must start with \//);
  assert.match(volumeRowIssue({ path: "/config", kind: "existing", source: "" }), /needs a storage source/);
  assert.equal(volumeRowIssue({ path: "/config", kind: "existing", source: "media" }), "");
});

test("temporary storage needs no source and new claims need a Kubernetes name", () => {
  assert.equal(volumeRowIssue({ path: "/tmp/cache", kind: "ephemeral", source: "" }), "");
  assert.match(volumeRowIssue({ path: "/config", kind: "new-rwo", source: "Frigate Config" }),
    /lowercase letters, numbers and dashes/);
  assert.equal(volumeRowIssue({ path: "/config", kind: "new-rwx", source: "frigate-config" }), "");
});

test("one container cannot mount two volumes on the same path", () => {
  const rows = [
    { path: "/config", kind: "existing", source: "frigate-config" },
    { path: "/config/", kind: "ephemeral", source: "" },
  ];
  assert.match(volumeListIssue(rows), /mounted twice/);
  assert.equal(volumeListIssue([rows[0], { path: "/media", kind: "pod", source: "config" }]), "");
  assert.equal(volumeListIssue([]), "");
});

test("a folder in a volume stays inside it", () => {
  assert.equal(volumeRowIssue({ path: "/config", kind: "existing", source: "app", sub_path: "config" }), "");
  assert.match(volumeRowIssue({ path: "/config", kind: "existing", source: "app", sub_path: "../x" }), /inside its volume/);
  assert.match(volumeRowIssue({ path: "/config", kind: "existing", source: "app", sub_path: "/etc" }), /inside its volume/);
});
