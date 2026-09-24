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
  if (!STATE.data.namespacesAll) { try { STATE.data.namespacesAll = (await api("/api/resources/list?group=&version=v1&resource=namespaces")).rows.map(r => r.name); } catch (e) { STATE.data.namespacesAll = []; } }
  renderResources();
}
window.viewResources = viewResources;

function renderResources() {
  const list = STATE.data.resList || { columns: [], rows: [] }, k = RES.pick, q = STATE.q.toLowerCase();
  const rows = list.rows.filter(r => !q || [r.name, r.namespace, ...r.cells].join(" ").toLowerCase().includes(q));
  const filter = RES.q.toLowerCase();
  const kinds = RES.kinds.filter(x => !filter || [x.kind, x.resource, x.group, ...x.short].join(" ").toLowerCase().includes(filter));
  const groups = [...new Set(kinds.map(x => x.category))];
  paint(`<div class="phead"><div><h2>Resources</h2><p>Every kind this cluster serves, ${RES.kinds.length} of them · the columns are the API server's own</p></div>
      <div class="row"><button class="btn pri" data-need="admin" onclick="resCreate()">＋ Create from YAML</button></div></div>
    <div class="res-layout">
      <aside class="res-kinds card flat">
        <input class="res-filter" placeholder="Find a kind" value="${esc(RES.q)}" oninput="resFilter(this.value)">
        <select class="res-kind-select" onchange="resPick(this.value)">${RES.kinds.map(x => `<option value="${esc(resKey(x))}" ${x === k ? "selected" : ""}>${esc(x.category)} · ${esc(resLabel(x))}</option>`).join("")}</select>
        <div class="res-kind-list">${groups.map(g => `<div class="res-group">${esc(g)}</div>${kinds.filter(x => x.category === g).map(x =>
          `<button class="res-kind ${x === k ? "on" : ""}" onclick="resPick('${esc(resKey(x))}')" title="${esc(x.group || "core")}/${esc(x.version)} · ${esc(x.resource)}">${esc(resLabel(x))}${x.group && x.category === "Custom resources" ? `<span class="dim xs"> ${esc(x.group)}</span>` : ""}</button>`).join("")}`).join("")}</div>
      </aside>
      <section class="res-main">
        <div class="between res-head"><div><b>${esc(resLabel(k))}</b> <span class="dim xs mono">${esc(k.group || "core")}/${esc(k.version)}</span>
            <span class="dim xs"> · ${rows.length}${list.more ? "+" : ""} shown</span></div>
          ${k.namespaced ? `<select onchange="resNamespace(this.value)"><option value="">All namespaces</option>${(STATE.data.namespacesAll || []).map(n =>
            `<option ${n === RES.ns ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>` : '<span class="dim xs">cluster-wide</span>'}</div>
        ${list.error ? `<div class="note bad">${esc(list.error)}</div>` : ""}
        <div class="card flat pad0"><div class="tblwrap"><table class="tbl dense stack" data-sort="res-${esc(k.resource)}"><thead><tr>
          ${k.namespaced && !RES.ns ? "<th>Namespace</th>" : ""}${list.columns.map(c => `<th title="${esc(c.description)}">${esc(c.name)}</th>`).join("")}</tr></thead><tbody>
          ${rows.map(r => `<tr class="clickable" onclick="resOpen('${esc(r.namespace)}','${esc(r.name)}')">
            ${k.namespaced && !RES.ns ? `<td class="mono small dim" data-label="Namespace">${esc(r.namespace)}</td>` : ""}
            ${r.cells.map((c, i) => `<td data-label="${esc(list.columns[i]?.name || "")}" class="${i === 0 ? "mono small" : "small"}">${i === 0 ? `<b>${esc(String(c))}</b>` : esc(typeof c === "object" ? JSON.stringify(c) : String(c ?? ""))}</td>`).join("")}</tr>`).join("")
            || `<tr><td colspan="${list.columns.length + 1}" class="empty">None${RES.ns ? ` in ${esc(RES.ns)}` : ""}.</td></tr>`}</tbody></table></div></div>
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
  catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  const pod = k.group === "" && k.resource === "pods";
  $("#mbody").innerHTML = `<div class="between"><div class="dim xs mono">${esc(k.group || "core")}/${esc(k.version)} · ${esc(k.kind)}${ns ? ` · ${esc(ns)}` : ""}</div>
      <div class="row">${pod ? `<button class="btn sm" onclick="resLogs('${esc(ns)}','${esc(name)}')">${icon("log")}Logs</button>` : ""}
        ${o.secret_hidden ? `<button class="btn sm" data-need="admin" onclick="resOpen('${esc(ns)}','${esc(name)}',true)">Reveal values</button>` : ""}
        <button class="btn sm" data-need="admin" id="res_edit" onclick="resEdit()">${icon("edit")}Edit</button>
        <button class="btn sm danger" data-need="admin" onclick="resDelete('${esc(ns)}','${esc(name)}')">${icon("trash")}Delete</button></div></div>
    <div class="seg" style="margin:10px 0">${["YAML", "Events"].map((t, i) => `<button class="${i ? "" : "on"}" onclick="resTab(this,'${t}')">${t}</button>`).join("")}</div>
    <div class="res-pane" data-pane="YAML"><textarea id="res_yaml" class="mono helm-values res-yaml" spellcheck="false" readonly>${esc(o.yaml)}</textarea>
      <div class="row" id="res_save_row" hidden style="margin-top:8px"><button class="btn pri" onclick="resSave('${esc(ns)}','${esc(name)}')">Save</button>
        <button class="btn" onclick="resOpen('${esc(ns)}','${esc(name)}',${reveal})">Cancel</button>
        <span class="dim xs">Saved as a replace; if someone changed it meanwhile, the save is refused rather than overwrite them.</span></div></div>
    <div class="res-pane" data-pane="Events" hidden><div id="res_events" class="dim small">loading</div></div>`;
  if (window.applyRole) applyRole();
  const uid = (o.object.metadata || {}).uid || "";
  api(`/api/resources/events?${new URLSearchParams({ ns, name, uid })}`).then(events => {
    const host = $("#res_events");
    if (host) host.innerHTML = events.length ? `<table class="tbl dense stack"><thead><tr><th>Type</th><th>Reason</th><th>Message</th><th>Count</th><th>Last</th></tr></thead><tbody>
      ${events.map(e => `<tr><td><span class="pill slim ${e.type === "Warning" ? "med" : "neutral"}">${esc(e.type)}</span></td><td class="small">${esc(e.reason)}</td>
        <td class="small">${esc(e.message)}</td><td class="mono">${e.count}</td><td class="small dim">${esc((e.last || "").replace("T", " ").slice(0, 16))}</td></tr>`).join("")}</tbody></table>`
      : "No recent events.";
  }).catch(() => {});
};
window.resTab = (button, pane) => {
  $$("#mbody .seg button").forEach(b => b.classList.toggle("on", b === button));
  $$("#mbody .res-pane").forEach(p => { p.hidden = p.dataset.pane !== pane; });
};
window.resEdit = () => { const t = $("#res_yaml"); t.readOnly = false; t.focus(); $("#res_save_row").hidden = false; $("#res_edit").hidden = true; };
const resPost = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
window.resSave = async (ns, name) => {
  const k = RES.pick;
  try {
    const r = await resPost("/api/resources/save", { group: k.group, version: k.version, resource: k.resource, ns, name, yaml: $("#res_yaml").value });
    toast(r.detail, "ok"); resOpen(ns, name);
  } catch (e) { toast(e.message, "bad"); }
};
window.resDelete = async (ns, name) => {
  const k = RES.pick;
  const typed = prompt(`Delete ${k.kind} ${name}${ns ? ` in ${ns}` : ""}? Whatever controls it may make it again; whatever depends on it may stop.\n\nType its name to delete it:`);
  if (typed !== name) { if (typed !== null) toast("The name did not match; nothing was deleted", "bad"); return; }
  try { const r = await resPost("/api/resources/delete", { group: k.group, version: k.version, resource: k.resource, ns, name });
    toast(r.detail, "ok"); closeModal(); viewResources(); } catch (e) { toast(e.message, "bad"); }
};
window.resLogs = (ns, pod) => { closeModal(); openLogs(pod, `/api/logs?ns=${encodeURIComponent(ns)}&pod=${encodeURIComponent(pod)}`); };
window.resCreate = () => {
  modal("Create from YAML", `<p class="muted small">One object or several, separated by <span class="mono">---</span>, created in order. Objects without a namespace go to the one chosen here.</p>
    <div class="f"><label>Namespace</label><select id="rc_ns">${(STATE.data.namespacesAll || ["default"]).map(n => `<option ${n === (RES.ns || "lab") ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div>
    <textarea id="rc_yaml" class="mono helm-values res-yaml" spellcheck="false" placeholder="apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: example\ndata:\n  hello: world"></textarea>
    <div class="row" style="margin-top:10px"><button class="btn pri" onclick="resCreateGo()">Create</button><button class="btn" onclick="closeModal()">Cancel</button></div>`, true);
};
window.resCreateGo = async () => {
  try { const r = await resPost("/api/resources/create", { yaml: $("#rc_yaml").value, ns: $("#rc_ns").value }); toast(r.detail, "ok"); closeModal(); viewResources(); }
  catch (e) { toast(e.message, "bad"); }
};
