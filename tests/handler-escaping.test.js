"use strict";

/* A value put into an inline handler must reach the handler exactly as it
   was, whatever it holds: the browser decodes the attribute's entities first,
   then runs the handler as JavaScript. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const context = { window: {}, console, document: { addEventListener() {} } };
vm.createContext(context);
const core = fs.readFileSync("web/js/core.js", "utf8");
vm.runInContext(core.slice(0, core.indexOf("/* Put text on the clipboard.")) + "; this.esc = esc; this.jsq = jsq; this.jsArg = jsArg;", context);

// What a browser does to a double-quoted attribute value before running it.
const decode = s => s.replace(/&(amp|lt|gt|quot|#39);/g, (_, e) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" }[e]));

const HOSTILE = ["plain", "o'brien", 'say "hi"', "');alert(1);//", '");alert(1);//', "back\\slash\\", "</script><img src=x onerror=alert(1)>",
  "line\nbreak", "&#39;already&quot;", "${`template`}", " sep"];

test("a value in an inline handler arrives unchanged", () => {
  for (const value of HOSTILE) {
    const attribute = `fn(${context.jsq(value)})`;
    assert.ok(!attribute.includes('"'), `the attribute cannot end early: ${attribute}`);
    let got;
    new Function("fn", decode(attribute))(v => { got = v; });
    assert.equal(got, value);
  }
});

test("handler text for UI.button is escaped once, by UI.button", () => {
  for (const value of HOSTILE) {
    const attribute = context.esc(`fn(${context.jsArg(value)})`);
    let got;
    new Function("fn", decode(attribute))(v => { got = v; });
    assert.equal(got, value);
  }
});

test("no inline handler puts a value inside a quoted string by hand", () => {
  const files = fs.readdirSync("web/js").filter(f => f.endsWith(".js") && f !== "demo.js");
  const offenders = [];
  for (const file of files) {
    fs.readFileSync(`web/js/${file}`, "utf8").split("\n").forEach((line, n) => {
      // A value inside a quoted string in a handler: '...${value}...'.
      if ((/on[a-z]+="/.test(line) && /'[^'",()]*\$\{(?!jsq|jsArg)[^}]*\}[^'",()]*'/.test(line)) || /'\$\{esc\(/.test(line))
        offenders.push(`${file}:${n + 1}`);
    });
  }
  assert.deepEqual(offenders, [], "use jsq() (or jsArg() inside UI.button) for values in handlers");
});
