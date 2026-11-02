/* Homestead core: chart primitives: sparklines, donuts, legends. A classic script, loaded right after core.js (#911). */
/* ---------------- chart primitives ---------------- */
const sparkAnimations = new WeakMap();
const sparkMotion = () => SET.motion !== "off" && !document.hidden &&
  !window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
function sparkPath(points) {
  points = points.map(point => point.map(value => Math.round(value * 1000) / 1000));
  let d = `M ${points[0][0]},${points[0][1]}`;
  for (let i = 1; i < points.length; i++) {
    const [x0, y0] = points[i - 1], [x1, y1] = points[i], cx = (x0 + x1) / 2;
    d += ` C ${cx},${y0} ${cx},${y1} ${x1},${y1}`;
  }
  return d;
}
function sparkPoints(d) {
  return (d.match(/[MC]\s*[^MCLZ]+/g) || []).map(command =>
    command.match(/[-+]?(?:\d*\.?\d+)(?:e[-+]?\d+)?/gi).slice(-2).map(Number));
}
/* Match observed samples: old points move left while the new point enters
   at the right. No samples are invented between polls. */
function sparkFrames(before, after, times, width) {
  const old = times.before, next = times.after;
  if (old.length === before.length && next.length === after.length && old.length > 1 && next.length > 1 &&
      next.at(-1) > old.at(-1)) {
    const start = old.indexOf(next[0]), last = next.indexOf(old.at(-1));
    if (start >= 0 && last >= 0 && next.length - last <= 5 &&
        old.slice(start).every((t, i) => t === next[i])) {
      const ids = [...old, ...next.slice(last + 1)];
      return { from: ids.map(t => {
        const i = old.indexOf(t);
        return i >= 0 ? before[i] : [width + (next.indexOf(t) - last) * width / (old.length - 1), before.at(-1)[1]];
      }), to: ids.map(t => {
        const i = next.indexOf(t);
        return i >= 0 ? after[i] : [(old.indexOf(t) - start) * width / (next.length - 1), before[old.indexOf(t)][1]];
      }) };
    }
    return null; // a gap or a reset is a fresh observation, not a scrolling continuation
  }
  return before.length === after.length ? { from: before, to: after } : null;
}
function updateSparkPath(path, target) {
  const active = sparkAnimations.get(path);
  if (active?.target === target && sparkMotion()) return;
  if (active) cancelAnimationFrame(active.frame);
  sparkAnimations.delete(path);
  const svg = path.closest(".spark"), [, , width, height] = svg.getAttribute("viewBox").split(/\s+/).map(Number);
  const before = sparkPoints(path.getAttribute("d") || ""), after = sparkPoints(target);
  // Dense month-long plots move by less than a pixel per observation; keep their refresh cheap.
  const frames = !svg.sparkReset && before.length > 1 && after.length > 1 &&
      before.length <= 400 && after.length <= 400 && sparkMotion()
    ? sparkFrames(before, after, svg.sparkWindow || { before: [], after: [] }, width) : null;
  if (!frames) { path.setAttribute("d", target); return; }
  const close = path.classList.contains("fl") ? ` L ${width},${height} L 0,${height} Z` : "";
  const state = { target, frame: 0, start: null };
  sparkAnimations.set(path, state);
  const tick = now => {
    if (!path.isConnected) { sparkAnimations.delete(path); return; }
    state.start ??= now;
    const progress = sparkMotion() ? Math.min(1, (now - state.start) / 650) : 1;
    if (progress === 1) { path.setAttribute("d", target); sparkAnimations.delete(path); return; }
    const eased = 1 - (1 - progress) ** 3;
    path.setAttribute("d", sparkPath(frames.from.map(([x, y], i) =>
      [x + (frames.to[i][0] - x) * eased, y + (frames.to[i][1] - y) * eased])) + close);
    state.frame = requestAnimationFrame(tick);
  };
  // Extra samples start beyond the clip; the visible first frame keeps the old shape.
  path.setAttribute("d", sparkPath(frames.from) + close);
  state.frame = requestAnimationFrame(tick);
}
function sparkMeta(times, key) {
  return ` data-spark-times="${esc(JSON.stringify(times || []))}" data-spark-key="${esc(key || "")}"`;
}
function sparkline(vals, { w = 300, h = 74, times, key } = {}) {
  if (!vals?.length) vals = [0, 0];
  else if (vals.length === 1) vals = [vals[0], vals[0]];
  const n = vals.length, mn = Math.min(...vals), mx = Math.max(...vals);
  const pad = (mx - mn) * .25 || 1, lo = mn - pad, hi = mx + pad;
  const d = sparkPath(vals.map((v, i) => [(i / (n - 1)) * w, h - ((v - lo) / (hi - lo)) * h]));
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"${sparkMeta(times, key)}>
    <path class="fl" d="${d} L ${w},${h} L 0,${h} Z"/><path class="ln" d="${d}"/></svg>`;
}
function dualSpark(a, b, { w = 300, h = 74, times, key } = {}) {
  if (a?.length === 1) a = [a[0], a[0]];
  if (b?.length === 1) b = [b[0], b[0]];
  const all = [...(a || []), ...(b || [])];
  if (all.length < 2) return sparkline([0, 0], { w, h, times, key });
  const mn = Math.min(...all), mx = Math.max(...all);
  const pad = (mx - mn) * .2 || 1, lo = mn - pad, hi = mx + pad;
  const line = (vals, cl) => {
    if (!vals || vals.length < 2) return "";
    const n = vals.length;
    const d = sparkPath(vals.map((v, i) => [(i / (n - 1)) * w, h - ((v - lo) / (hi - lo)) * h]));
    return `<path class="ln ${cl}" d="${d}"/>`;
  };
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"${sparkMeta(times, key)}>
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

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  sparkAnimations, sparkMotion, sparkPath, sparkPoints, sparkFrames, updateSparkPath, sparkMeta, sparkline,
  dualSpark, POL, arcPath, segDonut, legend, trend, meter
};
