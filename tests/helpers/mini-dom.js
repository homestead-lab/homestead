"use strict";
// A very small DOM for tests of morph(), syncAttrs() and loadAppIcons() (#906): nodes, attributes,
// childNodes, the tree edits morph makes, a document.activeElement that is lost when the focused node
// is moved (as a browser blurs it), and a selector for "tag.class[attr]" with descendant spaces.
// It is NOT a browser: the browser groups check the real thing.
class Node_ {
  constructor(type, name) { this.nodeType = type; this.nodeName = name; this.parentNode = null; }
  get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return n === DOC.root; }
  remove() { this.parentNode?.removeChild(this); }
}
class Text_ extends Node_ {
  constructor(value) { super(3, "#text"); this.nodeValue = value; }
  cloneNode() { return new Text_(this.nodeValue); }
  isEqualNode(o) { return o.nodeType === 3 && o.nodeValue === this.nodeValue; }
  replaceWith(n) { const p = this.parentNode; p.insertBefore(n, this); p.removeChild(this); }
}
class Element_ extends Node_ {
  constructor(name, attrs = {}, kids = []) {
    super(1, name.toUpperCase()); this.attrs = new Map(Object.entries(attrs)); this.childNodes = []; this.dataset = new Proxy({}, {
      get: (_, key) => this.attrs.get("data-" + kebab(key)), set: (_, key, v) => { this.attrs.set("data-" + kebab(key), String(v)); return true; },
      deleteProperty: (_, key) => { this.attrs.delete("data-" + kebab(key)); return true; } });
    for (const k of kids) this.appendChild(typeof k === "string" ? new Text_(k) : k);
    this.listeners = {};
  }
  get attributes() { return [...this.attrs].map(([name, value]) => ({ name, value })); }
  get classList() { const el = this; return { contains: c => (el.attrs.get("class") || "").split(/\s+/).includes(c) }; }
  getAttribute(n) { return this.attrs.has(n) ? this.attrs.get(n) : null; }
  setAttribute(n, v) { this.attrs.set(n, String(v)); }
  hasAttribute(n) { return this.attrs.has(n); }
  removeAttribute(n) { this.attrs.delete(n); }
  get hidden() { return this.attrs.has("hidden"); }
  set hidden(v) { if (v) this.attrs.set("hidden", ""); else this.attrs.delete("hidden"); }
  get src() { return this.attrs.get("src") || ""; }
  set src(v) { this.attrs.set("src", v); }
  get id() { return this.attrs.get("id") || ""; }
  get previousElementSibling() { const kids = this.parentNode?.childNodes.filter(n => n.nodeType === 1) || []; return kids[kids.indexOf(this) - 1] || null; }
  get nextElementSibling() { const kids = this.parentNode?.childNodes.filter(n => n.nodeType === 1) || []; return kids[kids.indexOf(this) + 1] || null; }
  get firstChild() { return this.childNodes[0] || null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type) { for (const fn of this.listeners[type] || []) fn(); }
  focus() { DOC.activeElement = this; }
  appendChild(n) { return this.insertBefore(n, null); }
  insertBefore(n, ref) {
    if (n.parentNode) n.parentNode.removeChild(n);
    const at = ref ? this.childNodes.indexOf(ref) : this.childNodes.length;
    this.childNodes.splice(at, 0, n); n.parentNode = this; return n;
  }
  removeChild(n) {
    const at = this.childNodes.indexOf(n); if (at < 0) return n;
    this.childNodes.splice(at, 1); n.parentNode = null;
    // A browser blurs a focused node that leaves the document, and one that is moved.
    if (DOC.activeElement && (DOC.activeElement === n || n.contains?.(DOC.activeElement))) DOC.activeElement = null;
    return n;
  }
  contains(o) { for (let n = o; n; n = n.parentNode) if (n === this) return true; return false; }
  replaceWith(n) { const p = this.parentNode; p.insertBefore(n, this); p.removeChild(this); }
  cloneNode(deep) { const c = new Element_(this.nodeName, Object.fromEntries(this.attrs)); if (deep) for (const k of this.childNodes) c.appendChild(k.cloneNode(true)); return c; }
  isEqualNode(o) {
    if (o.nodeType !== 1 || o.nodeName !== this.nodeName || o.attrs.size !== this.attrs.size || o.childNodes.length !== this.childNodes.length) return false;
    for (const [k, v] of this.attrs) if (o.attrs.get(k) !== v) return false;
    return this.childNodes.every((k, i) => k.isEqualNode(o.childNodes[i]));
  }
  closest(sel) { for (let n = this; n && n.nodeType === 1; n = n.parentNode) if (match(n, sel)) return n; return null; }
  querySelectorAll(sel) {
    const parts = sel.trim().split(/\s+/), out = [];
    const walk = (node, ancestors) => { for (const k of node.childNodes) if (k.nodeType === 1) {
      if (match(k, parts[parts.length - 1]) && chain(k, parts)) out.push(k);
      walk(k); } };
    walk(this); return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  set innerHTML(html) { throw new Error("mini-dom: build trees with el()"); }
}
const kebab = s => String(s).replace(/[A-Z]/g, c => "-" + c.toLowerCase());
function match(node, simple) {
  const m = /^([a-z0-9]*)((?:\.[\w-]+)*)((?:\[[\w-]+(?:="[^"]*")?\])*)$/i.exec(simple); if (!m) throw new Error("mini-dom selector: " + simple);
  if (m[1] && node.nodeName !== m[1].toUpperCase()) return false;
  for (const c of m[2].split(".").filter(Boolean)) if (!node.classList.contains(c)) return false;
  for (const a of [...m[3].matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)]) if (!node.hasAttribute(a[1]) || (a[2] !== undefined && node.getAttribute(a[1]) !== a[2])) return false;
  return true;
}
function chain(node, parts) {
  let i = parts.length - 2, n = node.parentNode;
  while (i >= 0 && n) { if (n.nodeType === 1 && match(n, parts[i])) i--; n = n.parentNode; }
  return i < 0;
}
const DOC = { activeElement: null, root: null };
const el = (name, attrs, ...kids) => new Element_(name, attrs, kids.flat());
const text = v => new Text_(v);
function mount(root) { DOC.root = root; return root; }
module.exports = { el, text, mount, DOC, Element_ };
