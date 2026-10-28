/* Match copies to hosts (#841). On a one-host cluster, app volumes that ask
   for more copies than the host can hold read degraded. Cluster Health offers
   this review: each volume, its app and its count before and after. Nothing
   changes until an admin clicks, and the click sends exactly the rows shown;
   the server refuses them if anything changed since. Once a second host can
   hold copies, the same review puts back the counts the volumes asked for. */
let oneHostCopiesShown = null;

async function oneHostCopiesReview() {
  let facts;
  try { facts = await api("/api/longhorn/one-host-copies"); }
  catch (e) {
    oneHostCopiesShown = null;
    return modal("Match copies to hosts", UI.callout("bad", "Could not read volume copies", esc(e.message)) + UI.actions(UI.cancel("Close")));
  }
  const raise = facts.state === "raise", rows = facts.volumes || [];
  oneHostCopiesShown = rows.length && ["lower", "raise"].includes(facts.state) ? facts : null;
  const title = raise ? "Put copies back" : "Match copies to hosts";
  if (!oneHostCopiesShown) {
    const why = facts.state === "unknown" ? UI.callout("warn", "Could not read volume copies", esc(facts.error || "")) :
      UI.empty(facts.state === "n/a" ? "Nothing to change on this cluster: Homestead does not match Longhorn copies here."
        : "Nothing to change: every volume's copies fit the hosts.");
    return modal(title, why + UI.actions(UI.cancel("Close")));
  }
  const n = rows.length, volumes = `${n} volume${n === 1 ? "" : "s"}`;
  const lead = raise
    ? `Raises ${volumes} back to the copies ${n === 1 ? "it" : "they"} asked for, now that ${facts.hosts} hosts can hold them.`
    : `Lowers ${volumes} to the one copy this host can hold.`;
  const table = UI.table([{ label: "Volume" }, { label: "App" }, { label: "Copies", className: "mono" }],
    rows.map(r => [esc(r.claim || r.volume), esc(r.app || "—"), esc(`${r.from} → ${r.to}`)]));
  const details = raise
    ? "Longhorn makes each new copy on another host while the volume stays in use. A volume keeps the count it asked for on itself until it has all of it back. A volume kept to tagged hosts or disks can still read degraded if fewer of those exist."
    : "The data stays whole and nothing moves. Each volume keeps the count it asked for, so it can be put back when a host joins. No storage class or Longhorn setting changes, and volumes made later still ask for their class's count.";
  modal(title, UI.lead(esc(lead)) + table + UI.more("What changes", `<p>${esc(details)}</p>`) +
    UI.actions(UI.button(`${raise ? "Raise" : "Lower"} ${n}`, "oneHostCopiesApply()", { kind: "pri", id: "ohc_apply", attrs: 'data-need="admin"' }), UI.cancel()));
}

async function oneHostCopiesApply() {
  const facts = oneHostCopiesShown;
  if (!facts) return;
  const button = $("#ohc_apply");
  if (button) button.disabled = true;
  try {
    const r = await api("/api/longhorn/one-host-copies", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: facts.state, volumes: facts.volumes.map(v => ({ volume: v.volume, from: v.from, to: v.to })) }) });
    toast(r.message, r.ok ? "ok" : "bad");
    if (r.ok) { oneHostCopiesShown = null; closeModal(); }
  } catch (e) { toast(e.message, "bad"); }
  finally { if (button) button.disabled = false; }
  HealthInsights.load(true);
}

if (typeof window !== "undefined") Object.assign(window, { oneHostCopiesReview, oneHostCopiesApply });
