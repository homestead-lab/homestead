/* HarvUI core — DOM helpers, settings, no-flash rendering, chart primitives */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const V = () => $("#views");
const STATE = { view: "dash", q: "", data: {}, busy: false };

const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const HARVUI_VERSION = "1.9.2";
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
  return s;
}
const tip = (text, label = "?") => `<span class="tip" tabindex="0" aria-label="${esc(text)}" data-tip="${esc(text)}">${esc(label)}</span>`;
const appAvatar = (name, icon, cls = "") => icon
  ? `<span class="av appav ${cls}"><img src="${esc(icon)}" alt="" referrerpolicy="no-referrer" onerror="this.parentNode.innerHTML='${esc(String(name || "?").slice(0, 2).toUpperCase())}'"></span>`
  : `<span class="av ${cls}">${esc(String(name || "?").slice(0, 2).toUpperCase())}</span>`;

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
  { theme: "dark", bg: "gold", blur: 26, motion: "on", refresh: 15 },
  JSON.parse(localStorage.getItem("harvui.settings") || "{}"));

function applySettings() {
  const root = document.documentElement;
  const dark = SET.theme === "dark" ||
    (SET.theme === "auto" && !window.matchMedia("(prefers-color-scheme: light)").matches);
  root.dataset.theme = dark ? "dark" : "light";
  root.dataset.bg = SET.bg;
  root.style.setProperty("--blur", SET.blur + "px");
  root.dataset.motion = SET.motion;
  localStorage.setItem("harvui.settings", JSON.stringify(SET));
}
applySettings();

/* ---------------- fetch ---------------- */
async function api(path, opts) {
  const r = await fetch(path, opts);
  const ct = r.headers.get("content-type") || "";
  const b = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((b && b.error) || r.statusText);
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
  const active = !$("#modal").classList.contains("hidden") && $("#mtitle").textContent !== "Hardware features";
  if (active) {
    const body = $("#mbody"), fragment = document.createDocumentFragment();
    const selected = selectedHardware(cls);
    while (body.firstChild) fragment.appendChild(body.firstChild);
    window.__hardwareReturn = { title: $("#mtitle").textContent, fragment, wide: $(".modalbox").classList.contains("wide"), cls, selected };
  }
  hardwareFeatureSettings();
};
window.hardwareManagerBack = () => {
  const back = window.__hardwareReturn;
  if (!back) return closeModal();
  $("#mtitle").textContent = back.title; $("#mbody").replaceChildren(back.fragment);
  $(".modalbox").classList.toggle("wide", back.wide);
  const host = $("#mbody .hwchoices");
  if (host) host.innerHTML = hardwareChoices(back.cls, back.selected);
  window.__hardwareReturn = null;
};

/* ---------------- toast / modal ---------------- */
function toast(msg, kind = "") {
  const d = document.createElement("div");
  d.className = "tst " + kind; d.textContent = msg;
  $("#toast").appendChild(d); setTimeout(() => d.remove(), 4200);
}
function modal(t, h, wide) {
  $("#mtitle").textContent = t;
  $("#mbody").innerHTML = h;
  $(".modalbox").classList.toggle("wide", !!wide);
  $("#modal").classList.remove("hidden");
}
function closeModal() {
  if (window.__hardwareReturn && /hardware feature/i.test($("#mtitle").textContent)) {
    return hardwareManagerBack();
  }
  $("#modal").classList.add("hidden");
  if (window.__logTimer) { clearInterval(window.__logTimer); window.__logTimer = null; }
  if (window.__updateTimer) { clearInterval(window.__updateTimer); window.__updateTimer = null; }
}

/* ---------------- no-flash rendering ----------------
   Re-rendering innerHTML on every poll is what makes the page flash and lose
   scroll/hover. paint() only touches nodes whose content actually changed. */
function paint(html) {
  const host = V();
  if (!host.dataset.painted) {
    host.innerHTML = html;
    host.dataset.painted = "1";
    host.dataset.sig = html.length + ":" + STATE.view;
    if (window.applyRole) window.applyRole();
    return;
  }
  const next = document.createElement("div");
  next.innerHTML = html;
  morph(host, next);
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
  for (const at of [...y.attributes]) if (x.getAttribute(at.name) !== at.value) x.setAttribute(at.name, at.value);
  for (const at of [...x.attributes]) if (!y.hasAttribute(at.name)) x.removeAttribute(at.name);
}
function resetPaint() { const h = V(); delete h.dataset.painted; h.innerHTML = ""; }

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
  const last = pts[pts.length - 1];
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <path class="fl" d="${d} L ${w},${h} L 0,${h} Z"/><path class="ln" d="${d}"/>
    <circle class="kn" cx="${last[0]}" cy="${last[1]}" r="3.2"/></svg>`;
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
    const last = pts[pts.length - 1];
    return `<path class="ln ${cl}" d="${d}"/><circle class="kn ${cl}" cx="${last[0]}" cy="${last[1]}" r="3"/>`;
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
