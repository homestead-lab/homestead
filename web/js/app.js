/* Homestead — router, appearance, mobile nav, quiet refresh loop */

/* Each page's sidebar section, how it is drawn and whether it refreshes
   itself. Its title is ROUTES.label in router.js, the one source (#508). */
const VIEWS = {
  dash:      ["overview",  viewDash,      true],
  timeline:  ["overview",  () => viewTimeline(), true],
  flow:      ["overview",  viewFlow,      true],
  nodes:     ["overview",  viewNodes,     true],
  network:   ["system",    viewNetworking,true],
  portal:    ["overview",  viewPortal,    true],
  workloads: ["workloads", viewWorkloads, true],
  vms:       ["workloads", viewVMs,       true],
  vmimport:  ["workloads", () => viewVmImport(), false],
  deploy:    ["workloads", () => viewDeploy(), false],
  store:     ["workloads", viewStore,     false],
  helm:      ["workloads", viewHelm,      true],
  shares:    ["storage",   viewShares,    false],
  storage:   ["storage",   viewStorage,   true],
  images:    ["storage",   viewImages,    false],
  protect:   ["storage",   viewProtect,   true],
  schedules: ["system",    viewSchedules, true],
  "schedule-week": ["system", () => viewScheduleWeek(), true],
  monitoring: ["workloads", () => viewMonitoring(), true],
  imports:   ["system",    viewImport,    true],
  events:    ["system",    viewEvents,    true],
  resources: ["system",    viewResources, true],
  cluster:   ["system",    viewCluster,   true],
  settings:  ["system",    viewSettings,  false],
  setup:     ["system",    () => viewSetup(), false],
  agents:    ["system", () => viewAgents(), true],
};

const FILTERABLE_VIEWS = new Set(["workloads", "storage", "images", "events", "store", "network", "portal", "helm"]);

function routeParamsForView(v, extra = {}) {
  const params = FILTERABLE_VIEWS.has(v) && STATE.q ? { q: STATE.q } : {};
  if (new URLSearchParams(location.search).get("demo") === "1") params.demo = "1";
  return Object.assign(params, extra);
}

function renderBreadcrumb(v, detail = "") {
  const host = $("#crumb");
  host.innerHTML = (window.fleetCrumb ? fleetCrumb() : "") + HomesteadRouter.breadcrumbs(v, detail).map((item, index) => {
    const sep = index ? '<span class="crumbsep" aria-hidden="true">/</span>' : "";
    if (item.current) return sep + `<span aria-current="page">${esc(item.label)}</span>`;
    // A section, or a detail with no page behind it, is text rather than a link.
    if (!item.url) return sep + `<span class="crumbsection">${esc(item.label)}</span>`;
    return sep + `<a href="${esc(item.url)}" data-route-view="${esc(HomesteadRouter.resolve(item.url).view)}">${esc(item.label)}</a>`;
  }).join("");
  $$("a[data-route-view]", host).forEach(a => a.onclick = e => {
    e.preventDefault(); go(a.dataset.routeView);
  });
}
window.renderBreadcrumb = renderBreadcrumb;

function setModalRoute(params, detail) {
  STATE.modalRoute = true;
  STATE.modalDetail = detail || "";
  const url = HomesteadRouter.urlFor(STATE.view, routeParamsForView(STATE.view, params));
  const current = window.location.pathname + window.location.search;
  if (url !== current) window.history.pushState({ view: STATE.view, modal: true }, "", url);
  renderBreadcrumb(STATE.view, STATE.modalDetail);
}
window.setModalRoute = setModalRoute;

function clearModalRoute() {
  if (!STATE.modalRoute) return;
  STATE.modalRoute = false;
  STATE.modalDetail = "";
  STATE.deepLinkToken = "";
  const url = HomesteadRouter.urlFor(STATE.view, routeParamsForView(STATE.view));
  window.history.replaceState({ view: STATE.view }, "", url);
  renderBreadcrumb(STATE.view);
}
window.clearModalRoute = clearModalRoute;

async function applyDeepLink(v, params) {
  const token = window.location.pathname + window.location.search;
  if (STATE.deepLinkToken === token) return;
  STATE.deepLinkToken = token;

  // ?job=<id>, from a failed job's notification opened in a new window.
  if (params.job) {
    const url = new URL(window.location.href);
    url.searchParams.delete("job");
    window.history.replaceState({ view: v }, "", url.pathname + url.search);
    return window.openJob?.(params.job);
  }
  if(v === "cluster" && params.section === "health") {
    window.focusInPage?.("clusterHealth");
    renderBreadcrumb(v,"Health");
    return;
  }
  let detail = "";
  let open = null;
  if (v === "nodes" && params.node) {
    // A page, not a dialog: the view drew it; the breadcrumb and title name it.
    renderBreadcrumb(v, params.node);
    $("#title").textContent = params.node;
    pageTitle(params.node);
    // A drive named too opens in the node's Storage section.
    if (params.disk) window.smartDisk?.(params.node, params.disk);
    return;
  } else if (v === "workloads" && params.panel === "edit" && params.ns && params.workload) {
    detail = params.workload;
    open = () => wlEdit(params.ns, params.workload, true);
  } else if (v === "workloads" && params.panel === "logs" && params.ns && params.workload) {
    const workload = (STATE.data.wl || []).find(x => x.ns === params.ns && x.name === params.workload);
    const pod = workload?.pods?.[0]?.name || "";
    if (workload) {
      detail = params.workload + " logs";
      open = () => wlLogs(params.ns, pod, params.workload, true);
    }
  } else if (v === "vms" && params.panel === "schedule" && params.ns && params.vm) {
    detail = params.vm + " schedule";
    open = () => vmSchedule(params.ns, params.vm);
  } else if (v === "workloads" && params.panel === "schedule" && params.ns && params.workload) {
    detail = params.workload + " schedule";
    open = () => wlSchedule(params.ns, params.workload);
  } else if (v === "vms" && params.panel === "monitoring" && params.ns && params.vm) {
    detail = params.vm + " monitoring";
    open = () => vmMonitoring(params.ns, params.vm);
  } else if (v === "workloads" && ["monitoring", "answering"].includes(params.panel) && params.ns && params.workload) {
    detail = params.workload + " monitoring";
    open = () => wlMonitoring(params.ns, params.workload);
  } else if (v === "workloads" && params.panel === "console" && params.ns && params.workload) {
    const workload = (STATE.data.wl || []).find(x => x.ns === params.ns && x.name === params.workload);
    if (workload) {
      detail = params.workload + " console";
      open = () => wlConsole(params.ns, params.workload, true);
    }
  } else if (v === "workloads" && params.panel === "updates") {
    // An image update's alert: the list of updates to review, not the page.
    detail = "Image updates";
    open = () => imageUpdateCenter();
  } else if (v === "workloads" && params.panel === "logalerts") {
    // A log alert's link: the list of log alerts, with their last hour.
    detail = "Log alerts";
    open = () => logAlerts();
  } else if (v === "workloads" && params.panel === "rebalance") {
    // A "host is back" alert's link: the suggested moves, not the page.
    detail = "Balance hosts";
    open = () => containerRebalance();
  } else if (v === "storage" && params.panel === "edit" && params.ns && params.volume) {
    const volume = (STATE.data.vols || []).find(x =>
      (x.namespace || "lab") === params.ns && (x.pvc_name || x.name) === params.volume);
    if (volume) {
      detail = params.volume;
      open = () => volumeEdit(volume, true);
    }
  }

  const requested = params.node || params.panel;
  if (!open) {
    if (requested) {
      toast("That linked resource is no longer available", "bad");
      STATE.modalRoute = true;
      clearModalRoute();
    }
    return;
  }
  STATE.modalRoute = true;
  STATE.modalDetail = detail;
  renderBreadcrumb(v, detail);
  open();
}

function go(v, options = {}) {
  if (STATE.view === "dash" && window.Dashboard?.editing() && !options.leave) {
    if (v === "dash") return;
    Dashboard.leave().then(ok => {
      if (ok) go(v, { ...options, leave: true });
      else if (options.fromLocation) window.history.replaceState({view:"dash"}, "", HomesteadRouter.urlFor("dash", routeParamsForView("dash")));
    });
    return;
  }
  // Leaving Settings with changes not saved asks first.
  if (STATE.view === "settings" && v !== "settings" && STATE.settingsDirty?.size && !options.leave) {
    settingsLeave().then(ok => { if (ok) go(v, { ...options, leave: true }); });
    return;
  }
  if (!VIEWS[v]) return;
  window.HomesteadRecorder?.note("navigation", { action: v });
  if (window.setupNavigation) setupNavigation(v);
  if (!$("#modal").classList.contains("hidden")) closeModal(false);
  // Anything still loading belongs to the page being left behind - and a
  // quiet refresh waiting on it never finishes (its reads are abandoned), so
  // it no longer counts as running. Its reads are cancelled, so they stop
  // holding the browser's connections the new page needs.
  window.NAV_TOKEN++;
  window.abortPageReads?.();
  STATE.busy = false;
  STATE.view = v;
  // A search narrows the page it was typed on. Going somewhere else starts
  // unfiltered - unless a search result was what led here, or the address
  // (back, forward, a link) says what to show.
  if (!options.keepSearch && !options.fromLocation && STATE.q) {
    STATE.q = "";
    $("#globalSearch").value = "";
    $("#searchResults")?.classList.add("hidden");
    document.body.classList.remove("searching");
    $("#searchbtn")?.classList.remove("filtering");
  }
  const locationParams = options.fromLocation ? HomesteadRouter.queryParams(window.location.search) : null;
  // The card a link names (#settingsHostUpdates), and a row to mark in it.
  let anchor = options.anchor || "", mark = options.mark || "";
  if (options.fromLocation && window.location.hash) {
    try { anchor = decodeURIComponent(window.location.hash.slice(1)); } catch (_) { /* no card */ }
  }
  if (locationParams) {
    STATE.q = typeof locationParams.q === "string" ? locationParams.q.trim() : "";
    $("#globalSearch").value = STATE.q;
    const find = typeof locationParams.find === "string" ? locationParams.find.trim() : "";
    if (find) {
      // Found and marked once, then dropped from the address, so a reload
      // does not look for it again. With a card named, it is marked in there.
      if (anchor) mark = find;
      else setTimeout(() => window.highlightInPage && highlightInPage(find), 300);
      const clean = new URL(window.location.href);
      clean.searchParams.delete("find");
      window.history.replaceState(window.history.state, "", clean.pathname + clean.search);
    }
  }
  const [, fn] = VIEWS[v], t = HomesteadRouter.ROUTES[v].label;
  $("#title").textContent = t;
  renderBreadcrumb(v);
  pageTitle(t);
  if (options.history !== false) {
    const url = HomesteadRouter.urlFor(v, routeParamsForView(v, options.params));
    const current = window.location.pathname + window.location.search;
    if (url !== current) window.history[options.replace ? "replaceState" : "pushState"]({ view: v }, "", url);
  }
  STATE.modalRoute = false;
  STATE.modalDetail = "";
  STATE.deepLinkToken = "";
  const home = HomesteadRouter.navView(v);
  $$("#nav a, #bottombar a[data-view]").forEach(a => a.classList.toggle("on", a.dataset.view === home));
  closeNav();
  resetPaint();
  pagePlaceholder(v);
  // A new page starts at its top, not wherever the last one was scrolled to.
  window.scrollPageTop();
  // The page is loading: a refresh tick (or coming back to the tab) waits for
  // it rather than starting a second load beside it. Its own marker, not
  // STATE.busy: views read STATE.busy as "a quiet refresh is running".
  const page = window.NAV_TOKEN;
  STATE.loadingFor = page; STATE.loadingSince = Date.now();
  Promise.resolve(fn()).then(() => applyDeepLink(v, locationParams || options.params || {}))
    .then(() => { if (anchor && STATE.view === v) window.focusInPage?.(anchor, mark); })
    .catch(e => { resetPaint(); V().innerHTML = UI.error(e, { retry: `go(${jsArg(v)})` }); })
    .finally(() => {
      if (STATE.loadingFor !== page) return;
      STATE.loadingFor = null;
      // A refresh asked for while the page loaded (after a change was saved,
      // say) runs now, so the page shows what the load may have missed.
      if (STATE.refreshAfterLoad === page) { STATE.refreshAfterLoad = null; refresh(true); }
    });
}

/* A page load still running - for at most 20 s, so a load stuck on a read
   that never answers cannot hold the refresh tick off for good. */
const LOAD_HOLDS_REFRESH_MS = 20000;
function pageLoading(page) {
  return STATE.loadingFor === page && Date.now() - (STATE.loadingSince || 0) < LOAD_HOLDS_REFRESH_MS;
}
window.go = go;

/* quiet refresh — repaints in place, never rebuilds the view */
async function refresh(force) {
  // Phone resume/visibility events also run while the sign-in gate is open.
  if (!$("#gate").classList.contains("hidden")) return false;
  const [, fn, live] = VIEWS[STATE.view];
  if (!live && !force) return false;
  // One at a time - on this page. A refresh of a page since left waits on
  // abandoned reads for ever, and must not hold up the next.
  const page = window.NAV_TOKEN;
  if (STATE.busy && STATE.busyFor === page) return false;
  // While the page is still loading, a tick has nothing to add; a refresh
  // asked for on purpose waits and runs once the load has finished.
  if (pageLoading(page)) { if (force) STATE.refreshAfterLoad = page; return false; }
  STATE.busy = true; STATE.busyFor = page;
  try { await fn(); return true; }
  catch (e) { if (force) toast(e.message || "Could not refresh this page", "bad"); return false; }
  finally { if (STATE.busyFor === page) STATE.busy = false; }
}
window.refresh = refresh;

let timer = null;
function startLoop() {
  clearInterval(timer);
  clearInterval(window.__loopTimer);
  const s = Math.max(5, +SET.refresh || 15);
  timer = setInterval(() => {
    if (document.hidden) return;                       // tab in background
    if (!$("#modal").classList.contains("hidden")) return; // modal open
    if (STATE.settingsDirty?.size) return;              // settings being edited
    refresh();
  }, s * 1000);
  window.__loopTimer = timer;
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

/* ---------------- nav ---------------- */
$$("#nav a, #bottombar a[data-view]").forEach(a => a.onclick = e => { e.preventDefault(); go(a.dataset.view); });
const moreLink = $("#bottombar a[data-more]");
if (moreLink) moreLink.onclick = e => { e.preventDefault(); openNav(); };

window.addEventListener("popstate", () => {
  closeModal(false);
  const route = HomesteadRouter.resolve(window.location.pathname);
  go(route.view, { history: false, fromLocation: true });
});
function openNav() { document.body.classList.add("navopen"); }
function closeNav() { document.body.classList.remove("navopen"); }
$("#hamburger").onclick = () => document.body.classList.toggle("navopen");
$("#navscrim").onclick = closeNav;

let searchTimer = null, searchHits = [], searchActive = -1;
const SEARCH_ICONS = { Container: "box", VM: "vm", Node: "node", Volume: "disk", Share: "share", "App Store": "store", Page: "list", Settings: "gear" };
function searchClose() {
  const out = $("#searchResults"), input = $("#globalSearch");
  out?.classList.add("hidden");
  input?.setAttribute("aria-expanded", "false");
  input?.removeAttribute("aria-activedescendant");
  searchActive = -1;
}
function searchMark(index) {
  searchActive = index;
  $$("#searchResults .sresult").forEach((option, i) => option.setAttribute("aria-selected", String(i === index)));
  const option = $(`#searchResult${index}`), input = $("#globalSearch");
  if (option) { input.setAttribute("aria-activedescendant", option.id); option.scrollIntoView({ block: "nearest" }); }
  else input.removeAttribute("aria-activedescendant");
}
// A result opens its own item, not only the page it is on.
async function searchOpen(index) {
  const hit = searchHits[index];
  if (!hit) return;
  searchClose();
  const open = hit.open;
  if (open.type === "workload") return openUpdateWorkload(open.name);
  if (open.type === "vm") { go("vms"); return vmOpen(open.ns, open.name); }
  if (open.type === "node") { go("nodes"); return nodeDetail(open.name); }
  if (open.type === "volume") return openWorkloadVolume(open.name);
  if (open.type === "share") { go("shares"); return highlightInPage(open.name); }
  if (open.type === "store") { STORE_APPS.set(storeKey(open.app), open.app); go("store"); return storeDetails(storeKey(open.app)); }
  if (open.type === "settings") return go("settings", { params: { tab: open.tab } });
  return go(open.view, open.tab ? { params: { tab: open.tab } } : {});
}
async function globalSearch(q) {
  const out = $("#searchResults"), input = $("#globalSearch");
  if (!q) { out.innerHTML = ""; searchHits = []; return searchClose(); }
  out.classList.remove("hidden");
  input.setAttribute("aria-expanded", "true");
  out.innerHTML = '<div class="searchloading" role="presentation"><span class="spin2"></span> searching…</div>';
  try {
    const cached = (key, path) => STATE.data[key] ? Promise.resolve(STATE.data[key]) : api(path);
    // Optional parts: one that could not be read is said under the results, and
    // the rest is still searched; it is not kept as none for the pages (#620).
    const unread = [];
    const part = (read, what) => read.catch(e => { unread.push(`${what}: ${e.message}`); return null; });
    const [wls, nodes, vols, vms, shares, store] = await Promise.all([
      cached("wl", "/api/workloads"), cached("nodes", "/api/nodes"), cached("vols", "/api/volumes"),
      part(cached("vms", "/api/vms"), "the VMs"),
      // Shares are an admin's to see: for anyone else they are not a read that failed.
      can("admin") ? part(cached("shares", "/api/shares"), "the shares") : Promise.resolve(null),
      part(api("/api/appstore?q=" + encodeURIComponent(q)), "the App Store"),
    ]);
    Object.assign(STATE.data, { wl: wls, nodes, vols }, vms ? { vms } : {}, shares ? { shares } : {});
    if (q !== STATE.q) return; // a newer search has started
    const pages = Object.entries(HomesteadRouter.ROUTES).filter(([view]) => VIEWS[view])
      .map(([view, route]) => ({ view, label: route.label, section: route.section }))
      .concat(typeof NETWORK_SECTIONS !== "undefined" ? NETWORK_SECTIONS.filter(([tab]) => tab !== "overview")
        .map(([tab, label]) => ({ view: "network", tab, label, section: "Networking" })) : []);
    searchHits = HomesteadSearch.hits(q, { workloads: wls, nodes, volumes: vols, vms: vms || [], shares: shares || [], apps: (store?.apps || []).slice(0, 5),
      pages, settings: typeof SETTINGS_SECTIONS !== "undefined" ? SETTINGS_SECTIONS : [] });
    out.innerHTML = searchHits.length ? searchHits.map((x, i) => `<div class="sresult" role="option" id="searchResult${i}" aria-selected="false" data-index="${i}">
      ${x.icon ? `<img src="${esc(x.icon)}" alt="" onerror="this.remove()">` : `<span class="smark">${icon(SEARCH_ICONS[x.kind] || "search")}</span>`}
      <span><b>${esc(x.name)}</b><small>${esc(x.kind)} · ${esc(x.sub)}</small></span></div>`).join("")
      : `<div class="searchloading" role="presentation">Nothing matches “${esc(q)}”.</div>`;
    if (unread.length) out.insertAdjacentHTML("beforeend", `<div class="searchloading" role="presentation">Could not search ${esc(unread.join("; "))}</div>`);
    searchActive = -1;
    $$(".sresult", out).forEach(option => {
      option.addEventListener("mousedown", e => e.preventDefault()); // keep focus in the search box
      option.onclick = () => searchOpen(+option.dataset.index);
    });
  } catch (e) { out.innerHTML = `<div class="searchloading" role="presentation">Search unavailable · ${esc(e.message)}</div>`; }
}
$("#globalSearch").addEventListener("input", e => {
  STATE.q = e.target.value.trim();
  if (FILTERABLE_VIEWS.has(STATE.view)) {
    const url = HomesteadRouter.urlFor(STATE.view, routeParamsForView(STATE.view));
    window.history.replaceState({ view: STATE.view }, "", url);
  }
  if (STATE.view === "workloads") renderWorkloads();
  else if (STATE.view === "storage") viewStorage();
  else if (STATE.view === "images") viewImages();
  else if (STATE.view === "events") viewEvents();
  else if (STATE.view === "network") viewNetworking();
  clearTimeout(searchTimer); searchTimer = setTimeout(() => globalSearch(STATE.q), 180);
});
$("#globalSearch").addEventListener("keydown", e => {
  const open = !$("#searchResults").classList.contains("hidden") && searchHits.length;
  if ((e.key === "ArrowDown" || e.key === "ArrowUp") && open) {
    e.preventDefault();
    searchMark(searchActive < 0 && e.key === "ArrowUp" ? searchHits.length - 1
      : (searchActive + (e.key === "ArrowDown" ? 1 : -1) + searchHits.length) % searchHits.length);
  } else if (e.key === "Enter" && open) {
    e.preventDefault(); searchOpen(Math.max(0, searchActive));
  } else if (e.key === "Escape" && open) {
    e.preventDefault(); e.stopPropagation(); searchClose();
  } else if (e.key === "Tab") searchClose();
});
document.addEventListener("click", e => {
  if (!e.target.closest("#searchwrap")) searchClose();
});
/* mobile: search collapses behind an icon so the bar has room for the title */
const sbtn = $("#searchbtn");
if (sbtn) sbtn.onclick = () => {
  document.body.classList.add("searching");
  setTimeout(() => $("#globalSearch").focus(), 40);
};
$("#globalSearch").addEventListener("blur", () => {
  if (!$("#globalSearch").value) document.body.classList.remove("searching");
});
$("#mclose").onclick = dismissModal;
// Clicking the backdrop deliberately does nothing: a modal here is usually a
// form worth several minutes, and losing it to a stray click is not a feature.
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    dismissModal(); closeNav();
    document.body.classList.remove("searching");
  }
  // "/" finds, except where it is typed: any field, editor or terminal - a
  // console's paths were losing every slash to the search box.
  if (e.key === "/" && !e.ctrlKey && !e.metaKey && !e.altKey
      && !e.target?.closest?.("input, textarea, select, [contenteditable], .xterm, .monaco-editor")) {
    e.preventDefault(); $("#globalSearch").focus();
  }
});

/* ---------------- appearance ----------------
   Settings › You holds it; the top bar's gear opens it there. */
$("#opensettings").onclick = () => { settingsTab("you"); go("settings"); };

function bindOpts(id, key, after) {
  const wrap = $("#" + id); if (!wrap) return;
  const sync = () => $$(".opt", wrap).forEach(o => o.classList.toggle("on", o.dataset.v === String(SET[key])));
  $$(".opt", wrap).forEach(o => o.onclick = () => {
    SET[key] = o.dataset.v; applySettings(); sync(); after && after();
  });
  sync();
}
/* The controls are drawn with Settings › You; bind them each time. They
   apply at once and belong to this browser, so they are not on the save bar. */
function bindAppearance() {
  bindOpts("optTheme", "theme");
  bindOpts("optBg", "bg");
  bindOpts("optMotion", "motion", () => { if (STATE.view === "flow") drawArch(); });
  const blur = $("#optBlur");
  if (blur) {
    blur.value = SET.blur;
    $("#blurVal").textContent = SET.blur;
    blur.oninput = () => { SET.blur = +blur.value; $("#blurVal").textContent = SET.blur; applySettings(); };
  }
  const rf = $("#optRefresh");
  if (rf) {
    rf.value = SET.refresh;
    $("#refreshVal").textContent = SET.refresh;
    rf.oninput = () => { SET.refresh = +rf.value; $("#refreshVal").textContent = SET.refresh; applySettings(); startLoop(); };
  }
}
window.bindAppearance = bindAppearance;
window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
  if (SET.theme === "auto") applySettings();
});
window.addEventListener("resize", () => { if (STATE.view === "flow") drawArch(); });

/* boot is driven by auth.js -> afterAuth(), so the app never renders
   (or starts polling) before we know who is signed in. */
window.__loopTimer = null;
