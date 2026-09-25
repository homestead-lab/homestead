/* The installable app: its service worker, the install prompt, and push
   notifications on this device.

   Browsers offer all of this only on a secure origin - HTTPS, or localhost -
   so on a plain-HTTP LAN address the Settings card explains what is missing
   rather than offering buttons that cannot work. */
const PWA = { registration: null, installPrompt: null, subscription: null, key: null };

const pwaSecure = () => window.isSecureContext && "serviceWorker" in navigator &&
  new URLSearchParams(location.search).get("demo") !== "1";
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
      toast(permission === "denied" ? "notifications are blocked for this site in the browser's settings"
        : "notifications were not allowed", "bad");
      return pwaPaint();
    }
    const registration = PWA.registration || await pwaRegister();
    if (!registration) throw new Error("the service worker could not start");
    await navigator.serviceWorker.ready;
    const key = await pwaKey();
    PWA.subscription = await registration.pushManager.getSubscription() ||
      await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: pwaKeyBytes(key.key) });
    await pwaPost("/api/push/subscribe", { subscription: PWA.subscription.toJSON(),
      categories: pwaChosen() || key.defaults, device: pwaDeviceName() });
    toast("notifications are on for this device", "ok");
  } catch (e) {
    toast(e.message || "could not turn notifications on", "bad");
  }
  pwaPaint();
};

window.pwaDisable = async () => {
  const sub = PWA.subscription;
  try {
    if (sub) {
      await pwaPost("/api/push/unsubscribe", { endpoint: sub.endpoint }).catch(() => null);
      await sub.unsubscribe().catch(() => null);
    }
    PWA.subscription = null;
    toast("notifications are off for this device", "ok");
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
    toast("test sent — it should arrive in a few seconds", "ok");
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
  if (new URLSearchParams(location.search).get("demo") === "1") {
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
      on && status.last_ok ? ` · last delivered ${esc(fmtAgo(Math.max(1, Date.now() / 1000 - status.last_ok)))}` : ""}${
      on && status.failures ? ` · ${status.failures} not delivered` : ""}</div></div>
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
  if (!rows.length) return '<div class="dim xs">Nothing has needed telling yet.</div>';
  return `<div class="sec">Recent alerts</div><div class="settings-list">${rows.map(a => `
    <div class="settings-list-row"><div><b>${esc(a.title)}</b>${a.body ? `<div class="dim xs">${esc(a.body)}</div>` : ""}</div>
      <div class="settings-list-meta"><span class="pill ${a.phase === "resolved" ? "ok" : a.severity === "critical" ? "crit" : a.severity === "degraded" ? "warn" : "neutral"}">${
        a.phase === "resolved" ? "resolved" : esc(a.category)}</span><span class="dim xs">${esc(fmtAgo(Math.max(1, Date.now() / 1000 - a.at)))}</span></div></div>`).join("")}</div>`;
}

function pwaCard() {
  return `<section class="card flat settings-wide" id="pwaCard" data-tab="device">
    <div class="settings-card-head"><div><div class="ctitle">Notifications on this device</div>
      <div class="csub">Outages, failed jobs and hosts joining, pushed even when Homestead is closed</div></div>
      <span id="pwaInstallSlot"></span></div>
    <div class="pwa-body"><div class="empty small"><span class="spin2"></span></div></div>
  </section>`;
}
window.pwaCard = pwaCard;
window.pwaPaint = pwaPaint;

pwaRegister();
