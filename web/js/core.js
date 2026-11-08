/* Homestead core — DOM helpers, escaping, the version, health thresholds, the brand
   and settings. The rest is in core-*.js (#911), loaded right after this file. */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const V = () => $("#views");
const STATE = { view: "dash", q: "", data: {}, busy: false };

const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
/* A value as a JavaScript string literal, for an inline handler written
   into HTML: onclick="fn(${jsq(name)})". esc() alone is not enough there -
   the browser turns &#39; back into ' before the handler runs, so a quote in
   the value would end its string. jsArg() is the same literal unescaped, for
   handler text given to UI.button(), which escapes what it is given. */
const jsArg = s => JSON.stringify(String(s ?? ""));
const jsq = s => esc(jsArg(s));
/* A link from data - an app's project page, release notes, a portal tile -
   as an href: http(s), or a path on this site. Anything else, a javascript:
   URL above all, becomes "#", so a click on it does nothing. */
const safeHref = url => {
  const text = String(url ?? "").trim();
  if (/^\/(?!\/)/.test(text)) return esc(text);
  try {
    const parsed = new URL(text);
    return ["http:", "https:"].includes(parsed.protocol) ? esc(parsed.href) : "#";
  } catch (e) { return "#"; }
};
/* Put text on the clipboard. The Clipboard API exists only on HTTPS and
   localhost, and Homestead is often opened at http://its-address, so the
   older copy command stands in there. */
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* not a secure page */ }
  const area = document.createElement("textarea");
  area.value = text; area.setAttribute("readonly", ""); area.style.cssText = "position:fixed;top:-1000px;opacity:0";
  document.body.appendChild(area); area.select();
  let done = false;
  try { done = document.execCommand("copy"); } catch (_) { done = false; }
  area.remove();
  return done;
}
/* The clipboard's text, or null where the page may not read it - an HTTP
   page, or a browser that asks and was refused. */
async function readClipboard() {
  try { return navigator.clipboard?.readText ? await navigator.clipboard.readText() : null; } catch (_) { return null; }
}
const HOMESTEAD_VERSION = "2.8.335-dev.11";
const ICON_BLOBS = new Map();
/* An icon that could not be fetched is not asked for again on every refresh: its app shows its initials,
   and it is tried again after ten minutes (or on a full reload, which forgets this). (#906) */
const ICON_FAILED = new Map();
const ICON_RETRY_MS = 10 * 60 * 1000;
const HEALTH_DEFAULTS = { thresholds: {
  cpu: { warning: 70, critical: 88 }, memory: { warning: 70, critical: 88 },
  disk: { warning: 75, critical: 90 }, temperature: { warning: 70, critical: 85 },
} };
let HEALTH = JSON.parse(JSON.stringify(HEALTH_DEFAULTS));
const healthPair = metric => HEALTH.thresholds?.[metric] || { warning: 70, critical: 88 };
const sev = (p, metric = "cpu") => {
  const t = healthPair(metric); return p >= t.critical ? "b" : p >= t.warning ? "w" : "";
};
const gcls = (p, metric = "cpu") => sev(p, metric) === "b" ? "g-bad" : sev(p, metric) === "w" ? "g-warn" : "g-ok";
const worstMetricClass = metrics => {
  const levels = metrics.map(x => sev(x.value ?? 0, x.metric));
  return levels.includes("b") ? "g-bad" : levels.includes("w") ? "g-warn" : "g-ok";
};
async function loadHealthSettings(force = false) {
  if (STATE.data.appSettings && !force) return STATE.data.appSettings;
  const s = await api("/api/settings").catch(() => HEALTH_DEFAULTS);
  HEALTH = { thresholds: { ...HEALTH_DEFAULTS.thresholds, ...(s.thresholds || {}) } };
  STATE.data.appSettings = s;
  paintBrand(s);
  return s;
}

/* The line under the wordmark: what this installation is called, and what it
   is running. The name is a setting, because "HOMELAB" described nobody. */
function paintBrand(settings = {}) {
  const site = String(settings.site_name || "").trim();
  window.__siteName = site;
  pageTitle();
  const host = $(".bver");
  if (!host) return;
  const version = (settings.info || {}).version || host.dataset.version || "";
  host.textContent = [site, version && "v" + version].filter(Boolean).join(" · ");
  host.title = site ? `${site} · Homestead v${version}` : `Homestead v${version}`;
  const foot = $("#appfoot");
  if (foot) foot.textContent = [site, "Homestead", version && "v" + version]
    .filter(Boolean).join(" · ");
}
window.paintBrand = paintBrand;
// Installed mobile apps scroll their main pane; browser tabs scroll the document.
window.scrollPageTop = () => {
  document.querySelector(".main")?.scrollTo(0, 0);
  window.scrollTo(0, 0);
};
/* The tab and bookmark title: the site first, when it has a name, so tabs and
   bookmarks from several clusters tell themselves apart. */
function pageTitle(view) {
  if (view !== undefined) window.__pageTitle = view;
  const site = window.__siteName || "";
  document.title = [site, window.__pageTitle, "Homestead"].filter(Boolean).join(" · ");
}
window.pageTitle = pageTitle;
const tip = (text, label = "?") => `<span class="tip" tabindex="0" aria-label="${esc(text)}" data-tip="${esc(text)}">${esc(label)}</span>`;
const icon = name => `<svg class="btnicon" aria-hidden="true"><use href="#i-${esc(name)}"/></svg>`;
const appAvatar = (name, icon, cls = "") => icon
  ? `<span class="av appav ${cls}"><span class="avfallback">${esc(String(name || "?").slice(0, 2).toUpperCase())}</span><img ${icon.startsWith("/api/icons/") ? `data-icon-src="${esc(icon)}"` : `src="${esc(icon)}"`} alt="" referrerpolicy="no-referrer"></span>`
  : `<span class="av ${cls}">${esc(String(name || "?").slice(0, 2).toUpperCase())}</span>`;

function loadAppIcons(root = document) {
  root.querySelectorAll(".appav img").forEach(img => {
    if (img.dataset.iconWired) return;
    img.dataset.iconWired = "1";
    const loaded = () => { if (img.previousElementSibling) img.previousElementSibling.hidden = true; };
    const failed = () => img.remove();
    img.addEventListener("load", loaded, { once: true });
    img.addEventListener("error", failed, { once: true });
    if (img.complete) {
      if (img.naturalWidth) loaded();
      else if (img.getAttribute("src")) failed();
    }
  });
  root.querySelectorAll("img[data-icon-src]").forEach(img => {
    if (img.dataset.iconLoading) return;
    const url = img.dataset.iconSrc;
    const failedAt = ICON_FAILED.get(url);
    if (failedAt !== undefined) {
      if (Date.now() - failedAt < ICON_RETRY_MS) { img.remove(); return; }
      ICON_FAILED.delete(url);
    }
    img.dataset.iconLoading = "1";
    let pending = ICON_BLOBS.get(url);
    if (!pending) {
      pending = fetch(url, { credentials: "same-origin", cache: "no-store" }).then(response => {
        if (!response.ok) throw new Error(`icon request failed (${response.status})`);
        return response.blob();
      }).then(blob => URL.createObjectURL(blob));
      ICON_BLOBS.set(url, pending);
      pending.catch(() => ICON_BLOBS.delete(url));
    }
    pending.then(objectUrl => { if (img.isConnected) img.src = objectUrl; })
      .catch(() => { ICON_FAILED.set(url, Date.now()); if (img.isConnected) img.remove(); });
  });
}

/* ---------------- settings ---------------- */
const SET = { theme: "dark", bg: "soft", blur: 26, motion: "on", refresh: 15 };
// Blocked storage or a damaged value: the defaults, not a page that never loads.
try { Object.assign(SET, JSON.parse(localStorage.getItem("homestead.settings") || "{}")); } catch (e) { /* defaults */ }

function applySettings() {
  const root = document.documentElement;
  const dark = SET.theme === "dark" ||
    (SET.theme === "auto" && !window.matchMedia("(prefers-color-scheme: light)").matches);
  root.dataset.theme = dark ? "dark" : "light";
  root.dataset.bg = SET.bg;
  root.style.setProperty("--blur", SET.blur + "px");
  root.dataset.motion = SET.motion;
  try { localStorage.setItem("homestead.settings", JSON.stringify(SET)); } catch (e) { /* this visit only */ }
}
applySettings();

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  $, $$, V, STATE, esc, jsArg, jsq, safeHref, copyText, readClipboard, HOMESTEAD_VERSION, ICON_BLOBS, ICON_FAILED,
  ICON_RETRY_MS, HEALTH_DEFAULTS, HEALTH, healthPair, sev, gcls, worstMetricClass, loadHealthSettings, paintBrand,
  pageTitle, tip, icon, appAvatar, loadAppIcons, SET, applySettings
};
