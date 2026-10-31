/* Stats beyond the browser: MQTT publishing and power control and the alert webhook
   (Settings > Connections), and the long-term history card on the Dashboard. */

/* ---------------- MQTT ---------------- */
async function mqttPaint() {
  const host = $("#mqttCard");
  if (!host) return;
  let m;
  try { m = await api("/api/mqtt"); } catch (e) { host.innerHTML = `<div class="note bad">${esc(e.message)}</div>`; return; }
  STATE.data.mqtt = m;
  host.innerHTML = mqttCardHtml(m);
}
/* Laid out as UniFi is beside it: the broker, its facts, then power control. */
function mqttCardHtml(m, now = Date.now() / 1000) {
  const s = m.status || {}, set = !!m.host;
  const state = !set ? ["neutral", "not connected"] : { publishing: ["ok", "publishing"], standby: ["neutral", "standby"], error: ["bad", "failing"],
    off: ["neutral", "off"] }[s.state] || ["neutral", s.state || "off"];
  const sensors = `${m.sensors?.cluster ?? 0} cluster sensors and ${m.sensors?.node ?? 0} for each host`;
  const facts = set ? UI.facts([
    ["Broker", `<span class="mono">${esc(m.host)}:${esc(m.port)}</span> <span class="dim xs">· ${m.tls ? "TLS" : "no TLS"}${m.username ? ` · as ${esc(m.username)}` : " · no sign-in"}</span>`],
    ["Topics", `<span class="mono">${esc(m.base)}/…</span> <span class="dim xs">· discovery under ${esc(m.discovery)}</span>`],
    ["Last publish", s.last_publish ? `${now - s.last_publish < 60 ? "under a minute ago" : esc(fmtAgo(now - s.last_publish))} <span class="dim xs">· every ${esc(m.interval)}s · ${esc(sensors)}</span>` : "not yet"]]) : "";
  const count = (m.controlled || []).length;
  const control = set ? serviceRow("Power control from Home Assistant",
    m.control ? UI.chip(`on · ${count}`, "ok") : UI.chip("off", "neutral"),
    m.control ? `${count === 1 ? "One container or VM" : `${count} containers and VMs`} with a Power switch and Restart; VMs also Force off and Reset`
      : "Optional: start, stop and restart the containers and VMs you pick from Home Assistant",
    actionBar([{ label: m.control ? "Choose" : "Turn on", run: "mqttControl()", need: "admin", pri: !m.control },
      m.control ? { label: "Turn off", run: "mqttControlOff()", need: "admin" } : null])) : "";
  return `${UI.moduleHeader(`MQTT and Home Assistant`, `Cluster and host stats to an MQTT broker, with Home Assistant discovery, and power control if you turn it on`, ``)}
    ${serviceRow(set ? "MQTT broker" : "No broker", UI.chip(state[1], state[0]),
      set ? (m.enabled ? esc(s.state === "publishing" ? "" : s.detail || "starting") : "Publishing is switched off") : `Optional: ${esc(sensors)}, for Home Assistant or anything else on MQTT`,
      actionBar([{ label: set ? "Edit" : "Connect MQTT", run: "mqttConfigure()", need: "admin", pri: !set },
        set ? { label: "What is published", run: "mqttPreview()" } : null]))}
    ${facts}
    ${s.error ? UI.callout("bad", "Publishing is failing", esc(s.error)) : ""}
    ${control}
    ${m.control && !(m.tls && m.username) ? UI.callout("warn", "Anyone who can publish to this broker can switch these",
      `Give the broker ${m.username ? "" : "a username and password, "}${m.tls ? "" : "TLS, "}and an ACL so only Home Assistant may publish to <span class="mono">${esc(m.base)}/control/#</span>.`) : ""}
    <pre class="mono helm-values" id="mqttPreview" hidden></pre>`;
}
window.mqttCardHtml = mqttCardHtml;
/* Its fields, in a dialog: broker, sign-in, topics, how often. */
window.mqttConfigure = () => {
  const m = STATE.data.mqtt || {};
  modal("MQTT and Home Assistant", UI.lead(`Publishes cluster and host stats to an MQTT broker, with Home Assistant discovery: ${m.sensors?.cluster ?? 0} cluster sensors and ${m.sensors?.node ?? 0} for each host.`) + `
    <label class="switch"><input type="checkbox" id="mq_on" ${m.enabled ? "checked" : ""}> Publish stats to MQTT</label>
    <div class="mqtt-grid">
      <div class="f"><label>Broker</label><input id="mq_host" class="mono" value="${esc(m.host || "")}" placeholder="192.0.2.177"></div>
      <div class="f"><label>Port</label><input id="mq_port" type="number" min="1" max="65535" value="${m.port || 1883}"></div>
      <div class="f"><label>Username</label><input id="mq_user" value="${esc(m.username || "")}" placeholder="none" autocomplete="off"></div>
      <div class="f"><label>Password</label><input id="mq_pass" type="password" autocomplete="new-password" placeholder="${m.has_password ? "saved · blank keeps it" : "none"}"></div>
      <div class="f"><label>Base topic ${tip("States go to <base>/cluster/state and <base>/node/<host>/state.")}</label><input id="mq_base" class="mono" value="${esc(m.base || "")}"></div>
      <div class="f"><label>Discovery prefix ${tip("Home Assistant listens for discovery under homeassistant unless it has been changed.")}</label><input id="mq_disc" class="mono" value="${esc(m.discovery || "")}"></div>
      <div class="f"><label>Every (seconds)</label><input id="mq_every" type="number" min="10" max="3600" value="${m.interval || 60}"></div>
      <div class="f"><label>Device name</label><input id="mq_name" value="${esc(m.device_name || "")}"></div>
    </div>
    <label class="switch"><input type="checkbox" id="mq_tls" ${m.tls ? "checked" : ""}> TLS ${tip("For a broker on 8883 with a certificate the system trusts.")}</label>` +
    UI.actions(UI.cancel() + UI.button("Save", "mqttSave(true)", { kind: "pri" }), UI.button("Test connection", "mqttTest()")));
};
window.mqttPaint = mqttPaint;
const mqttBody = () => ({ enabled: $("#mq_on").checked, host: $("#mq_host").value.trim(), port: +$("#mq_port").value,
  username: $("#mq_user").value.trim(), password: $("#mq_pass").value, base: $("#mq_base").value.trim(),
  discovery: $("#mq_disc").value.trim(), interval: +$("#mq_every").value, device_name: $("#mq_name").value, tls: $("#mq_tls").checked });
const mqttPost = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
window.mqttSave = async fromDialog => {
  try { await mqttPost("/api/mqtt", mqttBody()); toast("MQTT settings saved", "ok"); if (fromDialog) closeModal(); setTimeout(mqttPaint, 1500); }
  catch (e) { toast(e.message, "bad"); }
};
window.mqttTest = async () => {
  try { const r = await mqttPost("/api/mqtt/test", mqttBody()); toast(r.detail, "ok"); }
  catch (e) { toast(e.message, "bad"); }
};
window.mqttPreview = async () => {
  const pre = $("#mqttPreview");
  if (!pre.hidden) { pre.hidden = true; return; }
  try {
    const r = await api("/api/mqtt/preview");
    pre.textContent = r.states.map(s => `${s.topic}\n  ${JSON.stringify(s.payload)}`).join("\n\n");
    pre.hidden = false;
  } catch (e) { toast(e.message, "bad"); }
};

/* Power control: an action an admin takes, not a setting in the save bar -
   it lets whatever can publish to the broker start and stop things - so
   turning it on or off asks first and takes effect at once (#511). */
function mqttControlList(items, picked) {
  const group = (kind, title) => {
    const rows = items.filter(i => i.kind === kind).sort((a, b) => `${a.ns}/${a.name}`.localeCompare(`${b.ns}/${b.name}`));
    return rows.length ? `<div class="mqtt-pick-group"><b class="small">${title}</b>${rows.map(i => `<label class="mqtt-pick${i.refused ? " dim" : ""}" data-q="${esc(`${i.ns}/${i.name}`.toLowerCase())}">
      <input type="checkbox" value="${esc(i.id)}" ${picked.has(i.id) && !i.refused ? "checked" : ""} ${i.refused ? "disabled" : ""}>
      <span class="mono">${esc(i.ns)}/${esc(i.name)}</span>${i.refused ? ` <span class="dim xs">· ${esc(i.refused)}</span>` : ""}</label>`).join("")}</div>` : "";
  };
  return group("vm", "Virtual machines") + group("app", "Containers") || '<div class="empty small">No containers or VMs yet.</div>';
}
window.mqttControlList = mqttControlList;
window.mqttControl = async () => {
  let c;
  try { c = await api("/api/mqtt/control"); } catch (e) { toast(e.message, "bad"); return; }
  STATE.data.mqttControl = c;
  modal("Power control from Home Assistant", UI.lead("Each one you pick becomes a device in Home Assistant with a Power switch and a Restart button; a VM also gets Force off and Reset. A command is made as an operator would make it, through the same checks, and listed under AI agents' recent calls as Home Assistant's.")
    + `<input id="mqc_q" class="mqtt-pick-filter" placeholder="Filter" aria-label="Filter containers and VMs" oninput="mqttControlFilter(this.value)">
    <div class="mqtt-pick-list" id="mqc_list">${mqttControlList(c.items || [], new Set(c.controlled || []))}</div>`
    + UI.actions(UI.cancel() + UI.button(c.control ? "Save" : "Turn on power control", "mqttControlSave()", { kind: "pri" })));
};
window.mqttControlFilter = q => {
  q = String(q || "").trim().toLowerCase();
  document.querySelectorAll("#mqc_list .mqtt-pick").forEach(row => { row.hidden = !!q && !row.dataset.q.includes(q); });
};
const mqttControlPost = body => mqttPost("/api/mqtt/control", body);
window.mqttControlSave = async () => {
  const ids = [...document.querySelectorAll("#mqc_list input:checked")].map(i => i.value);
  if (!ids.length) { toast("Pick at least one container or VM", "bad"); return; }
  const m = STATE.data.mqtt || {};
  if (!STATE.data.mqttControl?.control && !(await ask(
    `Home Assistant, and anything else that can publish to ${m.host || "the broker"}, can then start, stop and restart ${ids.length === 1 ? "it" : `these ${ids.length}`}. Homestead's own containers never can be.`,
    { title: "Turn on power control", ok: "Turn on" }))) return;
  try {
    await mqttControlPost({ control: true, controlled: ids });
    toast(`Power control covers ${ids.length === 1 ? "one container or VM" : `${ids.length} containers and VMs`}`, "ok");
    closeModal();
  } catch (e) { toast(e.message, "bad"); return; }
  setTimeout(mqttPaint, 800);
};
window.mqttControlOff = async () => {
  const m = STATE.data.mqtt || {};
  if (!(await ask("Home Assistant's Power switches and buttons for them are removed, and commands are no longer listened for. What is picked is kept for next time.",
    { title: "Turn off power control", ok: "Turn off", danger: true }))) return;
  try { await mqttControlPost({ control: false, controlled: m.controlled || [] }); toast("Power control is off", "ok"); }
  catch (e) { toast(e.message, "bad"); }
  setTimeout(mqttPaint, 800);
};

/* ---------------- alert webhook (homestead_alert_webhook.py, #741) ----------------
   Alerts to one webhook in Generic JSON, ntfy or Gotify, with the outage
   webhook's Format control (webhookFormatSelect, uptime.js). Off until set. */
const ALERT_HOOK_LEAST = [["critical", "Critical only"], ["degraded", "Warnings and critical"], ["info", "Everything, information too"]];

async function alertHookPaint() {
  const host = $("#alertHookCard");
  if (!host) return;
  let h;
  try { h = await api("/api/alerts/webhook"); } catch (e) { host.innerHTML = UI.callout("bad", "Could not read the alert webhook", esc(e.message)); return; }
  STATE.data.alertHook = h;
  host.innerHTML = alertHookCardHtml(h);
}
window.alertHookPaint = alertHookPaint;

function alertHookCardHtml(h, now = Date.now() / 1000) {
  const set = !!h.webhook, last = h.last;
  const format = (OUTAGE_FORMATS.find(([v]) => v === h.format) || OUTAGE_FORMATS[0])[1];
  const least = (ALERT_HOOK_LEAST.find(([v]) => v === h.min_severity) || ALERT_HOOK_LEAST[1])[1];
  const lastText = !last ? "" : last.ok ? `Last call ${esc(fmtAgo(now - last.at))}: taken`
    : `Last call ${esc(fmtAgo(now - last.at))}: failed - ${esc(last.error || "")}`;
  return `${UI.moduleHeader("Alert webhook", "Alerts to ntfy, Gotify or any webhook, as push sends them to devices", "")}
    ${serviceRow(set ? "Alert webhook" : "No alert webhook", UI.chip(set ? "on" : "off", set ? "ok" : "neutral"),
      set ? `${esc(format)} · <span class="mono">${esc(h.webhook)}</span> · ${esc(least)}${lastText ? `<br><span class="dim xs">${lastText}</span>` : ""}`
        : "Optional: each alert once as it is raised, worsens or clears",
      actionBar([{ label: set ? "Edit" : "Set up", run: "alertHookConfigure()", need: "admin", pri: !set }]))}`;
}
window.alertHookCardHtml = alertHookCardHtml;

window.alertHookConfigure = () => {
  const h = STATE.data.alertHook || {};
  modal("Alert webhook", UI.lead("Sends each alert once as it is raised, worsens or clears, from the alerts at or above the level chosen.") +
    UI.fields(UI.field("Format", webhookFormatSelect("ah_format", h.format)),
      UI.field("Send", `<select id="ah_least" aria-label="Alerts to send">${ALERT_HOOK_LEAST.map(([v, label]) =>
        `<option value="${v}"${(h.min_severity || "degraded") === v ? " selected" : ""}>${label}</option>`).join("")}</select>`)) + `
    <div class="outage-url"><input id="ah_url" type="url" inputmode="url" placeholder="https://ntfy.example.com/homestead" value="${esc(h.webhook || "")}" aria-label="Webhook address">
      ${UI.button("Send a test", "alertHookTest()")}</div>
    <label class="switch"><input type="checkbox" id="ah_lan"${h.lan ? " checked" : ""}> Allow a LAN address (a self-hosted ntfy or Gotify, say)</label>
    ${UI.more("How it is sent", `<p class="dim xs">Generic JSON carries the alert's fields and a sentence as text, content and message; ntfy gets the alert's title, priority and a tag, Gotify its title, message and priority. Once saved, the address shows only up to its host. It never reaches this host, link-local or metadata addresses, and a LAN address only when allowed here.</p>`, false)}` +
    UI.actions(UI.cancel() + UI.button("Save", "alertHookSave()", { kind: "pri" }),
      h.webhook ? UI.button("Turn off", "alertHookOff()", { kind: "danger" }) : ""));
};

const alertHookInit = body => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const alertHookSend = async body => {
  try { const r = await api("/api/alerts/webhook", alertHookInit(body)); toast(r.detail || "Saved", "ok"); closeModal(); setTimeout(alertHookPaint, 300); }
  catch (e) { toast(e.message, "bad"); }
};
window.alertHookSave = () => {
  const webhook = ($("#ah_url")?.value || "").trim();
  if (!webhook) return toast("Type the webhook address, or Turn off", "bad");
  return alertHookSend({ webhook, format: $("#ah_format")?.value || "generic", min_severity: $("#ah_least")?.value || "degraded",
    lan: !!$("#ah_lan")?.checked });
};
window.alertHookOff = () => alertHookSend({ webhook: "" });
window.alertHookTest = async () => {
  const webhook = ($("#ah_url")?.value || "").trim();
  if (!webhook) return toast("Type the webhook address first", "bad");
  try {
    const r = await api("/api/alerts/webhook/test", alertHookInit({ webhook, format: $("#ah_format")?.value || "generic" }));
    toast(r.detail, r.ok ? "ok" : "bad");
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- long-term history ---------------- */
function historyRange(pick) {
  if (pick) { try { localStorage.setItem("homestead.history.range", pick); } catch (e) { /* this visit */ } historyPaint(); return pick; }
  try { return localStorage.getItem("homestead.history.range") || "24h"; } catch (e) { return "24h"; }
}
window.historyRange = historyRange;

function historyStat(label, values, unit, peak, times, key) {
  const nums = values.filter(v => typeof v === "number");
  const avg = nums.length ? nums.reduce((a, b) => a + b, 0) / nums.length : 0;
  const max = peak ?? (nums.length ? Math.max(...nums) : 0);
  return `<div class="hist-chart"><div class="between"><span class="dim xs">${esc(label)}</span>
      <span class="mono xs">avg ${avg.toFixed(1)}${unit} · peak ${(+max).toFixed(1)}${unit}</span></div>
    ${sparkline(nums, { w: 300, h: 56, times, key })}</div>`;
}

let historyRequest = 0;
let historyAbort = null;
let historyPending = null, historyHost = null, historyPendingRange = null;
// Long history is sampled every 5 minutes or hourly: read it again when its range
// changes or five minutes have passed, not on every dashboard refresh (#705).
const HISTORY_FRESH = 300000;
let historyLoaded = null;
function historyPaint({ fresh = false } = {}) {
  const host = $("#historyCard");
  if (!host) { historyAbort?.abort(); return Promise.resolve(); }
  const range = historyRange();
  if (historyPending && historyHost === host && historyPendingRange === range) return historyPending;
  if (!fresh && STATE.data.historyHtml && historyLoaded?.range === range && Date.now() - historyLoaded.at < HISTORY_FRESH
      && !host.querySelector?.(".history-error")) return Promise.resolve();
  const request = ++historyRequest;
  historyAbort?.abort();
  const abort = new AbortController();
  historyAbort = abort;
  historyHost = host; historyPendingRange = range;
  historyPending = historyLoad(host, range, request, abort);
  return historyPending;
}
async function historyLoad(host, range, request, abort) {
  const timeout = setTimeout(() => abort.abort(), 10000);
  let h;
  try { h = await api(`/api/history/long?range=${range}`, { signal: abort.signal }); }
  catch (e) {
    if (request !== historyRequest || host !== $("#historyCard") || range !== historyRange()) return;
    // The Over time card's own error state, not a page warning (#506):
    // with charts already drawn they stay, with a quiet line saying so.
    if (!STATE.data.historyHtml) {
      host.innerHTML = UI.error(e, { title: "Over time could not load", retry: "historyPaint()" });
      return;
    }
    let note = host.querySelector(".history-error");
    if (!note) { note = document.createElement("div"); note.className = "csub history-error"; note.setAttribute?.("role", "status"); host.appendChild(note); }
    note.textContent = "Saved history could not refresh; these charts are from before. Retrying on the next dashboard refresh.";
    return;
  } finally {
    clearTimeout(timeout);
    if (historyAbort === abort) { historyAbort = null; historyPending = null; }
  }
  if (request !== historyRequest || host !== $("#historyCard") || range !== historyRange()) return;
  historyLoaded = { range, at: Date.now() };
  const since = h.since ? fmtDay(h.since) : "";
  STATE.data.historyHtml = `<div class="between"><div><div class="ctitle">Over time</div>
      <div class="csub">${h.samples ? `Recorded every ${h.step === 300 ? "5 minutes" : "hour"} by Homestead, open or not${since ? ` · since ${esc(since)}` : ""}` : "Homestead records a sample every 5 minutes; the first appears shortly."}</div></div>
      <div class="seg">${["24h", "7d", "30d", "90d"].map(r => `<button class="${r === range ? "on" : ""}" onclick="historyRange(${jsq(r)})">${r}</button>`).join("")}</div></div>
    ${h.samples > 0 ? `<div class="hist-grid">
      ${historyStat("Cluster CPU", h.cpu, "%", h.cpu_max, h.t, range)}
      ${historyStat("Cluster RAM", h.mem, "%", h.mem_max, h.t, range)}
      <div class="hist-chart"><div class="between"><span class="dim xs">Network in / out</span><span class="mono xs">Mb/s</span></div>${dualSpark(h.rx, h.tx, { w: 300, h: 56, times: h.t, key: range })}</div>
      ${historyStat("Your pods", h.pods, "", undefined, h.t, range)}</div>
      <div class="hist-nodes">${h.nodes.map(n => `<div class="hist-node"><b>${esc(n.name)}</b>
        ${UI.chip(`${n.availability.toFixed(n.availability >= 99.95 ? 0 : 2)}% up`, n.availability >= 99.9 ? "ok" : n.availability >= 99 ? "warn" : "bad", { size: "slim", tip: "Share of samples in which the host was Ready" })}
        <span class="dim xs">cpu ${n.cpu}% · ram ${n.mem}%</span></div>`).join("")}</div>
      ${h.vol_bad.some(v => v > 0) ? `<div class="note" style="margin-top:8px">Volumes were degraded or faulted for part of this period (peak ${Math.max(...h.vol_bad)}).</div>` : ""}` : ""}`;
  const next = document.createElement("section");
  next.innerHTML = STATE.data.historyHtml;
  morph(host, next);
}
window.historyPaint = historyPaint;
