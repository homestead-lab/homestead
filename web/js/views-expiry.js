/* Networking > Certificates and keys: what expires, and when. Read only. The
   server reads each certificate's dates on the leader, about hourly, and keeps
   nothing else of it: no names from inside it, never its key. API keys and
   the linked-cluster key come from an admin-only answer, asked for only by an
   admin. */
let EXPIRY_FILTER = "all";

const EXPIRY_KIND = {
  "certificate": ["cert-manager Certificate", "Certificate"],
  "tls-secret": ["TLS Secret", "Secret"],
  "webhook-ca": ["Webhook CA", "Webhook"],
  "api-server": ["Kubernetes API", "Cluster"],
  "api-key": ["Homestead API key", "API key"],
  "fleet-key": ["Linked-cluster key", "Fleet"],
};
const EXPIRY_STATE = {
  expired: ["Expired", "bad"], critical: ["Expires soon", "bad"], warn: ["Expires soon", "warn"],
  ok: ["OK", "ok"], renews: ["Renews itself", "ok"], "no-expiry": ["No expiry", "neutral"], unknown: ["Not known", "neutral"],
};
// The server's names for what it reads, in words.
const EXPIRY_SOURCE = { "certificates": "cert-manager Certificates", "tls-secrets": "TLS Secrets",
  "webhook-cas": "Webhook CAs", "api-server": "the Kubernetes API server's certificate", "keys": "Homestead's keys" };
const EXPIRY_SOON = ["expired", "critical", "warn"];

/* Why "nothing expires" cannot be said: a source not read, a Secret list cut
   short, or a check that has not run for hours. "" when all was read. */
function expiryGap(d, failed) {
  if (!d?.checked_at) return "not checked yet";
  if (d.stale) return "the check has not run for hours";
  if (failed) return `could not read ${plural(failed, "source")}`;
  if (d.partial) return `only the first ${d.max_secrets || 2000} TLS Secrets were checked`;
  return "";
}

/* The section's line in the Networking list. It never says nothing is
   expiring when something could not be checked. */
function expiryLine(d) {
  const soon = EXPIRY_SOON.reduce((n, s) => n + (d?.counts?.[s] || 0), 0);
  if (soon && d.checked_at) return `${plural(soon, "item")} expiring within ${d.warn_days} days`;
  const gap = expiryGap(d, Object.keys(d?.errors || {}).length);
  return gap ? gap[0].toUpperCase() + gap.slice(1) : `${d.items.length} watched, none expiring soon`;
}

function expiryWhen(r) {
  if (r.state === "no-expiry") return `<span class="dim">Never</span>`;
  if (r.not_after == null) return `<span class="dim">Not known</span>`;
  const date = fmtDay(r.not_after);
  const left = r.days_left < 0 ? `${plural(-r.days_left, "day")} ago` : r.days_left === 0 ? "today" : `in ${plural(r.days_left, "day")}`;
  return `<b class="${EXPIRY_SOON.includes(r.state) ? "needs-attention" : ""}">${esc(left)}</b><div class="dim xs">${esc(date)}</div>`;
}

function expiryWhat(r) {
  const where = [r.namespace, EXPIRY_KIND[r.kind]?.[0] || r.kind, r.owner && `owner ${r.owner}`].filter(Boolean).map(esc).join(" · ");
  return `<b>${esc(r.name)}</b><div class="dim small">${where}</div>
    ${r.note ? `<div class="${EXPIRY_SOON.includes(r.state) ? "needs-attention" : "dim"} xs">${esc(r.note)}</div>` : ""}`;
}

async function viewExpiry() {
  const admin = can("admin");
  const [d, keys] = await Promise.all([api("/api/expiry"),
    admin ? api("/api/expiry/keys").catch(e => ({ items: [], error: e.message })) : null]);
  STATE.data.expiry = d;
  const errors = Object.entries({ ...(d.errors || {}), ...(keys?.error ? { keys: keys.error } : {}) });
  const items = [...d.items, ...(keys?.items || [])];
  const count = state => items.filter(r => r.state === state).length;
  const soon = count("expired") + count("critical") + count("warn");
  const q = (STATE.q || "").toLowerCase();
  const groups = { all: () => true, soon: r => EXPIRY_SOON.includes(r.state),
    certs: r => !["api-key", "fleet-key"].includes(r.kind), keys: r => ["api-key", "fleet-key"].includes(r.kind),
    renews: r => r.state === "renews" };
  if (!admin && EXPIRY_FILTER === "keys") EXPIRY_FILTER = "all";
  const shown = items.filter(r => groups[EXPIRY_FILTER](r) && `${r.namespace} ${r.name}`.toLowerCase().includes(q));
  const filters = [["all", "All"], ["soon", "Expiring soon"], ["certs", "Certificates"], admin && ["keys", "Keys"], ["renews", "Renews itself"]]
    .filter(Boolean).map(([key, label]) => `<button type="button" class="pf-chip" aria-pressed="${EXPIRY_FILTER === key}"
      onclick="expiryFilter(${esc(jsArg(key))})">${esc(label)} <span class="pf-n">${items.filter(groups[key]).length}</span></button>`).join("");
  const checked = d.checked_at ? `checked ${esc(agoText(d.checked_at))}` : "not checked yet";
  const gap = d.checked_at ? expiryGap(d, errors.length) : "";
  const verdict = soon ? `<span class="needs-attention">${plural(soon, "item")} expiring within ${d.warn_days} days</span>`
    : !d.checked_at ? "" : gap ? `<span class="needs-attention">${esc(gap)}</span>` : `nothing expires within ${d.warn_days} days`;
  networkPage("expiry", `
    ${summaryLine("expiry", [`<b>${items.length}</b> watched`, verdict, checked].filter(Boolean))}
    ${!d.checked_at ? UI.callout("info", "Not checked yet",
      "Homestead checks certificates about once an hour. The first check starts shortly after Homestead does.") : ""}
    ${errors.length ? UI.callout("warn", "Could not read everything: what is missing here is not known to be fine",
      errors.map(([k, v]) => `<div><b>Could not read ${esc(EXPIRY_SOURCE[k] || k)}</b>: <span class="dim small">${esc(v)}</span></div>`).join("")) : ""}
    ${d.stale ? UI.callout("warn", `The check has not run since ${agoText(d.checked_at)}`,
      "These are the dates from that check. Settings, Homestead's health, says whether the certificate expiry check is running.") : ""}
    ${d.partial ? UI.callout("warn", `Only the first ${d.max_secrets || 2000} TLS Secrets were checked`,
      "The cluster has more. The rest are not known to be fine.") : ""}
    ${UI.stats([
      { title: "Expired", value: count("expired"), tone: count("expired") ? "bad" : "", sub: count("expired") ? "Clients are refused now" : "None" },
      { title: `Within ${d.critical_days} days`, value: count("critical"), tone: count("critical") ? "bad" : "", sub: "Alerts as critical" },
      { title: `Within ${d.warn_days} days`, value: count("warn"), tone: count("warn") ? "warn" : "", sub: "Alerts as a warning" },
      { title: "Renews itself", value: count("renews"), sub: "cert-manager, and operators that rotate their own" },
    ])}
    <div class="pf-chips" role="group" aria-label="Show">${filters}</div>
    ${UI.table([{ label: "What" }, { label: "Kind" }, { label: "Expires" }, { label: "State" }], shown.map(r => {
      const [label, tone] = EXPIRY_STATE[r.state] || EXPIRY_STATE.unknown;
      return [expiryWhat(r), UI.chip(EXPIRY_KIND[r.kind]?.[1] || r.kind, "neutral", { size: "slim" }), expiryWhen(r), UI.chip(label, tone)];
    }), { empty: d.checked_at ? "Nothing matches." : "Not checked yet." })}
    ${UI.guide("How this is checked", `<p><b>Certificates:</b> when cert-manager is installed, its Certificates give their own dates and renewal time, and their Secrets are not opened. Other TLS Secrets are read on the server, and only the certificate's dates are kept: the private key is never read out, kept or sent here.</p>
      <p><b>Renews itself:</b> a cert-manager Certificate that is Ready with its renewal ahead, or a certificate an operator rotates (KubeVirt and CDI rotate their CAs every few days). These alert only if renewal is overdue or they expire.</p>
      <p><b>Kubernetes API:</b> the certificate the API server presents. k3s and RKE2 renew it when the server restarts close to its expiry, so a server that runs for a year without a restart can run out.</p>
      ${admin ? `<p><b>Keys:</b> Homestead API keys expire as set when they were made, and only admins see them here. The linked-cluster key never expires; it changes whenever a cluster is unlinked.</p>` : ""}
      <p>Checked about once an hour. Alerts come ${d.warn_days} days ahead (a warning) and ${d.critical_days} days ahead (critical).</p>`)}`);
}
window.viewExpiry = viewExpiry;
window.expiryLine = expiryLine;
window.expiryFilter = key => { EXPIRY_FILTER = key; viewExpiry(); };
