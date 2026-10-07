/* Whether each app answers at its address: checked every minute by the
   leading replica (homestead_uptime.py). Called Monitoring on screen, since
   a card's Uptime is how long its pod has run.

   A row says nothing while an app is up, and "Down" or "Slow" when
   it does not; a card shows the last day hour by hour and the last 30 days as
   a share; the Monitoring dialog shows what is asked and lets an operator change
   it: automatic, an HTTP path, a TCP connection, or no check. */
const ANSWER_WORDS = { up: "Up", slow: "Slow", down: "Down", unknown: "Checking", off: "Not monitored" };

// vm: a VM, kept as vm:<namespace>/<name> beside the apps.
const answerOf = (w, vm = false) => (STATE.data.uptime?.apps || {})[`${vm ? "vm:" : ""}${w.ns}/${w.name}`] || null;
const monitorOpen = (w, vm = false) => `${vm ? "vmMonitoring" : "wlMonitoring"}(${jsq(w.ns)},${jsq(w.name)})`;

const answerShare = value => value === null || value === undefined ? "—" : `${value >= 99.995 ? "100" : value.toFixed(value >= 99 ? 2 : 1)}%`;

function answerSince(at) {
  if (!at) return "";
  const d = new Date(at * 1000), today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : d.toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function answerDetail(a) {
  const last = a.last || {};
  if (a.state === "off") return a.why ? a.why[0].toUpperCase() + a.why.slice(1) : "Not checked";
  if (a.state === "down") return `Since ${answerSince(a.since)} · ${last.error || "no answer"}`;
  if (a.state === "unknown") return "First checks under way";
  return [last.code ? `HTTP ${last.code}` : "connection accepted", last.ms !== null && last.ms !== undefined ? `${last.ms} ms` : ""].filter(Boolean).join(" · ");
}

/* The row's mark: nothing while all is well. */
function answerTag(w, vm = false) {
  const a = answerOf(w, vm);
  if (!a || !["down", "slow"].includes(a.state)) return "";
  return `<button type="button" class="tag ${a.state === "down" ? "bad" : "warn"} tagbtn" data-tip="${esc(answerDetail(a))}"
    aria-label="${esc(`${ANSWER_WORDS[a.state]}: ${answerDetail(a)}`)}" onclick="${monitorOpen(w, vm)}">${a.state === "down" ? "Down" : "Slow"}</button>`;
}

function answerStrip(strip, cls = "", range = "day") {
  const hours = strip || [];
  if (range === "month") return `<span class="answer-strip ${cls}" role="img" aria-label="Last ${hours.length} days: ${hours.filter(h => h === "down" || h === "dip").length} with missed checks">${hours.map((h, i) =>
    `<i class="${h || "none"}" title="${esc(`${hours.length - i === 1 ? "Today" : `${hours.length - i - 1} days ago`}: ${h ? ({ up: "up", slow: "slow", dip: "a few missed checks", down: "down" })[h] : "not monitored"}`)}"></i>`).join("")}</span>`;
  return `<span class="answer-strip ${cls}" role="img" aria-label="Last ${hours.length} hours: ${hours.filter(h => h === "down").length} with missed checks">${hours.map((h, i) =>
    `<i class="${h || "none"}" title="${esc(`${hours.length - i === 1 ? "This hour" : `${hours.length - i - 1} h ago`}: ${h ? ({ up: "answered", slow: "slow", down: "missed checks" })[h] : "not checked"}`)}"></i>`).join("")}</span>`;
}

/* The card's line: state, the day's strip, and 30 days. */
function answerCardRow(w, vm = false) {
  const a = answerOf(w, vm);
  if (!a || (a.state === "off" && ["not an app of yours", "on another cluster"].includes(a.why))) return "";
  return `<button type="button" class="wanswer ${esc(a.state)}" onclick="${monitorOpen(w, vm)}"
      aria-label="${esc(`${ANSWER_WORDS[a.state] || a.state}: ${answerDetail(a)}`)}">
    <span class="dim xs">MONITORING</span>
    <span class="answer-state"><i class="answer-dot ${esc(a.state)}"></i>${esc(ANSWER_WORDS[a.state] || a.state)}</span>
    ${a.state === "off" ? `<span class="dim xs answer-why">${esc(answerDetail(a))}</span>` : `${answerStrip(a.strip)}<span class="mono xs answer-share" title="Share of checks answered over 30 days">${answerShare(a.uptime_30d)}</span>`}
  </button>`;
}

/* An app's setting in its parts: how, on which one port (or its main one),
   and at which path - "", "off", "tcp", "/health", "auto:8080", "tcp:9090",
   "http:9090/health". */
function monitorParts(value) {
  const ported = /^(auto|tcp|http):(\d+)(\/.*)?$/.exec(value || "");
  if (ported) return { mode: ported[1], port: ported[2], path: ported[1] === "http" ? ported[3] || "/" : "/" };
  if (!value) return { mode: "auto", port: "", path: "/" };
  if (value === "off" || value === "tcp") return { mode: value, port: "", path: "/" };
  return { mode: "http", port: "", path: value };
}
const answerModeOf = value => monitorParts(value).mode;

/* The TCP ports an app publishes, each number once, lowest first. */
function monitorPorts(ports) {
  const byPort = new Map();
  for (const p of ports || []) {
    if (!p.port || (p.protocol || "TCP") !== "TCP") continue;
    const seen = byPort.get(String(p.port));
    byPort.set(String(p.port), { port: +p.port, primary: !!(p.primary || seen?.primary), name: p.name || seen?.name || "" });
  }
  return [...byPort.values()].sort((a, b) => a.port - b.port);
}

/* Which port to ask: its main one (or any it publishes), or one chosen. Only
   offered when there is a choice - or when one was chosen before. */
function monitorPortSelect(id, ports, chosen = "") {
  const list = monitorPorts(ports);
  if (list.length < 2 && !chosen) return "";
  const main = list.some(p => p.primary) ? "Its main port" : "Any port it publishes";
  const gone = chosen && !list.some(p => String(p.port) === String(chosen));
  return `<select id="${id}" aria-label="Port to ask"><option value="">${main}</option>
    ${list.map(p => `<option value="${p.port}"${String(p.port) === String(chosen) ? " selected" : ""}>Port ${p.port}${p.name ? ` · ${esc(p.name)}` : ""}${p.primary ? " · main" : ""}</option>`).join("")}
    ${gone ? `<option value="${esc(chosen)}" selected>Port ${esc(chosen)} · no longer published</option>` : ""}</select>`;
}

/* The Monitoring field in Deploy's and Edit's Address step. */
function monitoringFieldHtml(prefix, value = "", ports = null) {
  const { mode, port, path } = monitorParts(value);
  const choice = (v, label) => `<option value="${v}" ${mode === v ? "selected" : ""}>${label}</option>`;
  const portSelect = ports ? monitorPortSelect(`${prefix}_mon_port`, ports, port) : "";
  return `<div class="f monitoring-field"><label for="${prefix}_mon">Monitoring ${typeof tip === "function" ? tip("Homestead asks the app at its address every minute and alerts you when it is down. Changing this never restarts it.") : ""}</label>
    <div class="row monitoring-row"><select id="${prefix}_mon" onchange="monitoringModeChanged(this)">
      ${choice("auto", "Automatic")}${choice("http", "A web page at a path")}${choice("tcp", "A connection to its port")}${choice("off", "Not monitored")}</select>
    ${portSelect ? `<span class="monitoring-port"${mode === "off" ? " hidden" : ""}>${portSelect}</span>` : ""}
    <input id="${prefix}_mon_path" class="monitoring-path" placeholder="/health" value="${esc(mode === "http" ? path : "")}" ${mode === "http" ? "" : "hidden"} aria-label="Path to ask"></div></div>`;
}

window.monitoringModeChanged = select => {
  const row = select.closest(".monitoring-row");
  const path = row?.querySelector(".monitoring-path"), port = row?.querySelector(".monitoring-port");
  if (path) path.hidden = select.value !== "http";
  if (port) port.hidden = select.value === "off";
};

function monitoringFieldValue(prefix) {
  const mode = $(`#${prefix}_mon`)?.value || "auto";
  return { mode, path: mode === "http" ? (($(`#${prefix}_mon_path`)?.value || "").trim() || "/") : "",
    port: mode === "off" ? "" : $(`#${prefix}_mon_port`)?.value || "" };
}

window.wlMonitoring = window.wlAnswering = (ns, name) => {
  const w = (STATE.data.wl || []).find(x => x.ns === ns && x.name === name) || { ns, name };
  const a = answerOf(w) || { state: "unknown", strip: [] };
  const u = STATE.data.uptime || {};
  const { mode, port, path } = monitorParts(w.answer_check);
  const portSelect = monitorPortSelect("ans_port", w.ports, port);
  const option = (value, label, help) => `<label class="answer-mode"><input type="radio" name="ans_mode" value="${value}" ${mode === value ? "checked" : ""}
    onchange="answerModeChanged()"><span><b>${esc(label)}</b><span class="dim small">${esc(help)}</span></span></label>`;
  modal(`Monitoring · ${name}`, `<div class="ui-stack">
    ${UI.lead(esc(`Homestead asks ${name} at its address ${(u.every || 60) === 60 ? "every minute" : `every ${Math.round(u.every / 60)} minutes`}, as a browser would. It is down after ${u.down_after || 3} misses in a row, which raises an alert, and up again at its first answer.`))}
    <div class="answer-head"><span class="answer-state big"><i class="answer-dot ${esc(a.state)}"></i>${esc(ANSWER_WORDS[a.state] || a.state)}</span>
      <span class="dim small">${esc(answerDetail(a))}</span></div>
    ${a.target ? `<div class="dim xs mono">${esc(a.target)}</div>` : ""}
    ${a.state === "off" ? "" : `<div class="answer-day">${answerStrip(a.strip, "wide")}<div class="answer-axis dim xs"><span>24 h ago</span><span>now</span></div></div>
    <div class="answer-figures"><div><span class="dim xs">LAST 24 HOURS</span><b class="mono">${answerShare(a.uptime_24h)}</b></div>
      <div><span class="dim xs">LAST 30 DAYS</span><b class="mono">${answerShare(a.uptime_30d)}</b></div></div>`}
    ${UI.section("How it is checked", `<div class="answer-modes">
      ${option("auto", "Automatic", "An HTTP answer below 500, or an accepted connection if it does not speak HTTP")}
      ${option("http", "A web page", "Only an HTTP answer below 500 at this path counts")}
      <div class="f answer-path"${mode === "http" ? "" : " hidden"}><label for="ans_path">Path</label><input id="ans_path" value="${esc(mode === "http" ? path : "/")}" placeholder="/health"></div>
      ${option("tcp", "A connection", "The port accepting a TCP connection is enough")}
      ${option("off", "Not monitored", "No checks and no alerts for this app")}</div>
      ${portSelect ? `<div class="f answer-port"${mode === "off" ? " hidden" : ""}><label for="ans_port">Port to ask</label>${portSelect}</div>` : ""}`)}
    ${outageSectionHtml("app", w)}
    ${UI.actions(UI.cancel() + UI.button("Save", "answerSave()", { kind: "pri", attrs: `data-need="operator" data-ns="${esc(ns)}" data-name="${esc(name)}" id="ans_go"` }))}</div>`);
};

window.answerModeChanged = () => {
  const mode = $("#mbody input[name=ans_mode]:checked")?.value;
  const path = $("#mbody .answer-path"), port = $("#mbody .answer-port");
  if (path) path.hidden = mode !== "http";
  if (port) port.hidden = mode === "off";
};

window.answerSave = async () => {
  const go = $("#ans_go"), mode = $("#mbody input[name=ans_mode]:checked")?.value || "auto";
  const body = { ns: go.dataset.ns, name: go.dataset.name, mode, path: ($("#ans_path")?.value || "").trim(),
    port: mode === "off" ? "" : $("#ans_port")?.value || "" };
  go.disabled = true;
  try {
    const result = await api("/api/uptime/setting", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(result.detail, "ok");
    const w = (STATE.data.wl || []).find(x => x.ns === body.ns && x.name === body.name) || { ns: body.ns, name: body.name };
    w.answer_check = result.value ?? (mode === "auto" ? "" : mode === "http" ? body.path || "/" : mode);
    await outageSave("app", w);
    closeModal();
    if (STATE.view === "workloads") renderWorkloads();
  } catch (e) { toast(e.message, "bad"); go.disabled = false; }
};

/* Loaded beside the containers: a failure leaves the page as it was. */
async function loadUptime() {
  try { STATE.data.uptime = await api("/api/uptime"); } catch (e) { /* the page works without it */ }
}



/* ---------------- a VM's monitoring ----------------
   A VM publishes no port: automatically, Homestead tries the usual ones and
   keeps the first that answers; or a port, or a web page on a port, is chosen. */
function vmMonitorChoice(value) {
  const m = /^(tcp|http):(\d+)(\/.*)?$/.exec(value || "");
  if (!value) return { mode: "auto", port: "", path: "/" };
  if (value === "off") return { mode: "off", port: "", path: "/" };
  if (m) return { mode: m[1], port: m[2], path: m[3] || "/" };
  return { mode: "auto", port: "", path: "/" };
}

window.vmMonitoring = (ns, name, back = null) => {
  const v = (STATE.data.vms || []).find(x => x.ns === ns && x.name === name) || { ns, name };
  const a = answerOf(v, true) || { state: "unknown", strip: [] };
  const c = vmMonitorChoice(v.monitoring);
  window.__vmMonitorBack = back;
  const option = (value, label, help, extra = "") => `<label class="answer-mode"><input type="radio" name="vm_mon" value="${value}" ${c.mode === value ? "checked" : ""}
    onchange="vmMonitorModeChanged()"><span><b>${esc(label)}</b><span class="dim small">${esc(help)}</span>${extra}</span></label>`;
  modal(`Monitoring · ${name}`, `<div class="ui-stack">
    ${UI.lead(esc(`Homestead asks ${name} at its address every minute. It is down after 3 misses in a row, which raises an alert, and up again at its first answer. Changing this never restarts the VM.`))}
    <div class="answer-head"><span class="answer-state big"><i class="answer-dot ${esc(a.state)}"></i>${esc(ANSWER_WORDS[a.state] || a.state)}</span>
      <span class="dim small">${esc(answerDetail(a))}</span></div>
    ${a.target ? `<div class="dim xs mono">${esc(a.target)}</div>` : ""}
    ${a.state === "off" || !(a.strip || []).length ? "" : `<div class="answer-day">${answerStrip(a.strip, "wide")}<div class="answer-axis dim xs"><span>24 h ago</span><span>now</span></div></div>`}
    ${UI.section("How it is checked", `<div class="answer-modes">
      ${option("auto", "Automatic", `The usual ports (SSH, RDP, HTTPS, HTTP, Proxmox, Home Assistant), and then the first that answers${a.port ? ` - ${a.port} now` : ""}. A VM none of them answer on is not monitored.`)}
      ${option("tcp", "A port", "The port taking a connection is enough.")}
      ${option("http", "A web page", "Only an HTTP answer below 500 counts.")}
      <div class="f2 vm-mon-where"${["tcp", "http"].includes(c.mode) ? "" : " hidden"}>
        <div class="f"><label for="vm_mon_port">Port</label><input id="vm_mon_port" inputmode="numeric" value="${esc(c.port || a.port || "")}" placeholder="22"></div>
        <div class="f vm-mon-path"${c.mode === "http" ? "" : " hidden"}><label for="vm_mon_path">Path</label><input id="vm_mon_path" value="${esc(c.path)}" placeholder="/"></div></div>
      ${option("off", "Not monitored", "No checks and no alerts for this VM.")}</div>`)}
    ${outageSectionHtml("vm", v)}
    ${UI.actions((back ? UI.button("Back", "vmMonitorBack()") : UI.cancel())
      + UI.button("Save", "vmMonitorSave()", { kind: "pri", id: "vm_mon_go", attrs: `data-need="operator" data-ns="${esc(ns)}" data-name="${esc(name)}"` }))}</div>`);
};

window.vmMonitorModeChanged = () => {
  const mode = $("#mbody input[name=vm_mon]:checked")?.value;
  const where = $("#mbody .vm-mon-where"), path = $("#mbody .vm-mon-path");
  if (where) where.hidden = !["tcp", "http"].includes(mode);
  if (path) path.hidden = mode !== "http";
};

window.vmMonitorBack = () => { const back = window.__vmMonitorBack; window.__vmMonitorBack = null; back ? back() : closeModal(); };

window.vmMonitorSave = async () => {
  const go = $("#vm_mon_go"), mode = $("#mbody input[name=vm_mon]:checked")?.value || "auto";
  const body = { ns: go.dataset.ns, name: go.dataset.name, mode, port: ($("#vm_mon_port")?.value || "").trim(), path: ($("#vm_mon_path")?.value || "").trim() || "/" };
  if (["tcp", "http"].includes(mode) && !body.port) return toast("Choose the port to ask", "bad");
  go.disabled = true;
  try {
    const result = await api("/api/vms/monitoring", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(result.detail, "ok");
    const v = (STATE.data.vms || []).find(x => x.ns === body.ns && x.name === body.name) || { ns: body.ns, name: body.name };
    v.monitoring = mode === "auto" ? "" : mode === "off" ? "off" : `${mode}:${body.port}${mode === "http" ? body.path : ""}`;
    await outageSave("vm", v);
    vmMonitorBack();
    if (STATE.view === "vms") window.viewVMs?.(); else if (STATE.view === "monitoring") renderMonitoring();
  } catch (e) { toast(e.message, "bad"); go.disabled = false; }
};

/* ---------------- when it is down (homestead_outage.py) ----------------
   Nothing, unless asked: restart it after so long down, a few times at most,
   and call a webhook. Kept on the app or VM, so saving restarts nothing. */
function outageSectionHtml(kind, row) {
  const a = row.outage_actions || {}, r = a.restart || null;
  const key = `${kind}:${row.ns}/${row.name}`, now = (STATE.data.uptime?.actions || {})[key] || {};
  const lastHook = now.webhook ? (now.webhook.ok ? `Last call: ${now.webhook.event}, taken` : `Last call: ${now.webhook.event}, failed - ${now.webhook.error}`) : "";
  const status = [now.restarts ? `Restarted ${now.restarts} time${now.restarts === 1 ? "" : "s"} in this outage${now.gave_up ? "; out of tries" : ""}` : "",
    now.last_error ? `The last restart could not be done: ${now.last_error}` : "", lastHook].filter(Boolean);
  return UI.section("When it is down", `<div class="ui-stack outage-actions">
    <p class="dim small">Nothing is done unless you choose it here.</p>
    <label class="switch"><input type="checkbox" id="oa_restart"${r ? " checked" : ""} onchange="outageToggle()"> Restart it</label>
    <div class="outage-restart"${r ? "" : " hidden"}>${UI.fields(
      UI.field("After it has been down for", `<div class="outage-unit"><input id="oa_after" type="number" inputmode="numeric" min="1" max="1440" value="${r?.after_min || 5}"><span class="dim small">minutes</span></div>`),
      UI.field("At most", `<div class="outage-unit"><input id="oa_max" type="number" inputmode="numeric" min="1" max="10" value="${r?.max || 3}"><span class="dim small">times</span></div>`,
        { help: "Then it is left alone and an alert says so. The count starts again once it has answered for 30 minutes." }))}
      <p class="dim xs">${kind === "vm" ? "A clean reboot, and only while it is monitored on a port you chose: automatically, Homestead cannot tell it is down. "
        : ""}Never while a job is working on it: an automatic update watches it itself and rolls the image back if it stays down.</p></div>
    <label class="switch"><input type="checkbox" id="oa_hook"${a.webhook ? " checked" : ""} onchange="outageToggle()"> Call a webhook</label>
    <div class="outage-hook"${a.webhook ? "" : " hidden"}>
      <div class="outage-url"><input id="oa_url" type="url" inputmode="url" placeholder="https://hooks.example.com/…" value="${esc(a.webhook || "")}" aria-label="Webhook address">
        ${UI.button("Send a test", `outageTest(${jsArg(kind)},${jsArg(row.ns)},${jsArg(row.name)})`, { attrs: 'data-need="operator"' })}</div>
      <p class="dim xs">A POST of JSON when it goes down, comes back, is restarted or runs out of tries, and when an automatic update of it finishes or is rolled back. Its text, content and message fields carry a sentence, for chat services and Home Assistant.</p></div>
    ${status.length ? `<div class="dim small">${status.map(esc).join("<br>")}</div>` : ""}</div>`);
}

window.outageToggle = () => {
  const restart = $("#mbody .outage-restart"), hook = $("#mbody .outage-hook");
  if (restart) restart.hidden = !$("#oa_restart")?.checked;
  if (hook) hook.hidden = !$("#oa_hook")?.checked;
};

function outageFromForm() {
  const out = {};
  if ($("#oa_restart")?.checked) out.restart = { after_min: +($("#oa_after")?.value || 0), max: +($("#oa_max")?.value || 0) };
  if ($("#oa_hook")?.checked && ($("#oa_url")?.value || "").trim()) out.webhook = $("#oa_url").value.trim();
  return Object.keys(out).length ? out : null;
}

const outageSame = (a, b) => JSON.stringify(a || null) === JSON.stringify(b || null);

/* After the check is saved: the actions, when they changed. */
async function outageSave(kind, row) {
  const actions = outageFromForm();
  if (outageSame(actions, row.outage_actions)) return;
  const result = await api("/api/monitoring/actions", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, ns: row.ns, name: row.name, actions }) });
  row.outage_actions = result.actions || null;
  toast(result.detail, "ok");
}

window.outageTest = async (kind, ns, name) => {
  const webhook = ($("#oa_url")?.value || "").trim();
  if (!webhook) return toast("Type the webhook address first", "bad");
  try {
    const r = await api("/api/monitoring/actions/test", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, ns, name, webhook }) });
    toast(r.detail, r.ok ? "ok" : "bad");
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- the Monitoring page ----------------
   Every app Homestead watches, problems first: its state, the last day hour by
   hour or the last 30 days day by day, its share of answered checks, and what
   is asked. Apps not monitored are listed last, with why. */
const MONITOR = { range: "day" };

const monitorRank = { down: 0, slow: 1, unknown: 2, up: 3, off: 4 };

function monitorRows() {
  const apps = STATE.data.uptime?.apps || {}, blank = { state: "unknown", strip: [], days: [] };
  return [...(STATE.data.wl || []).filter(w => !w.platform && !w.self && !w.homestead && !w.site)
      .map(w => ({ w, a: apps[`${w.ns}/${w.name}`] || blank })),
    ...(STATE.data.vms || []).filter(v => !v.site).map(v => ({ w: v, vm: true, a: apps[`vm:${v.ns}/${v.name}`] || blank }))]
    .sort((x, y) => (monitorRank[x.a.state] ?? 5) - (monitorRank[y.a.state] ?? 5) || x.w.name.localeCompare(y.w.name));
}

function monitorSummary(rows) {
  const count = state => rows.filter(r => r.a.state === state).length;
  return { total: rows.length, up: count("up"), slow: count("slow"), down: count("down"), unknown: count("unknown"), off: count("off") };
}

async function viewMonitoring() {
  [STATE.data.wl, STATE.data.vms] = await Promise.all([STATE.data.wl ? Promise.resolve(STATE.data.wl) : api("/api/workloads"),
    STATE.data.vms ? Promise.resolve(STATE.data.vms) : api("/api/vms").catch(() => []), loadUptime()]);
  renderMonitoring();
  api("/api/workloads").then(wl => { STATE.data.wl = wl; if (STATE.view === "monitoring" && $("#modal").classList.contains("hidden")) renderMonitoring(); }).catch(() => {});
}

window.monitorRange = range => { MONITOR.range = range; renderMonitoring(); };

function renderMonitoring() {
  const rows = monitorRows(), s = monitorSummary(rows), watched = rows.filter(r => r.a.state !== "off");
  const range = MONITOR.range, every = STATE.data.uptime?.every || 60;
  const said = [s.down && `<span class="crit-text">${s.down} down</span>`, s.slow && `<span class="med-text">${s.slow} slow</span>`,
    `${s.up} up`, s.off && `${s.off} not monitored`].filter(Boolean).join(" · ");
  const head = `<tr><th>App or VM</th><th>State</th><th class="mon-bars">${range === "day" ? "Last 24 hours" : "Last 30 days"}</th>
    <th data-nosort>${range === "day" ? "24 h" : "30 days"}</th><th class="mon-target">Asked</th><th data-nosort></th></tr>`;
  const row = ({ w, a, vm }) => `<tr class="clickable" onclick="if(!event.target.closest('button,a'))${monitorOpen(w, vm)}">
      <td class="cell-name" data-sort="${esc(w.name)}"><div class="vm-name-cell">${vm && typeof vmAvatar === "function" ? vmAvatar(w) : appAvatar(w.name, w.icon)}<div class="vm-name-text"><b>${esc(w.name)}</b><div class="dim xs">${esc(vm ? `${w.ns} · VM` : w.ns)}</div></div></div></td>
      <td data-label="State" data-sort="${monitorRank[a.state] ?? 5}"><span class="answer-state"><i class="answer-dot ${esc(a.state)}"></i>${esc(ANSWER_WORDS[a.state] || a.state)}</span>
        <div class="dim xs">${esc(answerDetail(a))}</div></td>
      <td data-label="${range === "day" ? "24 hours" : "30 days"}" class="mon-bars">${a.state === "off" ? '<span class="dim xs">—</span>' : answerStrip(range === "day" ? a.strip : a.days, "wide mon", range)}</td>
      <td data-label="Share" class="mono small" data-sort="${(range === "day" ? a.uptime_24h : a.uptime_30d) ?? -1}">${a.state === "off" ? "—" : answerShare(range === "day" ? a.uptime_24h : a.uptime_30d)}</td>
      <td data-label="Asked" class="mono xs dim mon-target">${esc(a.target || "")}</td>
      <td data-actions>${actionBar([{ label: "Change", icon: "gear", run: monitorOpen(w, vm), need: "operator", ariaLabel: `Change how ${w.name} is monitored` }], { label: `Actions for ${w.name}` })}</td></tr>`;
  paint(`${UI.pageHeader("Monitoring", `${watched.length} app${watched.length === 1 ? "" : "s"} and VM${watched.length === 1 ? "" : "s"} asked every ${every === 60 ? "minute" : `${Math.round(every / 60)} minutes`} · ${said}`,
      `<div class="seg" role="group" aria-label="Range"><button type="button" class="${range === "day" ? "on" : ""}" aria-pressed="${range === "day"}" onclick="monitorRange('day')">24 hours</button><button type="button" class="${range === "month" ? "on" : ""}" aria-pressed="${range === "month"}" onclick="monitorRange('month')">30 days</button></div>`)}
    ${rows.length ? `<div class="card flat pad0"><div class="tblwrap"><table class="tbl stack compact mon-table" data-sort="monitoring"><thead>${head}</thead><tbody>${rows.map(row).join("")}</tbody></table></div></div>`
      : '<div class="empty">No apps yet. Deploy one, and Homestead starts asking it at its address.</div>'}
    ${UI.more("How monitoring works", `<p>Every minute the leading Homestead asks each running app at its address, as a browser would: an HTTP answer below 500 counts, and so does a port that takes the connection but does not speak HTTP. With no main port chosen, each of its ports is tried. Three misses in a row is down, which raises an alert; the first answer clears it. Over 2 seconds is slow. Stopped apps are not asked.</p><p>A VM publishes no port, so Homestead tries the usual ones - SSH, RDP, HTTPS, HTTP, Proxmox, Home Assistant - and keeps the first that answers; a VM none of them answer on is not monitored until you choose its port.</p><p>Choose what is asked for each one here, in an app's Edit dialog under Address, or from a VM's menu. Changing it never restarts anything.</p>`)}`);
}
window.viewMonitoring = viewMonitoring;


/* ---------------- the Monitoring dashboard widget ----------------
   One third: the counts and whatever is wrong. Half: every app, a dot and its
   30 days. Two thirds and wider: every app with its last 24 hours. */
function monitorWidget(item = {}) {
  if (!STATE.data.uptime) return '<div class="empty small"><span class="spin2"></span> Loading…</div>';
  const rows = monitorRows().filter(r => r.a.state !== "off"), s = monitorSummary(rows), width = item.width || 6;
  if (!rows.length) return '<div class="empty small">No apps are monitored yet.</div>';
  const open = r => `onclick="${monitorOpen(r.w, r.vm)}"`;
  const wrong = rows.filter(r => ["down", "slow"].includes(r.a.state));
  const counts = `<div class="mon-counts">${[["down", s.down, "down"], ["slow", s.slow, "slow"], ["up", s.up, "up"]]
    .map(([cls, n, word]) => `<div class="mon-count ${n && cls !== "up" ? cls : ""}"><b class="mono">${n}</b><span class="dim xs">${word}</span></div>`).join("")}</div>`;
  if (width <= 4) {
    return counts + (wrong.length ? `<div class="mon-list">${wrong.slice(0, 6).map(r => `<button type="button" class="mon-item" ${open(r)}>
        <i class="answer-dot ${esc(r.a.state)}"></i><b>${esc(r.w.name)}</b><span class="dim xs">${esc(r.a.state === "down" ? (r.a.last?.error || "down") : `${r.a.last?.ms ?? ""} ms`)}</span></button>`).join("")}</div>`
      : `<div class="dim small mon-allup">All ${s.up} up</div>`);
  }
  const bars = width >= 8;
  return `${width >= 8 ? "" : counts}<div class="mon-list${bars ? " bars" : ""}">${rows.map(r => `<button type="button" class="mon-item" ${open(r)}>
      ${r.vm && typeof vmAvatar === "function" ? vmAvatar(r.w) : appAvatar(r.w.name, r.w.icon)}<span class="mon-name"><b>${esc(r.w.name)}</b><span class="dim xs">${esc(ANSWER_WORDS[r.a.state] || r.a.state)}${r.a.state === "down" ? ` · ${esc(r.a.last?.error || "")}` : ""}</span></span>
      ${bars ? answerStrip(r.a.strip, "mon-mini") : `<i class="answer-dot ${esc(r.a.state)}"></i>`}
      <span class="mono xs mon-share" title="Answered over 30 days">${answerShare(r.a.uptime_30d)}</span></button>`).join("")}</div>`;
}


/* Edit's Monitoring step: what is asked, how it is going, and the choice. */
function monitoringStepHtml(w) {
  const a = answerOf(w);
  return `<div class="sec" style="margin-top:0">Monitoring</div>
    <p class="dim small">Homestead asks this app at its address every minute and alerts you when it is down. Changing how it is asked never restarts it.</p>
    ${a ? `<div class="answer-head"><span class="answer-state"><i class="answer-dot ${esc(a.state)}"></i>${esc(ANSWER_WORDS[a.state] || a.state)}</span>
      <span class="dim small">${esc(answerDetail(a))}</span></div>${a.target ? `<div class="dim xs mono">${esc(a.target)}</div>` : ""}
      ${a.state === "off" || !(a.strip || []).length ? "" : `<div class="answer-day">${answerStrip(a.strip, "wide")}<div class="answer-axis dim xs"><span>24 h ago</span><span>now</span></div></div>`}` : ""}
    ${monitoringFieldHtml("e", w.answer_check || "", w.ports || [])}`;
}
