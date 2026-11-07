"use strict";
// The shared time helpers from core-format.js (#503), for a test that renders a
// view without the rest of core.js.
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./between");
module.exports = ctx => {
  const core = require("./core-source")();
  if (!ctx.esc) ctx.esc = x => String(x ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;");
  if (!ctx.Intl) ctx.Intl = Intl;
  vm.runInContext(between(core, "const fmtUp", "const upChip"), ctx);
};
