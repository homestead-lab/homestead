/* Homestead views: moving a volume's copies to other disks. Split from views-storage.js (#911); a classic script. */
/* ---------------- moving a volume's copies to other disks ----------------
   Where its copies live, by the tags on Longhorn's disks and hosts: each copy
   off the chosen disks is rebuilt on one of them and then removed, so the
   volume stays in use throughout (homestead_copy_move.py). */
let COPY_MOVE = null;
window.volumeCopyMove = async (x, preset) => {
  COPY_MOVE = { volume: x.name, claim: x.pvc_name || x.name, plan: null, disk: new Set(preset?.disk_tags || []), node: new Set(preset?.node_tags || []) };
  modal(`Move copies · ${COPY_MOVE.claim}`, UI.lead("Move this volume's copies to disks with other tags - onto an NVMe, say. Each copy is rebuilt on a chosen disk before the old one is removed, so what uses it keeps running.") +
    '<div id="cm_body"><div class="empty"><span class="spin2"></span> reading where its copies are</div></div>' +
    UI.actions(UI.cancel() + UI.button("Move copies", "volumeCopyMoveStart()", { kind: "pri", id: "cm_go", disabled: true })), false, "operation-review");
  volumeCopyMovePlan();
};
window.volumeCopyMoveTag = (kind, tag, on) => {
  if (!COPY_MOVE) return;
  COPY_MOVE[kind][on ? "add" : "delete"](tag);
  volumeCopyMovePlan();
};
/* The plan for moving a volume's copies (#909), on the shared review flow: it keeps the sequence and the
   one-shot send. COPY_MOVE is the dialog's choices (the volume, the disk and host tags ticked). */
let COPY_REPLAN = false;
const COPY_PLAN = UI.reviewFlow({
  loading: ({ go }) => { if (go) go.disabled = true; },
  live: ({ host }) => $("#cm_body") === host,
  preview: ({ review }) => api(`/api/longhorn/copies/plan?${new URLSearchParams({ volume: review.volume, disk_tags: [...review.disk].join(","), node_tags: [...review.node].join(",") })}`),
  render: (p, { review, host }) => {
  const pick = (kind, tags) => tags.length ? `<div class="row wrap" style="gap:6px">${tags.map(t => `<label class="check"><input type="checkbox" ${review[kind].has(t) ? "checked" : ""}
      onchange="volumeCopyMoveTag(${jsq(kind)},${jsq(t)},this.checked)"> ${esc(t)}</label>`).join("")}</div>` : '<span class="dim small">None set in Longhorn</span>';
  const ready = p.copies.filter(c => c.moves).length;
  const messages = [...p.blockers.map(m => ["bad", m]), ...p.warnings.map(m => ["warn", m])];
  host.innerHTML = UI.fields(UI.field("Disk tags", pick("disk", p.choices.disk_tags), { help: "Copies go to disks carrying every tag ticked" }),
      ...(p.choices.node_tags.length ? [UI.field("Host tags", pick("node", p.choices.node_tags))] : [])) +
    (messages.length && (review.disk.size || review.node.size) ? UI.notices(messages) : "") +
    UI.section("Its copies", UI.table([{ label: "Host" }, { label: "Disk" }, { label: "Tags" }, { label: "" }],
      p.copies.map(c => [esc(c.host), `<span class="mono xs">${esc(c.path || c.disk)}</span>`, esc(c.tags.join(", ") || "none"),
        c.moves ? UI.chip("moves", "info") : review.disk.size || review.node.size ? UI.chip("stays", "ok") : ""]), { empty: "Longhorn lists no copies of it" })) +
    (p.targets.length ? UI.section("Disks it can go to", UI.table([{ label: "Host" }, { label: "Disk" }, { label: "Room" }],
      p.targets.map(t => [esc(t.host), `<span class="mono xs">${esc(t.path)}</span>`, t.fits ? `${esc(sizeText(t.room_gb))}` : UI.chip("no room", "bad")]))) : "") +
    UI.facts([["Size", `${esc(sizeText(p.size_gb))} · ${esc(sizeText(p.used_gb))} written`], ["State", esc(p.state === "attached" ? `in use on ${p.attached_to}` : p.state)],
      ["To move", ready ? `${ready} of ${p.copies.length} cop${p.copies.length === 1 ? "y" : "ies"}, one at a time` : "nothing yet"]]) +
    UI.more("What happens", "The volume's disk and host tags are set, so Longhorn builds each new copy on a chosen disk. One copy more is asked for while each is built, and the old copy is removed once the new one is whole - the volume never has fewer whole copies than it asks for. Its storage class name stays as it is: a claim's class cannot change, only where its copies live. Copies Longhorn rebuilds later follow the new tags. Stopping the job puts the old tags back; copies already moved stay.");
    COPY_PLAN.ready();
    if (window.applyRole) applyRole();
  },
  failed: (e, { host }) => { host.innerHTML = UI.callout("bad", "Review unavailable", esc(e.message)); },
  blocked: plan => !plan.ok, button: "cm_go",
  apply: async plan => {
    const review = COPY_MOVE, go = $("#cm_go");
    go.disabled = true;
    try {
      const r = await api("/api/longhorn/copies/move", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ volume: review.volume, disk_tags: plan.disk_tags, node_tags: plan.node_tags, review_token: plan.review_token }) });
      if (typeof noteOperation === "function") noteOperation(r.operation);
      toast(`Moving the copies of ${review.claim}; follow it in Jobs`, "ok");
      closeModal(); refresh(true);
    } catch (e) {
      toast(e.message, "bad");
      COPY_REPLAN = true;   // read the plan again once this send has settled
    }
  },
  after: () => { if (COPY_REPLAN) { COPY_REPLAN = false; volumeCopyMovePlan(); } },
});
window.volumeCopyMovePlan = () => {
  const review = COPY_MOVE, host = $("#cm_body");
  if (!review || !host) return;
  return COPY_PLAN.open({ review, host, go: $("#cm_go") });
};
window.volumeCopyMoveStart = async () => {
  if (!COPY_PLAN.ready()) return;
  await COPY_PLAN.apply();
};

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  COPY_MOVE, COPY_REPLAN, COPY_PLAN
};
