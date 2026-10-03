/* The installable app: its service worker, the install prompt, and push
   notifications on this device.

   Browsers offer all of this only on a secure origin - HTTPS, or localhost -
   so on a plain-HTTP LAN address the Settings card explains what is missing
   rather than offering buttons that cannot work. */
const PWA = { registration: null, installPrompt: null, subscription: null, key: null };

const pwaSecure = () => window.isSecureContext && "serviceWorker" in navigator &&
  new URLSearchParams(location.search).get("demo") !== "1" && window.HOMESTEAD_DEMO !== true;
const pwaPushable = () => pwaSecure() && "PushManager" in window && "Notification" in window;
const pwaInstalled = () => matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
const pwaApple = () => /iPhone|iPad|iPod/.test(navigator.userAgent) ||
  (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

function pwaKeyBytes(text) {
  const raw = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - text.length % 4) % 4));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

function pwaDeviceName() {
  const ua = navigator.userAgent;
  const os = /Android/.test(ua) ? "Android" : pwaApple() ? (/iPad/.test(ua) || navigator.maxTouchPoints > 1 && !/iPhone/.test(ua) ? "iPad" : "iPhone")
    : /Windows/.test(ua) ? "Windows" : /Mac OS X/.test(ua) ? "Mac" : /Linux/.test(ua) ? "Linux" : "Device";
  const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome"
    : /Safari\//.test(ua) ? "Safari" : "browser";
  return `${os} · ${browser}${pwaInstalled() ? " (app)" : ""}`;
}

async function pwaRegister() {
  if (!pwaSecure()) return null;
  try {
    PWA.registration = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    PWA.subscription = await PWA.registration.pushManager?.getSubscription() || null;
  } catch (e) { PWA.registration = null; }
  return PWA.registration;
}

window.addEventListener("beforeinstallprompt", event => {
  event.preventDefault();
  PWA.installPrompt = event;
  if (STATE.view === "settings") pwaPaint();
});
window.addEventListener("appinstalled", () => { PWA.installPrompt = null; if (STATE.view === "settings") pwaPaint(); });

/* A notification tapped while Homestead is open: go where it points. */
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.addEventListener("message", event => {
    if (event.data?.type !== "homestead-open") return;
    // The same door as the job tray: the item a job is about is found on its
    // page, never made the site-wide search.
    window.openOperation(event.data.href);
  });
}

/* The number on the app icon follows what is wrong now. */
function pwaBadge(count) {
  if (!navigator.setAppBadge || !pwaInstalled()) return;
  (count ? navigator.setAppBadge(count) : navigator.clearAppBadge()).catch(() => {});
}
window.pwaBadge = pwaBadge;

window.pwaInstall = async () => {
  if (!PWA.installPrompt) return;
  PWA.installPrompt.prompt();
  await PWA.installPrompt.userChoice.catch(() => null);
  PWA.installPrompt = null;
  pwaPaint();
};

const pwaPost = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body), keep: true });

async function pwaKey() {
  if (!PWA.key) PWA.key = await api("/api/push/key", { keep: true });
  return PWA.key;
}

function pwaChosen() {
  const boxes = $$("#pwaCard .pwa-cat input");
  return boxes.length ? boxes.filter(b => b.checked).map(b => b.value) : null;
}

window.pwaEnable = async () => {
  const button = $("#pwaEnable");
  if (button) { button.disabled = true; button.textContent = "Asking…"; }
  try {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      toast(permission === "denied" ? "Notifications are blocked in this browser’s site settings."
        : "Notification permission was not granted.", "bad");
      return pwaPaint();
    }
    const registration = PWA.registration || await pwaRegister();
    if (!registration) throw new Error("The notification service could not start.");
    await navigator.serviceWorker.ready;
    const key = await pwaKey();
    PWA.subscription = await registration.pushManager.getSubscription() ||
      await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: pwaKeyBytes(key.key) });
    await pwaPost("/api/push/subscribe", { subscription: PWA.subscription.toJSON(),
      categories: pwaChosen() || key.defaults, device: pwaDeviceName() });
    toast("Notifications are enabled for this device.", "ok");
  } catch (e) {
    toast(e.message || "Notifications could not be enabled.", "bad");
  }
  pwaPaint();
};

window.pwaDisable = async () => {
  const sub = PWA.subscription;
  try {
    if (sub) {
      await pwaPost("/api/push/unsubscribe", { endpoint: sub.endpoint });
      await sub.unsubscribe();
    }
    PWA.subscription = null;
    toast("Notifications are disabled for this device.", "ok");
  } catch (e) { toast(e.message, "bad"); }
  pwaPaint();
};

window.pwaCategories = async () => {
  if (!PWA.subscription) return;
  try {
    await pwaPost("/api/push/subscribe", { subscription: PWA.subscription.toJSON(), categories: pwaChosen() || [] });
  } catch (e) { toast(e.message, "bad"); }
};

window.pwaTest = async () => {
  if (!PWA.subscription) return;
  const button = $("#pwaTest");
  if (button) { button.disabled = true; button.textContent = "Sending…"; }
  try {
    await pwaPost("/api/push/test", { endpoint: PWA.subscription.endpoint });
    toast("Test notification requested. Check this device’s notifications.", "ok");
  } catch (e) { toast(e.message, "bad"); }
  if (button) { button.disabled = false; button.textContent = "Send a test"; }
};

/* Signing out here stops this device's notifications: they would only say "sign in". */
window.pwaForgetDevice = async () => {
  const sub = PWA.subscription || await PWA.registration?.pushManager?.getSubscription().catch(() => null);
  if (!sub) return;
  await pwaPost("/api/push/unsubscribe", { endpoint: sub.endpoint }).catch(() => null);
  await sub.unsubscribe().catch(() => null);
  PWA.subscription = null;
};

function pwaWhy() {
  if (new URLSearchParams(location.search).get("demo") === "1" || window.HOMESTEAD_DEMO === true) {
    return '<div class="note">Notifications are not part of the demo.</div>';
  }
  if (!window.isSecureContext) {
    return `<div class="note"><b>Needs HTTPS.</b> Browsers only install apps and deliver notifications to a secure address.
      Open Homestead through its <span class="mono">https://</span> hostname — a Cloudflare Tunnel or a reverse proxy
      with a certificate — and this card will offer both. This page is on <span class="mono">${esc(location.origin)}</span>.</div>`;
  }
  if (pwaApple() && !pwaInstalled()) {
    return `<div class="note"><b>Add Homestead to your Home Screen first.</b> On iPhone and iPad, notifications
      reach installed web apps only (iOS 16.4 or later): tap Share, then <b>Add to Home Screen</b>, and open Homestead from there.</div>`;
  }
  return `<div class="note">This browser cannot receive push notifications.</div>`;
}

async function pwaPaint() {
  refreshPwaAlerts();
  const card = $("#pwaCard .pwa-body");
  if (!card) return;
  const installLine = pwaInstalled() ? '<span class="pill ok">installed</span>'
    : PWA.installPrompt ? '<button class="btn sm" onclick="pwaInstall()">Install app</button>'
    : "";
  $("#pwaInstallSlot").innerHTML = installLine;
  if (!pwaPushable() || (pwaApple() && !pwaInstalled())) { card.innerHTML = pwaWhy(); return; }
  let key, status = { known: false }, alerts = { active: [], log: [], devices: [] };
  try {
    [key, alerts] = await Promise.all([pwaKey(), api("/api/alerts", { keep: true })]);
    PWA.subscription = await (PWA.registration || await pwaRegister())?.pushManager.getSubscription() || null;
    if (PWA.subscription) status = await pwaPost("/api/push/status", { endpoint: PWA.subscription.endpoint });
  } catch (e) {
    card.innerHTML = `<div class="empty small">${esc(e.message)}</div>`;
    return;
  }
  const permission = Notification.permission;
  const on = !!(PWA.subscription && status.known && permission === "granted");
  const chosen = on ? status.categories : key.defaults;
  const others = (alerts.devices || []).filter(d => !PWA.subscription || d.tag !== status.tag);
  const state = permission === "denied"
    ? '<span class="pill crit">blocked in browser settings</span>'
    : on ? '<span class="pill ok">on</span>' : '<span class="pill neutral">off</span>';
  card.innerHTML = `
    <div class="pwa-state"><div><b>This device</b> ${state}<div class="dim xs">${esc(pwaDeviceName())}${
      on && status.last_ok ? ` · last sent ${esc(fmtAgo(Math.max(1, Date.now() / 1000 - status.last_ok)))}` : ""}${
      on && status.failures ? ` · ${status.failures} send failures` : ""}</div></div>
      <div class="row">${on
        ? '<button class="btn sm" id="pwaTest" onclick="pwaTest()">Send a test</button><button class="btn sm" onclick="pwaDisable()">Turn off</button>'
        : permission === "denied" ? ""
        : '<button class="btn sm pri" id="pwaEnable" onclick="pwaEnable()">Turn on notifications</button>'}</div></div>
    ${permission === "denied" ? `<div class="note"><b>Notifications are blocked for this site.</b> Allow them in the
      browser's site settings (the icon beside the address), then reload this page.</div>` : ""}
    <div class="pwa-cats">${Object.entries(key.categories).map(([id, label]) => {
      const [name, detail] = label.split(": ");
      return `<label class="pwa-cat switch"><input type="checkbox" value="${esc(id)}" ${chosen.includes(id) ? "checked" : ""}
        ${on ? 'onchange="pwaCategories()"' : ""} ${permission === "denied" ? "disabled" : ""}><span><b>${esc(name)}</b>${detail ? `<span class="dim xs">${esc(detail)}</span>` : ""}</span></label>`;
    }).join("")}</div>
    ${others.length ? `<div class="dim xs">Also on for ${others.length} other device${others.length === 1 ? "" : "s"} of yours: ${
      esc(others.map(d => d.device || "unnamed").join(", "))}</div>` : ""}
    ${pwaRecent(alerts)}`;
}

function pwaRecent(alerts) {
  const rows = (alerts.log || []).slice(-6).reverse();
  if (!rows.length) return '<div class="dim xs">No notifications recorded.</div>';
  return `<div class="sec">Recent alerts</div><div class="settings-list">${rows.map(a => `
    <div class="settings-list-row"><div><b>${esc(a.title)}</b>${a.body ? `<div class="dim xs">${esc(a.body)}</div>` : ""}</div>
      <div class="settings-list-meta"><span class="pill ${a.phase === "resolved" ? "ok" : a.severity === "critical" ? "crit" : a.severity === "degraded" ? "warn" : "neutral"}">${
        a.phase === "resolved" ? "Cleared" : a.phase === "worsened" ? "Worsened" : esc({outage:"Critical",degraded:"Warning",jobs:"Job",joins:"Host",updates:"Update"}[a.category] || "Information")}</span><span class="dim xs">${esc(fmtAgo(Math.max(1, Date.now() / 1000 - a.at)))}</span></div></div>`).join("")}</div>`;
}

function pwaCard() {
  return `${UI.settingsCard(`
    ${UI.moduleHeader(`Notifications on this device`, `Choose what this device receives when Homestead is closed.`, `<span id="pwaInstallSlot"></span>`)}
    <div class="pwa-body"><div class="empty small"><span class="spin2"></span></div></div>
    <div id="pwaAlerts"></div>
  `, {tab:`device`, id:`pwaCard`})}`;
}
const PWA_ALERTS={report:null,at:0,request:null,user:null,timer:null,epoch:0};
function pwaAlertsHtml(report) {
  const active=report?.active || [], pending=active.filter(a=>!a.acknowledged), acknowledged=active.filter(a=>a.acknowledged);
  const rows=items=>UI.insightList(items.map(a=>({title:a.title,detail:a.body,
    tone:a.acknowledged?"info":a.severity==="critical"?"bad":a.severity==="info"?"info":"warn",label:a.acknowledged?"Acknowledged":a.severity==="critical"?"Critical":a.severity==="info"?"Information":"Warning",
    actionsHtml:UI.button("Review",`closeModal();openOperation(${jsArg(a.href || '/')})`)+UI.button(a.acknowledged?"Undo":"Acknowledge",`pwaAcknowledge(${jsArg(a.key)},${jsArg(a.version)},${!!a.acknowledged})`)
  })));
  return UI.lead("Acknowledgement quiets this condition across your devices until it worsens. Health checks remain visible.")+
    (pending.length?rows(pending):'<div class="empty small">No unacknowledged conditions.</div>')+
    (acknowledged.length?UI.more(`Acknowledged · ${acknowledged.length}`,rows(acknowledged)):"");
}
function paintPwaAlerts(report) {
      pwaBadge((report.active || []).filter(a=>!a.acknowledged).length);
      window.paintBell?.();
      const settings=$("#pwaAlerts");if(settings)settings.innerHTML=UI.moduleHeader("Active alerts","",UI.button("Review alerts","pwaAlertsDialog()"))+`<div class="ui-help">${(report.active || []).filter(a=>!a.acknowledged).length} need attention · ${(report.active || []).filter(a=>a.acknowledged).length} acknowledged</div>`;
      const dialog=$("#pwaAlertsDialog");if(dialog){const expanded=!!dialog.querySelector("details[open]");dialog.innerHTML=pwaAlertsHtml(report);if(expanded && dialog.querySelector("details"))dialog.querySelector("details").open=true;}
}
async function refreshPwaAlerts(force=false) {
  if(typeof ME==="undefined" || !ME)return;
  if(PWA_ALERTS.user!==ME){PWA_ALERTS.report=null;PWA_ALERTS.at=0;PWA_ALERTS.user=ME;}
  if(PWA_ALERTS.request)return PWA_ALERTS.request;
  if(!force && Date.now()-PWA_ALERTS.at<15000){paintPwaAlerts(PWA_ALERTS.report);return;}
  const user=ME, epoch=PWA_ALERTS.epoch;
  PWA_ALERTS.request=(async()=>{
    try{
      const report=await api("/api/alerts",{keep:true});if(user!==ME || epoch!==PWA_ALERTS.epoch)return;
      PWA_ALERTS.report=report;PWA_ALERTS.at=Date.now();
      paintPwaAlerts(report);
    }catch(error){if(user===ME && epoch===PWA_ALERTS.epoch && $("#pwaAlertsDialog"))$("#pwaAlertsDialog").innerHTML=UI.callout("warn","Alerts could not be refreshed",esc(error.message))+UI.button("Retry","refreshPwaAlerts(true)");}
    finally{if(epoch===PWA_ALERTS.epoch)PWA_ALERTS.request=null;}
  })();return PWA_ALERTS.request;
}
window.pwaAlertsDialog=async()=>{
  modal("Alerts",`<div id="pwaAlertsDialog"><div class="empty small">Loading alerts…</div></div>${UI.actions(UI.cancel("Close"))}`,true);
  await refreshPwaAlerts(true);
};
window.pwaAcknowledge=async(key,version,undo=false)=>{
  try{await pwaPost("/api/alerts/acknowledge",{key,version,undo});toast(undo?"Acknowledgement removed":"Acknowledged until this condition worsens","ok");}
  catch(error){toast(error.message,"bad");}
  await refreshPwaAlerts(true);
};
window.stopAlertChecks=()=>{clearInterval(PWA_ALERTS.timer);PWA_ALERTS.epoch++;PWA_ALERTS.report=null;PWA_ALERTS.request=null;PWA_ALERTS.at=0;PWA_ALERTS.user=null;};
window.startAlertChecks=()=>{
  clearInterval(PWA_ALERTS.timer);refreshPwaAlerts(true);
  PWA_ALERTS.timer=setInterval(()=>{if(!document.hidden)refreshPwaAlerts();},20000);
};
window.refreshPwaAlerts=refreshPwaAlerts;
window.PWA_ALERTS=PWA_ALERTS;
window.pwaCard = pwaCard;
window.pwaPaint = pwaPaint;

pwaRegister();
