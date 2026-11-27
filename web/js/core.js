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
const HOMESTEAD_VERSION = "2.9.1-dev.4";
const ICON_BLOBS = new Map();
// The picture of each logo that has arrived: a panel or dialog drawn again sets it at once (#1211).
const ICON_READY = new Map();
/* An icon that could not be fetched is not asked for again on every refresh: its app shows its initials,
   and it is tried again after ten minutes (or on a full reload, which forgets this). (#906) A failure
   that may pass - no answer, a 5xx, or an answer that is not a picture, such as a proxy's sign-in
   page - is tried again after ICON_RETRY_SOON_MS instead, then twice as long each time it fails again
   up to the ten minutes; a 404 waits the ten minutes at once (#1211). */
const ICON_FAILED = new Map();
const ICON_RETRY_MS = 10 * 60 * 1000;
const ICON_RETRY_SOON_MS = 15 * 1000;
const ICON_FAILS = new Map();      // a logo's failures in a row that may pass: its wait doubles with each
/* What the page fetches as an app's logo: exactly this Homestead's /api/icons/<sha256>.<ext> (the
   names homestead_icons.ICON_NAME allows), or a linked cluster's, named by ?hs_cluster= (#1211).
   A logo annotation is written by anyone who may edit the app, so nothing else is fetched. */
const ICON_PATH = /^\/api\/icons\/[0-9a-f]{64}\.(png|jpg|gif|webp|ico|svg)(\?hs_cluster=[A-Za-z0-9._~%-]+)?$/;
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
const appAvatar = (name, icon, cls = "") => icon && (ICON_PATH.test(icon) || !icon.startsWith("/api/icons/"))
  ? `<span class="av appav ${cls}"><span class="avfallback">${esc(String(name || "?").slice(0, 2).toUpperCase())}</span><img ${ICON_PATH.test(icon) ? `data-icon-src="${esc(icon)}"` : `src="${esc(icon)}"`} alt="" referrerpolicy="no-referrer"></span>`
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
    if (!ICON_PATH.test(url)) { img.remove(); return; }
    const failedAt = ICON_FAILED.get(url);
    if (failedAt !== undefined) {
      if (Date.now() - failedAt < ICON_RETRY_MS) { img.remove(); return; }
      ICON_FAILED.delete(url);
    }
    img.dataset.iconLoading = "1";
    if (ICON_READY.has(url)) { img.src = ICON_READY.get(url); return; }
    let pending = ICON_BLOBS.get(url);
    if (!pending) {
      // The address names the picture's content, so the browser keeps it for the next page load (#1211);
      // a missing one is answered no-store, and asked for again after ICON_RETRY_MS.
      pending = fetch(url, { credentials: "same-origin" }).then(response => {
        if (!response.ok) throw Object.assign(new Error(`icon request failed (${response.status})`), { missing: response.status === 404 });
        if (!/^image\//i.test(response.headers?.get?.("content-type") || "")) throw new Error("the icon answer is not a picture");
        return response.blob();
      }).then(blob => { const objectUrl = URL.createObjectURL(blob); ICON_READY.set(url, objectUrl); ICON_FAILS.delete(url); return objectUrl; });
      ICON_BLOBS.set(url, pending);
      // Once per failure, however many images wait on it: a 404 waits ICON_RETRY_MS; a failure that may
      // pass waits ICON_RETRY_SOON_MS, twice as long each time it fails again, up to ICON_RETRY_MS. It is
      // kept as if it had failed that long before the long wait ends, which is what iconRetryDue reads.
      pending.catch(error => {
        ICON_BLOBS.delete(url);
        const fails = error?.missing ? 0 : (ICON_FAILS.get(url) || 0) + 1;
        if (fails) ICON_FAILS.set(url, fails); else ICON_FAILS.delete(url);
        const wait = fails ? Math.min(ICON_RETRY_SOON_MS * 2 ** (fails - 1), ICON_RETRY_MS) : ICON_RETRY_MS;
        ICON_FAILED.set(url, Date.now() - ICON_RETRY_MS + wait);
      });
    }
    pending.then(objectUrl => { if (img.isConnected) img.src = objectUrl; })
      .catch(() => { if (img.isConnected) img.remove(); });
  });
}

/* A logo is drawn wherever an app is: a page's paint, the details panel, a dialog body written in place,
   a list in a section. Each of those would have to call loadAppIcons itself, so one watcher does it for
   all of them: a change that adds an <img data-icon-src> has its logos loaded once the change is in (#1211). */
(() => {
  if (typeof MutationObserver !== "function" || typeof document === "undefined" || !document.documentElement) return;
  let queued = false;
  const adds = records => records.some(r => [...r.addedNodes].some(n => n.nodeType === 1 &&
    (n.matches("img[data-icon-src]") || n.querySelector("img[data-icon-src]"))));
  new MutationObserver(records => {
    if (queued || !adds(records)) return;
    queued = true;
    queueMicrotask(() => { queued = false; loadAppIcons(document); });
  }).observe(document.documentElement, { childList: true, subtree: true });
})();

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
  $, $$, V, STATE, esc, jsArg, jsq, safeHref, copyText, readClipboard, HOMESTEAD_VERSION, ICON_BLOBS, ICON_READY, ICON_FAILED,
  ICON_RETRY_MS, ICON_RETRY_SOON_MS, ICON_FAILS, ICON_PATH, HEALTH_DEFAULTS, HEALTH, healthPair, sev, gcls, worstMetricClass, loadHealthSettings, paintBrand,
  pageTitle, tip, icon, appAvatar, loadAppIcons, SET, applySettings
};
