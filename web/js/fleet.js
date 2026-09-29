/* Linked clusters: the switch in the top bar, the view of every cluster at
   once, and linking. docs/multi-cluster.md says how it works.

   Picking one cluster reloads the app from it, relayed by the Homestead this
   browser signed in to - its own pages, so a different release still works.
   "All clusters" keeps this Homestead's app and asks every linked cluster for
   its containers, VMs, nodes and volumes; each row carries its cluster, and
   an action on a row goes to that cluster. */

// Each list, and the page that shows it from every cluster. Elsewhere - a
// deploy placing pods, say - a list means this cluster's own.
const FLEET_LISTS = { "/api/workloads": "workloads", "/api/vms": "vms", "/api/nodes": "nodes", "/api/volumes": "storage" };
window.FLEET = { view: null, target: "", missingSaid: new Set() };

function fleetMode() {
  try { return localStorage.getItem("homestead.fleet.mode") === "all" ? "all" : "one"; }
  catch (e) { return "one"; }
}
function setFleetMode(mode) {
  try { localStorage.setItem("homestead.fleet.mode", mode); } catch (e) { /* this visit only */ }
}
/* Whether the page shows every linked cluster together. */
const fleetAll = () => fleetMode() === "all" && !!FLEET.view?.linked;
window.fleetAll = fleetAll;

/* Where api() sends a request. The four lists come from every cluster in
   the view of all of them; an action taken on another cluster's row - or a
   dialog opened from one - goes to that cluster. */
window.fleetRoute = (path, opts = {}) => {
  // A request that names its cluster itself - updating every linked
  // Homestead from one dialog - goes where it says.
  if (opts.headers?.["X-Homestead-Cluster"]) return { path, opts };
  const method = String(opts.method || "GET").toUpperCase();
  if (method === "GET" && FLEET_LISTS[path] === STATE.view && fleetAll()) return { path: `/api/fleet/all/${path.slice(5)}`, opts };
  const target = FLEET.target;
  if (!target || target === FLEET.view?.self || path.startsWith("/api/fleet") || path.startsWith("/api/auth/")) return { path, opts };
  const dialog = !$("#modal")?.classList.contains("hidden");
  // A plain list read with no dialog open is the page refreshing itself.
  if (method === "GET" && !path.includes("?") && !dialog) return { path, opts };
  return { path, opts: { ...opts, headers: { ...(opts.headers || {}), "X-Homestead-Cluster": target } } };
};

/* A console cannot send a header, so it names the cluster in its address. */
window.fleetSocketUrl = url => {
  const target = FLEET.target;
  if (!target || target === FLEET.view?.self) return url;
  return `${url}${url.includes("?") ? "&" : "?"}hs_cluster=${encodeURIComponent(target)}`;
};

/* A cluster that did not answer the view of all of them is said once. */
window.fleetNoteMissing = header => {
  let rows = [];
  try { rows = JSON.parse(decodeURIComponent(header)); } catch (e) { return; }
  rows.filter(row => !FLEET.missingSaid.has(row.id)).forEach(row => {
    FLEET.missingSaid.add(row.id);
    toast(`${row.name} is not answering, so it is missing from this list`, "warn");
  });
};

// Which cluster a click is about: the row it was made in.
document.addEventListener("click", event => {
  const row = event.target.closest?.("[data-cluster]");
  if (row) FLEET.target = row.dataset.cluster;
  else if (event.target.closest?.("#nav, .fleet-switch, .fleet-menu")) FLEET.target = "";
}, true);

/* Rows in the view of all clusters: whose each is. */
const clusterOf = row => (row?.site && fleetAll() ? row.site : null);
window.clusterAttr = row => { const c = clusterOf(row); return c ? ` data-cluster="${esc(c.id)}"` : ""; };
window.clusterTag = row => {
  const c = clusterOf(row);
  return c ? `<span class="tag cluster-tag" title="On ${esc(c.name)}">${esc(c.name)}</span>` : "";
};
/* A row that lives on another cluster: this one's own records - image
   updates, say - do not describe it. */
window.remoteRow = row => !!(row?.site && !row.site.self);

async function fleetLoad() {
  try { FLEET.view = await api("/api/fleet", { keep: true }); }
  catch (e) { FLEET.view = null; }
  if (window.renderBreadcrumb) renderBreadcrumb(STATE.view, STATE.modalDetail);
  return FLEET.view;
}
window.fleetLoad = fleetLoad;

/* ---------------------------------------------------------------- switch */
window.fleetCrumb = () => {
  const view = FLEET.view;
  if (!view?.linked) return "";
  const me = view.members.find(m => m.self);
  const label = fleetAll() ? "All clusters" : me?.name || "This cluster";
  return `<button type="button" class="fleet-switch" onclick="fleetMenu(this)" aria-haspopup="menu" aria-label="Switch cluster">
    ${icon("layers")}<span class="fleet-name">${esc(label)}</span><span class="fleet-caret" aria-hidden="true">▾</span></button>
    <span class="crumbsep" aria-hidden="true">/</span>`;
};

function fleetMenuHtml() {
  const view = FLEET.view || { members: [] };
  const all = fleetAll();
  const item = (checked, onclick, iconName, title, sub, extra = "") => `<button type="button" role="menuitemradio"
    aria-checked="${checked}" class="${checked ? "on" : ""}" onclick="${onclick}">${iconName}
    <span class="fleet-item"><b>${esc(title)}</b><span>${sub}</span></span>${extra}</button>`;
  const dot = m => `<span class="fleet-dot ${m.reachable ? (m.compatible === false ? "warn" : "ok") : "bad"}" aria-hidden="true"></span>`;
  return `${view.via ? `<div class="fleet-via">Through ${esc(view.via)}</div>` : ""}
    ${item(all, "fleetShowAll()", `<span class="fleet-dot all" aria-hidden="true"></span>`, "All clusters",
      `${view.members.length} together`)}
    <div class="fleet-sep"></div>
    ${view.members.map(m => item(!all && m.self, `fleetSwitch(${jsq(m.id)})`, dot(m), m.name,
      esc(m.self ? `this one · v${m.version}` : m.reachable ? `v${m.version}` : "not answering"))).join("")}
    <div class="fleet-sep"></div>
    <button type="button" data-need="admin" onclick="fleetMenuClose();fleetLink()">${icon("plus")}<span class="fleet-item"><b>Link a cluster</b></span></button>
    <button type="button" onclick="fleetOpenSettings()">${icon("gear")}<span class="fleet-item"><b>Manage clusters</b></span></button>`;
}

window.fleetMenuClose = () => {
  $$(".fleet-menu").forEach(menu => menu.remove());
  document.removeEventListener("click", fleetMenuOutside, true);
  document.removeEventListener("keydown", fleetMenuKey, true);
};
function fleetMenuOutside(event) {
  if (!event.target.closest(".fleet-menu, .fleet-switch")) fleetMenuClose();
}
function fleetMenuKey(event) { if (event.key === "Escape") fleetMenuClose(); }

window.fleetMenu = button => {
  if ($(".fleet-menu")) return fleetMenuClose();
  const menu = document.createElement("div");
  menu.className = "fleet-menu";
  menu.setAttribute("role", "menu");
  document.body.appendChild(menu);
  const paint = () => {
    menu.innerHTML = fleetMenuHtml();
    const box = button.getBoundingClientRect();
    menu.style.top = `${Math.round(box.bottom + 6)}px`;
    menu.style.left = `${Math.round(Math.max(8, Math.min(box.left, window.innerWidth - menu.offsetWidth - 8)))}px`;
  };
  paint();
  // What each cluster says now, painted in when it arrives.
  api("/api/fleet", { keep: true }).then(view => { FLEET.view = view; if (menu.isConnected) paint(); }).catch(() => {});
  setTimeout(() => {
    document.addEventListener("click", fleetMenuOutside, true);
    document.addEventListener("keydown", fleetMenuKey, true);
  });
};

window.fleetSwitch = async id => {
  fleetMenuClose();
  setFleetMode("one");
  try {
    await api("/api/fleet/switch", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }) });
    window.location.reload();
  } catch (e) { toast(e.message, "bad"); }
};

window.fleetShowAll = () => {
  fleetMenuClose();
  setFleetMode("all");
  window.location.reload();
};

/* ---------------------------------------------------------------- manage */
/* Linked clusters live in Settings: the switch's "Manage clusters" and the
   Import page both lead there. */
window.fleetOpenSettings = () => {
  fleetMenuClose();
  closeModal();
  settingsTab("fleet");
  if (STATE.view === "settings") fleetSettingsPaint(); else go("settings");
};
window.fleetManage = window.fleetOpenSettings;

// One line each: the cluster, where it is and what it runs, and its button.
function fleetMembersHtml(view) {
  return view.members.map(m => `<li>
    <span class="fleet-dot ${m.self || m.reachable ? (m.compatible === false ? "warn" : "ok") : "bad"}" aria-hidden="true"></span>
    <div class="fleet-row"><b>${esc(m.name)}</b>${m.self ? ` ${UI.chip("this one")}` : ""}${m.compatible === false ? ` ${UI.chip("update needed", "warn")}` : ""}
      <span class="fleet-sub"${m.self || m.reachable ? "" : ` title="${esc(m.error || "")}"`}><span class="mono">${esc(m.url || "no address")}</span>
        · ${m.self || m.reachable ? `v${esc(m.version)}` : "not answering"}</span></div>
    <div class="row nowrap fleet-row-acts"><span class="fleet-out" id="fout_${esc(m.id)}"></span>
    ${m.self ? (view.linked ? UI.button("Leave", "fleetLeave()", { attrs: 'data-need="admin"' }) : "")
      : UI.button("Unlink", `fleetUnlink(${jsArg(m.id)},${jsArg(m.name)})`, { attrs: 'data-need="admin"' })}</div></li>`).join("");
}

/* Migration from each cluster: workloads can move from it to another linked
   cluster while its backup storage runs - a move backs their volumes up
   there and restores them where they go. Each cluster's row has one button
   saying whether it is on, which opens everything about it. */
FLEET.transfers = {};
function fleetTransferCall(m, body = null) {
  if (m.self) return api("/api/objectstore/transfers", body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  return api("/api/move/clusters/transfers", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: m.handle, ...(body || {}) }) });
}

function fleetMigrationButton(m, state) {
  const on = !!state?.allowed;
  const label = !state ? "Migration" : on ? "Migration on" : "Migration off";
  return `<button type="button" class="btn sm fleet-mig${on ? " on" : ""}" onclick="fleetMigration(${jsq(m.id)})"
    title="${esc(on ? `Workloads can move from ${m.name} to another cluster` : `Workloads cannot move from ${m.name} yet`)}">
    <span class="fleet-dot ${on ? "ok" : ""}" aria-hidden="true"></span>${esc(label)}</button>`;
}

window.fleetTransfersPaint = () => {
  (FLEET.view?.members || []).forEach(async m => {
    const host = document.getElementById(`fout_${m.id}`);
    if (!host) return;
    if (!m.self && !m.reachable) { host.innerHTML = ""; return; }
    let state = null;
    try { state = await fleetTransferCall(m); } catch (e) { state = null; }
    FLEET.transfers[m.id] = state;
    const now = document.getElementById(`fout_${m.id}`);
    if (now) now.innerHTML = fleetMigrationButton(m, state);
    if (window.applyRole) applyRole();
  });
};

/* Everything about migration from one cluster: on or off, its backup
   storage, and what, if anything, needs fixing. */
window.fleetMigration = async id => {
  const m = (FLEET.view?.members || []).find(x => x.id === id);
  if (!m) return;
  modal(`Migration from ${m.name}`, '<div class="empty"><span class="spin2"></span> Asking about its backup storage…</div>', true);
  let state, ready = null;
  try { state = await fleetTransferCall(m); FLEET.transfers[id] = state; }
  catch (e) {
    $("#mbody").innerHTML = UI.callout("bad", `Could not ask ${m.name}.`, esc(e.message)) + UI.actions(UI.cancel("Close"));
    return;
  }
  if (!m.self) {
    ready = await api("/api/move/clusters/readiness", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: m.handle }) }).catch(() => null);
    if (ready) (window.__clusterReady ||= {})[m.handle] = ready;
  }
  if (!$("#mbody")) return;
  const lead = UI.lead(`Lets workloads move from ${esc(m.name)} to another linked cluster. Each one's volumes are backed up to
    ${esc(m.name)}'s backup storage - an S3 store (RustFS) on a Longhorn volume - and restored where it goes.`);
  const target = ready?.target || {};
  let body;
  if (state.allowed) {
    const problems = [];
    const retryTarget = UI.button("Retry target switch", `fleetTargetRetry(${jsArg(id)})`, { attrs: 'data-need="admin"' });
    const same = (a, b) => String(a || "").replace(/\/+$/, "") === String(b || "").replace(/\/+$/, "");
    if (["pending", "applying"].includes(state.longhorn?.state)) problems.push(["info", "Longhorn's backup target is switching.",
      `${state.longhorn.detail}. Homestead retries automatically for up to ten minutes, even with this dialog closed.`, retryTarget]);
    else if (state.longhorn?.state === "failed") problems.push(["warn", "Longhorn's backup target needs attention.", state.longhorn.detail, retryTarget]);
    else if (!state.ready && state.volume_problem) problems.push(["bad", "Its backup storage cannot start: Longhorn has no room for its volume.",
      `Longhorn says: ${state.volume_problem}. Disable migration, remove the backup storage under ${m.name}'s Data protection, and enable migration again with a smaller size.`, ""]);
    else if (!state.ready) problems.push(["warn", "Its backup storage is starting.", "Its first start downloads the S3 server; this takes a minute or two.", ""]);
    else if (!state.reachable_off_cluster) problems.push(["warn", "It has no LAN address, so no other cluster can read it.", "",
      m.self ? "" : UI.button("Give it an address", `clusterStorage(${jsArg(m.handle)},true,()=>fleetMigration(${jsArg(id)}))`)]);
    else if (ready && target.configured && target.endpoint && state.endpoint && !same(target.endpoint, state.endpoint))
      problems.push(["warn", `${m.name}'s Longhorn backs up to ${target.endpoint}, not to its backup storage at ${state.endpoint}.`,
        "Pointing Longhorn at the store sends moves' backups where this cluster can read them.",
        retryTarget]);
    else if (state.longhorn?.pointed === false) problems.push(["warn", "Longhorn is not writing to this backup storage.",
      state.longhorn.endpoint ? `Current endpoint: ${state.longhorn.endpoint}` : "", retryTarget]);
    else if (ready && target.configured && !target.answers) problems.push(["warn", `This cluster cannot reach it at ${target.endpoint}.`,
      "It may still be starting; otherwise the address is taken or firewalled.",
      UI.button("Give it another address", `clusterStorage(${jsArg(m.handle)},true,()=>fleetMigration(${jsArg(id)}))`)]);
    else if (ready && !target.configured) problems.push(["warn", `${m.name}'s Longhorn is not pointed at it yet.`, "",
      retryTarget]);
    body = `${lead}
      ${UI.facts([["Migration", "On"], ["Backup storage", `<span class="mono">${esc((state.endpoint || "—").replace(/^https?:\/\//, ""))}</span>`],
        ...(m.self ? [] : [["Reachable from here", ready ? (target.answers ? "Yes" : "No") : "—"]]), ["Size", state.size_gb ? `${state.size_gb} GB` : "—"]])}
      ${problems.map(([tone, title, detail, fix]) => UI.callout(tone, title, `${detail ? `<p>${esc(detail)}</p>` : ""}${fix}`)).join("")}
      ${state.backups_here ? UI.callout("info", `${m.name}'s Longhorn backs up here too.`, "Disabling migration pauses those backups until it is enabled again.") : ""}
      ${UI.actions(UI.cancel("Close") + UI.button("Disable migration", `fleetMigrationSet(${jsArg(id)}, false)`, { kind: "danger", id: "fm_go", attrs: 'data-need="admin"' }))}`;
  } else if (state.deployed) {
    body = `${lead}
      ${UI.callout("info", "Its backup storage is stopped.", "Enabling migration starts it again as it was, with the backups on it.")}
      ${UI.actions(UI.cancel() + UI.button("Enable migration", `fleetMigrationSet(${jsArg(id)}, true)`, { kind: "pri", id: "fm_go", attrs: 'data-need="admin"' }))}`;
  } else {
    const shared = ready?.shared_vip;
    body = `${lead}
      ${UI.fields(
        UI.field("Size (GB)", '<input id="fm_size" type="number" min="5" value="100">', { help: "Holds the backups moves make; they are incremental." }),
        UI.field("Address", `<select id="fm_pick" onchange="$('#fm_ip').hidden = this.value !== '__typed'">
            <option value="__shared">${shared ? `Its shared address · ${esc(shared)}` : "Its shared address"}</option>
            <option value="__typed">An address of its own…</option></select>
          <input id="fm_ip" class="mono" placeholder="192.168.1.243" hidden data-ipam>`,
          { help: "Shared with its apps, on its own port. Choose an address of its own only to keep its traffic apart." }),
        UI.field("Port", '<input id="fm_port" type="number" min="1" max="65534" placeholder="9000" class="mono">',
          { help: "Where the store answers. Pick another if an app there already uses 9000; the next port up is its console." }))}
      ${UI.actions(UI.cancel() + UI.button("Enable migration", `fleetMigrationSet(${jsArg(id)}, true)`, { kind: "pri", id: "fm_go", attrs: 'data-need="admin"' }))}`;
  }
  $("#mbody").innerHTML = `<div class="ui-stack">${body}</div>`;
  if (window.applyRole) applyRole();
};

window.fleetTargetRetry = async id => {
  const m = (FLEET.view?.members || []).find(x => x.id === id);
  if (!m) return;
  if (!m.self) return clusterPointTarget(m.handle, () => fleetMigration(id));
  try {
    const r = await api("/api/objectstore/longhorn", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast(r.detail, "ok"); fleetMigration(id);
  } catch (e) { toast(e.message, "bad"); }
};

window.fleetMigrationSet = async (id, allow) => {
  const m = (FLEET.view?.members || []).find(x => x.id === id);
  const state = FLEET.transfers[id] || {};
  if (!allow && !(await ask(`Disable migration from ${m.name}?${String.fromCharCode(10, 10)}Its backup storage stops; its volume and the backups on it are kept.`
      + (state.backups_here ? ` Longhorn on ${m.name} backs up there too, so those backups pause until it is enabled again.` : "")))) return;
  const pick = $("#fm_pick")?.value || "__shared";
  const address = pick === "__typed" ? ($("#fm_ip")?.value || "").trim() : "";
  if (pick === "__typed" && !address) return toast("Type the address for its backup storage", "bad");
  const go = $("#fm_go");
  if (go) { go.disabled = true; go.textContent = allow ? "Enabling…" : "Disabling…"; }
  try {
    const r = await fleetTransferCall(m, { allow, size_gb: +($("#fm_size")?.value || 100), lb_ip: address,
      vip_mode: pick === "__shared" ? "shared" : "", port: +($("#fm_port")?.value || 0) || undefined });
    toast(`${m.name}: ${r.detail}`, "ok");
    fleetTransfersPaint();
    if (window.fleetMovesPaint) fleetMovesPaint();
    fleetMigration(id);
  } catch (e) {
    toast(e.message, "bad");
    if (go) { go.disabled = false; go.textContent = allow ? "Enable migration" : "Disable migration"; }
  }
};

/* A cluster a move can come from, on one line: whether a move can come
   from it now, and its workloads. */
function moveSourceRow(c) {
  return `<li><span class="fleet-dot" id="msd_${esc(c.name)}" aria-hidden="true"></span>
    <div class="fleet-row"><b>${esc(c.label || c.name)}</b><span class="fleet-sub" id="mss_${esc(c.name)}">checking…</span></div>
    ${UI.button("Browse workloads", `clusterBrowse(${jsArg(c.name)})`)}</li>`;
}

async function moveSourceCheck(c) {
  const dot = document.getElementById(`msd_${c.name}`), text = document.getElementById(`mss_${c.name}`);
  let r = null, error = "";
  try {
    r = await api("/api/move/clusters/readiness", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: c.name }) });
  } catch (e) { error = e.message; }
  if (!text) return;
  (window.__clusterReady ||= {})[c.name] = r;
  const member = (FLEET.view?.members || []).find(m => m.handle === c.name);
  const fix = member ? ` - <a class="linkish" onclick="fleetMigration(${jsq(member.id)})">migration…</a>`
    : ` - <a class="linkish" data-need="admin" onclick="clusterStorage(${jsq(c.name)})">set it up</a>`;
  const [tone, words] = error ? ["bad", esc(error)]
    : r.version?.compatible === false ? ["warn", esc(r.version.message || "a release too far from this one")]
    : r.version?.state === "unreachable" ? ["bad", "not answering"]
    : r.ready ? ["ok", "ready to move from"]
    : r.storage?.stopped || !r.storage?.deployed ? ["", `migration from it is off${fix}`]
    : ["warn", `its backup storage needs attention${fix}`];
  if (dot) dot.className = `fleet-dot ${tone}`;
  text.innerHTML = words;
  if (window.applyRole) applyRole();
}

// Clusters added for moves with a stored account, before linking existed.
function fleetLegacyHtml(rows) {
  return rows.map(r => `<li>
    <span class="fleet-dot" aria-hidden="true"></span>
    <div class="fleet-row"><b>${esc(r.name)}</b>
      <span class="fleet-sub">${r.linked_as ? `linked as ${esc(r.linked_as.name)}` : `<span class="mono">${esc(r.user)}@${esc(r.url)}</span>`}</span></div>
    <div class="row nowrap">${UI.button("Forget", `fleetForgetLegacy(${jsArg(r.name)})`, { attrs: 'data-need="admin"' })}
      ${UI.button(r.linked_as ? "Use the link" : "Link", `fleetLinkLegacy(${jsArg(r.name)})`, { kind: "pri", attrs: 'data-need="admin"' })}</div></li>`).join("");
}

window.fleetSettingsPaint = async () => {
  const host = $("#fleetCard");
  if (!host) return;
  fleetMovesPaint();
  const [view, legacy] = await Promise.all([api("/api/fleet").catch(() => null), api("/api/fleet/legacy").catch(() => [])]);
  if (!$("#fleetCard")) return;
  if (view) FLEET.view = view;
  const all = fleetMode() === "all";
  const mode = (value, label) => `<button type="button" class="${(value === "all") === all ? "on" : ""}" aria-pressed="${(value === "all") === all}"
    onclick="fleetSetMode(${jsq(value)})">${label}</button>`;
  host.innerHTML = `<div class="settings-card-head"><div><div class="ctitle">Linked clusters</div>
      <div class="csub">Other Homesteads managed from this one - even when only this one is reachable from outside.</div></div>
      ${UI.button("Link a cluster", "fleetLink()", { kind: "pri", attrs: 'data-need="admin"' })}</div>
    ${view ? `<ul class="fleet-list">${fleetMembersHtml(view)}</ul>` : UI.callout("bad", "Could not read the linked clusters.")}
    ${view?.linked ? UI.section("How they show", `<div class="seg fleet-mode" role="group" aria-label="How linked clusters show">
        ${mode("one", "One cluster at a time")}${mode("all", "All clusters together")}</div>
      <p class="ui-help">${all
        ? "Containers, Virtual Machines, Nodes and Volumes list every cluster, each row tagged with its own. The switch at the start of the top bar still opens one cluster on its own."
        : "The switch at the start of the top bar opens any cluster here, with your sign-in and role. Its All clusters shows them together."}</p>`) : ""}
    ${legacy.length ? UI.section("Added before linking", `<p class="ui-help">Added on Import for moves, with an account kept here.
      Link each to manage it from here too; its password is then deleted, and moves already made keep working.</p>
      <ul class="fleet-list">${fleetLegacyHtml(legacy)}</ul>`) : ""}
    ${UI.section("Where the others reach this Homestead", UI.field("Address",
      `<div class="fleet-address"><input id="fleetAddress" value="${esc(view?.address || view?.suggested_address || "")}" placeholder="http://192.168.1.242:8088">
      ${UI.button("Save", "fleetSaveAddress()", { attrs: 'data-need="admin"' })}</div>`,
      { help: "Its LAN address and port. Linked clusters use it to relay pages and consoles to this one." }))}
    ${UI.more("How linking works", `<p>Linked Homesteads share a key that signs every request between them. Nobody's
      password is kept: the admin account asked for when linking is used once. A person signed in to one cluster keeps
      their role on the others, and each cluster applies its own rules to it.</p>
      <p>Unlinking a cluster gives the rest a new key, so the one that left can no longer act for them.</p>`)}`;
  fleetTransfersPaint();
};

/* Moving workloads here from another cluster: the moves under way, and for
   each cluster whether a move can come from it - its release, and backup
   storage over there that this cluster can reach - and its workloads. */
window.fleetMovesPaint = async () => {
  const host = $("#fleetMovesCard");
  if (!host) return;
  const [moves, clusters] = await Promise.all([api("/api/move/moves").catch(() => []), api("/api/move/clusters").catch(() => [])]);
  if (!$("#fleetMovesCard")) return;
  host.hidden = !moves.length && !clusters.length;
  host.innerHTML = `<div class="settings-card-head"><div><div class="ctitle">Moving workloads</div>
      <div class="csub">Bring containers and VMs here from another cluster. A cluster's <b>Migration</b> button above lets workloads move from it.</div></div>
      ${moves.some(m => ["succeeded", "cancelled"].includes(m.status)) ? UI.button("Clear finished", "moveDismiss()", { attrs: 'data-need="admin"' }) : ""}</div>
    <div id="movesList">${moves.length ? movesHtml(moves) : ""}</div>
    ${clusters.length ? `<ul class="fleet-list move-sources">${clusters.map(moveSourceRow).join("")}</ul>` : ""}`;
  if (window.applyRole) applyRole();
  // Each check waits on the other Homestead answering, so after the card is up.
  clusters.forEach(moveSourceCheck);
  clearTimeout(window.__moveTimer);
  if (moves.some(m => m.status === "running")) window.__moveTimer = setTimeout(watchMoves, 4000);
  fleetPendingMove();
};

window.fleetSetMode = mode => {
  setFleetMode(mode);
  STATE.data.wl = STATE.data.vms = STATE.data.nodes = STATE.data.vols = null;
  if (window.renderBreadcrumb) renderBreadcrumb(STATE.view, STATE.modalDetail);
  fleetSettingsPaint();
};

async function fleetRefreshAfter() {
  await fleetLoad();
  if ($("#fleetCard")) fleetSettingsPaint();
}

window.fleetLinkLegacy = async name => {
  try {
    const result = await api("/api/fleet/link-legacy", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, own_url: FLEET.view?.address || "" }) });
    toast(`${name} is linked as ${result.member?.name || name}`, "ok");
    await fleetRefreshAfter();
  } catch (e) { toast(e.message, "bad"); }
};

window.fleetForgetLegacy = async name => {
  if (!(await ask(`Forget ${name}?${String.fromCharCode(10, 10)}Its stored account is deleted. Nothing on that cluster is touched.`))) return;
  try {
    await api("/api/move/clusters/remove", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast(`${name} forgotten`, "ok");
    await fleetRefreshAfter();
  } catch (e) { toast(e.message, "bad"); }
};

window.fleetLink = () => {
  const suggested = FLEET.view?.address || FLEET.view?.suggested_address || "";
  const open = $("#modal") && !$("#modal").classList.contains("hidden");
  (open ? childModal : modal)("Link a cluster", `<div class="ui-stack">
    ${UI.lead("Links another Homestead to this one and to every cluster already linked here. It must not be linked to others yet.")}
    ${UI.fields(
      UI.field("Its address", '<input id="fl_url" placeholder="http://192.168.1.250:8088" autocomplete="off">', { wide: true,
        help: "What you open it at in a browser on your LAN, with the port." }),
      UI.field("Admin username there", '<input id="fl_user" placeholder="admin" autocomplete="off">'),
      UI.field("Password there", '<input id="fl_pass" type="password" autocomplete="new-password">'),
      UI.field("This Homestead's address", `<input id="fl_own" value="${esc(suggested)}" autocomplete="off">`, { wide: true,
        help: "How that cluster reaches this one." }))}
    ${UI.more("What is kept", "<p>The password signs in once to exchange a shared key, and is not stored. From then on the clusters sign their requests to each other with that key.</p>")}
    ${UI.actions((open ? UI.button("Back", "modalBack()") : UI.cancel()) + UI.button("Link", "fleetLinkSave()", { kind: "pri", id: "fl_go" }))}
  </div>`);
  setTimeout(() => $("#fl_url")?.focus(), 30);
};

window.fleetLinkSave = async () => {
  const body = { url: $("#fl_url").value.trim(), username: $("#fl_user").value.trim(),
    password: $("#fl_pass").value, own_url: $("#fl_own").value.trim() };
  if (!body.url || !body.username || !body.password) return toast("Its address, and an admin username and password there", "bad");
  const go = $("#fl_go");
  if (go) { go.disabled = true; go.textContent = "Linking…"; }
  try {
    const result = await api("/api/fleet/join", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    toast(`${result.member?.name || "The cluster"} is linked${result.missed?.length ? `; ${result.missed.length} other cluster(s) will catch up when they answer` : ""}`, "ok");
    closeModal();
    await fleetRefreshAfter();
  } catch (e) {
    toast(e.message, "bad");
    if (go) { go.disabled = false; go.textContent = "Link"; }
  }
};

window.fleetUnlink = async (id, name) => {
  if (!(await ask(`Unlink ${name}?${String.fromCharCode(10, 10)}It keeps running as it is. The linked clusters get a new key, so ${name} can no longer act for them.`))) return;
  try {
    const result = await api("/api/fleet/remove", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }) });
    toast(result.told ? `${name} is unlinked` : `${name} is unlinked here; it did not answer, so it still lists the others until it does`, result.told ? "ok" : "warn");
    await fleetRefreshAfter();
  } catch (e) { toast(e.message, "bad"); }
};

window.fleetLeave = async () => {
  if (!(await ask("Stop linking this Homestead to the others?" + String.fromCharCode(10, 10) + "The others stay linked to each other."))) return;
  try {
    await api("/api/fleet/leave", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    setFleetMode("one");
    toast("This Homestead stands alone again", "ok");
    await fleetRefreshAfter();
  } catch (e) { toast(e.message, "bad"); }
};

window.fleetSaveAddress = async () => {
  try {
    const result = await api("/api/fleet/address", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: $("#fleetAddress").value.trim() }) });
    toast(result.missed?.length ? `Saved; ${result.missed.length} cluster(s) will hear when they answer` : "Saved", "ok");
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------------------------------------------------------- moving */
/* Moves are started by the cluster a workload goes to, so "move to" opens
   that cluster at its review of this workload. */
window.moveToCluster = (kind, name, sourceHandle = "") => {
  const view = FLEET.view;
  const me = view?.members.find(m => m.self);
  const from = sourceHandle || me?.handle || "";
  const choices = (view?.members || []).filter(m => m.handle !== from);
  if (!choices.length) return fleetLink();
  modal(`Move to cluster · ${name}`, `<div class="ui-stack">
    ${UI.lead(`Opens the cluster you pick at its review of this move. Nothing stops until you start it there.`)}
    <div class="fleet-pick">${choices.map(m => `<button type="button" class="btn" ${m.reachable ? "" : "disabled"}
      onclick="moveToClusterGo(${jsq(m.id)},${jsq(from)},${jsq(kind)},${jsq(name)})">
      <span class="fleet-dot ${m.reachable ? "ok" : "bad"}"></span><b>${esc(m.name)}</b>
      <span class="dim xs">${m.reachable ? `v${esc(m.version)}` : "not answering"}</span></button>`).join("")}</div>
    ${UI.actions(UI.cancel())}</div>`);
};

window.moveToClusterGo = async (id, from, kind, name) => {
  setFleetMode("one");
  try {
    await api("/api/fleet/switch", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }) });
    try { localStorage.setItem("homestead.settings.tab", "fleet"); } catch (e) { /* the page still opens */ }
    window.location.href = `/settings?${new URLSearchParams({ move: `${from}:${kind}:${name}` })}`;
  } catch (e) { toast(e.message, "bad"); }
};

/* Arriving from "move to": open the review the other cluster asked for. */
window.fleetPendingMove = () => {
  const wanted = new URLSearchParams(window.location.search).get("move");
  if (!wanted) return;
  window.history.replaceState(window.history.state, "", window.location.pathname);
  const [cluster, kind, ...rest] = wanted.split(":");
  const name = rest.join(":");
  // With no dialog open, the review opens on its own and Cancel closes it.
  if (cluster && kind && name && window.moveReview) moveReview(cluster, kind, name);
};
