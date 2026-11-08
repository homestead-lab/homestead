/* Homestead App Store, and an app's primary port (#911). One of the scripts views-workloads.js was split into; they share the page's globals and load in the order index.html lists them. */
/* ---------------- app store ----------------
   The Community Applications catalogue, laid out the way Community
   Applications lays it out: a front page of this month's spotlights, the
   newest templates and what is trending, and a page for each. Any app opens a
   full description - project, support, spotlight note, what it asks for -
   before anything is configured. */
let STORE_MODE = "home";
const STORE_MODES = {
  home: ["Home", ""],
  spotlight: ["Spotlight", "Picked by the Unraid team each month, newest first"],
  recent: ["Recently added", "The newest templates, by the date the feed first saw them"],
  trending: ["Top trending", "Rising fastest in downloads right now"],
  popular: ["Top performing", "The best performers in the feed"],
};
const STORE_APPS = new Map();

function storeCount(value) {
  return new Intl.NumberFormat(undefined, { notation: Number(value || 0) >= 10000 ? "compact" : "standard",
    maximumFractionDigits: 1 }).format(Number(value || 0));
}

const storeKey = app => `${app.name}|${app.repo}`;
const storeDate = seconds => seconds ? fmtDay(seconds) : "";

function storeMetric(app, mode) {
  if (mode === "spotlight" && app.spotlight) return `${icon("clock")} Spotlight · ${esc(app.spotlight.month)}`;
  if (mode === "recent" && app.first_seen) return `${icon("clock")} Added ${storeDate(app.first_seen)}`;
  if (mode === "trending" && (app.top_trending || app.trending)) {
    return `${icon("update")} ${Number(app.top_trending || app.trending || 0).toFixed(1)}% trend`;
  }
  if (mode === "popular" && app.top_performing) return `${icon("update")} ${Number(app.top_performing).toFixed(1)}% performance`;
  return app.downloads ? `${icon("import")} ${storeCount(app.downloads)} downloads` : `${icon("box")} Community template`;
}

function storeIcon(app, cls = "ico") {
  return app.icon ? `<img class="${cls}" src="${esc(app.icon)}" alt="" referrerpolicy="no-referrer" onerror="this.style.display='none'">`
    : `<span class="${cls} store-noicon">${icon("store")}</span>`;
}

function storeCard(app, mode) {
  STORE_APPS.set(storeKey(app), app);
  const key = esc(JSON.stringify(storeKey(app)));
  const label = appCategoryLabel(app.categories || app.cat);
  return `<div class="card app" role="button" tabindex="0" onclick="storeDetails(${key})"
      onkeydown="if(event.key==='Enter')storeDetails(${key})">
    <div class="row" style="gap:11px">${storeIcon(app)}
      <div style="min-width:0"><div class="nm">${esc(app.name)}</div>
        <div class="dim xs store-by">${esc(app.maintainer || "")}${app.official ? ' · <span class="store-official">official</span>' : ""}</div></div></div>
    <div class="row store-tags">${label ? UI.chip(label, "neutral") : ""}
      ${app.deploy?.app_profile ? UI.chip(app.deploy.app_profile.label, app.deploy.app_profile.level === "dependency" ? "warn" : "neutral") : ""}
      ${app.beta ? UI.chip("beta", "warn") : ""}</div>
    <div class="store-metric">${storeMetric(app, mode)}</div>
    ${mode === "spotlight" && app.spotlight?.reason ? `<div class="store-why">“${esc(app.spotlight.reason)}”</div>` : ""}
    <div class="ds">${esc(app.desc || "No description provided.")}</div>
    <div class="rp">${esc(app.repo)}</div>
    <div class="row store-card-actions"><button class="btn sm" onclick="event.stopPropagation();storeDetails(${key})">Details</button>
      <button class="btn pri sm" onclick="event.stopPropagation();storeInstall(${key})">Configure &amp; deploy</button></div></div>`;
}

/* Say where the listings come from, once the server has said: the public feed,
   or one set in Settings. The trademark line shows whatever the feed. */
function storeSource(source) {
  const host = $("#s_source");
  if (!host) return;
  const feed = !source ? ""
    : source.default
    ? `Listings are read on demand from the public <a href="https://github.com/Squidly271/AppFeed" target="_blank" rel="noopener">Community Applications feed</a> and cached for six hours; another feed can be set in Settings. Homestead is independent and is not endorsed by the catalogue maintainers. `
    : `Listings are read from <span class="mono">${esc(source.url)}</span>, set in Settings, and cached for six hours. Templates come from whoever publishes that feed; review what each one asks for before deploying it. `;
  host.innerHTML = `${feed}Unraid® is a registered trademark of Lime Technology, Inc. This application is not affiliated with, endorsed, or sponsored by Lime Technology, Inc.`;
}

function storeSection(mode, apps, total) {
  const [title, detail] = STORE_MODES[mode];
  return `<section class="store-section">
    <div class="store-section-head"><div><h3>${title}</h3><span>${detail}</span></div>
      <button class="btn sm" onclick="storeBrowse(${jsq(mode)})">Show more</button></div>
    ${apps.length ? `<div class="apps">${apps.map(a => storeCard(a, mode)).join("")}</div>` : '<div class="empty small">Nothing here yet.</div>'}
  </section>`;
}

async function viewStore() {
  resetPaint();
  paint(`${UI.pageHeader(`Community catalogue`, `Third-party Community Applications templates adapted into reviewed Kubernetes workloads`, `<input class="search" id="s_q" aria-label="Search the App Store" placeholder="plex, nextcloud, jellyfin…" value="${esc(STATE.q)}" style="width:260px;padding-left:16px">
      <button class="btn pri" onclick="storeSearch()">Search</button>`, {actionsClass:`store-search`})}
    <div class="store-browse-head"><div class="seg store-modes" id="s_modes">
      ${Object.entries(STORE_MODES).map(([mode, [label]]) => `<button data-mode="${mode}" class="${STORE_MODE === mode ? "on" : ""}" onclick="storeBrowse(${jsq(mode)})">${label}</button>`).join("")}
    </div></div>
    <div id="s_res"><div class="empty"><span class="spin2"></span>loading catalogue…</div></div>
    <div class="note catalogue-notice" id="s_source"></div>`);
  $("#s_q").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); storeSearch(); } });
  storeSource(null);
  if (STATE.q) storeSearch(); else storeBrowse(STORE_MODE);
}
window.storeSearch = async () => {
  const q = $("#s_q").value.trim();
  if (!q) return storeBrowse(STORE_MODE);
  $$("#s_modes button").forEach(button => button.classList.remove("on"));
  $("#s_res").innerHTML = `<div class="empty"><span class="spin2"></span>searching catalogue…</div>`;
  try {
    const r = await api("/api/appstore?q=" + encodeURIComponent(q));
    storeSource(r.source);
    $("#s_res").innerHTML = r.apps.length
      ? `<div class="dim small" style="margin-bottom:12px">${r.total} match${r.total === 1 ? "" : "es"} · showing ${r.apps.length}</div>
        <div class="apps stagger">${r.apps.map(a => storeCard(a, "search")).join("")}</div>`
      : `<div class="empty">nothing matched “${esc(q)}”</div>`;
  } catch (e) { $("#s_res").innerHTML = UI.error(e, { retry: "storeSearch()" }); }
};
window.storeBrowse = async mode => {
  STORE_MODE = STORE_MODES[mode] ? mode : "home";
  STATE.q = "";
  if ($("#s_q")) $("#s_q").value = "";
  $$("#s_modes button").forEach(button => button.classList.toggle("on", button.dataset.mode === STORE_MODE));
  $("#s_res").innerHTML = `<div class="empty"><span class="spin2"></span>loading catalogue…</div>`;
  try {
    const r = await api("/api/appstore?sort=" + encodeURIComponent(STORE_MODE));
    storeSource(r.source);
    if (r.sections) {
      $("#s_res").innerHTML = ["spotlight", "recent", "trending", "popular"]
        .map(section => storeSection(section, r.sections[section] || [])).join("");
      return;
    }
    const [title, detail] = STORE_MODES[STORE_MODE];
    $("#s_res").innerHTML = `<div class="store-section-head"><div><h3>${title}</h3><span>${detail}</span></div><b>${r.apps.length}</b></div>
      ${r.apps.length ? `<div class="apps stagger">${r.apps.map(a => storeCard(a, STORE_MODE)).join("")}</div>` : `<div class="empty">No apps to show.</div>`}`;
  } catch (e) { $("#s_res").innerHTML = UI.error(e, { retry: `storeBrowse(${jsArg(mode)})` }); }
};

window.storeInstall = key => {
  const a = STORE_APPS.get(key);
  if (!a) return toast("That app is no longer in the catalogue", "bad");
  closeModal();
  window.__deployPrefill = a.deploy || { name: a.name, image: a.repo, icon: a.icon || "" };
  go("deploy");
  toast(`"${a.name}" loaded — check storage paths before deploying`);
};

const STORE_LINKS = [["project", "Project"], ["support", "Support"], ["github", "GitHub"], ["registry", "Registry"],
  ["readme", "Read me"], ["video", "Video"], ["discord", "Discord"], ["web", "Website"]];

/* What Homestead makes of a template, for Details. app_profile.blocked is
   true or false (#1294), never a list: what blocks it is in its notes. */
function storeProfileHtml(profile) {
  if (!profile) return "";
  const said = [...(profile.blocked ? (profile.notes || []).slice(0, 1) : []), ...(profile.dependencies || []), ...(profile.notes || [])]
    .map(n => typeof n === "string" ? n : (n.message || n.reason || n.name || "")).filter(Boolean);
  const lines = [...new Set(said)].slice(0, 4);
  return UI.callout(profile.blocked ? "bad" : profile.level === "dependency" ? "warn" : "", { html: `${esc(profile.label)}.` },
    esc(lines.join(" · ") || "Homestead reviews its storage, ports and hardware when you configure it."));
}

/* One app, in full: what it is, who keeps it, why it was picked, what it asks for. */
window.storeDetails = async key => {
  const summary = STORE_APPS.get(key) || {};
  modal(summary.name || "App", '<div class="empty"><span class="spin2"></span>reading the template</div>', true, "store");
  let app;
  try { app = Object.assign({}, summary, await api("/api/appstore/app?key=" + encodeURIComponent(key))); }
  catch (e) {
    $("#mbody").innerHTML = UI.error(e, { retry: `storeDetails(${jsArg(key)})`, dialog: true,
      title: e.body?.loading ? "The catalogue is still loading" : "Couldn't read this app" });
    return;
  }
  STORE_APPS.set(key, app);
  // A template Homestead reads oddly still shows an error with Try again, not a dialog stuck loading (#1294).
  try { storeDetailsPaint(key, app); }
  catch (e) { $("#mbody").innerHTML = UI.error(e, { retry: `storeDetails(${jsArg(key)})`, dialog: true, title: "This app could not be shown" }); }
};
function storeDetailsPaint(key, app) {
  const jsKey = esc(JSON.stringify(key));
  const profile = app.deploy?.app_profile;
  const facts = [
    ["Categories", (app.categories || []).join(", ")],
    ["Maintainer", app.maintainer],
    ["Added", storeDate(app.first_seen)],
    ["Updated", storeDate(app.last_update)],
    ["Downloads", app.downloads ? storeCount(app.downloads) : ""],
    ["Stars", app.stars ? String(app.stars) : ""],
    ["Image", app.repo],
    ["Network", app.network],
    ["License", app.license],
  ].filter(([, value]) => value);
  $("#mbody").innerHTML = `
    <div class="store-detail-head">${storeIcon(app, "store-detail-icon")}
      <div class="store-detail-title"><h3>${esc(app.name)}</h3>
        <div class="dim small">${esc(app.maintainer || "")}${app.official ? ' · <span class="store-official">official container</span>' : ""}</div>
        <div class="row store-tags">${(app.categories || []).slice(0, 4).map(c => UI.chip(c, "neutral")).join("")}
          ${app.beta ? UI.chip("beta", "warn") : ""}${app.privileged ? UI.chip("asks for privileged", "bad") : ""}</div></div>
      <button class="btn pri" onclick="storeInstall(${jsKey})">Configure &amp; deploy</button></div>
    <div class="row store-links">${STORE_LINKS.filter(([k]) => app.links?.[k]).map(([k, label]) =>
      `<a class="btn sm" href="${safeHref(app.links[k])}" target="_blank" rel="noopener noreferrer">${label} ${icon("ext")}</a>`).join("")}</div>
    ${app.spotlight ? `<div class="store-spot"><div class="store-spot-badge"><b>Monthly<br>spotlight</b><span>${esc(app.spotlight.month)}</span></div>
      <div><b>Why it was picked</b><p>${esc(app.spotlight.reason || "")}</p>${app.spotlight.who ? `<span class="dim xs">— ${esc(app.spotlight.who)}</span>` : ""}</div></div>` : ""}
    <div class="store-overview">${esc(app.overview || app.desc || "No description provided.")}</div>
    ${app.comment ? `${UI.callout("warn", { html: `From the catalogue moderators:` }, `${esc(app.comment)}`)}` : ""}
    ${app.requires ? `<div class="note"><b>Requires:</b> ${esc(app.requires)}</div>` : ""}
    ${storeProfileHtml(profile)}
    ${(app.screenshots || []).length ? `<div class="sec">Screenshots</div><div class="store-shots">${app.screenshots.map(src =>
      `<a href="${safeHref(src)}" target="_blank" rel="noopener noreferrer"><img src="${esc(src)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.parentElement.remove()"></a>`).join("")}</div>` : ""}
    <div class="sec">Details</div>
    <div class="store-facts">${facts.map(([label, value]) => `<div><span>${label}</span><b class="${label === "Image" ? "mono" : ""}">${esc(value)}</b></div>`).join("")}</div>`;
};

/* The port a card links to first: the app's web UI, usually. */
window.wlPrimaryPort = (ns, name) => {
  const w = (STATE.data.wl || []).find(x => x.ns === ns && x.name === name) || { ports: [] };
  const current = (w.ports.find(p => p.primary) || {}).port || 0;
  modal(`Main port · ${name}`, `<p class="small">The card links to this port first - usually the app's web UI.</p>
    <div class="primary-ports">${w.ports.map(p => `<label class="switch"><input type="radio" name="pp" value="${p.port}" ${p.port === current ? "checked" : ""}>
      <b class="mono">${p.port}</b> <span class="dim xs">${esc(p.name || "")}${p.ip ? ` · ${esc(p.ip)}` : ""}</span></label>`).join("")}
      <label class="switch"><input type="radio" name="pp" value="0" ${current ? "" : "checked"}> <span class="dim">No preference - their own order</span></label></div>
    ${UI.actions(`<button class="btn pri" onclick="wlPrimaryPortSave(${jsq(ns)},${jsq(name)})">Save</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
};
window.wlPrimaryPortSave = async (ns, name) => {
  const port = +(document.querySelector('input[name="pp"]:checked')?.value || 0);
  try {
    const r = await api("/api/workload/primary-port", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ns, name, port }) });
    toast(r.detail, "ok"); closeModal(); refresh(true);
  } catch (e) { toast(e.message, "bad"); }
};

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  STORE_MODE, STORE_MODES, STORE_APPS, storeCount, storeKey, storeDate, storeMetric, storeIcon, storeCard, storeSource, storeSection, viewStore, STORE_LINKS, storeProfileHtml, storeDetailsPaint
};
