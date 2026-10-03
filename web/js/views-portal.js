/* Portal - every web interface worth opening, as tiles.

   Apps in the cluster and the devices around it (router, switches, access
   points, NAS) each have a management page. The portal keeps them in
   sections with their icons and a dot for whether each answers right now.
   Links are edited here or from Settings; containers can be picked rather
   than typed. */

const PORTAL_ICON_LABELS = { router: "Router", switch: "Switch", wifi: "Access point", firewall: "Firewall",
  nas: "NAS", server: "Server", printer: "Printer", camera: "Camera", ups: "UPS", globe: "Website" };

function portalIcon(link, size = "") {
  const shown = link.shown || {};
  if (shown.kind === "builtin") return `<span class="portal-icon ${size}"><svg><use href="#d-${esc(shown.src)}"/></svg></span>`;
  if (shown.kind === "image" && shown.src) return `<span class="portal-icon img ${size}"><img src="${esc(shown.src)}" alt=""></span>`;
  const letters = link.title.split(/\s+/).map(word => word[0]).join("").slice(0, 2).toUpperCase();
  return `<span class="portal-icon letter ${size}">${esc(letters || "?")}</span>`;
}

function portalHost(url) {
  try { const u = new URL(url); return u.host + (u.pathname !== "/" ? u.pathname : ""); } catch (e) { return url; }
}

function portalSections(links) {
  const order = [], by = {};
  links.forEach(link => {
    const key = link.section || "";
    if (!by[key]) { by[key] = []; order.push(key); }
    by[key].push(link);
  });
  // Unsectioned links first, then sections in the order they first appear.
  order.sort((a, b) => (a === "") - (b === "") ? (a === "" ? -1 : 1) : 0);
  return order.map(key => [key, by[key]]);
}

async function viewPortal() {
  const data = await api("/api/portal");
  STATE.data.portal = data;
  renderPortal();
  api("/api/portal/status").then(status => { STATE.data.portalStatus = status; portalDots(); }).catch(() => {});
}

function renderPortal() {
  const links = (STATE.data.portal || {}).links || [];
  const q = STATE.q.toLowerCase();
  const rows = links.filter(link => !q || link.title.toLowerCase().includes(q) || link.url.toLowerCase().includes(q) ||
    (link.section || "").toLowerCase().includes(q) || (link.note || "").toLowerCase().includes(q));
  paint(`<div class="phead"><div><h2>Portal</h2><p>${links.length} link${links.length === 1 ? "" : "s"}${q ? ` · ${rows.length} matching “${esc(q)}”` : ""} to apps and devices</p></div>
      <div class="row">${moreMenu([{ label: "Check links", icon: "refresh", run: "portalRecheck()", tip: "See which links answer" }])}
      <button class="btn pri" data-need="admin" onclick="portalEdit()">${icon("edit")}Edit links</button></div></div>
    ${!links.length ? `<div class="empty portal-empty"><b>No links yet.</b> Add the containers you open most, and the router, switches and NAS around them.
        <div class="row" style="justify-content:center;margin-top:12px"><button class="btn pri" data-need="admin" onclick="portalEdit(true)">Pick from containers</button>
        <button class="btn" data-need="admin" onclick="portalEdit()">Add a link</button></div></div>`
      : !rows.length ? `<div class="empty">Nothing matches that search.</div>`
      : portalSections(rows).map(([section, members]) => `<section class="portal-section">
        ${section ? `<div class="sec">${esc(section)}</div>` : ""}
        <div class="portal-grid">${members.map(link => `<a class="portal-tile card flat" href="${safeHref(link.url)}" target="_blank" rel="noopener noreferrer" data-link="${esc(link.id)}">
          ${portalIcon(link)}<span class="portal-text"><b>${esc(link.title)}</b>
            <span class="dim xs mono">${esc(portalHost(link.url))}</span>${link.note ? `<span class="dim xs">${esc(link.note)}</span>` : ""}</span>
          <span class="portal-dot" data-tip="not checked yet"></span></a>`).join("")}</div></section>`).join("")}`);
  portalDots();
}

function portalDots() {
  const status = STATE.data.portalStatus || {};
  $$(".portal-tile").forEach(tile => {
    const s = status[tile.dataset.link], dot = $(".portal-dot", tile);
    if (!dot) return;
    dot.className = "portal-dot" + (s ? (s.up ? " up" : " down") : "");
    dot.dataset.tip = !s ? "not checked yet" : s.up ? `answering · ${s.ms} ms` : "not answering on its port";
  });
}

window.portalRecheck = async () => {
  try { STATE.data.portalStatus = await api("/api/portal/status?force=1"); portalDots(); toast("links checked", "ok"); }
  catch (e) { toast(e.message, "bad"); }
};

/* ---------------- editor ---------------- */
let PORTAL_EDIT = [];
let PORTAL_WORKLOADS = [];

function portalIconOptions(value) {
  const builtins = Object.entries(PORTAL_ICON_LABELS).map(([key, label]) =>
    `<option value="builtin:${key}" ${value === `builtin:${key}` ? "selected" : ""}>${esc(label)}</option>`).join("");
  const apps = PORTAL_WORKLOADS.map(w => `<option value="workload:${esc(w.ns)}/${esc(w.name)}" ${value === `workload:${w.ns}/${w.name}` ? "selected" : ""}>${esc(w.name)}'s logo</option>`).join("");
  const cached = String(value || "").startsWith("/api/icons/");
  const known = !value || cached || value.startsWith("builtin:") || PORTAL_WORKLOADS.some(w => value === `workload:${w.ns}/${w.name}`);
  return `<option value="" ${!value ? "selected" : ""}>Initials</option>
    <optgroup label="Devices">${builtins}</optgroup>
    ${apps ? `<optgroup label="Containers">${apps}</optgroup>` : ""}
    ${cached ? `<option value="${esc(value)}" selected>Current image</option>` : ""}
    ${known ? "" : `<option value="${esc(value)}" selected>${esc(value)}</option>`}
    <option value="url">Image from a URL…</option>`;
}

function portalEditRows() {
  const sections = [...new Set(PORTAL_EDIT.map(row => row.section).filter(Boolean))];
  return `<datalist id="pe_sections">${sections.map(s => `<option value="${esc(s)}">`).join("")}</datalist>
    ${PORTAL_EDIT.map((row, i) => `<div class="pe-row card flat" data-i="${i}">
      <div class="pe-icon">${portalIcon({ ...row, shown: portalPreview(row) }, "sm")}</div>
      <div class="pe-fields">
        <div class="pe-line"><input class="pe-title" value="${esc(row.title)}" placeholder="Title" maxlength="60" data-k="title">
          <input class="pe-url mono" value="${esc(row.url)}" placeholder="http://192.0.2.1" data-k="url"></div>
        <div class="pe-line"><input class="pe-section" list="pe_sections" value="${esc(row.section || "")}" placeholder="Section, e.g. Network" maxlength="40" data-k="section">
          <select class="pe-iconpick" data-k="icon">${portalIconOptions(row.icon || "")}</select>
          ${row.icon === "url" ? `<input class="pe-iconurl" value="${esc(row.icon_url || "")}" placeholder="https://…/logo.png" data-k="icon_url">` : ""}</div>
        <input class="pe-note" value="${esc(row.note || "")}" placeholder="Note (optional): what or where it is" maxlength="120" data-k="note">
      </div>
      <div class="pe-move">
        <button class="iconbtn" type="button" title="Move up" onclick="portalMove(${i},-1)" ${i ? "" : "disabled"}>↑</button>
        <button class="iconbtn" type="button" title="Move down" onclick="portalMove(${i},1)" ${i < PORTAL_EDIT.length - 1 ? "" : "disabled"}>↓</button>
        <button class="iconbtn danger" type="button" title="Remove" onclick="portalMove(${i},0)">×</button></div>
    </div>`).join("") || '<div class="empty">No links. Add one, or pick from containers.</div>'}`;
}

/* What an edited row's icon looks like before it is saved. */
function portalPreview(row) {
  const value = row.icon || "";
  if (value.startsWith("builtin:")) return { kind: "builtin", src: value.slice(8) };
  if (value.startsWith("workload:")) {
    const w = PORTAL_WORKLOADS.find(x => value === `workload:${x.ns}/${x.name}`);
    return w && w.icon ? { kind: "image", src: w.icon } : { kind: "letter" };
  }
  if (value === "url") return row.icon_url && /^https:\/\//.test(row.icon_url) ? { kind: "image", src: row.icon_url } : { kind: "letter" };
  if (value.startsWith("/api/icons/")) return row.shown || { kind: "letter" };
  return { kind: "letter" };
}

function portalEditPaint() {
  const host = $("#pe_rows");
  if (host) host.innerHTML = portalEditRows();
}

window.portalEdit = async (pick = false) => {
  modal("Portal links", `<div class="empty"><span class="spin2"></span>loading</div>`, true);
  try {
    const [data, wl] = await Promise.all([api("/api/portal"), STATE.data.wl ? Promise.resolve(STATE.data.wl) : api("/api/workloads").catch(() => [])]);
    PORTAL_WORKLOADS = wl || [];
    PORTAL_EDIT = (data.links || []).map(link => ({ ...link }));
    $("#mbody").innerHTML = `<p class="muted small">Organize portal links or add links from containers. All signed-in users can see these addresses; do not include passwords.</p>
      <div class="row" style="gap:8px;margin:12px 0"><button class="btn" onclick="portalAdd()">＋ Link</button>
        <button class="btn" onclick="portalPick()">＋ From containers</button></div>
      <div id="pe_rows">${portalEditRows()}</div>
      ${UI.actions(`<button class="btn pri" id="pe_save" onclick="portalSave()">Save links</button>
        <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`;
    if (pick) portalPick();
  } catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};

document.addEventListener("input", event => {
  const row = event.target.closest(".pe-row"), key = event.target.dataset.k;
  if (!row || !key || key === "icon") return;
  PORTAL_EDIT[+row.dataset.i][key] = event.target.value;
});
document.addEventListener("change", event => {
  const row = event.target.closest(".pe-row"), key = event.target.dataset.k;
  if (!row || !key) return;
  PORTAL_EDIT[+row.dataset.i][key] = event.target.value;
  // A new icon choice redraws the row's preview, and the URL box when asked for.
  if (key === "icon" || key === "icon_url") portalEditPaint();
});

window.portalAdd = (row = {}) => {
  PORTAL_EDIT.push({ id: "", title: "", url: "", section: "", icon: "", note: "", ...row });
  portalEditPaint();
  const inputs = $$("#pe_rows .pe-title");
  if (!row.title && inputs.length) inputs[inputs.length - 1].focus();
};
window.portalMove = (i, step) => {
  if (!step) PORTAL_EDIT.splice(i, 1);
  else if (PORTAL_EDIT[i + step]) [PORTAL_EDIT[i], PORTAL_EDIT[i + step]] = [PORTAL_EDIT[i + step], PORTAL_EDIT[i]];
  portalEditPaint();
};

window.portalPick = async () => {
  let candidates = [];
  try { candidates = await api("/api/portal/candidates"); } catch (e) { return toast(e.message, "bad"); }
  const have = new Set(PORTAL_EDIT.map(row => row.url));
  childModal("Pick from containers", candidates.length ? `<p class="muted small">Each exposed port of each container, at the address it listens on.</p>
    <div class="wg-list">${candidates.map((c, i) => `<label class="wg-item"><input type="checkbox" data-i="${i}" ${have.has(c.url) ? "disabled" : ""}>
      ${appAvatar(c.name, (PORTAL_WORKLOADS.find(w => w.ns === c.ns && w.name === c.name) || {}).icon)}
      <span><b>${esc(c.title)}</b><span class="dim xs mono"> ${esc(c.url)}</span>${c.port_name ? `<span class="dim xs"> · ${esc(c.port_name)}</span>` : ""}</span>
      ${have.has(c.url) ? '<span class="pill slim neutral">added</span>' : ""}</label>`).join("")}</div>
    <div class="row"><button class="btn pri" onclick="portalPickAdd()">Add ticked</button></div>`
    : `<div class="empty">No container has an exposed port yet.</div>`);
  window.__portalCandidates = candidates;
  loadAppIcons($("#mbody"));
};
window.portalPickAdd = () => {
  const picked = $$("#mbody .wg-item input:checked").map(box => window.__portalCandidates[+box.dataset.i]).filter(Boolean);
  // One tile per container: a second port of the same app says which it is.
  const counts = {};
  picked.forEach(c => { counts[c.name] = (counts[c.name] || 0) + 1; });
  picked.forEach(c => PORTAL_EDIT.push({ id: "", title: counts[c.name] > 1 ? `${c.title} :${c.port}` : c.title, url: c.url,
    section: c.group || "Apps", icon: c.has_logo ? c.icon : "", note: "" }));
  // Back to the editor, whose rows are redrawn from what was typed so far.
  modalBack();
  portalEditPaint();
};

window.portalSave = async () => {
  const links = PORTAL_EDIT.map(row => ({ id: row.id, title: String(row.title || "").trim(), url: String(row.url || "").trim(),
    section: String(row.section || "").trim(), note: String(row.note || "").trim(),
    icon: row.icon === "url" ? String(row.icon_url || "").trim() : row.icon || "" }));
  const blank = links.findIndex(link => !link.title || !link.url);
  if (blank >= 0) return toast(`link ${blank + 1} needs a title and an address`, "bad");
  const button = $("#pe_save");
  button.disabled = true; button.textContent = "Saving…";
  try {
    const result = await api("/api/portal", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ links }) });
    STATE.data.portal = { ...(STATE.data.portal || {}), links: result.links };
    toast(`${links.length} link${links.length === 1 ? "" : "s"} saved`, "ok"); closeModal();
    if (STATE.view === "portal") { resetPaint(); viewPortal(); }
  } catch (e) { toast(e.message, "bad"); button.disabled = false; button.textContent = "Save links"; }
};

window.viewPortal = viewPortal;
