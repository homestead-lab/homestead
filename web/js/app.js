/* HarvUI — router, settings drawer, mobile nav, quiet refresh loop */

const VIEWS = {
  dash:      ["Dashboard",      "overview",  viewDash,      true],
  flow:      ["Architecture",   "overview",  viewFlow,      true],
  nodes:     ["Nodes",          "overview",  viewNodes,     true],
  workloads: ["Containers",     "workloads", viewWorkloads, true],
  vms:       ["Virtual Machines","workloads", viewVMs,       true],
  deploy:    ["Deploy",         "workloads", () => viewDeploy(), false],
  store:     ["App Store",      "workloads", viewStore,     false],
  shares:    ["Network Shares", "storage",   viewShares,    false],
  storage:   ["Volumes",        "storage",   viewStorage,   true],
  images:    ["Image Cache",    "storage",   viewImages,    false],
  protect:   ["Data Protection","storage",   viewProtect,   true],
  schedules: ["Schedules",      "system",    viewSchedules, true],
  imports:   ["Import",         "system",    viewImport,    false],
  events:    ["Events",         "system",    viewEvents,    true],
  settings:  ["Settings",       "system",    viewSettings,  false],
};

function go(v) {
  if (!VIEWS[v]) return;
  STATE.view = v;
  const [t, c, fn] = VIEWS[v];
  $("#title").textContent = t;
  $("#crumb").textContent = c;
  $$("#nav a").forEach(a => a.classList.toggle("on", a.dataset.view === v));
  closeNav();
  resetPaint();
  V().innerHTML = `<div class="empty"><span class="spin2"></span>loading…</div>`;
  Promise.resolve(fn()).catch(e => { resetPaint(); V().innerHTML = `<div class="empty">${esc(e.message)}</div>`; });
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
$$("#nav a").forEach(a => a.onclick = () => go(a.dataset.view));
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
  if (STATE.view === "workloads") renderWorkloads();
  else if (STATE.view === "storage") viewStorage();
  else if (STATE.view === "images") viewImages();
  else if (STATE.view === "events") viewEvents();
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
$("#mclose").onclick = closeModal;
$("#modal").onclick = e => { if (e.target.id === "modal") closeModal(); };
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    closeModal(); closeNav(); $("#drawer").classList.remove("open");
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
