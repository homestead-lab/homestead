/* What storage leaves behind (#932), opened from Cluster Health.
   Released volumes: each one a Retain class kept after its claim was deleted,
   with the reviewed delete Homestead already has for it (an admin types the
   name there), or Remove old copy for the original a class change kept.
   Stale iSCSI sessions: a host's sessions to an instance-manager that is gone,
   with the commands that end them on the host. Homestead runs nothing here. */
let releasedVolumesShown = [];

const leftoverGiB = bytes => { const g = (bytes || 0) / 1024 ** 3; return `${g >= 10 ? Math.round(g) : Math.round(g * 10) / 10} GiB`; };
const leftoverAge = at => { const t = Date.parse(at); return Number.isFinite(t) ? fmtAgo(Math.max(0, (Date.now() - t) / 1000)) : "—"; };

async function leftoverFacts(title) {
  try { return await api("/api/storage/leftovers"); }
  catch (e) { modal(title, UI.callout("bad", "Could not read storage", esc(e.message)) + UI.actions(UI.cancel("Close"))); return null; }
}

async function releasedVolumesReview() {
  const title = "Released volumes", facts = await leftoverFacts(title);
  if (!facts) return;
  const r = facts.released || {}, rows = r.volumes || [];
  releasedVolumesShown = rows;
  if (r.state === "unknown") return modal(title, UI.callout("warn", "Could not read Released volumes", esc(r.error || "")) + UI.actions(UI.cancel("Close")));
  if (!rows.length) return modal(title, UI.empty("No Released volumes: every kept volume has a claim.") + UI.actions(UI.cancel("Close")));
  const n = rows.length, total = leftoverGiB(rows.reduce((sum, v) => sum + (v.allocated_bytes || 0), 0));
  // Deleting after review finds the volume's claim by name; a new claim of
  // that name is another volume's, and that review would refuse it.
  const action = (v, i) => v.old_copy
    ? UI.button("Remove old copy", `releasedVolumeDelete(${i})`, { kind: "danger", attrs: 'data-need="admin"' })
    : v.longhorn && v.claim_reused ? '<span class="dim xs">Its claim name is in use again: delete it in Longhorn</span>'
    : v.longhorn ? UI.button("Review delete", `releasedVolumeDelete(${i})`, { kind: "danger", attrs: 'data-need="admin"' })
    : '<span class="dim xs">Use your storage\'s own tools</span>';
  const table = UI.table([{ label: "Volume" }, { label: "Class" }, { label: "Holds", className: "mono" }, { label: "Released" }, { label: "" }],
    rows.map((v, i) => [
      `${esc(v.namespace && v.claim ? `${v.namespace}/${v.claim}` : v.pv)}<span class="sub mono">${esc(v.pv)}${v.old_copy ? " · old copy of a class change" : ""}</span>`,
      esc(v.class || "—"),
      `${esc(v.size || "?")}${v.hosts?.length ? `<span class="sub">${esc(`${v.hosts.length} ${v.hosts.length === 1 ? "copy" : "copies"} · ${v.hosts.join(", ")}`)}</span>` : ""}`,
      esc(leftoverAge(v.released_at)), action(v, i)]));
  modal(title, UI.lead(esc(`${n} volume${n === 1 ? " was" : "s were"} kept after ${n === 1 ? "its claim was" : "their claims were"} deleted and hold${n === 1 ? "s" : ""} ${total}.`)) + table +
    UI.more("Why they are kept", "<p>Their class's reclaim policy is Retain, so Kubernetes keeps the data when a claim is deleted, and Longhorn keeps the whole size allocated on each host with a copy. Each delete opens its own review: it checks that nothing uses the volume and counts its snapshots and backups, and an admin types the name to confirm. External backups are kept.</p>") +
    UI.actions(UI.cancel("Close")), true);
}

async function releasedVolumeDelete(i) {
  const v = releasedVolumesShown[i];
  if (!v) return;
  // Its confirmation replaces this list; show the list again once it is done.
  if (v.old_copy) { await reclassRemoveOld(v.pv); return releasedVolumesReview(); }
  return volumeDelete({ name: v.longhorn, pvc_name: v.claim, namespace: v.namespace });
}

async function staleSessionsReview(node) {
  const title = `Stale iSCSI sessions · ${node}`, facts = await leftoverFacts(title);
  if (!facts) return;
  const sessions = (facts.iscsi?.hosts || []).find(h => h.node === node)?.sessions || [];
  if (!sessions.length) return modal(title, UI.empty(`No stale iSCSI sessions on ${node}.`) + UI.actions(UI.cancel("Close")));
  const n = sessions.length, portal = s => `${s.address}:${s.port || 3260}`;
  const table = UI.table([{ label: "Session", className: "mono" }, { label: "Volume" }, { label: "Was served from", className: "mono" }, { label: "Disks", className: "mono" }],
    sessions.map(s => [esc(s.sid), `${esc(s.claim ? `${s.namespace}/${s.claim}` : s.volume)}<span class="sub">${esc(s.state || "")}</span>`, esc(portal(s)), esc((s.disks || []).join(", ") || "—")]));
  const logout = sessions.map(s => `sudo iscsiadm -m session -r ${s.sid} -u`).join("\n");
  // The disks are found from the session as the host has it when this runs,
  // never from the names read earlier: once a session is gone, its disk name
  // can be a live volume's. One with a holder, a mount or a user is skipped.
  const force = sessions.flatMap(s => [
    `for b in /sys/class/iscsi_session/session${s.sid}/device/target*/*/block/*; do d=\${b##*/}; ` +
    `[ -e "$b" ] && [ -z "$(ls /sys/block/$d/holders)" ] && ! findmnt -S /dev/$d >/dev/null && ! sudo fuser -s /dev/$d && ` +
    `! sudo fuser -s /dev/longhorn/${s.volume} && echo 1 | sudo tee /sys/block/$d/device/delete; done`,
    `sudo iscsiadm -m node -T ${s.target} -p ${portal(s)} -o delete`]).join("\n");
  modal(title, UI.lead(esc(`${node} keeps ${n === 1 ? "a session" : `${n} sessions`} to a Longhorn instance-manager that is gone. Attaching ${n === 1 ? "this volume" : "these volumes"} on ${node} can fail in a loop until ${n === 1 ? "it ends" : "they end"}.`)) + table +
    UI.section("End them on the host", UI.steps([
      { title: `On ${node}, log ${n === 1 ? "it" : "them"} out`, detailHtml: `<pre>${esc(logout)}</pre>` },
      { title: "If that fails with exit code 32, remove the session's disks that nothing uses, then its record", detailHtml: `<pre>${esc(force)}</pre>` },
      { title: `Or restart ${node}`, detailHtml: esc("Drain it first from Hosts. The sessions end with the restart.") },
    ])) + UI.more("Why it happens", "<p>Longhorn serves a volume to the host from its instance-manager pod. When a host is lost while a volume's engine runs on it, the instance-manager is replaced, and the host keeps its session to the old address once it comes back. Only the engine that made a session logs it out, and that engine is gone.</p>") +
    UI.actions(UI.cancel("Close")), true);
}

if (typeof window !== "undefined") Object.assign(window, { releasedVolumesReview, releasedVolumeDelete, staleSessionsReview });
