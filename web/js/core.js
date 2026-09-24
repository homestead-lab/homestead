/* Homestead core — DOM helpers, settings, no-flash rendering, chart primitives */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const V = () => $("#views");
const STATE = { view: "dash", q: "", data: {}, busy: false };

const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const HOMESTEAD_VERSION = "2.8.117";
const ICON_BLOBS = new Map();
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
  const host = $(".bver");
  if (!host) return;
  const site = String(settings.site_name || "").trim();
  const version = (settings.info || {}).version || host.dataset.version || "";
  host.textContent = [site, version && "v" + version].filter(Boolean).join(" · ");
  host.title = site ? `${site} · Homestead v${version}` : `Homestead v${version}`;
  const foot = $("#appfoot");
  if (foot) foot.textContent = [site, "Homestead", version && "v" + version]
    .filter(Boolean).join(" · ");
}
window.paintBrand = paintBrand;
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
    img.dataset.iconLoading = "1";
    const url = img.dataset.iconSrc;
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
      .catch(() => { if (img.isConnected) img.remove(); });
  });
}

const fmtUp = sec => {
  if (!sec || sec < 0) return "—";
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
};
const fmtAgo = sec => sec ? fmtUp(sec) + " ago" : "—";
const ageSecs = iso => iso ? Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000)) : 0;
const upChip = sec => `<span class="uptime"><span class="ld"></span>${fmtUp(sec)}</span>`;
const svcUrl = (ip, port) => `${[443, 8443, 9443].includes(+port) ? "https" : "http"}://${ip}:${port}`;
window.openSvc = (ip, port) => window.open(svcUrl(ip, port), "_blank", "noopener");

/* ---------------- settings ---------------- */
const SET = Object.assign(
  { theme: "dark", bg: "soft", blur: 26, motion: "on", refresh: 15 },
  JSON.parse(localStorage.getItem("homestead.settings") || "{}"));

function applySettings() {
  const root = document.documentElement;
  const dark = SET.theme === "dark" ||
    (SET.theme === "auto" && !window.matchMedia("(prefers-color-scheme: light)").matches);
  root.dataset.theme = dark ? "dark" : "light";
  root.dataset.bg = SET.bg;
  root.style.setProperty("--blur", SET.blur + "px");
  root.dataset.motion = SET.motion;
  localStorage.setItem("homestead.settings", JSON.stringify(SET));
}
applySettings();

/* ---------------- fetch ---------------- */
/* Every navigation bumps this. A view builds its page across several awaits,
   so a slow one could still be waiting when you move on - and then paint its
   answer over the page you actually asked for, leaving the URL, the title and
   the body disagreeing. A read whose answer arrives after you have navigated
   away is for a page nobody is looking at, so it is abandoned rather than
   returned. Writes are never abandoned: their result has to be reported. */
window.NAV_TOKEN = 0;
const ABANDONED = new Promise(() => { });

async function api(path, opts) {
  // `keep` marks a read whose answer matters after the page changes.
  const readOnly = (!opts || !opts.method || opts.method === "GET") && !opts?.keep;
  const startedAt = window.NAV_TOKEN;
  const r = await fetch(path, opts);
  const ct = r.headers.get("content-type") || "";
  const b = ct.includes("json") ? await r.json() : await r.text();
  if (readOnly && startedAt !== window.NAV_TOKEN) return ABANDONED;
  if (!r.ok) throw new Error((b && b.error) || r.statusText);
  if (b && b.operation && window.noteOperation) window.noteOperation(b.operation);
  return b;
}

/* ---------------- configurable hardware ---------------- */
async function loadHardwareFeatures(force = false) {
  if (!force && Array.isArray(STATE.data.hardwareFeatures)) return STATE.data.hardwareFeatures;
  STATE.data.hardwareFeatures = await api("/api/hardware/features");
  return STATE.data.hardwareFeatures;
}
function hardwareDef(id) {
  return (STATE.data.hardwareFeatures || []).find(x => x.id === id) ||
    { id, name: id.replace(/[_-]+/g, " "), host_path: "" };
}
const hardwareName = id => hardwareDef(id).name;
const hardwareTags = ids => (ids || []).map(id => {
  const f = hardwareDef(id);
  return `<span class="tag hw" title="${esc(f.host_path || "Configured hardware feature")}">${esc(f.name)}</span>`;
}).join("");
function hardwareChoices(cls, selected = []) {
  const chosen = new Set(selected || []);
  const nodes = STATE.data.nodes || STATE.data.ov?.nodes || [];
  const choices = (STATE.data.hardwareFeatures || []).map(f => {
    const hosts = nodes.filter(n => (n.hardware || {})[f.id]).map(n => n.name);
    return `<label class="switch hwchoice">
    <input type="checkbox" class="${esc(cls)}" data-hwid="${esc(f.id)}" ${chosen.has(f.id) ? "checked" : ""}>
    <span><b>${esc(f.name)}</b>${f.builtin ? ' <span class="tag">built in</span>' : ""}
    <span class="tag hw">${esc(f.host_path)} → ${esc(f.container_path || f.host_path)}</span>
    ${f.usb_ids?.length ? `<span class="dim xs"> USB ${esc(f.usb_ids.join(", "))}</span>` : ""}
    <span class="dim xs hw-hosts">${hosts.length ? `${hosts.length} host${hosts.length === 1 ? "" : "s"}: ${esc(hosts.map(x => x.replace("harvester-", "")).join(", "))}` : "no eligible host detected"}</span></span></label>`;
  }).join("");
  return choices + `<button class="btn sm hw-manage" type="button" onclick="openHardwareManager('${esc(cls)}')">Browse host devices / add mapping</button>`;
}
const selectedHardware = cls => $$(`.${cls}:checked`).map(x => x.dataset.hwid);
window.openHardwareManager = cls => {
  if ($("#mtitle").textContent !== "Hardware features") {
    // Coming back, the editor's choices are redrawn: a feature may have been added.
    const selected = selectedHardware(cls);
    pushModal(() => {
      const host = $("#mbody .hwchoices");
      if (host) host.innerHTML = hardwareChoices(cls, selected);
    });
  }
  hardwareFeatureSettings();
};
window.hardwareManagerBack = () => modalBack();

/* ---------------- toast / modal ---------------- */
function toast(msg, kind = "") {
  liftToasts();
  const d = document.createElement("div");
  d.className = "tst " + kind; d.textContent = msg;
  $("#toast").appendChild(d); setTimeout(() => d.remove(), 4200);
}
/* Toasts and the job tray share the bottom-right corner, so toasts stand on
   top of the tray - however tall it is, open or closed - rather than over it. */
function liftToasts() {
  const tray = $("#jobTray");
  const height = tray && !tray.classList.contains("hidden") ? tray.getBoundingClientRect().height : 0;
  document.documentElement.style.setProperty("--tray-lift", height ? `${Math.ceil(height) + 8}px` : "4px");
}
if (window.ResizeObserver && $("#jobTray")) {
  new ResizeObserver(liftToasts).observe($("#jobTray"));
  new MutationObserver(liftToasts).observe($("#jobTray"), { attributes: true, attributeFilter: ["class"] });
}
function modal(t, h, wide, contextClass = "") {
  window.__modalGuard = null;   // each modal decides for itself what is at stake
  $("#mtitle").textContent = t;
  $("#mbody").innerHTML = h;
  enhanceActions($("#mbody"));
  $(".modalbox").classList.toggle("wide", !!wide);
  $(".modalbox").classList.toggle("node-detail-modal", contextClass === "node-detail-modal");
  $(".modalbox").dataset.context = contextClass || "";
  $("#modal").classList.toggle("node-detail-view", contextClass === "node-detail-modal");
  $("#modal").classList.remove("hidden");
  paintModalBack();
}

/* Dialogs opened from dialogs - a drive from its host, a feature from the
   hardware list - sit on top of the one they came from. Closing one goes back
   a level, with the dialog underneath as it was left: scrolled, half-filled,
   its address restored. Code that finishes a job calls closeModal, which
   closes the whole stack. */
const MODAL_STACK = [];
const modalIsOpen = () => !$("#modal").classList.contains("hidden");

function pushModal(onReturn) {
  if (!modalIsOpen()) return false;
  const body = $("#mbody"), fragment = document.createDocumentFragment();
  const scroll = $(".modalbox").scrollTop;     // the box scrolls, not its body
  while (body.firstChild) fragment.appendChild(body.firstChild);
  MODAL_STACK.push({ title: $("#mtitle").textContent, fragment, scroll,
    wide: $(".modalbox").classList.contains("wide"), context: $(".modalbox").dataset.context || "",
    guard: window.__modalGuard, url: window.location.pathname + window.location.search,
    route: STATE.modalRoute, detail: STATE.modalDetail, onReturn });
  return true;
}

/* Opens a dialog on top of the current one; with none open, it is a dialog.
   The same title again is the same dialog redrawing, not a new level. */
function childModal(t, h, wide, contextClass = "") {
  const deeper = modalIsOpen() && $("#mtitle").textContent !== t;
  if (deeper) pushModal();
  modal(t, h, wide, contextClass);
  if (deeper) $(".modalbox").scrollTop = 0;
}

function modalBack() {
  const back = MODAL_STACK.pop();
  if (!back) return closeModal();
  modal(back.title, "", back.wide, back.context);
  $("#mbody").replaceChildren(back.fragment);
  $(".modalbox").scrollTop = back.scroll;
  window.__modalGuard = back.guard;
  if (back.url !== window.location.pathname + window.location.search) {
    window.history.replaceState(window.history.state, "", back.url);
  }
  STATE.modalRoute = back.route;
  STATE.modalDetail = back.detail;
  if (window.renderBreadcrumb) renderBreadcrumb(STATE.view, STATE.modalDetail);
  if (back.onReturn) back.onReturn();
}

function paintModalBack() {
  const button = $("#mback");
  if (!button) return;
  const under = MODAL_STACK[MODAL_STACK.length - 1];
  button.hidden = !under;
  if (under) {
    button.querySelector("span").textContent = under.title;
    button.title = `Back to ${under.title}`;
  }
}
window.childModal = childModal;

/* Some pages come as cards or as rows. Which is the reader's choice, kept per
   browser and per page; nothing about it is worth a round trip. */
function viewLayout(page) {
  try { return localStorage.getItem(`homestead.layout.${page}`) === "rows" ? "rows" : "cards"; }
  catch (e) { return "cards"; }
}
function layoutSwitch(page, redraw) {
  const layout = viewLayout(page);
  const option = (value, label, iconName) => `<button class="${layout === value ? "on" : ""}" title="${label}"
    aria-label="Show as ${label.toLowerCase()}" aria-pressed="${layout === value}"
    onclick="setViewLayout('${page}','${redraw}','${value}')">${icon(iconName)}</button>`;
  return `<div class="seg iconseg" role="group" aria-label="Layout">${option("cards", "Cards", "dash")}${option("rows", "Rows", "list")}</div>`;
}
window.setViewLayout = (page, redraw, layout) => {
  try { localStorage.setItem(`homestead.layout.${page}`, layout); } catch (e) { /* this visit only */ }
  resetPaint();
  if (typeof window[redraw] === "function") window[redraw]();
};
window.modalBack = modalBack;
window.pushModal = pushModal;
/* The X and Escape are the only ways a person dismisses a modal, and either can
   land on unsaved work, so both ask a modal that has something at stake first.
   Code that closes a modal after finishing its job calls closeModal directly. */
function dismissModal() {
  const guard = window.__modalGuard;
  if (typeof guard === "function") {
    const question = guard();
    if (question && !confirm(question)) return;
  }
  if (MODAL_STACK.length) return modalBack();
  closeModal();
}
function closeModal(updateRoute = true) {
  window.__modalGuard = null;
  MODAL_STACK.length = 0;
  paintModalBack();
  $("#modal").classList.add("hidden");
  if (window.__logTimer) { clearInterval(window.__logTimer); window.__logTimer = null; }
  if (window.__updateTimer) { clearInterval(window.__updateTimer); window.__updateTimer = null; }
  if (window.__consoleSocket) { window.__consoleSocket.close(); window.__consoleSocket = null; }
  if (window.__consoleResize) { window.__consoleResize.disconnect(); window.__consoleResize = null; }
  if (updateRoute && window.clearModalRoute) window.clearModalRoute();
}

/* ---------------- shared, viewport-safe tooltips ---------------- */
let tooltipOwner = null;
function tooltipTarget(node) {
  return node instanceof Element ? node.closest("[data-tip],[title]") : null;
}
function hideTooltip(owner) {
  if (owner && owner !== tooltipOwner) return;
  const bubble = $("#uiTooltip");
  if (bubble) bubble.hidden = true;
  if (tooltipOwner) tooltipOwner.removeAttribute("aria-describedby");
  tooltipOwner = null;
}
function showTooltip(owner) {
  if (!owner) return;
  const nativeTitle = owner.getAttribute("title");
  if (nativeTitle && !owner.dataset.tip) owner.dataset.tip = nativeTitle;
  if (nativeTitle) owner.removeAttribute("title");
  const message = owner.dataset.tip;
  if (!message) return;

  let bubble = $("#uiTooltip");
  if (!bubble) {
    bubble = document.createElement("div");
    bubble.id = "uiTooltip";
    bubble.className = "uitooltip";
    bubble.setAttribute("role", "tooltip");
    document.body.appendChild(bubble);
  }
  hideTooltip();
  tooltipOwner = owner;
  owner.setAttribute("aria-describedby", "uiTooltip");
  if (!owner.getAttribute("aria-label") && owner.matches(".iconbtn")) owner.setAttribute("aria-label", message);
  bubble.textContent = message;
  bubble.hidden = false;

  const rect = owner.getBoundingClientRect();
  const gap = 9;
  const margin = 10;
  const width = bubble.offsetWidth;
  const height = bubble.offsetHeight;
  const left = Math.max(margin, Math.min(rect.left + rect.width / 2 - width / 2, window.innerWidth - width - margin));
  const above = rect.top - height - gap;
  const top = above >= margin ? above : Math.min(window.innerHeight - height - margin, rect.bottom + gap);
  bubble.style.left = Math.round(left) + "px";
  bubble.style.top = Math.round(Math.max(margin, top)) + "px";
}
document.addEventListener("pointerover", e => {
  const owner = tooltipTarget(e.target);
  if (owner && !owner.contains(e.relatedTarget)) showTooltip(owner);
});
document.addEventListener("pointerout", e => {
  if (tooltipOwner && tooltipOwner.contains(e.target) && !tooltipOwner.contains(e.relatedTarget)) hideTooltip(tooltipOwner);
});
document.addEventListener("focusin", e => showTooltip(tooltipTarget(e.target)));
document.addEventListener("focusout", e => {
  if (tooltipOwner && tooltipOwner.contains(e.target)) hideTooltip(tooltipOwner);
});
window.addEventListener("scroll", () => hideTooltip(), true);
window.addEventListener("resize", () => hideTooltip());

const ACTION_ICONS = [
  [/^logs?\b/, "log"], [/^console\b/, "console"], [/^edit\b/, "edit"], [/^(move|migrate)\b/, "move"],
  [/^(restart|reboot)\b/, "restart"], [/^(delete|remove)\b/, "trash"],
  [/^start\b/, "play"], [/^(stop|shut down)\b/, "stop"],
  [/^rollback\b/, "rollback"], [/^update\b/, "update"],
];
function enhanceActions(root = document) {
  labelStackTables(root);
  sortTables(root);
  $$("button.btn", root).forEach(button => {
    if ($(".btnicon", button)) return;
    const label = button.textContent.trim().replace(/^[＋↻←]+\s*/, "").toLowerCase();
    const match = ACTION_ICONS.find(([pattern]) => pattern.test(label));
    if (match) button.insertAdjacentHTML("afterbegin", icon(match[1]));
  });
}
window.enhanceActions = enhanceActions;

/* A table marked "stack" turns each row into a small card on a phone, and each
   cell needs its column's name to make sense on its own there. The headings
   already say it, so each cell is labelled from them rather than by hand. */
function labelStackTables(root = document) {
  $$("table.stack", root).forEach(table => {
    const heads = $$("thead th", table).map(th => th.textContent.trim());
    $$("tbody tr", table).forEach(row => {
      let column = 0;
      [...row.children].forEach(cell => {
        const span = +cell.getAttribute("colspan") || 1;
        if (span === 1 && !cell.hasAttribute("data-label")) cell.dataset.label = heads[column] || "";
        column += span;
      });
    });
  });
}

/* A table marked data-sort="<name>" sorts by whichever heading the reader
   clicks, and remembers it per table in this browser. Rows are put in order as
   each render is built, before it is compared with the page, so a live refresh
   keeps the reader's order instead of shuffling back. A cell's data-sort gives
   the value to compare when its text is not it (a ratio, a size in bytes). */
const SORT_UNITS = { "": 1, "%": 1, b: 1, kb: 1e3, mb: 1e6, gb: 1e9, tb: 1e12, kib: 1024, mib: 1024 ** 2,
  gib: 1024 ** 3, tib: 1024 ** 4 };
function sortState(name) {
  try { return JSON.parse(localStorage.getItem(`homestead.sort.${name}`) || "null"); } catch (e) { return null; }
}
function sortValue(cell) {
  if (!cell) return null;
  const given = cell.dataset.sort;
  const text = (given ?? cell.textContent).trim();
  if (!text || text === "—") return null;
  if (given !== undefined && given !== "" && Number.isFinite(+given)) return +given;
  const size = text.match(/^(-?\d+(?:[.,]\d+)?)\s*(%|[kmgt]i?b|b)?(?:\s|$)/i);
  if (size) return parseFloat(size[1].replace(",", ".")) * (SORT_UNITS[(size[2] || "").toLowerCase()] || 1);
  return text.toLowerCase();
}
function sortRows(table) {
  const state = sortState(table.dataset.sort);
  const heads = $$("thead th", table);
  heads.forEach((th, index) => {
    if (!th.textContent.trim() || th.hasAttribute("data-nosort")) return;
    th.classList.add("sortable");
    th.dataset.col = index;
    th.setAttribute("aria-sort", state && state.col === index ? (state.dir > 0 ? "ascending" : "descending") : "none");
  });
  sortBar(table, heads, state);
  if (!state || !heads[state.col]) return;
  // A table in sections (a heading body, then its rows) sorts each on its own.
  [...table.tBodies].filter(body => !body.classList.contains("grouphead")).forEach(body => sortBody(body, heads, state));
}
function firstDataRow(table) {
  const width = $$("thead th", table).length;
  for (const body of table.tBodies) for (const row of body.rows) if (row.cells.length === width) return row;
  return null;
}
function sortBody(body, heads, state) {
  const rows = [...body.rows];
  const keyed = rows.filter(row => row.cells.length === heads.length);
  const rest = rows.filter(row => row.cells.length !== heads.length);
  keyed.map((row, at) => ({ row, at, value: sortValue(row.cells[state.col]) }))
    .sort((a, b) => {
      // Empty cells go last whichever way the column is sorted.
      if (a.value === null || b.value === null) return (a.value === null) - (b.value === null) || a.at - b.at;
      const order = typeof a.value === "number" && typeof b.value === "number" ? a.value - b.value
        : String(a.value).localeCompare(String(b.value), undefined, { numeric: true });
      return order * state.dir || a.at - b.at;
    })
    .concat(rest.map(row => ({ row })))
    .forEach(({ row }) => body.appendChild(row));
}
/* On a phone a stacked table has no headings to click, so it gets a menu. */
function sortBar(table, heads, state) {
  if (!table.classList.contains("stack")) return;
  const wrap = table.closest(".card") || table.parentElement;
  let bar = wrap.previousElementSibling;
  if (!bar || !bar.classList.contains("sortbar")) {
    bar = document.createElement("div");
    bar.className = "sortbar";
    wrap.before(bar);
  }
  const options = heads.map((th, index) => th.classList.contains("sortable")
    ? `<option value="${index}" ${state && state.col === index ? "selected" : ""}>${esc(th.textContent.replace(/[↑↓]/g, "").trim())}</option>` : "").join("");
  bar.innerHTML = `<label>Sort by <select data-sort-for="${esc(table.dataset.sort)}">
      <option value="" ${state ? "" : "selected"}>as listed</option>${options}</select></label>
    ${state ? `<button class="btn sm" data-sort-flip="${esc(table.dataset.sort)}">${state.dir > 0 ? "↑ ascending" : "↓ descending"}</button>` : ""}`;
}
function saveSort(name, state) {
  try {
    if (state) localStorage.setItem(`homestead.sort.${name}`, JSON.stringify(state));
    else localStorage.removeItem(`homestead.sort.${name}`);
  } catch (e) { /* this visit only */ }
  const table = document.querySelector(`table[data-sort="${CSS.escape(name)}"]`);
  if (table) { if (!state) refresh(true); else sortRows(table); }
}
document.addEventListener("change", event => {
  const select = event.target.closest("select[data-sort-for]");
  if (!select) return;
  const col = select.value === "" ? null : +select.value;
  const table = document.querySelector(`table[data-sort="${CSS.escape(select.dataset.sortFor)}"]`);
  const numeric = col !== null && table && typeof sortValue(firstDataRow(table)?.cells[col]) === "number";
  saveSort(select.dataset.sortFor, col === null ? null : { col, dir: numeric ? -1 : 1 });
});
document.addEventListener("click", event => {
  const flip = event.target.closest("[data-sort-flip]");
  if (!flip) return;
  const current = sortState(flip.dataset.sortFlip);
  if (current) saveSort(flip.dataset.sortFlip, { col: current.col, dir: -current.dir });
});
function sortTables(root = document) {
  $$("table[data-sort]", root).forEach(sortRows);
}
document.addEventListener("click", event => {
  const th = event.target.closest("table[data-sort] th.sortable");
  if (!th) return;
  const table = th.closest("table");
  const col = +th.dataset.col, current = sortState(table.dataset.sort);
  // Numbers read best biggest first; names from A.
  const firstRow = firstDataRow(table);
  const numeric = typeof sortValue(firstRow?.cells[col]) === "number";
  const dir = current && current.col === col ? -current.dir : numeric ? -1 : 1;
  saveSort(table.dataset.sort, { col, dir });
});

/* ---------------- no-flash rendering ----------------
   Re-rendering innerHTML on every poll is what makes the page flash and lose
   scroll/hover. paint() only touches nodes whose content actually changed. */
/* A page opens at once. One seen before shows what it showed last time until
   its data arrives, and is then brought up to date in place; one not seen yet
   shows its shape - panels with a passing glint - rather than a spinner. */
const PAGE_SNAPSHOT = {};
function pagePlaceholder(view) {
  const host = V();
  const snapshot = PAGE_SNAPSHOT[view];
  if (snapshot) {
    host.innerHTML = snapshot;
    enhanceActions(host);
    loadAppIcons(host);
    host.dataset.painted = "1";
    host.classList.add("refreshing");
    if (window.applyRole) window.applyRole();
    return;
  }
  host.innerHTML = pageSkeleton(view);
}
window.pagePlaceholder = pagePlaceholder;

function pageSkeleton(view) {
  const bar = (width, height = 12) => `<span class="skel" style="width:${width};height:${height}px"></span>`;
  const repeat = (count, fn) => Array.from({ length: count }, (_, i) => fn(i)).join("");
  const head = `<div class="phead"><div>${bar("min(280px,60vw)")}</div><div class="row skel-actions">${bar("84px", 32)}${bar("118px", 32)}</div></div>`;
  const cards = (count, height) => `<div class="skel-grid">${repeat(count, () => `<div class="skel skel-card" style="height:${height}px"></div>`)}</div>`;
  const table = count => `<div class="card flat pad0 skel-table">${repeat(count, i => `<div class="skel-row">
      ${bar("30px", 30)}${bar(`${18 + (i * 7) % 12}%`)}${bar("9%")}${bar(`${20 + (i * 5) % 16}%`)}${bar("7%")}</div>`)}</div>`;
  const tiles = `<div class="skel-tiles">${repeat(4, () => '<div class="skel skel-card" style="height:96px"></div>')}</div>`;
  const body = {
    dash: tiles + cards(2, 260), cluster: tiles + cards(4, 170), flow: cards(1, 440),
    nodes: viewLayout("nodes") === "rows" ? table(4) : cards(3, 290),
    workloads: viewLayout("containers") === "rows" ? table(8) : cards(6, 230),
    vms: cards(3, 200), store: cards(9, 150), deploy: cards(2, 320), settings: cards(4, 240),
  }[view] || table(8);
  return `<div class="skeleton" aria-busy="true" aria-label="Loading">${head}${body}</div>`;
}

function paint(html) {
  const host = V();
  PAGE_SNAPSHOT[STATE.view] = html;
  host.classList.remove("refreshing");
  if (!host.dataset.painted) {
    host.innerHTML = html;
    enhanceActions(host);
    loadAppIcons(host);
    host.dataset.painted = "1";
    host.dataset.sig = html.length + ":" + STATE.view;
    if (window.applyRole) window.applyRole();
    return;
  }
  const next = document.createElement("div");
  next.innerHTML = html;
  enhanceActions(next);
  morph(host, next);
  loadAppIcons(host);
  if (window.applyRole) window.applyRole();
}
function morph(a, b) {
  // fast path: identical markup, nothing to do
  if (a.isEqualNode(b)) return;
  const an = [...a.childNodes], bn = [...b.childNodes];
  for (let i = 0; i < Math.max(an.length, bn.length); i++) {
    const x = an[i], y = bn[i];
    if (!y) { x && x.remove(); continue; }
    if (!x) { a.appendChild(y.cloneNode(true)); continue; }
    if (x.nodeType !== y.nodeType || x.nodeName !== y.nodeName) { x.replaceWith(y.cloneNode(true)); continue; }
    if (x.nodeType === 3) { if (x.nodeValue !== y.nodeValue) x.nodeValue = y.nodeValue; continue; }
    if (x.nodeType !== 1) continue;
    // never stomp on a field the user is typing in
    if (document.activeElement === x && /^(INPUT|SELECT|TEXTAREA)$/.test(x.nodeName)) continue;
    syncAttrs(x, y);
    if (x.isEqualNode(y)) continue;
    morph(x, y);
  }
}
function syncAttrs(x, y) {
  // Whether a <details> is open belongs to the reader, the same way the text in
  // an input does: a background refresh must not fold up a pod tree mid-read.
  const readerOwnsOpen = x.nodeName === "DETAILS";
  for (const at of [...y.attributes]) if (!(readerOwnsOpen && at.name === "open") &&
      x.getAttribute(at.name) !== at.value) {
    const animateSpark = at.name === "d" && x.closest(".spark") &&
      (x.classList.contains("ln") || x.classList.contains("fl"));
    x.setAttribute(at.name, at.value);
    if (animateSpark && SET.motion !== "off" && x.animate) {
      x.animate([{ transform: "translateX(7px)", opacity: .72 }, { transform: "translateX(0)", opacity: 1 }],
        { duration: 420, easing: "cubic-bezier(.2,.8,.2,1)" });
    }
  }
  for (const at of [...x.attributes]) {
    if (readerOwnsOpen && at.name === "open") continue;
    if (!y.hasAttribute(at.name)) x.removeAttribute(at.name);
  }
}
function resetPaint() { const h = V(); delete h.dataset.painted; h.classList.remove("refreshing"); h.innerHTML = ""; }

/* ---------------- chart primitives ---------------- */
function sparkline(vals, { w = 300, h = 74 } = {}) {
  if (!vals || vals.length < 2) vals = [0, 0];
  const n = vals.length, mn = Math.min(...vals), mx = Math.max(...vals);
  const pad = (mx - mn) * .25 || 1, lo = mn - pad, hi = mx + pad;
  const pts = vals.map((v, i) => [(i / (n - 1)) * w, h - ((v - lo) / (hi - lo)) * h]);
  let d = `M ${pts[0][0]},${pts[0][1]}`;
  for (let i = 1; i < pts.length; i++) {
    const [x0, y0] = pts[i - 1], [x1, y1] = pts[i], cx = (x0 + x1) / 2;
    d += ` C ${cx},${y0} ${cx},${y1} ${x1},${y1}`;
  }
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <path class="fl" d="${d} L ${w},${h} L 0,${h} Z"/><path class="ln" d="${d}"/></svg>`;
}
function dualSpark(a, b, { w = 300, h = 74 } = {}) {
  const all = [...(a || []), ...(b || [])];
  if (all.length < 2) return sparkline([0, 0], { w, h });
  const mn = Math.min(...all), mx = Math.max(...all);
  const pad = (mx - mn) * .2 || 1, lo = mn - pad, hi = mx + pad;
  const line = (vals, cl) => {
    if (!vals || vals.length < 2) return "";
    const n = vals.length;
    const pts = vals.map((v, i) => [(i / (n - 1)) * w, h - ((v - lo) / (hi - lo)) * h]);
    let d = `M ${pts[0][0]},${pts[0][1]}`;
    for (let i = 1; i < pts.length; i++) {
      const [x0, y0] = pts[i - 1], [x1, y1] = pts[i], cx = (x0 + x1) / 2;
      d += ` C ${cx},${y0} ${cx},${y1} ${x1},${y1}`;
    }
    return `<path class="ln ${cl}" d="${d}"/>`;
  };
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    ${line(a, "s1")}${line(b, "s2")}</svg>`;
}
const POL = (cx, cy, r, a) =>
  [cx + r * Math.cos((a - 90) * Math.PI / 180), cy + r * Math.sin((a - 90) * Math.PI / 180)];
function arcPath(cx, cy, r, a0, a1) {
  const [x0, y0] = POL(cx, cy, r, a0), [x1, y1] = POL(cx, cy, r, a1);
  return `M ${x0} ${y0} A ${r} ${r} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${x1} ${y1}`;
}
function segDonut(parts, big, label, size = 168) {
  const c = size / 2, r = c - 15, tot = parts.reduce((s, p) => s + p.v, 0) || 1;
  let a = 0, out = "";
  const shades = ["#ffffff", "#9a9aa4", "#5c5c66", "#3a3a42"];
  parts.forEach((p, i) => {
    const sw = p.v / tot * 359.4, a1 = a + sw;
    if (sw > .4) out += `<path class="seg" stroke="${p.c || shades[i % shades.length]}"
      d="${arcPath(c, c, r, a + 1, a1)}"><title>${esc(p.n)}: ${p.v}</title></path>`;
    a = a1;
  });
  return `<svg class="donut" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
    <circle class="trk" cx="${c}" cy="${c}" r="${r}"/>${out}
    <text class="dcenter" x="${c}" y="${c + 2}" text-anchor="middle">${big}</text>
    <text class="dlabel" x="${c}" y="${c + 20}" text-anchor="middle">${esc(label)}</text></svg>`;
}
function legend(parts, tot) {
  const shades = ["#ffffff", "#9a9aa4", "#5c5c66", "#3a3a42"];
  return `<div class="legend">${parts.map((p, i) => `<div class="lg">
    <span class="dt" style="background:${p.c || shades[i % shades.length]}"></span>
    <span class="nm">${esc(p.n)}</span><span class="v1">${p.v}</span>
    <span class="v2">${tot ? Math.round(p.v / tot * 100) : 0}%</span></div>`).join("")}</div>`;
}
function trend(vals) {
  if (!vals || vals.length < 4) return "";
  const half = Math.floor(vals.length / 2);
  const a = vals.slice(0, half).reduce((s, v) => s + v, 0) / half;
  const b = vals.slice(half).reduce((s, v) => s + v, 0) / (vals.length - half);
  if (!a) return "";
  const d = Math.round((b - a) / a * 100);
  if (Math.abs(d) < 1) return `<span class="badge">steady</span>`;
  return `<span class="badge">${d > 0 ? "+" : ""}${d}% <span class="arw">${d > 0 ? "↑" : "↓"}</span></span>`;
}
const meter = (pct, extra = "", metric = "cpu") =>
  `<div class="meter ${sev(pct, metric)}" ${extra}><span style="width:${Math.min(100, pct)}%"></span></div>`;
