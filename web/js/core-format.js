/* Homestead core: how times, counts and Kubernetes states are written. A classic script, loaded right after core.js (#911). */
const fmtUp = sec => {
  if (!sec || sec < 0) return "—";
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  if (d) return h ? `${d}d ${h}h` : `${d}d`;
  if (h) return m ? `${h}h ${m}m` : `${h}h`;
  return `${m}m`;
};
const fmtAgo = sec => sec ? fmtUp(sec) + " ago" : "—";
/* One way to write a time (#503). An API time is ISO text, epoch seconds or
   milliseconds. It is shown in the reader's own time, without seconds, with
   the year only when it isn't this year, and its UTC value on hover. */
const asDate = v => { const at = v instanceof Date ? v : typeof v === "number" ? new Date(v < 1e12 ? v * 1000 : v) : v ? new Date(v) : null; return at && !isNaN(at) ? at : null; };
const ageSecs = v => { const at = asDate(v); return at ? Math.max(0, Math.floor((Date.now() - at.getTime()) / 1000)) : 0; };
const thisYear = at => at.getFullYear() === new Date().getFullYear() ? {} : { year: "numeric" };
const fmtAt = (v, { weekday = false } = {}) => { const at = asDate(v);
  return at ? at.toLocaleString(undefined, { ...(weekday ? { weekday: "short" } : {}), day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", ...thisYear(at) }) : ""; };
const fmtDay = v => { const at = asDate(v); return at ? at.toLocaleDateString(undefined, { day: "numeric", month: "short", ...thisYear(at) }) : ""; };
/* A day as a heading: "Saturday 24 October". */
const fmtWeekday = v => { const at = asDate(v); return at ? at.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", ...thisYear(at) }) : ""; };
const fmtClock = v => { const at = asDate(v); return at ? at.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : ""; };
const utcTip = at => at.toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC");
/* A past time and how long ago: "22 Sep, 05:00 · 6h 2m ago". */
const fmtWhen = v => {
  const at = asDate(v);
  if (!at) return "—";
  const ago = ageSecs(at) >= 60 ? ` · ${fmtAgo(ageSecs(at))}` : "";
  return `<span data-tip="${esc(utcTip(at))}">${esc(fmtAt(at) + ago)}</span>`;
};
/* A time to come, such as a next run: "Tue 22 Sep, 05:00". */
const fmtNext = v => { const at = asDate(v); return at ? `<span data-tip="${esc(utcTip(at))}">${esc(fmtAt(at, { weekday: true }))}</span>` : "—"; };
/* Said once on a page or dialog that shows times to come in local time. */
const yourZone = () => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch { return "UTC"; } };
const zoneLine = () => `Times are in your time zone (${yourZone()})`;
const upChip = sec => `<span class="uptime"><span class="ld"></span>${fmtUp(sec)}</span>`;
const svcUrl = (ip, port) => `${[443, 8443, 9443].includes(+port) ? "https" : "http"}://${ip}:${port}`;
window.openSvc = (ip, port) => window.open(svcUrl(ip, port), "_blank", "noopener");
/* A count in words: plural(1, "pod") is "1 pod", plural(3, "pod") "3 pods". */
const plural = (n, word, many = word + "s") => `${n} ${Number(n) === 1 ? word : many}`;
/* A Kubernetes state as people write it: "NotReady" is "Not ready". Callers
   keep the raw state in a tooltip. */
const STATE_WORDS = { ErrorUnschedulable: "Can't be scheduled", ErrImagePull: "Image can't be pulled",
  ImagePullBackOff: "Image can't be pulled" };
const stateWords = state => STATE_WORDS[state] || String(state ?? "").replace(/([a-z])([A-Z])/g, "$1 $2")
  .replace(/ (.*)$/, (all, rest) => " " + rest.toLowerCase());

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  fmtUp, fmtAgo, asDate, ageSecs, thisYear, fmtAt, fmtDay, fmtWeekday, fmtClock, utcTip, fmtWhen, fmtNext, yourZone,
  zoneLine, upChip, svcUrl, plural, STATE_WORDS, stateWords
};
