"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const context = { window: {}, console };
vm.createContext(context);
vm.runInContext(fs.readFileSync("web/js/views-storage.js", "utf8"), context);

test("volume deletion confirmation is exact and whitespace tolerant", () => {
  assert.equal(context.window.volumeDeleteConfirmationValid("media", "media"), true);
  assert.equal(context.window.volumeDeleteConfirmationValid("media", "  media  "), true);
  assert.equal(context.window.volumeDeleteConfirmationValid("media", "Media"), false);
  assert.equal(context.window.volumeDeleteConfirmationValid("media", "media-data"), false);
  assert.equal(context.window.volumeDeleteConfirmationValid("media", ""), false);
});
