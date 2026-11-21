/* Homestead: the details sidebar (design.md "Details sidebar"). A click on a
   row - an app, a VM, a volume, a share - opens everything about it at the
   right, over the list, which stays where it was: its main actions, then
   tabs (Overview first), each built from the page's own data. A phone shows it
   as a sheet from the bottom. A classic script, loaded after ui.js. */

// kind -> { head(key) -> { title, sub, iconHtml, cluster? }, actions(key) -> actionBar items, tabs: [{ id, label, html(key) }] }
// cluster: the linked cluster an item belongs to, so what is done from the panel goes to it (fleet.js).
const DETAIL_KINDS = {};
const DETAILS = { kind: "", key: "", tab: "", opener: null, sig: "" };

/* The panel's markup for one item and tab: "" when the kind is unknown or the
   item has nothing to show (it is gone). Pure, so tests read it directly. */
function detailsHtml(kind, key, tab) {
  const spec = DETAIL_KINDS[kind];
  if (!spec) return "";
  const head = spec.head(key);
  if (!head) return "";
  const tabs = (spec.tabs || []).filter(t => !t.when || t.when(key));
  const current = tabs.find(t => t.id === tab) || tabs[0];
  const actions = spec.actions ? spec.actions(key) || [] : [];
  return `<div class="drawerhd details-hd"><div class="details-title">${head.iconHtml || ""}<div><h3 id="detailsTitle">${esc(head.title)}</h3>`
    + `${head.sub ? `<div class="dim xs">${esc(head.sub)}</div>` : ""}</div></div>`
    + `<button type="button" class="iconbtn" aria-label="Close" onclick="closeDetails()">${icon("x")}</button></div>`
    + `<div class="details-body">`
    + (actions.length ? `<div class="details-actions">${actionBar(actions, { shown: 3 })}</div>` : "")
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
if (typeof module === "object" && module.exports) module.exports = {
  DETAIL_KINDS, DETAILS, detailsHtml, detailsElements, renderDetails, openDetails, closeDetails, detailsTab, refreshDetails,
};
