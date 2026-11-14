/* Homestead core: no-flash rendering: placeholders, paint() and morph(). A classic script, loaded right after core.js (#911). */
/* ---------------- no-flash rendering ----------------
   Re-rendering innerHTML on every poll is what makes the page flash and lose
   scroll/hover. paint() only touches nodes whose content actually changed. */
/* A page opens at once. One seen before shows what it showed last time until
   its data arrives, and is then brought up to date in place; one not seen yet
   shows its shape - panels with a passing glint - rather than a spinner. */
const PAGE_SNAPSHOT = new Map();
/* Only the most recent 20 are kept (#987): each host or app page is an
   address of its own, and a tab left open for days visits many. */
const PAGE_SNAPSHOT_KEEP = 20;
function keepSnapshot(key, html) {
  PAGE_SNAPSHOT.delete(key);
  PAGE_SNAPSHOT.set(key, html);
  while (PAGE_SNAPSHOT.size > PAGE_SNAPSHOT_KEEP) PAGE_SNAPSHOT.delete(PAGE_SNAPSHOT.keys().next().value);
}
/* Kept by address, not by page: the Nodes list and one node's page are the
   same page, and the list opened showing the node just left - for as long
   as the list took to load, which on a real cluster is seconds. */
const snapshotKey = view => `${view}${window.location.search || ""}`;
function pagePlaceholder(view) {
  const host = V();
  const snapshot = PAGE_SNAPSHOT.get(snapshotKey(view));
  if (snapshot) {
    host.innerHTML = snapshot;
    delete host.dataset.hash;   // what is shown is the snapshot, not a paint to compare with
    enhanceActions(host);
    loadAppIcons(host);
    host.dataset.painted = "1";
    host.classList.add("refreshing");
    if (window.applyRole) window.applyRole();
    return;
  }
  delete host.dataset.hash;
  host.innerHTML = pageSkeleton(view);
}
window.pagePlaceholder = pagePlaceholder;

function pageSkeleton(view) {
  const bar = (width, height = 12) => `<span class="skel" style="width:${width};height:${height}px"></span>`;
  const repeat = (count, fn) => Array.from({ length: count }, (_, i) => fn(i)).join("");
  const head = `${UI.pageHeader(``, ``, `${bar("84px", 32)}${bar("118px", 32)}`, {actionsClass:`skel-actions`, extraHtml:`${bar("min(280px,60vw)")}`})}`;
  const cards = (count, height) => `<div class="skel-grid">${repeat(count, () => `<div class="skel skel-card" style="height:${height}px"></div>`)}</div>`;
  const table = count => `<div class="card flat pad0 skel-table">${repeat(count, i => `<div class="skel-row">
      ${bar("30px", 30)}${bar(`${18 + (i * 7) % 12}%`)}${bar("9%")}${bar(`${20 + (i * 5) % 16}%`)}${bar("7%")}</div>`)}</div>`;
  const tiles = `<div class="skel-tiles">${repeat(4, () => '<div class="skel skel-card" style="height:96px"></div>')}</div>`;
  const body = {
    dash: tiles + cards(2, 260), cluster: tiles + cards(4, 170), flow: cards(1, 440),
    nodes: viewLayout("nodes") === "rows" ? table(4) : cards(3, 290),
    workloads: viewLayout("containers") === "rows" ? table(8) : cards(6, 230),
    vms: cards(3, 200), store: cards(9, 150), deploy: cards(2, 320), settings: cards(4, 240),
  }[view] || table(8);
  return `<div class="skeleton" aria-busy="true" aria-label="Loading">${head}${body}</div>`;
}

/* A search narrows these pages. On a phone the search box folds away behind
   its icon, and a list filtered by a search no longer on screen looked like
   things had gone missing - so the page itself says what it is showing. */
function filterBar() {
  const filterable = typeof FILTERABLE_VIEWS !== "undefined" && FILTERABLE_VIEWS.has(STATE.view);
  const q = filterable ? String(STATE.q || "") : "";
  $("#searchbtn")?.classList.toggle("filtering", !!q);
  return q ? `<div class="filterbar"><span>Showing matches for <b>“${esc(q)}”</b></span>
    <button class="btn sm" onclick="clearSearch()">Clear</button></div>` : "";
}
window.clearSearch = () => {
  const input = $("#globalSearch");
  if (input) { input.value = ""; input.dispatchEvent(new Event("input")); }
  document.body.classList.remove("searching");
};

/* A page folded into another (Images into Containers, Events into Cluster)
   shows its family's tabs, in the standard .seg, at the start of its header:
   the header's actions sit beside them, not a row below. */
function pageTabs(view) {
  const tabs = window.HomesteadRouter ? HomesteadRouter.tabsFor(view) : [];
  return tabs.length ? `<nav class="seg pagetabs" aria-label="Pages here">${tabs.map(([id, label]) =>
    `<a href="${esc(HomesteadRouter.urlFor(id))}" class="${id === view ? "on" : ""}"${id === view ? ' aria-current="page"' : ""} onclick="event.preventDefault();go(${jsq(id)})">${esc(label)}</a>`).join("")}</nav>` : "";
}
function paint(html) {
  const host = V();
  const watcher = paintWatcher(host);
  // Changes since the last paint that the watcher has not been told of yet.
  if (watcher && watcher.takeRecords().length) delete host.dataset.hash;
  const tabs = pageTabs(STATE.view);
  if (tabs) html = html.replace(/<div class="phead">\s*<div>/, match => `${match}${tabs}`);
  html = filterBar() + html;
  keepSnapshot(snapshotKey(STATE.view), html);
  host.classList.remove("refreshing");
  // The same data as the last paint of this page is nothing to do: no element built,
  // nothing compared (#906). Not once a failed /api/icons/ icon on it is due its retry:
  // the paint is where loadAppIcons asks again. A different viewer is a different paint.
  const sig = paintSignature(html);
  if (host.dataset.painted && host.dataset.hash === sig && !iconRetryDue(html)) { watcher?.takeRecords(); return; }
  host.dataset.hash = sig;
  if (!host.dataset.painted) {
    host.innerHTML = html;
    enhanceActions(host);
    loadAppIcons(host);
    host.dataset.painted = "1";
    host.dataset.sig = html.length + ":" + STATE.view;
    if (window.applyRole) window.applyRole();
    dropOrphanTooltip();
    watcher?.takeRecords();
    return;
  }
  const next = document.createElement("div");
  next.innerHTML = html;
  enhanceActions(next);
  // A part morph left alone on purpose (an open "…" menu, a field being typed in) is
  // still behind this markup: forget the hash, so the next paint morphs and catches it up.
  // Focus a moved row lost is given back after paint's own records are dropped: a focus handler
  // that changes the page is then a change outside paint(), which the watcher sees.
  // Focus comes back even when the paint fails part way, and only while nothing else has taken it.
  const report = { focusLater: true };
  try {
    morph(host, next, report);
    if (report.skipped) delete host.dataset.hash;
    loadAppIcons(host);
    if (window.applyRole) window.applyRole();
    dropOrphanTooltip();
    watcher?.takeRecords();
  } finally {
    const focused = report.refocus, now = document.activeElement;
    if (focused && (!now || now === document.body) && host.contains(focused)) focused.focus({ preventScroll: true });
  }
}
/* The paint skip above compares with the last paint, so content that changes between paints - a
   sort that moves rows itself (sortRows), a menu lifted out, an icon taken away - must make the next
   paint morph again: a watcher forgets the hash when nodes or text change outside paint(). paint()
   drops the records of its own changes. Attributes are not watched: code that marks the page after
   a paint (linkFieldLabels' data-wraps, a tabindex) would otherwise undo the skip on every refresh,
   and a morph would only strip what it then adds back. */
function paintWatcher(host) {
  if (typeof MutationObserver === "undefined") return null;
  if (!host.paintWatcher) {
    host.paintWatcher = new MutationObserver(records => { if (records.length) delete host.dataset.hash; });
    host.paintWatcher.observe(host, { childList: true, subtree: true, characterData: true });
  }
  return host.paintWatcher;
}
/* A cheap signature of what a paint would show: length, a 32-bit FNV-1a hash of the markup, and who is
   looking (a viewer and an admin see the same markup with different buttons applied by applyRole).
   Two different markups of the same length can, very rarely (about 1 in 4 billion), share a hash: that
   paint is then skipped, and the page catches up at its next change. */
function paintSignature(html) {
  let h = 0x811c9dc5;
  for (let i = 0; i < html.length; i++) { h ^= html.charCodeAt(i); h = Math.imul(h, 0x01000193); }
  const who = (typeof ROLE !== "undefined" ? ROLE : "") + "|" + (typeof ME !== "undefined" ? ME : "");
  return `${html.length}:${(h >>> 0).toString(36)}:${who}`;
}
/* Only Homestead's own icons (ICON_FAILED, core.js) count: an external icon that failed is the
   browser's to retry, and counting every .appav without an img kept such a page painting every tick. */
function iconRetryDue(html) {
  if (typeof ICON_FAILED === "undefined" || !ICON_FAILED.size) return false;
  const now = Date.now();
  for (const [url, at] of ICON_FAILED) if (now - at >= ICON_RETRY_MS && html.includes(url)) return true;
  return false;
}
/* data-mkey (not data-key, which other code uses) pairs a row by what it is, not where it is, so a
   list that re-sorts, gains or loses a row keeps each row's node, focus and state (#906). Children
   without one are paired by position as before. */
const mkeyOf = node => node.nodeType === 1 ? node.getAttribute("data-mkey") : null;
/* report.skipped is set when a subtree is left behind on purpose (paint() uses it). With
   report.focusLater, the node to give focus back to is left in report.refocus for the caller. */
function morphNode(x, y, report = {}) {
  if (x.nodeType !== y.nodeType || x.nodeName !== y.nodeName) { const copy = y.cloneNode(true); x.replaceWith(copy); return copy; }
  if (x.nodeType === 3) { if (x.nodeValue !== y.nodeValue) x.nodeValue = y.nodeValue; return x; }
  if (x.nodeType !== 1) return x;
  // never stomp on a field the user is typing in
  if (document.activeElement === x && /^(INPUT|SELECT|TEXTAREA)$/.test(x.nodeName)) {
    if (!x.isEqualNode(y)) report.skipped = true;
    return x;
  }
  syncAttrs(x, y);
  if (x.isEqualNode(y)) return x;
  // An open "…" menu lives on the page while it is open (ui.js), so its
  // own <details> looks empty: filling it back in would show the menu
  // twice. It is refreshed when it closes, with the next repaint.
  if (x.shell) { report.skipped = true; return x; }
  morph(x, y, report);
  return x;
}
function morphKeyed(a, b, report = {}) {
  const an = [...a.childNodes], bn = [...b.childNodes];
  const byKey = new Map(), plain = [];
  // Only keyless old children are paired by position: a second child with a key already seen is
  // dropped, never handed to a keyless new child.
  for (const x of an) { const key = mkeyOf(x); if (key === null) plain.push(x); else if (!byKey.has(key)) byKey.set(key, x); }
  const result = [];
  let next = 0;
  for (const y of bn) {
    const key = mkeyOf(y);
    let x = null;
    if (key !== null && byKey.has(key)) { x = byKey.get(key); byKey.delete(key); }
    else if (key === null && next < plain.length) x = plain[next++];
    result.push(x ? morphNode(x, y, report) : y.cloneNode(true));
  }
  const keep = new Set(result);
  for (const x of an) if (!keep.has(x)) x.remove();
  const focused = document.activeElement;
  let moved = false;
  result.forEach((node, i) => {
    if (a.childNodes[i] !== node) { a.insertBefore(node, a.childNodes[i] || null); moved = true; }
  });
  // A browser blurs a node that is moved: give focus back to the one the reader was on.
  if (moved && focused && focused !== document.activeElement && a.contains(focused)) {
    if (report.focusLater) report.refocus = focused;
    else focused.focus({ preventScroll: true });
  }
}
function morph(a, b, report = {}) {
  // fast path: identical markup, nothing to do
  if (a.isEqualNode(b)) return;
  const an = [...a.childNodes], bn = [...b.childNodes];
  if (an.some(node => mkeyOf(node) !== null) || bn.some(node => mkeyOf(node) !== null)) return morphKeyed(a, b, report);
  for (let i = 0; i < Math.max(an.length, bn.length); i++) {
    const x = an[i], y = bn[i];
    if (!y) { x && x.remove(); continue; }
    if (!x) { a.appendChild(y.cloneNode(true)); continue; }
    morphNode(x, y, report);
  }
}
function syncAttrs(x, y) {
  if (x.nodeName.toLowerCase() === "svg" && x.classList.contains("spark")) {
    x.sparkReset = x.getAttribute("data-spark-key") !== y.getAttribute("data-spark-key");
    x.sparkWindow = { before: JSON.parse(x.getAttribute("data-spark-times") || "[]"),
      after: JSON.parse(y.getAttribute("data-spark-times") || "[]") };
  }
  // An app icon that has loaded keeps its picture and its hidden initials through a repaint:
  // the new markup carries neither, and stripping them made every tick set every icon again.
  const iconState = x.nodeName === "IMG" && x.hasAttribute("data-icon-src");
  if (iconState && y.getAttribute("data-icon-src") !== x.getAttribute("data-icon-src")) {
    for (const name of ["src", "data-icon-wired", "data-icon-loading"]) x.removeAttribute(name);
    if (x.previousElementSibling?.classList.contains("avfallback")) x.previousElementSibling.hidden = false;
  }
  const initials = x.classList.contains("avfallback") && x.nextElementSibling?.hasAttribute("data-icon-src") && x.nextElementSibling.hasAttribute("src");
  // Whether a <details> is open belongs to the reader, the same way the text in
  // an input does: a background refresh must not fold up a pod tree mid-read.
  const readerOwnsOpen = x.nodeName === "DETAILS";
  for (const at of [...y.attributes]) if (!(readerOwnsOpen && at.name === "open") &&
      x.getAttribute(at.name) !== at.value) {
    if (at.name === "d" && x.closest(".spark") &&
        (x.classList.contains("ln") || x.classList.contains("fl"))) {
      updateSparkPath(x, at.value);
    } else x.setAttribute(at.name, at.value);
  }
  for (const at of [...x.attributes]) {
    if (readerOwnsOpen && at.name === "open") continue;
    if (iconState && (at.name === "src" || at.name === "data-icon-wired" || at.name === "data-icon-loading")) continue;
    if (initials && at.name === "hidden") continue;
    if (!y.hasAttribute(at.name)) x.removeAttribute(at.name);
  }
}
function resetPaint() { const h = V(); delete h.dataset.painted; delete h.dataset.hash; h.classList.remove("refreshing"); h.innerHTML = ""; dropOrphanTooltip(); }

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  PAGE_SNAPSHOT, PAGE_SNAPSHOT_KEEP, keepSnapshot, snapshotKey, pagePlaceholder, pageSkeleton, filterBar, pageTabs,
  paint, paintWatcher, paintSignature, iconRetryDue, mkeyOf, morphNode, morphKeyed, morph, syncAttrs,
  resetPaint
};
