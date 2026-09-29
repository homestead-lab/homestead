"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");

/* A browser can silence confirm(), prompt() and alert() for a page, and an
   installed app window may not show them: the button then does nothing at
   all. Homestead asks with its own ask() and askText() instead. */
test("no page code uses the browser's own dialogs", () => {
  const dir = path.join(__dirname, "..", "web", "js");
  const found = [];
  for (const name of fs.readdirSync(dir).filter(f => f.endsWith(".js") && f !== "demo.js")) {
    fs.readFileSync(path.join(dir, name), "utf8").split("\n").forEach((line, i) => {
      if (/(^|[^\w.$])(confirm|prompt|alert)\(/.test(line) && !/^\s*(\/\/|\*)/.test(line)) found.push(`${name}:${i + 1}`);
    });
  }
  assert.deepEqual(found, []);
});

test("ask resolves to the answer, and askText to the text or null", async () => {
  const listeners = {}, body = { children: [], appendChild(el) { this.children.push(el); } };
  const make = () => {
    const el = { className: "", attrs: {}, children: {}, handlers: {}, value: "", placeholder: "", textContent: "",
      setAttribute(k, v) { this.attrs[k] = v; }, addEventListener(t, f) { this.handlers[t] = f; },
      remove() { body.children = body.children.filter(c => c !== this); }, focus() {},
      querySelector(sel) { return sel in this.children ? this.children[sel] : (this.children[sel] = make()); } };
    Object.defineProperty(el, "innerHTML", { set(html) { this.html = html; if (!html.includes("askin")) this.children[".askin"] = null; }, get() { return this.html; } });
    return el;
  };
  const document = { createElement: make, body, activeElement: null,
    addEventListener: (t, f) => { listeners[t] = f; }, removeEventListener: t => { delete listeners[t]; } };
  const ctx = { document, window: {}, esc: s => String(s) };
  vm.createContext(ctx);
  const source = fs.readFileSync(path.join(__dirname, "..", "web", "js", "core.js"), "utf8");
  const start = source.indexOf("const ASK_DANGER"), end = source.indexOf("/* The X and Escape");
  vm.runInContext(source.slice(start, end), ctx);
  const click = answer => body.children[0].handlers.click({ target: { closest: () => ({ dataset: { a: answer } }) } });

  let pending = ctx.window.ask("Delete it?");
  assert.match(body.children[0].html, /btn danger/, "a destructive question gets a danger button");
  click("yes");
  assert.equal(await pending, true);
  assert.equal(body.children.length, 0, "the dialog is gone once answered");

  pending = ctx.window.ask("Use this address?");
  listeners.keydown({ key: "Escape", preventDefault() {}, stopPropagation() {} });
  assert.equal(await pending, false, "Escape cancels");

  pending = ctx.window.askText("Label for 192.0.2.1", "old");
  body.children[0].querySelector(".askin").value = "new";
  click("yes");
  assert.equal(await pending, "new");
  pending = ctx.window.askText("Type its name");
  click("no");
  assert.equal(await pending, null);
});
