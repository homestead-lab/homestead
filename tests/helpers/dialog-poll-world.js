"use strict";
// A dialog that follows a job, loaded with the real ui.js (so the real UI.poll) and
// one view file, for the tests of the dialog loops moved onto UI.poll: a clock the
// test moves by hand, a tab that can be hidden, and a modal that keeps the elements
// of the last body drawn (by id) until another body replaces them, as the browser's
// #mbody does. close() is closeModal: the box is hidden, its elements stay, and the
// dialog's polls are stopped, after the hooks closeModal calls first (the snapshot
// browser's and the data move's). Every read is held until the test answers it, and a
// read cancelled through its signal rejects as fetch does.
// It is NOT a browser: the browser groups open these dialogs for real.
const fs = require("node:fs"), vm = require("node:vm");

function fakeClock() {
  let now = 0, seq = 0;
  const timers = new Map();
  const settle = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
  return {
    setTimeout: (fn, ms = 0) => { const id = ++seq; timers.set(id, { at: now + ms, fn }); return id; },
    clearTimeout: id => { timers.delete(id); },
    pending: () => timers.size,
    now: () => now,
    async advance(ms) {
      const end = now + ms;
      await settle();
      for (;;) {
        const due = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        timers.delete(due[0]); now = due[1].at; due[1].fn(); await settle();
      }
      now = end; await settle();
    },
    settle,
  };
}

function fakeDocument() {
  const listeners = new Map();
  return {
    visibilityState: "visible", hidden: false,
    addEventListener(type, fn) { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(fn); },
    removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
    getElementById: () => null,
    show(visible) { this.visibilityState = visible ? "visible" : "hidden"; this.hidden = !visible; for (const fn of [...(listeners.get("visibilitychange") || [])]) fn(); },
  };
}

module.exports = function dialogPollWorld(files, extra = {}) {
  const clock = fakeClock(), document = fakeDocument();
  const fields = {}, reads = [];
  let hidden = true, title = "";
  const register = body => {
    for (const [tag, id] of String(body).matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)) {
      let content = "";
      const dataset = Object.fromEntries([...tag.matchAll(/\sdata-([a-z-]+)="([^"]*)"/g)].map(([, k, v]) => [k.replace(/-([a-z])/g, (_, c) => c.toUpperCase()), v]));
      const el = { id, checked: false, disabled: /\sdisabled\b/.test(tag), value: "", dataset, outerHTML: tag, textContent: "",
        classList: { contains: () => false, add() {}, remove() {}, toggle() {} } };
      Object.defineProperty(el, "innerHTML", { get: () => content, set: value => { content = value; register(value); } });
      fields["#" + id] = el;
    }
  };
  const draw = (t, body) => { for (const key of Object.keys(fields)) delete fields[key]; title = t; hidden = false; register(body); };
  const answers = { reply: null };
  const ctx = {
    console, document, AbortController, AbortSignal, DOMException, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    setInterval: () => { throw new Error("setInterval: a repeating read belongs on UI.poll"); }, clearInterval() {},
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"),
    jsArg: s => JSON.stringify(String(s ?? "")), jsq: s => JSON.stringify(s).replaceAll('"', "&quot;"), icon: () => "",
    can: () => true, toast() {}, noteOperation() {}, applyRole() {}, actionBar: actions => actions.filter(Boolean).map(a => a.label).join(" "),
    MODAL_STACK: [], pushModal() { return true; }, jobLog() {}, jobLogs() {}, enhanceActions() {},
    $: sel => sel === "#modal" ? { classList: { contains: c => c === "hidden" && hidden } } : sel === "#mtitle" ? { textContent: title } : fields[sel],
    modal: draw, childModal: draw,
    // A read waits for the test: answer(value) / fail(error). A cancelled one rejects as fetch does.
    api: (path, opts = {}) => new Promise((resolve, reject) => {
      if (opts.method && opts.method !== "GET") { resolve(answers.reply ? answers.reply(path, opts) : {}); return; }
      const read = { path, signal: opts.signal, answer: resolve, fail: reject, at: clock.now() };
      reads.push(read);
      opts.signal?.addEventListener?.("abort", () => reject(opts.signal.reason), { once: true });
      if (answers.auto) Promise.resolve().then(() => answers.auto(read));
    }),
    ...extra,
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  for (const file of [].concat(files)) vm.runInContext(fs.readFileSync(`web/js/${file}`, "utf8"), ctx);
  return {
    ctx, clock, document, fields, reads, answers,
    run: code => vm.runInContext(code, ctx),
    // closeModal(): the box hides, its body stays, and the dialog's polls stop (#983).
    close() { ctx.window.snapshotFilesDismiss?.(); ctx.window.selfDataClose?.(); hidden = true; ctx.UI.stopPolls(); },
    get open() { return !hidden; },
    get title() { return title; },
  };
};
module.exports.fakeClock = fakeClock;
