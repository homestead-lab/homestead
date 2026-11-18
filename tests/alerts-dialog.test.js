"use strict";

/* The Alerts dialog: grouped by severity, critical first, newest first within
   each group, and a row is its title, its detail and its buttons, with no
   severity chip of its own (the group's heading names it). */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

const pwa = fs.readFileSync("web/js/pwa.js", "utf8");

function dialog(active) {
  const ctx = { window: {}, console, jsArg: s => JSON.stringify(String(s ?? "")),
    esc: value => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;") };
  ctx.jsq = s => ctx.jsArg(s);
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  ctx.UI = ctx.window.UI || ctx.UI;
  vm.runInContext(`${between(pwa, "const ALERT_LEVELS", "\n}\n", { through: true })}; this.pwaAlertsHtml = pwaAlertsHtml;`, ctx);
  return ctx.pwaAlertsHtml({ active });
}

const alert = (severity, title, since, extra = {}) => ({ key: title, version: 1, severity, title, body: `${title} detail`, since, href: "/", ...extra });

test("alerts are grouped critical, warnings, information, newest first in each", () => {
  const html = dialog([
    alert("degraded", "Older warning", 100), alert("info", "Some information", 500),
    alert("critical", "Older critical", 200), alert("degraded", "Newer warning", 300),
    alert("critical", "Newer critical", 400)]);
  const order = ["Critical · 2", "Newer critical", "Older critical", "Warnings · 2", "Newer warning", "Older warning",
    "Information · 1", "Some information"].map(text => html.indexOf(text));
  assert.ok(order.every(at => at >= 0), "every heading and title is shown");
  assert.deepEqual(order, [...order].sort((a, b) => a - b));
  assert.match(html, /Critical first, then warnings and information, newest first in each\./);
});

test("a row carries no severity chip of its own, and acknowledged alerts wait under their own heading", () => {
  const html = dialog([alert("critical", "Volume is faulted", 10), alert("degraded", "Seen already", 5, { acknowledged: true })]);
  assert.doesNotMatch(html, /class="ui-chip[^"]*">(Critical|Warning|Information)</);
  assert.match(html, /Acknowledged · 1/);
  assert.match(html, />Acknowledge</);
  assert.match(html, />Undo</);
});
