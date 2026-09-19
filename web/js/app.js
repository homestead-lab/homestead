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
  schedules: ["Schedules",      "system",    viewSchedules, true],
  imports:   ["Import",         "system",    viewImport,    false],
  events:    ["Events",         "system",    viewEvents,    true],
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
  const s = Math.max(5, +SET.refresh || 15);
  timer = setInterval(() => {
    if (document.hidden) return;                       // tab in background
    if (!$("#modal").classList.contains("hidden")) return; // modal open
    if ($("#drawer").classList.contains("open")) return;   // settings open
    refresh();
  }, s * 1000);
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
$("#globalSearch").addEventListener("input", e => {
  STATE.q = e.target.value.trim();
  if (STATE.view === "workloads") renderWorkloads();
  else if (STATE.view === "storage") viewStorage();
  else if (STATE.view === "images") viewImages();
});
$("#mclose").onclick = closeModal;
$("#modal").onclick = e => { if (e.target.id === "modal") closeModal(); };
document.addEventListener("keydown", e => {
  if (e.key === "Escape") { closeModal(); closeNav(); $("#drawer").classList.remove("open"); }
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

go("dash");
startLoop();
