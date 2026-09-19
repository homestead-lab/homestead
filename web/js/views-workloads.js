/* Containers, Deploy, App Store */

async function viewWorkloads() { STATE.data.wl = await api("/api/workloads"); renderWorkloads(); }

function renderWorkloads() {
  const q = STATE.q.toLowerCase();
  const rows = (STATE.data.wl || []).filter(x => !q || x.name.includes(q) || x.ns.includes(q) ||
    x.images.join(" ").toLowerCase().includes(q) || x.nodes.join(" ").includes(q));
  paint(`<div class="phead">
      <div><h2>Containers</h2><p>${rows.length} workload${rows.length === 1 ? "" : "s"}${q ? ` matching “${esc(q)}”` : ""} · Harvester system pods hidden</p></div>
      <button class="btn pri hide-sm" onclick="go('deploy')">＋ Deploy</button></div>

    <div class="cardlist">${rows.map(w => {
      const ok = w.ready === w.desired && w.desired > 0, off = w.desired === 0;
      return `<div class="wcard card flat">
        <div class="between">
          <div class="row" style="gap:10px;min-width:0">
            <div class="av">${esc(w.name.slice(0, 2).toUpperCase())}</div>
            <div style="min-width:0"><div style="font-weight:680">${esc(w.name)}</div>
              <div class="dim xs">${esc(w.ns)} · ${esc(w.nodes.join(", ") || "unscheduled")}</div></div>
          </div>
          <span class="pill ${ok ? "ok" : off ? "low" : "crit"}">${w.ready}/${w.desired}</span>
        </div>
        <div class="wmeta">
          <div><div class="dim xs">UPTIME</div>${w.uptime ? upChip(w.uptime) : '<span class="dim">—</span>'}</div>
          <div><div class="dim xs">CPU</div><div class="mono small">${w.cpu} <span class="dim">cores</span></div></div>
          <div><div class="dim xs">RAM</div><div class="mono small">${w.mem_mb} <span class="dim">MB</span></div></div>
          <div><div class="dim xs">ACCESS</div><div>${w.ports.map(p => p.ip
            ? `<span class="plink" title="Open ${esc(svcUrl(p.ip, p.port))}" onclick="openSvc('${esc(p.ip)}',${p.port})">${p.port}<svg class="ext" width="9" height="9"><use href="#i-ext"/></svg></span>`
            : `<span class="tag">${p.port}</span>`).join("") || '<span class="dim">—</span>'}</div></div>
        </div>
        <div class="dim xs mono wimg">${w.images.map(esc).join(" · ")}${w.gpu ? ' <span class="tag gpu">iGPU</span>' : ""}</div>
        <div class="row wacts">
          <button class="btn sm" onclick="wlLogs('${w.ns}','${w.pods[0] ? w.pods[0].name : ""}')">Logs</button>
          <button class="btn sm" onclick="wlEdit('${w.ns}','${w.name}')">Edit</button>
          <button class="btn sm" onclick="wlMove('${w.ns}','${w.name}')">Move</button>
          <button class="btn sm" onclick="wlRestart('${w.ns}','${w.name}')">Restart</button>
          ${off ? `<button class="btn sm" onclick="wlScale('${w.ns}','${w.name}',1)">Start</button>`
                : `<button class="btn sm" onclick="wlScale('${w.ns}','${w.name}',0)">Stop</button>`}
          <button class="btn sm danger" onclick="wlDelete('${w.ns}','${w.name}')">Delete</button>
        </div></div>`;
    }).join("") || `<div class="empty">nothing here yet</div>`}</div>`);
}

window.wlScale = async (ns, name, n) => {
  try {
    await api("/api/scale", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name, replicas: n }) });
    toast(`${name} ${n ? "started" : "stopped"}`, "ok"); setTimeout(() => refresh(true), 900);
  } catch (e) { toast(e.message, "bad"); }
};
window.wlRestart = async (ns, name) => {
  try {
    await api("/api/restart", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name }) });
    toast(`${name} restarting`, "ok"); setTimeout(() => refresh(true), 1300);
  } catch (e) { toast(e.message, "bad"); }
};
window.wlDelete = async (ns, name) => {
  if (!confirm(`Delete "${name}" in ${ns}?\n\nRemoves the Deployment and its Service.\nPersistent volumes are kept.`)) return;
  try { await api(`/api/workload/${ns}/${name}`, { method: "DELETE" });
    toast(`${name} deleted`, "ok"); setTimeout(() => refresh(true), 900);
  } catch (e) { toast(e.message, "bad"); }
};
window.wlLogs = async (ns, pod) => {
  if (!pod) return toast("no running pod", "bad");
  modal("Logs · " + pod, `<div class="empty"><span class="spin2"></span>loading</div>`, true);
  try { const t = await api(`/api/logs?ns=${ns}&pod=${pod}`);
    $("#mbody").innerHTML = `<pre>${esc(t) || "(empty)"}</pre>`;
    const pre = $("#mbody pre"); pre.scrollTop = pre.scrollHeight;
  } catch (e) { $("#mbody").innerHTML = `<pre>${esc(e.message)}</pre>`; }
};

/* ---------------- deploy ---------------- */
let DCFG = { name: "", image: "", namespace: "lab", replicas: 1, cpu: "50m", memory: "128Mi",
             ports: [], env: {}, volumes: [], gpu: false };
async function viewDeploy(pre) {
  if (pre) DCFG = Object.assign({ namespace: "lab", replicas: 1, cpu: "50m", memory: "128Mi",
    ports: [], env: {}, volumes: [], gpu: false }, pre);
  const nss = await api("/api/namespaces").catch(() => ["lab"]);
  resetPaint();
  paint(`<div class="phead"><div><h2>Deploy a container</h2>
      <p>Point at any Docker image, map ports and storage — HarvUI builds the Kubernetes objects</p></div></div>
  <div class="split">
    <div class="card flat">
      <div class="f"><label>Name</label><input type="text" id="d_name" value="${esc(DCFG.name)}" placeholder="my-app"></div>
      <div class="f"><label>Docker image</label><input type="text" id="d_image" value="${esc(DCFG.image)}" placeholder="nginx:alpine · ghcr.io/user/app:tag"></div>
      <div class="f2">
        <div class="f"><label>Namespace</label><select id="d_ns">${nss.map(n => `<option ${n === DCFG.namespace ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div>
        <div class="f"><label>Replicas</label><input type="number" id="d_rep" value="${DCFG.replicas}" min="0" max="5"></div>
      </div>
      <div class="f2">
        <div class="f"><label>CPU request</label><input type="text" id="d_cpu" value="${esc(DCFG.cpu)}"></div>
        <div class="f"><label>Memory request</label><input type="text" id="d_mem" value="${esc(DCFG.memory)}"></div>
      </div>
      <label class="switch"><input type="checkbox" id="d_gpu" ${DCFG.gpu ? "checked" : ""}> Give this container the Intel iGPU <span class="tag gpu">/dev/dri</span></label>
      <div class="sec">Ports</div><div id="d_ports"></div><button class="btn sm" onclick="addPort()">＋ add port</button>
      <div class="sec">Storage</div><div id="d_vols"></div><button class="btn sm" onclick="addVol()">＋ add volume</button>
      <div class="sec">Environment</div><div id="d_env"></div><button class="btn sm" onclick="addEnv()">＋ add variable</button>
      <div class="row" style="margin-top:24px">
        <button class="btn pri" onclick="doDeploy()">Deploy container</button>
        <button class="btn" onclick="previewYaml()">Preview manifest</button>
      </div>
    </div>
    <div class="card flat"><div class="ctitle">Configuration</div><div class="csub">Live summary</div>
      <div id="d_summary" style="margin-top:14px"></div>
      <button class="btn pri wide" style="margin-top:18px" onclick="doDeploy()">Deploy</button></div>
  </div>`);
  renderPorts(); renderVols(); renderEnv(); syncSummary();
  ["d_name", "d_image", "d_ns", "d_rep", "d_cpu", "d_mem", "d_gpu"].forEach(id => {
    const el = $("#" + id); if (!el) return;
    el.addEventListener("input", syncSummary); el.addEventListener("change", syncSummary);
  });
}
function collect() {
  DCFG.name = $("#d_name").value.trim(); DCFG.image = $("#d_image").value.trim();
  DCFG.namespace = $("#d_ns").value; DCFG.replicas = +$("#d_rep").value;
  DCFG.cpu = $("#d_cpu").value.trim(); DCFG.memory = $("#d_mem").value.trim(); DCFG.gpu = $("#d_gpu").checked;
  DCFG.ports = $$("#d_ports .f3").map(r => ({ container: +$(".pc", r).value,
    host: +$(".ph", r).value || +$(".pc", r).value, expose: $(".pe", r).checked }));
  DCFG.volumes = $$("#d_vols .f3").map(r => ({ path: $(".vp", r).value.trim(), source: $(".vs", r).value.trim(),
    type: $(".vt", r).value, size_gb: 5, create: $(".vt", r).value === "pvc" }));
  DCFG.env = {}; $$("#d_env .f3").forEach(r => { const k = $(".ek", r).value.trim(); if (k) DCFG.env[k] = $(".ev", r).value; });
  return DCFG;
}
function syncSummary() {
  const c = collect();
  const row = (i, l, v) => `<div class="drow"><div class="di">${i}</div><div class="dl">${l}</div><div class="dv">${v}</div></div>`;
  $("#d_summary").innerHTML =
    row("◈", "Name", c.name ? `<b>${esc(c.name)}</b>` : '<span class="dim">—</span>') +
    row("❏", "Image", c.image ? `<span class="small mono">${esc(c.image)}</span>` : '<span class="dim">—</span>') +
    row("⌗", "Namespace", esc(c.namespace)) + row("⧉", "Replicas", c.replicas) +
    row("◴", "Requests", `<span class="small mono">${esc(c.cpu)} · ${esc(c.memory)}</span>`) +
    row("▤", "iGPU", c.gpu ? '<span class="tag gpu">enabled</span>' : '<span class="dim">no</span>') +
    row("⇄", "Ports", c.ports.length ? c.ports.map(p => `<span class="tag ${p.expose ? "info" : ""}">${p.host}→${p.container}</span>`).join("") : '<span class="dim">—</span>') +
    row("▥", "Storage", c.volumes.length ? c.volumes.map(v => `<span class="tag">${esc(v.source || "?")}</span>`).join("") : '<span class="dim">—</span>') +
    row("≡", "Env vars", Object.keys(c.env).length ? `<span class="tag">${Object.keys(c.env).length} set</span>` : '<span class="dim">—</span>');
}
function addPort(cp = "", hp = "", ex = true) {
  const d = document.createElement("div"); d.className = "f3";
  d.innerHTML = `<div><label>Container port</label><input class="pc" type="number" value="${cp}"></div>
    <div><label>Exposed port</label><input class="ph" type="number" value="${hp}"></div>
    <label class="switch" style="margin:0 0 10px"><input class="pe" type="checkbox" ${ex ? "checked" : ""}>LB</label>`;
  $("#d_ports").appendChild(d); d.addEventListener("input", syncSummary); syncSummary();
}
function addVol(path = "", src = "", type = "pvc") {
  const d = document.createElement("div"); d.className = "f3";
  d.innerHTML = `<div><label>Mount path</label><input class="vp" type="text" value="${esc(path)}" placeholder="/data"></div>
    <div><label>Source</label><input class="vs" type="text" value="${esc(src)}" placeholder="volume name"></div>
    <div><label>Type</label><select class="vt"><option value="pvc" ${type === "pvc" ? "selected" : ""}>New volume</option>
      <option value="host" ${type === "host" ? "selected" : ""}>Host path</option></select></div>`;
  $("#d_vols").appendChild(d); d.addEventListener("input", syncSummary); syncSummary();
}
function addEnv(k = "", v = "") {
  const d = document.createElement("div"); d.className = "f3";
  d.innerHTML = `<div><label>Key</label><input class="ek" type="text" value="${esc(k)}"></div>
    <div><label>Value</label><input class="ev" type="text" value="${esc(v)}"></div><div></div>`;
  $("#d_env").appendChild(d); d.addEventListener("input", syncSummary); syncSummary();
}
function renderPorts() { $("#d_ports").innerHTML = ""; (DCFG.ports || []).forEach(p => addPort(p.container, p.host, p.expose !== false)); }
function renderVols() { $("#d_vols").innerHTML = ""; (DCFG.volumes || []).forEach(v => addVol(v.path, v.source, v.type)); }
function renderEnv() { $("#d_env").innerHTML = ""; Object.entries(DCFG.env || {}).forEach(([k, v]) => addEnv(k, v)); }
window.addPort = addPort; window.addVol = addVol; window.addEnv = addEnv;
window.previewYaml = async () => {
  try { const r = await api("/api/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(collect()) });
    modal("Manifest preview", `<pre>${esc(JSON.stringify(r, null, 2))}</pre>`, true); } catch (e) { toast(e.message, "bad"); }
};
window.doDeploy = async () => {
  const c = collect();
  if (!c.name || !c.image) return toast("name and image are required", "bad");
  try { await api("/api/deploy", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(c) });
    toast(`${c.name} deployed`, "ok"); go("workloads"); } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- app store ---------------- */
async function viewStore() {
  resetPaint();
  paint(`<div class="phead">
      <div><h2>App Store</h2><p>Unraid Community Applications catalogue, deployed as Kubernetes workloads</p></div>
      <div class="row"><input class="search" id="s_q" placeholder="plex, nextcloud, jellyfin…" value="${esc(STATE.q)}" style="width:260px;padding-left:16px">
      <button class="btn pri" onclick="storeSearch()">Search</button></div></div>
    <div id="s_res"><div class="empty">Search the catalogue — it is fetched live from the Unraid CA feed.</div></div>`);
  $("#s_q").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); storeSearch(); } });
  if (STATE.q) storeSearch();
}
window.storeSearch = async () => {
  const q = $("#s_q").value.trim();
  $("#s_res").innerHTML = `<div class="empty"><span class="spin2"></span>searching catalogue…</div>`;
  try {
    const r = await api("/api/appstore?q=" + encodeURIComponent(q));
    STATE.data.apps = r.apps;
    $("#s_res").innerHTML = r.apps.length
      ? `<div class="dim small" style="margin-bottom:12px">${r.total} match${r.total === 1 ? "" : "es"} · showing ${r.apps.length}</div>
        <div class="apps stagger">${r.apps.map((a, i) => `<div class="card app">
          <div class="row" style="gap:11px">${a.icon ? `<img class="ico" src="${esc(a.icon)}" onerror="this.style.display='none'">` : ""}
            <div style="min-width:0"><div class="nm">${esc(a.name)}</div>
            ${a.cat ? `<span class="tag">${esc(a.cat.split(" ")[0])}</span>` : ""}</div></div>
          <div class="ds">${esc(a.desc || "No description provided.")}</div>
          <div class="rp">${esc(a.repo)}</div>
          <button class="btn pri wide" onclick="storeInstall(${i})">Configure &amp; deploy</button></div>`).join("")}</div>`
      : `<div class="empty">nothing matched “${esc(q)}”</div>`;
  } catch (e) { $("#s_res").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};
window.storeInstall = i => {
  const a = STATE.data.apps[i], at = c => c["@attributes"] || c;
  const cf = (a.config || []).filter(c => c && typeof c === "object");
  const ports = cf.filter(c => at(c).Type === "Port")
    .map(c => ({ container: +at(c).Target, host: +at(c).Target, expose: true })).filter(p => p.container);
  const env = {}; cf.filter(c => at(c).Type === "Variable")
    .forEach(c => { if (at(c).Target) env[at(c).Target] = c.value || at(c).Default || ""; });
  const name = a.name.toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
  const vols = cf.filter(c => at(c).Type === "Path")
    .map((c, k) => ({ path: at(c).Target, source: `${name}-data${k || ""}`, type: "pvc" })).filter(v => v.path);
  STATE.view = "deploy";
  $$("#nav a").forEach(x => x.classList.toggle("on", x.dataset.view === "deploy"));
  $("#title").textContent = "Deploy"; $("#crumb").textContent = "workloads";
  viewDeploy({ name, image: a.repo, ports, env, volumes: vols });
  toast(`"${a.name}" loaded — check storage paths before deploying`);
};
