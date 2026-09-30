/* Stats beyond the browser: MQTT publishing (Settings > Monitoring) and the
   long-term history card on the Dashboard. */

/* ---------------- MQTT ---------------- */
async function mqttPaint() {
  const host = $("#mqttCard");
  if (!host) return;
  let m;
  try { m = await api("/api/mqtt"); } catch (e) { host.innerHTML = `<div class="note bad">${esc(e.message)}</div>`; return; }
  STATE.data.mqtt = m;
  const s = m.status || {}, admin = can("admin");
  const state = { publishing: ["ok", "publishing"], standby: ["neutral", "standby"], error: ["crit", "error"], off: ["low", "off"] }[s.state] || ["low", s.state || "off"];
  host.innerHTML = `<div class="settings-card-head"><div><div class="ctitle">Publish</div>
      <div class="csub">Cluster and node stats to other systems</div></div></div>
    ${serviceRow("MQTT and Home Assistant", `<span class="pill ${state[0]}">${esc(state[1])}</span>`,
      s.state === "publishing" ? `${esc(s.detail)} · ${m.sensors.cluster + m.sensors.node} sensors · last ${Date.now() / 1000 - s.last_publish < 60 ? "under a minute ago" : esc(fmtAgo(Date.now() / 1000 - s.last_publish))}`
        : m.enabled ? esc(s.detail || "starting") : `Off · ${m.sensors.cluster} cluster sensors and ${m.sensors.node} for each node, with Home Assistant discovery`,
      actionBar([admin ? { label: "Configure", run: "mqttConfigure()" } : null, { label: "What is published", run: "mqttPreview()" }].filter(Boolean)))}
    ${s.error ? `<div class="note bad">${esc(s.error)}</div>` : ""}
    <pre class="mono helm-values" id="mqttPreview" hidden></pre>`;
}
/* Its fields, in a dialog: broker, sign-in, topics, how often. */
window.mqttConfigure = () => {
  const m = STATE.data.mqtt || {};
  modal("MQTT and Home Assistant", UI.lead(`Publishes cluster and node stats to an MQTT broker, with Home Assistant discovery: ${m.sensors?.cluster ?? 0} cluster sensors and ${m.sensors?.node ?? 0} for each node.`) + `
    <label class="switch"><input type="checkbox" id="mq_on" ${m.enabled ? "checked" : ""}> Publish stats to MQTT</label>
    <div class="mqtt-grid">
      <div class="f"><label>Broker</label><input id="mq_host" class="mono" value="${esc(m.host || "")}" placeholder="192.0.2.177"></div>
      <div class="f"><label>Port</label><input id="mq_port" type="number" min="1" max="65535" value="${m.port || 1883}"></div>
      <div class="f"><label>Username</label><input id="mq_user" value="${esc(m.username || "")}" placeholder="none" autocomplete="off"></div>
      <div class="f"><label>Password</label><input id="mq_pass" type="password" autocomplete="new-password" placeholder="${m.has_password ? "saved · blank keeps it" : "none"}"></div>
      <div class="f"><label>Base topic ${tip("States go to <base>/cluster/state and <base>/node/<node>/state.")}</label><input id="mq_base" class="mono" value="${esc(m.base || "")}"></div>
      <div class="f"><label>Discovery prefix ${tip("Home Assistant listens for discovery under homeassistant unless it has been changed.")}</label><input id="mq_disc" class="mono" value="${esc(m.discovery || "")}"></div>
      <div class="f"><label>Every (seconds)</label><input id="mq_every" type="number" min="10" max="3600" value="${m.interval || 60}"></div>
      <div class="f"><label>Device name</label><input id="mq_name" value="${esc(m.device_name || "")}"></div>
    </div>
    <label class="switch"><input type="checkbox" id="mq_tls" ${m.tls ? "checked" : ""}> TLS ${tip("For a broker on 8883 with a certificate the system trusts.")}</label>` +
    UI.actions(UI.button("Test connection", "mqttTest()") + UI.cancel() + UI.button("Save", "mqttSave(true)", { kind: "pri" }), true));
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

/* ---------------- long-term history ---------------- */
function historyRange(pick) {
  if (pick) { try { localStorage.setItem("homestead.history.range", pick); } catch (e) { /* this visit */ } historyPaint(); return pick; }
  try { return localStorage.getItem("homestead.history.range") || "24h"; } catch (e) { return "24h"; }
}
window.historyRange = historyRange;

function historyStat(label, values, unit, peak) {
  const nums = values.filter(v => typeof v === "number");
  const avg = nums.length ? nums.reduce((a, b) => a + b, 0) / nums.length : 0;
  const max = peak ?? (nums.length ? Math.max(...nums) : 0);
  return `<div class="hist-chart"><div class="between"><span class="dim xs">${esc(label)}</span>
      <span class="mono xs">avg ${avg.toFixed(1)}${unit} · peak ${(+max).toFixed(1)}${unit}</span></div>
    ${sparkline(nums.length > 1 ? nums : [0, 0], { w: 300, h: 56 })}</div>`;
}

async function historyPaint() {
  const host = $("#historyCard");
  if (!host) return;
  const range = historyRange();
  let h;
  try { h = await api(`/api/history/long?range=${range}`); } catch (e) { return; }
  const since = h.since ? new Date(h.since * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" }) : "";
  STATE.data.historyHtml = `<div class="between"><div><div class="ctitle">Over time</div>
      <div class="csub">${h.samples ? `Recorded every ${h.step === 300 ? "5 minutes" : "hour"} by Homestead, open or not${since ? ` · since ${esc(since)}` : ""}` : "Homestead records a sample every 5 minutes; the first appears shortly."}</div></div>
      <div class="seg">${["24h", "7d", "30d", "90d"].map(r => `<button class="${r === range ? "on" : ""}" onclick="historyRange(${jsq(r)})">${r}</button>`).join("")}</div></div>
    ${h.samples > 1 ? `<div class="hist-grid">
      ${historyStat("Cluster CPU", h.cpu, "%", h.cpu_max)}
      ${historyStat("Cluster RAM", h.mem, "%", h.mem_max)}
      <div class="hist-chart"><div class="between"><span class="dim xs">Network in / out</span><span class="mono xs">Mbit/s</span></div>${dualSpark(h.rx, h.tx, { w: 300, h: 56 })}</div>
      ${historyStat("Workload pods", h.pods, "")}</div>
      <div class="hist-nodes">${h.nodes.map(n => `<div class="hist-node"><b>${esc(n.name)}</b>
        <span class="pill slim ${n.availability >= 99.9 ? "ok" : n.availability >= 99 ? "med" : "crit"}" data-tip="Share of samples in which the node was Ready">${n.availability.toFixed(n.availability >= 99.95 ? 0 : 2)}% up</span>
        <span class="dim xs">cpu ${n.cpu}% · ram ${n.mem}%</span></div>`).join("")}</div>
      ${h.vol_bad.some(v => v > 0) ? `<div class="note" style="margin-top:8px">Volumes were degraded or faulted for part of this period (peak ${Math.max(...h.vol_bad)}).</div>` : ""}` : ""}`;
  host.innerHTML = STATE.data.historyHtml;
}
window.historyPaint = historyPaint;
