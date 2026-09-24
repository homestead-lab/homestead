/* Helm: every release in the cluster, and charts installed through RKE2's
   Helm controller. Releases made some other way are shown, not changed. */

const HELM_MANAGED = { homestead: ["installed here", "ok", "Installed from Homestead as a HelmChart: it can be upgraded and uninstalled here"],
  helmchart: ["HelmChart", "info", "Managed by a HelmChart object, which Homestead can change"] };

function helmStatus(status) {
  const tone = status === "deployed" ? "ok" : /pending/.test(status) ? "med" : status === "superseded" ? "neutral" : "crit";
  return `<span class="pill slim ${tone}">${esc(status || "unknown")}</span>`;
}

async function viewHelm() {
  STATE.data.helm = await api("/api/helm");
  renderHelm();
}
window.viewHelm = viewHelm;
window.helmToggleSystem = () => { STATE.helmSystem = !STATE.helmSystem; renderHelm(); };

function renderHelm() {
  const all = STATE.data.helm || [], q = STATE.q.toLowerCase();
  const rows = all.filter(r => (STATE.helmSystem || !r.system) &&
    (!q || [r.name, r.namespace, r.chart, r.chart_version, r.app_version].join(" ").toLowerCase().includes(q)));
  const hidden = all.filter(r => r.system).length;
  paint(`<div class="phead"><div><h2>Helm</h2><p>${rows.length} release${rows.length === 1 ? "" : "s"}${!STATE.helmSystem && hidden ? ` · ${hidden} of the platform's hidden` : ""} · charts from here install through RKE2's Helm controller</p></div>
      <div class="row"><button class="btn" onclick="helmToggleSystem()">${STATE.helmSystem ? "Hide" : "Show"} platform</button>
      <button class="btn pri" data-need="admin" onclick="helmInstall()">＋ Install chart</button></div></div>
    ${rows.length ? `<div class="card flat pad0"><div class="tblwrap"><table class="tbl dense stack" data-sort="helm"><thead><tr>
      <th>Release</th><th>Chart</th><th>App</th><th>Status</th><th>Revision</th><th data-nosort>Updated</th></tr></thead><tbody>
      ${rows.map(r => `<tr class="clickable" onclick="helmRelease('${esc(r.namespace)}','${esc(r.name)}')">
        <td><div class="row nowrap" style="gap:9px">${appAvatar(r.name, r.icon && /^https:/.test(r.icon) ? r.icon : "")}<div><b>${esc(r.name)}</b>
          ${HELM_MANAGED[r.managed] ? `<span class="tag ${HELM_MANAGED[r.managed][1]}" data-tip="${esc(HELM_MANAGED[r.managed][2])}">${esc(HELM_MANAGED[r.managed][0])}</span>` : ""}
          <div class="dim xs mono">${esc(r.namespace)}</div></div></div></td>
        <td data-label="Chart" class="mono small">${esc(r.chart)}${r.chart_version ? `-${esc(r.chart_version)}` : ""}</td>
        <td data-label="App" class="mono small">${esc(r.app_version || "—")}</td>
        <td data-label="Status" data-sort="${esc(r.status)}">${helmStatus(r.status)}</td>
        <td data-label="Revision" class="mono">${r.revision || "—"}</td>
        <td data-label="Updated" class="small dim">${r.updated ? esc(fmtAgo(Math.max(0, (Date.now() - Date.parse(r.updated)) / 1000))) : "—"}</td></tr>`).join("")}
      </tbody></table></div></div>`
      : `<div class="empty">${q ? "Nothing matches that search." : `No Helm releases${hidden ? " outside the platform's own" : ""} yet. <a style="cursor:pointer;text-decoration:underline" onclick="helmInstall()">Install a chart</a> from Artifact Hub.`}</div>`}`);
  loadAppIcons(V());
}

window.helmRelease = async (ns, name) => {
  modal(`Helm · ${name}`, `<div class="empty"><span class="spin2"></span>loading</div>`, true);
  try {
    const r = await api(`/api/helm/release?ns=${encodeURIComponent(ns)}&name=${encodeURIComponent(name)}`);
    const editable = !!r.source;
    $("#mbody").innerHTML = `<div class="between"><div><b>${esc(r.chart)} ${esc(r.chart_version)}</b> <span class="dim small">app ${esc(r.app_version || "—")}</span>
        <div class="dim xs">${esc(r.description || "")}</div></div><div>${helmStatus(r.status)} <span class="pill slim neutral">revision ${r.revision}</span></div></div>
      ${editable ? `<div class="note" style="margin-top:10px">From <span class="mono">${esc(r.source.repo || r.source.chart)}</span> through a HelmChart: change the version or values and the Helm controller upgrades it.</div>`
        : `<div class="note" style="margin-top:10px">Installed some other way${r.system ? " - it is part of the platform" : ""}. Homestead shows it but does not change it, since whatever installed it would change it back.</div>`}
      <div class="seg" style="margin:12px 0" role="tablist">${["values", "notes", "history", "objects"].map((t, i) =>
        `<button class="${i ? "" : "on"}" onclick="helmTab(this,'${t}')">${{ values: "Values", notes: "Notes", history: "History", objects: `Objects · ${r.objects.length}` }[t]}</button>`).join("")}</div>
      <div class="helm-pane" data-pane="values">
        ${editable ? `<div class="f2"><div class="f"><label>Chart version</label><input id="hr_version" class="mono" value="${esc(r.source.version || "")}" placeholder="latest"></div><div></div></div>
          <label>Values ${tip("Only what differs from the chart's defaults. Saved as the HelmChart's valuesContent.")}</label>
          <textarea id="hr_values" class="mono helm-values" rows="16" spellcheck="false">${esc(r.source.values || "")}</textarea>
          <div class="row" style="margin-top:10px"><button class="btn pri" data-need="admin" onclick="helmUpgrade('${esc(ns)}','${esc(name)}')">Upgrade</button>
            <button class="btn danger" data-need="admin" onclick="helmUninstall('${esc(ns)}','${esc(name)}')">Uninstall</button></div>`
        : `<pre class="mono helm-values">${esc(r.values || "# installed with the chart's defaults")}</pre>`}</div>
      <div class="helm-pane" data-pane="notes" hidden><pre class="helm-values">${esc(r.notes || "The chart left no notes.")}</pre></div>
      <div class="helm-pane" data-pane="history" hidden><table class="tbl dense stack"><thead><tr><th>Revision</th><th>Status</th><th>Chart</th><th>Updated</th><th>Description</th></tr></thead><tbody>
        ${r.history.map(h => `<tr><td class="mono">${h.revision}</td><td>${helmStatus(h.status)}</td><td class="mono small">${esc(h.chart_version)}</td>
          <td class="small dim">${esc((h.updated || "").replace("T", " ").slice(0, 16))}</td><td class="small">${esc(h.description)}</td></tr>`).join("")}</tbody></table></div>
      <div class="helm-pane" data-pane="objects" hidden><table class="tbl dense stack"><thead><tr><th>Kind</th><th>Name</th><th>Namespace</th></tr></thead><tbody>
        ${r.objects.map(o => `<tr><td>${esc(o.kind)}</td><td class="mono small">${esc(o.name)}</td><td class="mono small dim">${esc(o.namespace || r.namespace)}</td></tr>`).join("")}</tbody></table></div>`;
    if (window.applyRole) applyRole();
  } catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};
window.helmTab = (button, pane) => {
  $$("#mbody .seg button").forEach(b => b.classList.toggle("on", b === button));
  $$("#mbody .helm-pane").forEach(p => { p.hidden = p.dataset.pane !== pane; });
};
const helmPost = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
window.helmUpgrade = async (namespace, name) => {
  try { const r = await helmPost("/api/helm/upgrade", { namespace, name, version: $("#hr_version").value.trim(), values: $("#hr_values").value });
    toast(r.detail, "ok"); closeModal(); setTimeout(viewHelm, 1500); } catch (e) { toast(e.message, "bad"); }
};
window.helmUninstall = async (namespace, name) => {
  if (!confirm(`Uninstall ${name} from ${namespace}?\n\nThe Helm controller removes what the chart made. Volumes the chart marked to keep are left.`)) return;
  try { const r = await helmPost("/api/helm/uninstall", { namespace, name }); toast(r.detail, "ok"); closeModal(); setTimeout(viewHelm, 1500); }
  catch (e) { toast(e.message, "bad"); }
};

/* ---------------- install ---------------- */
window.helmInstall = async () => {
  let namespaces = [];
  try { namespaces = await api("/api/namespaces"); } catch (e) { /* type one */ }
  modal("Install a chart", `<div class="f"><label>Find a chart ${tip("Searches Artifact Hub, the public index of Helm charts.")}</label>
      <input id="hi_q" placeholder="e.g. grafana, cert-manager, immich" oninput="helmSearchSoon()"></div>
    <div id="hi_results" class="helm-results"></div>
    <div id="hi_form" hidden>
      <div class="f2"><div class="f"><label>Repository</label><input id="hi_repo" class="mono" placeholder="https://charts.example.com"></div>
        <div class="f"><label>Chart</label><input id="hi_chart" class="mono"></div></div>
      <div class="f2"><div class="f"><label>Version</label><select id="hi_version"></select></div>
        <div class="f"><label>Release name</label><input id="hi_name" class="mono"></div></div>
      <div class="f"><label>Namespace</label><input id="hi_ns" class="mono" list="hi_ns_list" value="${esc(namespaces.includes("lab") ? "lab" : namespaces[0] || "default")}">
        <datalist id="hi_ns_list">${namespaces.map(n => `<option value="${esc(n)}">`).join("")}</datalist></div>
      <div class="f"><label>Values ${tip("Only what you change from the chart's defaults. The defaults are shown on the right to copy from.")}</label>
        <div class="helm-values-pair"><textarea id="hi_values" class="mono helm-values" rows="14" spellcheck="false" placeholder="# e.g.\n# persistence:\n#   enabled: true"></textarea>
        <pre id="hi_defaults" class="mono helm-values dim" title="The chart's defaults"></pre></div></div>
      <div class="row"><button class="btn pri" data-need="admin" onclick="helmInstallGo()">Install</button><button class="btn" onclick="closeModal()">Cancel</button>
        <a class="btn" id="hi_readme" target="_blank" rel="noopener noreferrer" hidden>${icon("ext")}Chart page</a></div></div>
    <div class="dim xs" style="margin-top:8px"><a style="cursor:pointer;text-decoration:underline" onclick="helmManual()">Or enter a repository and chart by hand</a></div>`, true);
  setTimeout(() => $("#hi_q")?.focus(), 30);
};
let helmSearchTimer = 0;
window.helmSearchSoon = () => { clearTimeout(helmSearchTimer); helmSearchTimer = setTimeout(helmSearch, 350); };
async function helmSearch() {
  const q = $("#hi_q").value.trim(), host = $("#hi_results");
  if (q.length < 2) { host.innerHTML = ""; return; }
  host.innerHTML = '<div class="dim small"><span class="spin2"></span> searching Artifact Hub</div>';
  try {
    const rows = STATE.data.helmResults = await api(`/api/helm/search?q=${encodeURIComponent(q)}`);
    host.innerHTML = rows.map((r, i) => `<button type="button" class="helm-hit card flat" onclick="helmPick(${i})">
        ${r.logo ? `<img src="${esc(r.logo)}" alt="" loading="lazy">` : `<span class="helm-hit-blank">${esc(r.name.slice(0, 2).toUpperCase())}</span>`}
        <span><b>${esc(r.name)}</b> <span class="dim xs">${esc(r.version)}</span> ${r.official ? '<span class="tag ok">official</span>' : r.verified ? '<span class="tag info">verified</span>' : ""}
          <span class="dim xs"> · ${esc(r.publisher || r.repo_name)}</span><div class="dim xs">${esc(r.description)}</div></span></button>`).join("")
      || '<div class="dim small">No charts found.</div>';
  } catch (e) { host.innerHTML = `<div class="note bad">${esc(e.message)}</div>`; }
}
window.helmPick = async i => {
  const r = STATE.data.helmResults[i];
  $("#hi_results").innerHTML = "";
  $("#hi_form").hidden = false;
  $("#hi_repo").value = r.repo; $("#hi_chart").value = r.name; $("#hi_name").value = r.name.toLowerCase().replace(/[^a-z0-9-]/g, "-").slice(0, 53);
  $("#hi_version").innerHTML = `<option value="${esc(r.version)}">${esc(r.version)}</option>`;
  $("#hi_defaults").textContent = "loading the chart's defaults…";
  try {
    const c = await api(`/api/helm/chart?repo=${encodeURIComponent(r.repo_name)}&name=${encodeURIComponent(r.name)}`);
    $("#hi_version").innerHTML = c.versions.map(v => `<option ${v === c.version ? "selected" : ""}>${esc(v)}</option>`).join("");
    $("#hi_defaults").textContent = c.values || "# the chart publishes no default values";
    $("#hi_readme").href = c.readme_url; $("#hi_readme").hidden = false;
  } catch (e) { $("#hi_defaults").textContent = "# defaults unavailable: " + e.message; }
};
window.helmManual = () => {
  $("#hi_form").hidden = false;
  $("#hi_version").outerHTML = '<input id="hi_version" class="mono" placeholder="latest">';
  $("#hi_defaults").textContent = "# enter the repository and chart; defaults are shown for charts found by search";
};
window.helmInstallGo = async () => {
  const body = { repo: $("#hi_repo").value.trim(), chart: $("#hi_chart").value.trim(), version: $("#hi_version").value.trim(),
    name: $("#hi_name").value.trim(), namespace: $("#hi_ns").value.trim(), values: $("#hi_values").value };
  try { const r = await helmPost("/api/helm/install", body); toast(r.detail, "ok"); closeModal(); setTimeout(viewHelm, 1500); }
  catch (e) { toast(e.message, "bad"); }
};
