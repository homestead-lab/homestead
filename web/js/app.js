/* Homestead — router, settings drawer, mobile nav, quiet refresh loop */

const VIEWS = {
  dash:      ["Dashboard",      "overview",  viewDash,      true],
  flow:      ["Architecture",   "overview",  viewFlow,      true],
  nodes:     ["Nodes",          "overview",  viewNodes,     true],
  network:   ["Networking",     "overview",  viewNetworking,true],
  portal:    ["Portal",         "overview",  viewPortal,    true],
  workloads: ["Containers",     "workloads", viewWorkloads, true],
  vms:       ["Virtual Machines","workloads", viewVMs,       true],
  deploy:    ["Deploy",         "workloads", () => viewDeploy(), false],
  store:     ["App Store",      "workloads", viewStore,     false],
  helm:      ["Helm",           "workloads", viewHelm,      true],
  shares:    ["Network Shares", "storage",   viewShares,    false],
  storage:   ["Volumes",        "storage",   viewStorage,   true],
  images:    ["Image Cache",    "storage",   viewImages,    false],
  protect:   ["Data Protection","storage",   viewProtect,   true],
  schedules: ["Schedules",      "system",    viewSchedules, true],
  imports:   ["Import",         "system",    viewImport,    false],
  events:    ["Events",         "system",    viewEvents,    true],
  cluster:   ["Cluster",        "system",    viewCluster,   true],
  settings:  ["Settings",       "system",    viewSettings,  false],
};

const FILTERABLE_VIEWS = new Set(["workloads", "storage", "images", "events", "store", "network", "portal", "helm"]);

function routeParamsForView(v, extra = {}) {
  const params = FILTERABLE_VIEWS.has(v) && STATE.q ? { q: STATE.q } : {};
  return Object.assign(params, extra);
}

function renderBreadcrumb(v, detail = "") {
  const host = $("#crumb");
  host.innerHTML = HomesteadRouter.breadcrumbs(v, detail).map((item, index) => {
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

  let detail = "";
  let open = null;
  if (v === "nodes" && params.node) {
    detail = params.node;
    open = () => nodeDetail(params.node, true);
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
  } else if (v === "workloads" && params.panel === "console" && params.ns && params.workload) {
    const workload = (STATE.data.wl || []).find(x => x.ns === params.ns && x.name === params.workload);
    if (workload) {
      detail = params.workload + " console";
      open = () => wlConsole(params.ns, params.workload, true);
    }
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
  if (!VIEWS[v]) return;
  if (!$("#modal").classList.contains("hidden")) closeModal(false);
  // Anything still loading belongs to the page being left behind.
  window.NAV_TOKEN++;
  STATE.view = v;
  const locationParams = options.fromLocation ? HomesteadRouter.queryParams(window.location.search) : null;
  if (locationParams) {
    STATE.q = typeof locationParams.q === "string" ? locationParams.q.trim() : "";
    $("#globalSearch").value = STATE.q;
  }
  const [t, , fn] = VIEWS[v];
  $("#title").textContent = t;
  renderBreadcrumb(v);
  document.title = `${t} · Homestead`;
  if (options.history !== false) {
    const url = HomesteadRouter.urlFor(v, options.params || routeParamsForView(v));
    const current = window.location.pathname + window.location.search;
    if (url !== current) window.history[options.replace ? "replaceState" : "pushState"]({ view: v }, "", url);
  }
  STATE.modalRoute = false;
  STATE.modalDetail = "";
  STATE.deepLinkToken = "";
  $$("#nav a").forEach(a => a.classList.toggle("on", a.dataset.view === v));
  closeNav();
  resetPaint();
  pagePlaceholder(v);
  // A new page starts at its top, not wherever the last one was scrolled to.
  window.scrollTo(0, 0);
  Promise.resolve(fn()).then(() => applyDeepLink(v, locationParams || options.params || {}))
    .catch(e => { resetPaint(); V().innerHTML = `<div class="empty">${esc(e.message)}</div>`; });
}
window.go = go;

/* quiet refresh — repaints in place, never rebuilds the view */
async function refresh(force) {
  const [, , fn, live] = VIEWS[STATE.view];
  if (!live && !force) return;
  if (STATE.busy) return;
  STATE.busy = true;
  try { await fn(); } catch (e) { /* keep the last good render */ }
  finally { STATE.busy = false; }
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
    if ($("#drawer").classList.contains("open")) return;   // settings open
    refresh();
  }, s * 1000);
  window.__loopTimer = timer;
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

/* ---------------- nav ---------------- */
$$("#nav a").forEach(a => a.onclick = e => { e.preventDefault(); go(a.dataset.view); });
window.addEventListener("popstate", () => {
  closeModal(false);
  const route = HomesteadRouter.resolve(window.location.pathname);
  go(route.view, { history: false, fromLocation: true });
});
function openNav() { document.body.classList.add("navopen"); }
function closeNav() { document.body.classList.remove("navopen"); }
$("#hamburger").onclick = () => document.body.classList.toggle("navopen");
$("#navscrim").onclick = closeNav;

$("#refresh").onclick = e => {
  const b = e.currentTarget;
  b.classList.add("spinning");
  setTimeout(() => b.classList.remove("spinning"), 900);
  refresh(true);
};
let searchTimer = null;
async function globalSearch(q) {
  const wrap = $("#searchwrap");
  let out = $("#searchResults");
  if (!out) {
    out = document.createElement("div"); out.id = "searchResults"; out.className = "searchresults hidden";
    wrap.appendChild(out);
  }
  if (!q) { out.classList.add("hidden"); out.innerHTML = ""; return; }
  out.classList.remove("hidden");
  out.innerHTML = '<div class="searchloading"><span class="spin2"></span> searching…</div>';
  try {
    const [wls, nodes, vols] = await Promise.all([
      STATE.data.wl ? Promise.resolve(STATE.data.wl) : api("/api/workloads"),
      STATE.data.nodes ? Promise.resolve(STATE.data.nodes) : api("/api/nodes"),
      STATE.data.vols ? Promise.resolve(STATE.data.vols) : api("/api/volumes"),
    ]);
    STATE.data.wl = wls; STATE.data.nodes = nodes; STATE.data.vols = vols;
    const s = q.toLowerCase();
    const hits = [
      ...wls.filter(x => [x.name, x.ns, ...(x.images || []), ...(x.nodes || [])].join(" ").toLowerCase().includes(s))
        .map(x => ({ view: "workloads", kind: "Container", name: x.name, sub: `${x.ns} · ${(x.nodes || []).join(", ") || "unscheduled"}`, icon: x.icon })),
      ...nodes.filter(x => [x.name, x.os, ...(x.roles || []), ...(x.workloads || [])].join(" ").toLowerCase().includes(s))
        .map(x => ({ view: "nodes", kind: "Node", name: x.name, sub: `${x.status} · ${x.pods_wl || 0} workloads` })),
      ...vols.filter(x => [x.name, x.pvc_name, x.namespace, x.attached_to].join(" ").toLowerCase().includes(s))
        .map(x => ({ view: "storage", kind: "Volume", name: x.pvc_name || x.name, sub: `${x.size_gb} GB · ${x.state}` })),
    ].slice(0, 12);
    out.innerHTML = hits.length ? hits.map((x, i) => `<button class="sresult" data-view="${x.view}" ${i === 0 ? 'data-first="1"' : ""}>
      ${x.icon ? `<img src="${esc(x.icon)}" alt="" onerror="this.remove()">` : '<span class="smark">⌕</span>'}
      <span><b>${esc(x.name)}</b><small>${esc(x.kind)} · ${esc(x.sub)}</small></span></button>`).join("")
      : `<div class="searchloading">No containers, nodes or volumes match “${esc(q)}”.</div>`;
    $$(".sresult", out).forEach(b => b.onclick = () => { out.classList.add("hidden"); go(b.dataset.view); });
  } catch (e) { out.innerHTML = `<div class="searchloading">Search unavailable · ${esc(e.message)}</div>`; }
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
  if (e.key === "Enter") { const first = $("#searchResults .sresult[data-first]"); if (first) first.click(); }
});
document.addEventListener("click", e => {
  if (!e.target.closest("#searchwrap")) $("#searchResults")?.classList.add("hidden");
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
    dismissModal(); closeNav(); $("#drawer").classList.remove("open");
    document.body.classList.remove("searching");
  }
  if (e.key === "/" && document.activeElement.tagName !== "INPUT") { e.preventDefault(); $("#globalSearch").focus(); }
});

/* ---------------- settings drawer ---------------- */
$("#opensettings").onclick = () => $("#drawer").classList.add("open");
$("#closedrawer").onclick = () => $("#drawer").classList.remove("open");

function bindOpts(id, key, after) {
  const wrap = $("#" + id); if (!wrap) return;
  const sync = () => $$(".opt", wrap).forEach(o => o.classList.toggle("on", o.dataset.v === String(SET[key])));
  $$(".opt", wrap).forEach(o => o.onclick = () => {
    SET[key] = o.dataset.v; applySettings(); sync(); after && after();
  });
  sync();
}
bindOpts("optTheme", "theme");
bindOpts("optBg", "bg");
bindOpts("optMotion", "motion", () => { if (STATE.view === "flow") drawArch(); });

const blur = $("#optBlur");
if (blur) {
  blur.value = SET.blur;
  $("#blurVal").textContent = SET.blur;
  blur.addEventListener("input", () => {
    SET.blur = +blur.value; $("#blurVal").textContent = SET.blur; applySettings();
  });
}
const rf = $("#optRefresh");
if (rf) {
  rf.value = SET.refresh;
  $("#refreshVal").textContent = SET.refresh;
  rf.addEventListener("input", () => {
    SET.refresh = +rf.value; $("#refreshVal").textContent = SET.refresh; applySettings(); startLoop();
  });
}
window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
  if (SET.theme === "auto") applySettings();
});
window.addEventListener("resize", () => { if (STATE.view === "flow") drawArch(); });

/* boot is driven by auth.js -> afterAuth(), so the app never renders
   (or starts polling) before we know who is signed in. */
window.__loopTimer = null;
