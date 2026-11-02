"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const between = require("./helpers/between");

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
  vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  const source = require("./helpers/core-source")();
  vm.runInContext(between(source, "function askDialog(", "/* The X and Escape"), ctx);
  const click = answer => body.children[0].handlers.click({ target: { closest: () => ({ dataset: { a: answer } }) } });

  let pending = ctx.window.ask("Delete it?", { title: "Delete", ok: "Delete", danger: true });
  assert.match(body.children[0].html, /btn danger/, "a question marked danger gets a danger button");
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

test("ask shows the title and the button label it is given", () => {
  const body = { children: [], appendChild(el) { this.children.push(el); } };
  const make = () => ({ attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, addEventListener() {}, focus() {},
    querySelector(sel) { return sel === ".askin" ? null : make(); }, set innerHTML(html) { this.html = html; }, get innerHTML() { return this.html; } });
  const ctx = { document: { createElement: make, body, addEventListener() {}, removeEventListener() {} }, window: {}, esc: s => String(s) };
  vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  const source = require("./helpers/core-source")();
  vm.runInContext(between(source, "function askDialog(", "/* The X and Escape"), ctx);
  ctx.window.ask("Delete backup \"nightly\"?", { title: "Delete backup", ok: "Delete backup", danger: true });
  const html = body.children[0].html;
  assert.match(html, /<h3>Delete backup<\/h3>/);
  assert.match(html, /class="btn danger" data-a="yes">Delete backup</);
  assert.doesNotMatch(html, /Continue/);
});

/* The whole ask(...) call starting at `from`, skipping over strings and
   template literals so a "(s)" inside the message doesn't end it early. */
function askCall(source, from) {
  let depth = 0;
  for (let i = from; i < source.length; i++) {
    const c = source[i];
    if (c === '"' || c === "'") { for (i++; source[i] !== c; i++) if (source[i] === "\\") i++; }
    else if (c === "`") {
      for (i++; source[i] !== "`"; i++) {
        if (source[i] === "\\") i++;
        else if (source[i] === "$" && source[i + 1] === "{") { let d = 0; for (; ; i++) { if (source[i] === "{") d++; else if (source[i] === "}" && --d === 0) break; } }
      }
    }
    else if (c === "(") depth++;
    else if (c === ")" && --depth === 0) return source.slice(from, i + 1);
  }
  return source.slice(from);
}

/* A storage prompt that deletes, removes, erases or disables something says
   so in its title and on its button ("Delete backup", "Erase disk"), and
   stays red - never an untitled box with a red Continue. */
test("storage prompts that delete or remove something have a title and a button naming the result", () => {
  const dir = path.join(__dirname, "..", "web", "js");
  const checked = [], missing = [];
  for (const name of [...require("./helpers/storage-source").names(), "views-protect.js", "longhorn-v2-setup.js"]) {
    const source = fs.readFileSync(path.join(dir, name), "utf8");
    for (const match of source.matchAll(/\bask\(\s*["'`](Delete|Remove|Erase|Disable)\b/g)) {
      const call = askCall(source, match.index + 3);
      const where = `${name}:${source.slice(0, match.index).split("\n").length}`;
      checked.push(where);
      if (!/\btitle:\s*["'`]/.test(call) || !/\bok:\s*["'`]/.test(call) || !/\bdanger:\s*true\b/.test(call)) missing.push(where);
    }
  }
  assert.equal(checked.length, 10, `expected the ten storage prompts, found ${checked.join(", ")}`);
  assert.deepEqual(missing, []);
});

/* Every confirmation says what it is and what its button does: a title and
   a button naming the result, never an untitled box with "Continue". */
test("every ask() has a title and a button that names the result", () => {
  const dir = path.join(__dirname, "..", "web", "js"), missing = [];
  let checked = 0;
  for (const name of fs.readdirSync(dir).filter(f => f.endsWith(".js"))) {
    const source = fs.readFileSync(path.join(dir, name), "utf8");
    for (const match of source.matchAll(/(^|[^.\w])ask\(/gm)) {
      const from = match.index + match[1].length;
      if (/function\s+$/.test(source.slice(Math.max(0, from - 10), from))) continue;
      const call = askCall(source, from + 3);
      if (!call.includes(",")) { if (/^\(\)/.test(call)) continue; }
      checked++;
      if (!/\btitle:/.test(call) || !/\bok:/.test(call)) missing.push(`${name}:${source.slice(0, from).split("\n").length}`);
    }
  }
  assert.ok(checked > 50, `found ${checked} ask() calls`);
  assert.deepEqual(missing, []);
});

test("a red button is the caller's choice, not a guess from the first word", () => {
  const body = { children: [], appendChild(el) { this.children.push(el); } };
  const make = () => ({ attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, addEventListener() {}, focus() {},
    querySelector(sel) { return sel === ".askin" ? null : make(); }, set innerHTML(html) { this.html = html; }, get innerHTML() { return this.html; } });
  const ctx = { document: { createElement: make, body, addEventListener() {}, removeEventListener() {} }, window: {}, esc: s => String(s) };
  vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  const source = require("./helpers/core-source")();
  vm.runInContext(between(source, "function askDialog(", "/* The X and Escape"), ctx);
  ctx.window.ask("Delete the draft?", { title: "Delete draft", ok: "Delete draft" });
  assert.match(body.children[0].html, /class="btn pri" data-a="yes">Delete draft</);
});

