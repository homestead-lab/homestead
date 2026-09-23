/* Schedules without cron.

   Longhorn runs its recurring jobs from a five-field cron expression on the
   cluster's clock, which is UTC. People think in "every day at two" and in
   their own time zone, so the job editor offers those shapes and writes the
   cron for them, and every schedule on screen is read back in words with its
   next runs in local time. Anything the shapes cannot express stays editable
   as cron. */
(function () {
  const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  const SHORT = DAYS.map(day => day.slice(0, 3));
  const MINUTE_STEPS = [5, 10, 15, 20, 30];
  const HOUR_STEPS = [1, 2, 3, 4, 6, 8, 12];
  const pad = n => String(n).padStart(2, "0");
  const int = text => /^\d+$/.test(text) ? +text : NaN;

  /* One field's values, or null when it is malformed. */
  function field(text, lo, hi) {
    const out = new Set();
    for (const part of String(text).split(",")) {
      const [range, stepText] = part.split("/");
      const step = stepText === undefined ? 1 : int(stepText);
      if (!(step >= 1)) return null;
      let a, b;
      if (range === "*") { a = lo; b = hi; }
      else if (range.includes("-")) { [a, b] = range.split("-").map(int); }
      else { a = int(range); b = stepText === undefined ? a : hi; }
      if (!(a >= lo && b <= hi && a <= b)) return null;
      for (let v = a; v <= b; v += step) out.add(v);
    }
    return out;
  }

  function parse(cron) {
    const parts = String(cron || "").trim().split(/\s+/);
    if (parts.length !== 5) return null;
    const [m, h, dom, mon, dow] = parts;
    const spec = { minute: field(m, 0, 59), hour: field(h, 0, 23), dom: field(dom, 1, 31),
      month: field(mon, 1, 12), dow: field(dow.replace(/\b7\b/g, "0"), 0, 6),
      anyDom: dom === "*", anyDow: dow === "*" };
    return Object.values(spec).some(v => v === null) ? null : spec;
  }

  /* The next count run times after from, in UTC as the cluster keeps them. */
  function next(cron, from = new Date(), count = 3) {
    const spec = parse(cron);
    if (!spec) return [];
    const out = [];
    const hours = [...spec.hour].sort((a, b) => a - b), minutes = [...spec.minute].sort((a, b) => a - b);
    const start = Date.UTC(from.getUTCFullYear(), from.getUTCMonth(), from.getUTCDate());
    for (let day = 0; day < 800 && out.length < count; day++) {
      const date = new Date(start + day * 86400000);
      if (!spec.month.has(date.getUTCMonth() + 1)) continue;
      const domOk = spec.dom.has(date.getUTCDate()), dowOk = spec.dow.has(date.getUTCDay());
      // Cron's rule: with both day fields restricted, either one will do.
      const dayOk = spec.anyDom ? dowOk : spec.anyDow ? domOk : domOk || dowOk;
      if (!dayOk) continue;
      for (const h of hours) {
        for (const m of minutes) {
          const at = new Date(start + day * 86400000 + (h * 60 + m) * 60000);
          if (at > from) { out.push(at); if (out.length >= count) return out; }
        }
      }
    }
    return out;
  }

  /* The editor's shapes. Anything else is "custom". */
  function toPlan(cron) {
    const text = String(cron || "").trim().replace(/\s+/g, " ");
    let match;
    if ((match = text.match(/^\*\/(\d+) \* \* \* \*$/)) && MINUTE_STEPS.includes(+match[1])) return { kind: "minutes", every: +match[1] };
    if ((match = text.match(/^(\d+) \* \* \* \*$/)) && +match[1] < 60) return { kind: "hours", every: 1, minute: +match[1] };
    if ((match = text.match(/^(\d+) \*\/(\d+) \* \* \*$/)) && +match[1] < 60 && HOUR_STEPS.includes(+match[2])) return { kind: "hours", every: +match[2], minute: +match[1] };
    if ((match = text.match(/^(\d+) (\d+) \* \* \*$/)) && +match[1] < 60 && +match[2] < 24) return { kind: "daily", hour: +match[2], minute: +match[1] };
    if ((match = text.match(/^(\d+) (\d+) \* \* ([0-7](?:,[0-7])*)$/)) && +match[1] < 60 && +match[2] < 24) {
      const days = [...new Set(match[3].split(",").map(d => +d % 7))].sort();
      return { kind: "weekly", hour: +match[2], minute: +match[1], days };
    }
    if ((match = text.match(/^(\d+) (\d+) (\d+) \* \*$/)) && +match[1] < 60 && +match[2] < 24 && +match[3] >= 1 && +match[3] <= 28) {
      return { kind: "monthly", hour: +match[2], minute: +match[1], day: +match[3] };
    }
    return { kind: "custom", cron: text };
  }

  function fromPlan(plan) {
    const m = +plan.minute || 0, h = +plan.hour || 0;
    switch (plan.kind) {
      case "minutes": return `*/${plan.every} * * * *`;
      case "hours": return +plan.every === 1 ? `${m} * * * *` : `${m} */${plan.every} * * *`;
      case "daily": return `${m} ${h} * * *`;
      case "weekly": return `${m} ${h} * * ${(plan.days && plan.days.length ? plan.days : [0]).join(",")}`;
      case "monthly": return `${m} ${h} ${plan.day || 1} * *`;
      default: return String(plan.cron || "").trim();
    }
  }

  /* A schedule in words: "Every day at 02:00 UTC". */
  function describe(cron) {
    const plan = toPlan(cron);
    const at = `${pad(plan.hour)}:${pad(plan.minute)} UTC`;
    switch (plan.kind) {
      case "minutes": return `Every ${plan.every} minutes`;
      case "hours": return plan.every === 1 ? `Every hour at :${pad(plan.minute)}` : `Every ${plan.every} hours at :${pad(plan.minute)}`;
      case "daily": return `Every day at ${at}`;
      case "weekly": return plan.days.length === 7 ? `Every day at ${at}`
        : plan.days.join(",") === "1,2,3,4,5" ? `Weekdays at ${at}`
        : plan.days.join(",") === "0,6" ? `Weekends at ${at}`
        : `${plan.days.map(d => plan.days.length === 1 ? DAYS[d] : SHORT[d]).join(", ")} at ${at}`;
      case "monthly": return `Monthly on the ${ordinal(plan.day)} at ${at}`;
      default: return parse(cron) ? `Custom · ${plan.cron}` : `Invalid · ${plan.cron}`;
    }
  }

  function ordinal(n) {
    const tail = n % 100 >= 11 && n % 100 <= 13 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" })[n % 10] || "th";
    return `${n}${tail}`;
  }

  const api = { parse, next, toPlan, fromPlan, describe, DAYS, SHORT, MINUTE_STEPS, HOUR_STEPS };
  if (typeof window !== "undefined") window.CRON = api;
  if (typeof module !== "undefined") module.exports = api;
})();
