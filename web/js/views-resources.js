/* Resources: every kind the cluster serves, as Headlamp or kubectl shows it.
   Kinds on the left, grouped; the chosen kind's objects with the API server's
   own columns; any object as YAML, with its events, edited or deleted. */

const RES = { kinds: null, pick: null, ns: "", q: "" };

function resKey(k) { return `${k.group}/${k.version}/${k.resource}`; }
function resLabel(k) { return k.kind.replace(/([a-z])([A-Z])/g, "$1 $2"); }

async function viewResources() {
  if (!RES.kinds) {
    RES.kinds = await api("/api/resources/kinds");
    try { const saved = localStorage.getItem("homestead.resources.kind"); RES.pick = RES.kinds.find(k => resKey(k) === saved) || null; } catch (e) { /* default */ }
    RES.pick = RES.pick || RES.kinds.find(k => k.resource === "pods" && k.group === "") || RES.kinds[0];
  }
  const params = new URLSearchParams({ group: RES.pick.group, version: RES.pick.version, resource: RES.pick.resource, ns: RES.pick.namespaced ? RES.ns : "" });
  let list;
  try { list = await api(`/api/resources/list?${params}`); } catch (e) { list = { error: e.message, columns: [], rows: [] }; }
  STATE.data.resList = list;
  // Unread namespaces are said, and read again next time: never kept as none.
  if (!STATE.data.namespacesAll) {
    try { STATE.data.namespacesAll = (await api("/api/resources/list?group=&version=v1&resource=namespaces")).rows.map(r => r.name); RES.nsUnread = null; }
    catch (e) { RES.nsUnread = e.message; }
  }
  renderResources();
}
window.viewResources = viewResources;

function renderResources() {
  const list = STATE.data.resList || { columns: [], rows: [] }, k = RES.pick, q = STATE.q.toLowerCase();
  const rows = list.rows.filter(r => !q || [r.name, r.namespace, ...r.cells].join(" ").toLowerCase().includes(q));
  const filter = RES.q.toLowerCase();
  const kinds = RES.kinds.filter(x => !filter || [x.kind, x.resource, x.group, ...x.short].join(" ").toLowerCase().includes(filter));
  const groups = [...new Set(kinds.map(x => x.category))];
  paint(`${UI.pageHeader(`Resources`, `Every kind this cluster serves, ${RES.kinds.length} of them · the columns are the API server's own`, `<button class="btn pri" data-need="admin" onclick="resCreate()">＋ Create from YAML</button>`)}
    <div class="res-layout">
      <aside class="res-kinds card flat">
        <input class="res-filter" aria-label="Find a kind" placeholder="Find a kind" value="${esc(RES.q)}" oninput="resFilter(this.value)">
        <select class="res-kind-select" aria-label="Kind" onchange="resPick(this.value)">${RES.kinds.map(x => `<option value="${esc(resKey(x))}" ${x === k ? "selected" : ""}>${esc(x.category)} · ${esc(resLabel(x))}</option>`).join("")}</select>
        <div class="res-kind-list">${groups.map(g => `<div class="res-group">${esc(g)}</div>${kinds.filter(x => x.category === g).map(x =>
          `<button class="res-kind ${x === k ? "on" : ""}" onclick="resPick(${jsq(resKey(x))})" title="${esc(x.group || "core")}/${esc(x.version)} · ${esc(x.resource)}">${esc(resLabel(x))}${x.group && x.category === "Custom resources" ? `<span class="dim xs"> ${esc(x.group)}</span>` : ""}</button>`).join("")}`).join("")}</div>
      </aside>
      <section class="res-main">
        <div class="between res-head"><div><b>${esc(resLabel(k))}</b> <span class="dim xs mono">${esc(k.group || "core")}/${esc(k.version)}</span>
            <span class="dim xs"> · ${rows.length}${list.more ? "+" : ""} shown</span></div>
          ${k.namespaced ? `<select aria-label="Namespace" onchange="resNamespace(this.value)"><option value="">All namespaces</option>${(STATE.data.namespacesAll || (RES.ns ? [RES.ns] : [])).map(n =>
            `<option ${n === RES.ns ? "selected" : ""}>${esc(n)}</option>`).join("")}${STATE.data.namespacesAll ? "" : '<option disabled>The namespaces could not be read</option>'}</select>` : '<span class="dim xs">cluster-wide</span>'}</div>
        ${list.error ? `${UI.callout("bad", "", `${esc(list.error)}`)}` : ""}
        <div class="card flat pad0"><div class="tblwrap"><table class="tbl dense stack" data-sort="res-${esc(k.resource)}"><thead><tr>
          ${k.namespaced && !RES.ns ? "<th>Namespace</th>" : ""}${list.columns.map(c => `<th title="${esc(c.description)}">${esc(c.name)}</th>`).join("")}</tr></thead><tbody>
          ${rows.map(r => `<tr class="clickable" onclick="resOpen(${jsq(r.namespace)},${jsq(r.name)})">
            ${k.namespaced && !RES.ns ? `<td class="mono small dim" data-label="Namespace">${esc(r.namespace)}</td>` : ""}
            ${r.cells.map((c, i) => `<td data-label="${esc(list.columns[i]?.name || "")}" class="${i === 0 ? "mono small" : "small"}">${i === 0 ? `<b>${esc(String(c))}</b>` : esc(typeof c === "object" ? JSON.stringify(c) : String(c ?? ""))}</td>`).join("")}</tr>`).join("")
            || `<tr><td colspan="${list.columns.length + 1}" class="empty">${list.error ? "They could not be read." : `None${RES.ns ? ` in ${esc(RES.ns)}` : ""}.`}</td></tr>`}</tbody></table></div></div>
      </section></div>`);
}

window.resFilter = value => { RES.q = value; renderResources(); const f = $(".res-filter"); if (f) { f.focus(); f.setSelectionRange(value.length, value.length); } };
window.resPick = key => {
  RES.pick = RES.kinds.find(k => resKey(k) === key) || RES.pick;
  try { localStorage.setItem("homestead.resources.kind", key); } catch (e) { /* this visit */ }
  resetPaint(); viewResources();
};
window.resNamespace = ns => { RES.ns = ns; viewResources(); };

const resParams = (ns, name) => new URLSearchParams({ group: RES.pick.group, version: RES.pick.version, resource: RES.pick.resource, ns, name });

window.resOpen = async (ns, name, reveal = false) => {
  const k = RES.pick;
  modal(`${resLabel(k)} · ${name}`, `<div class="empty"><span class="spin2"></span>loading</div>`, true);
  let o;
  try { o = await api(`/api/resources/${reveal ? "reveal" : "object"}?${resParams(ns, name)}`); }
  catch (e) { $("#mbody").innerHTML = UI.error(e, { retry: `resOpen(${jsArg(ns)},${jsArg(name)})`, dialog: true }); return; }
  const pod = k.group === "" && k.resource === "pods";
  $("#mbody").innerHTML = `<div class="between"><div class="dim xs mono">${esc(k.group || "core")}/${esc(k.version)} · ${esc(k.kind)}${ns ? ` · ${esc(ns)}` : ""}</div>
      <div class="row">${pod ? `<button class="btn sm" onclick="resLogs(${jsq(ns)},${jsq(name)})">${icon("log")}Logs</button>` : ""}
        ${o.secret_hidden ? `<button class="btn sm" data-need="admin" onclick="resOpen(${jsq(ns)},${jsq(name)},true)">Reveal values</button>` : ""}
        <button class="btn sm" data-need="admin" id="res_edit" onclick="resEdit()">${icon("edit")}Edit</button>
        <span data-need="admin">${actionBar([{ label: "Delete", icon: "trash", run: `resDelete(${jsq(ns)},${jsq(name)})`, need: "admin", danger: true }], { shown: 0, label: `More actions for ${name}` })}</span></div></div>
    ${UI.sectionForm("res_obj", [
      // YAML and Events are the shared section tabs, as a VM's details are (#559).
      { key: "yaml", title: "YAML", html: `<textarea id="res_yaml" aria-label="YAML" class="mono helm-values res-yaml" spellcheck="false" readonly>${esc(o.yaml)}</textarea>
        ${UI.actions(`<button class="btn pri" onclick="resSave(${jsq(ns)},${jsq(name)})">Save</button>
          <button data-dialog-dismiss="true" class="btn" onclick="resOpen(${jsq(ns)},${jsq(name)},${reveal})">Cancel</button>
          <span class="dim xs">Saved as a replacement. If someone changed it in the meantime, the save is refused instead of overwriting their change.</span>`, "", {attrs:`id="res_save_row" hidden`})}` },
      { key: "events", title: "Events", html: '<div id="res_events" class="dim small">loading</div>' }], "", { always: true, cancelHtml: UI.cancel("Close") })}`;
  if (window.applyRole) applyRole();
  const uid = (o.object.metadata || {}).uid || "";
  api(`/api/resources/events?${new URLSearchParams({ ns, name, uid })}`).then(events => {
    const host = $("#res_events");
    if (host) host.innerHTML = events.length ? `<table class="tbl dense stack"><thead><tr><th>Type</th><th>Reason</th><th>Message</th><th>Count</th><th>Last</th></tr></thead><tbody>
      ${events.map(e => `<tr><td>${UI.chip(e.type, e.type === "Warning" ? "warn" : "neutral", { size: "slim" })}</td><td class="small">${esc(e.reason)}</td>
        <td class="small">${esc(e.message)}</td><td class="mono">${e.count}</td><td class="small dim">${fmtWhen(e.last)}</td></tr>`).join("")}</tbody></table>`
      : "No recent events.";
  }, e => { const host = $("#res_events"); if (host) host.innerHTML = UI.callout("warn", "The events could not be read", esc(e.message)); });
};
window.resEdit = () => { const t = $("#res_yaml"); t.readOnly = false; t.focus(); $("#res_save_row").hidden = false; $("#res_edit").hidden = true; const foot = $("#res_obj > .stepper-foot"); if (foot) foot.hidden = true; };
const resPost = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
window.resSave = async (ns, name) => {
  const k = RES.pick;
  try {
    const r = await resPost("/api/resources/save", { group: k.group, version: k.version, resource: k.resource, ns, name, yaml: $("#res_yaml").value });
    toast(r.detail, "ok"); resOpen(ns, name);
  } catch (e) { toast(e.message, "bad"); }
};
window.resDelete = (ns, name) => {
  const k = RES.pick;
  childModal(`Delete ${k.kind} · ${name}`, `${UI.lead(`Deletes <b>${esc(name)}</b>${ns ? ` in ${esc(ns)}` : ""}. Whatever controls it may create it again, and whatever depends on it may stop.`)}
    ${UI.typedConfirm({ id: "res_delete_confirm", expect: name, button: "res_delete_go" })}
    ${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="modalBack()">Back</button><button class="btn danger" id="res_delete_go" disabled onclick="resDeleteGo(${jsq(ns)},${jsq(name)})">Delete ${esc(k.kind)}</button>`)}`);
};
window.resDeleteGo = async (ns, name) => {
  if (!typedConfirmMatches("res_delete_confirm")) return;
  const k = RES.pick;
  try { const r = await resPost("/api/resources/delete", { group: k.group, version: k.version, resource: k.resource, ns, name });
    toast(r.detail, "ok"); closeModal(); viewResources(); } catch (e) { toast(e.message, "bad"); }
};
window.resLogs = (ns, pod) => { closeModal(); openLogs(pod, `/api/logs?ns=${encodeURIComponent(ns)}&pod=${encodeURIComponent(pod)}`); };
window.resCreate = () => {
  modal("Create from YAML", `<p class="muted small">One object or several, separated by <span class="mono">---</span>, created in order. Objects without a namespace go to the one chosen here.</p>
    ${STATE.data.namespacesAll ? `<div class="f"><label for="rc_ns">Namespace</label><select id="rc_ns">${STATE.data.namespacesAll.map(n => `<option ${n === (RES.ns || "lab") ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div>`
      : `${UI.callout("warn", "The namespaces could not be read", `${esc(RES.nsUnread || "")} · Enter the one to use.`)}<div class="f"><label for="rc_ns">Namespace</label><input id="rc_ns" value="${esc(RES.ns)}" placeholder="default" autocomplete="off"></div>`}
    <textarea id="rc_yaml" aria-label="YAML to create" class="mono helm-values res-yaml" spellcheck="false" placeholder="apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: example\ndata:\n  hello: world"></textarea>
    ${UI.actions(`<button class="btn pri" onclick="resCreateGo()">Create</button><button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`, true);
};
window.resCreateGo = async () => {
  const ns = $("#rc_ns").value.trim();
  if (!ns) return toast("Type the namespace to use", "bad");
  try { const r = await resPost("/api/resources/create", { yaml: $("#rc_yaml").value, ns }); toast(r.detail, "ok"); closeModal(); viewResources(); }
  catch (e) { toast(e.message, "bad"); }
};
