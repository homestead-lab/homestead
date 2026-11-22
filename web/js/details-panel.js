/* Homestead: the details sidebar (design.md "Details sidebar"). A click on a
   row - an app, a VM, a volume, a share - opens everything about it at the
   right, over the list, which stays where it was: its main actions, then
   tabs (Overview first), each built from the page's own data. A phone shows it
   as a sheet from the bottom. A classic script, loaded after ui.js. */

// kind -> { head(key) -> { title, sub, iconHtml, cluster? }, actions(key) -> actionBar items, shown?, tabs: [{ id, label, html(key) }] }
// cluster: the linked cluster an item belongs to, so what is done from the panel goes to it (fleet.js).
// shown: how many actions are buttons (3 when not given); the rest are in ⋯.
const DETAIL_KINDS = {};
const DETAILS = { kind: "", key: "", tab: "", opener: null, sig: "", building: "" };

/* What a tab reads from the server - snapshots, logs, activity - read when
   it is first shown and kept for DETAILS_FRESH_MS, then read again when the
   page next refreshes. Keyed by the item, so another item reads its own. */
const DETAILS_FRESH_MS = 30000;
const DETAILS_DATA = new Map();      // "kind|key|name" -> { data, error, at, busy }
const DETAILS_WANTED = new Map();    // "kind|key|name" -> read()

/* The data a tab named `name` reads with read() (a promise): { data } once
   read, { error } when it failed, { loading: true } until then. Asking for it
   is what starts the read, after the panel is drawn. */
function detailsData(name, read) {
  const id = `${DETAILS.building}|${name}`, got = DETAILS_DATA.get(id);
  if (!got || (!got.busy && Date.now() - got.at > DETAILS_FRESH_MS)) DETAILS_WANTED.set(id, read);
  // Read again in the background, it keeps showing what it read before.
  return got && (got.data !== undefined || got.error) ? got : { loading: true };
}

/* A part of a tab drawn from detailsData: a line while it is read, a notice
   when it could not be, otherwise render(data). */
function detailsSlot(state, render, { reading = "Reading…", failed = "This could not be read" } = {}) {
  if (state.loading) return `<div class="details-reading dim small"><span class="spin2"></span>${esc(reading)}</div>`;
  if (state.error) return UI.callout("warn", failed, esc(state.error));
  return render(state.data);
}

/* Reads the open item's `name` again on its next draw, after a change. */
function detailsForget(name) {
  for (const id of DETAILS_DATA.keys()) if (id.endsWith(`|${name}`)) DETAILS_DATA.delete(id);
  if (DETAILS.kind) { DETAILS.sig = ""; renderDetails(); }
}

/* The cluster's recent events, for an Activity tab, and Kubernetes' reason
   in words: ProvisioningFailed -> Provisioning failed. */
const clusterEventsRead = () => api("/api/events", { keep: true });
const eventWords = reason => { const s = String(reason || "Event").replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase(); return s[0].toUpperCase() + s.slice(1); };

function detailsReadWanted() {
  const wanted = [...DETAILS_WANTED];
  DETAILS_WANTED.clear();
  for (const [id, read] of wanted) {
    const before = DETAILS_DATA.get(id);
    DETAILS_DATA.set(id, { ...(before || {}), busy: true, at: before?.at || 0 });
    Promise.resolve().then(read)
      .then(data => DETAILS_DATA.set(id, { data, at: Date.now() }),
        e => DETAILS_DATA.set(id, { error: String(e?.message || e || "It could not be read."), at: Date.now() }))
      .then(() => { if (id.startsWith(`${DETAILS.kind}|${DETAILS.key}|`)) { DETAILS.sig = ""; renderDetails(); } });
  }
}

/* The panel's markup for one item and tab: "" when the kind is unknown or the
   item has nothing to show (it is gone). Pure, so tests read it directly. */
function detailsHtml(kind, key, tab) {
  const spec = DETAIL_KINDS[kind];
  if (!spec) return "";
  const head = spec.head(key);
  if (!head) return "";
  DETAILS.building = `${kind}|${key}`;
  const tabs = (spec.tabs || []).filter(t => !t.when || t.when(key));
  const current = tabs.find(t => t.id === tab) || tabs[0];
  // Words alone on the buttons, the first one the main action (design.md "Details sidebar").
  const actions = (spec.actions ? spec.actions(key) || [] : []).filter(Boolean).map((item, i) => ({ ...item, plain: true, pri: i === 0 }));
  return `<div class="drawerhd details-hd"><div class="details-title">${head.iconHtml || ""}<div><h3 id="detailsTitle">${esc(head.title)}</h3>`
    + `${head.sub ? `<div class="dim xs">${esc(head.sub)}</div>` : ""}</div></div>`
    + `<button type="button" class="iconbtn" aria-label="Close" onclick="closeDetails()">${icon("x")}</button></div>`
    + `<div class="details-body">`
    + (actions.length ? `<div class="details-actions">${actionBar(actions, { shown: spec.shown || 3, label: `More actions for ${head.title}` })}</div>` : "")
    + (tabs.length > 1 ? `<nav class="seg pagetabs details-tabs" aria-label="Sections">${tabs.map(t =>
      `<button type="button" class="${t === current ? "on" : ""}"${t === current ? ' aria-current="true"' : ""} onclick="detailsTab(${jsq(t.id)})">${esc(t.label)}</button>`).join("")}</nav>` : "")
    + `<div class="details-tab" data-tab="${esc(current?.id || "")}">${current ? current.html(key) : ""}</div></div>`;
}

function detailsElements() {
  let panel = document.getElementById("detailsPanel"), scrim = document.getElementById("detailsScrim");
  if (!panel) {
    scrim = document.createElement("div");
    scrim.id = "detailsScrim";
    scrim.className = "details-scrim";
    scrim.hidden = true;
    scrim.addEventListener("click", () => closeDetails());
    panel = document.createElement("aside");
    panel.id = "detailsPanel";
    panel.className = "drawer details-panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-labelledby", "detailsTitle");
    document.body.append(scrim, panel);
  }
  return { panel, scrim };
}

/* Draws the open item again, keeping its tab and where it was scrolled to.
   Nothing is redrawn when nothing changed, so a page refresh leaves it alone.
   An item that is gone closes the panel. */
function renderDetails() {
  if (!DETAILS.kind) return;
  const html = detailsHtml(DETAILS.kind, DETAILS.key, DETAILS.tab);
  if (!html) { closeDetails(); return; }
  if (html === DETAILS.sig) return;
  DETAILS.sig = html;
  const { panel } = detailsElements();
  const body = panel.querySelector(".details-body"), scroll = body ? body.scrollTop : 0;
  const cluster = DETAIL_KINDS[DETAILS.kind].head(DETAILS.key)?.cluster || "";
  if (cluster) panel.dataset.cluster = cluster; else delete panel.dataset.cluster;
  panel.innerHTML = html;
  if (typeof enhanceActions === "function") enhanceActions(panel);
  const next = panel.querySelector(".details-body");
  if (next) next.scrollTop = scroll;
  detailsReadWanted();
}

function openDetails(kind, key, tab = "") {
  if (!DETAIL_KINDS[kind]) return false;
  if (typeof hideCard === "function") hideCard();
  const reopening = DETAILS.kind === kind && DETAILS.key === key;
  if (!DETAILS.kind) DETAILS.opener = document.activeElement;
  Object.assign(DETAILS, { kind, key, tab: tab || (reopening ? DETAILS.tab : ""), sig: "" });
  const { panel, scrim } = detailsElements();
  renderDetails();
  if (!DETAILS.kind) return false;
  scrim.hidden = false;
  panel.classList.add("open");
  document.body.classList.add("details-open");
  panel.querySelector("#detailsTitle")?.setAttribute("tabindex", "-1");
  panel.querySelector("#detailsTitle")?.focus({ preventScroll: true });
  return true;
}

function closeDetails() {
  if (!DETAILS.kind) return;
  const { panel, scrim } = detailsElements();
  panel.classList.remove("open");
  scrim.hidden = true;
  document.body.classList.remove("details-open");
  const opener = DETAILS.opener;
  Object.assign(DETAILS, { kind: "", key: "", tab: "", opener: null, sig: "" });
  DETAILS_WANTED.clear();
  if (opener && opener.isConnected) opener.focus({ preventScroll: true });
}

function detailsTab(id) {
  DETAILS.tab = id;
  DETAILS.sig = "";
  renderDetails();
}

/* A page paints its data again: the open panel shows the same item with it. */
function refreshDetails() { if (DETAILS.kind) renderDetails(); }

document.addEventListener("keydown", e => {
  if (e.key === "Escape" && DETAILS.kind && !document.querySelector("#modal:not(.hidden)")) closeDetails();
});

window.DETAIL_KINDS = DETAIL_KINDS;
window.openDetails = openDetails;
window.closeDetails = closeDetails;
window.detailsTab = detailsTab;
window.refreshDetails = refreshDetails;
window.detailsForget = detailsForget;
if (typeof module === "object" && module.exports) module.exports = {
  DETAIL_KINDS, DETAILS, DETAILS_DATA, DETAILS_WANTED, DETAILS_FRESH_MS, detailsData, detailsSlot, detailsForget, detailsReadWanted, clusterEventsRead, eventWords, detailsHtml, detailsElements, renderDetails, openDetails, closeDetails, detailsTab, refreshDetails,
};
