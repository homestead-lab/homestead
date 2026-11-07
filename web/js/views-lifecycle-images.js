/* Homestead views: the Images page: the image cache. Split from views-lifecycle.js (#911); a classic script. */
/* ---------------- image cache ---------------- */
/* Why an image is kept: something runs it, will run it, or can go back to it. */
const IMAGE_REASONS = {
  active: { tone: "ok", tip: "a running container uses it" },
  rollback: { tone: "neutral", tip: "kept so Roll back can return to it" },
  stopped: { tone: "neutral", tip: "stopped, and starts from this image; delete it or change its image to clean this up" },
  scheduled: { tone: "neutral", tip: "a scheduled job that runs this image" },
  unreadable: { tone: "warn", tip: "Kept: a record that says which images are in use couldn't be read" },
};
/* Why one image is kept, as the server words it (label and detail), with the
   page that searches for the thing keeping it where there is one. A Job,
   scheduled job, pool, template, snapshot or import has no page that finds it
   by name, so it is named, not linked. */
const IMAGE_KEEPER_PAGES = { Deployment: "workloads", StatefulSet: "workloads", DaemonSet: "workloads", VirtualMachine: "vms" };
function imageKeeper(r) {
  const known = IMAGE_REASONS[r.reason] || {};
  const where = `${r.namespace} · ${r.workload} · ${r.container}`;
  const chip = UI.chip(r.label || r.reason, known.tone ?? "ok", { size: "slim", tip: `${where} - ${r.detail || known.tip || ""}` });
  const page = r.reason === "unreadable" ? null : IMAGE_KEEPER_PAGES[r.kind];
  const name = page && r.workload
    ? `<button type="button" class="linkish xs" aria-label="Open ${esc(r.workload)}" onclick="imageKeeperOpen(${jsq(page)},${jsq(r.workload)})">${esc(r.workload)}</button>`
    : `<span class="dim xs">${esc(r.workload)}</span>`;
  return `<span class="image-keeper">${chip} ${name}</span>`;
}
window.imageKeeperOpen = (page, name) => {
  closeModal(false);
  STATE.q = name;
  const search = $("#globalSearch");
  if (search) search.value = name;
  go(page, { keepSearch: true });
};
async function viewImages() {
  // VM images that could not be read are said, not left out of the page (#620).
  const [d, vm, auto] = await Promise.all([api("/api/images"), api("/api/images/vm").catch(e => ({ unread: e.message })),
    api("/api/images/cleanup-auto").catch(error => ({ error: error.message }))]);
  STATE.data.imageCache = d;
  STATE.data.vmImages = vm;
  STATE.data.imageCleanupAuto = auto;
  // Kubernetes lists only each node's largest images; the full list is
  // containerd's. Homestead asks for it itself when its last answer is old,
  // so this only waits for a scan that is running to finish.
  if ((d.scanning || []).length) setTimeout(() => { if (STATE.view === "images") viewImages(); }, 4000);
  const q = STATE.q.toLowerCase();
  const core = n => /(^|\/)(rancher|harvester|longhornio|kubevirt|cdi-|cilium|kube-|metrics-server|registry\.k8s\.io|pause|traefik|fleet|system-upgrade|k8snetworkplumbingwg|multus|whereabouts|kubeovn|calico|canal|flannel|coredns|etcd|rke2|neuvector|suse\/sles\/)/i.test(n);
  const all = d.images.filter(i => !q || i.name.toLowerCase().includes(q));
  const hidden = all.filter(i => core(i.name)).length;
  const imgs = all.filter(i => STATE.showCoreImages || !core(i.name));
  paint(`${UI.pageHeader(`Image cache`, `${imgs.length} app images across ${d.nodes.length} hosts · ${d.protected || 0} kept (running, stopped or for rollback) · ${hidden && !STATE.showCoreImages ? `${hidden} Harvester/system images hidden` : `${d.distinct} total`} ${tip("Each host's containerd is asked for every image it holds every 15 minutes or so; images pulled since are added from what Kubernetes reports.")}`, `<label class="switch"><input type="checkbox" ${STATE.showCoreImages ? "checked" : ""} onchange="STATE.showCoreImages=this.checked;viewImages()"> Show Harvester/system images</label>`)}
    ${imageCleanupRow(d, auto)}
    ${UI.guide("How images are kept", `<p><b>Clean up</b> removes an image nothing uses from the hosts that hold it.
      <b>running</b> images are what running containers use. An image also stays while something that is not running yet will start from it:
      a paused or waiting Job, an app scaled to zero or without pods yet, a scheduled job, a stopped VM, a VM template or snapshot, or a disk import. Each says which, and the app or VM is a link where it has a page.
      <b>rollback</b> copies are the image a container had before its last update, kept so <b>Roll back</b> can return to it. <b>Forget</b> one and it becomes unused, to clean up like the rest.</p>`)}
    <div class="grid g3 statgrid" style="margin-bottom:18px">
      ${d.nodes.map(n => `<div class="card flat"><div class="ctitle" title="${esc(n.node)}">${esc(n.node)}</div>
        <div class="bignum" style="margin-top:8px">${n.total_gb}<span class="unit">GiB</span></div>
        <div class="csub">${n.count} images cached${n.scanned_at ? ` · scanned ${esc(fmtAgo(Date.now() / 1000 - n.scanned_at))}` : " · not scanned yet"}</div></div>`).join("")}
    </div>
    ${(d.scanning || []).length ? `<div class="note" style="margin-bottom:12px"><span class="spin2"></span> Asking containerd on ${d.scanning.length} host${d.scanning.length === 1 ? "" : "s"} for every image it holds…</div>`
      : !d.complete ? `${UI.callout("warn", "", `<span>Kubernetes reports only each host's largest images, so some - running ones included - are missing here.
          A scan asks each host's containerd for all of them.</span><button class="btn sm" data-need="admin" onclick="imageScan()">Scan every host</button>`, { className: "between", attrs: 'style="margin-bottom:12px"' })}` : ""}
    ${(d.pulls || []).map(pull => `${UI.callout("warn", "", `<span>Pre-pulling <span class="mono">${esc(pull.image)}</span> · ${pull.ready} of ${pull.desired} host${pull.desired === 1 ? "" : "s"} done.
        It runs until every host has the image; its pods come straight back if you delete them.</span>
      <button class="btn sm danger" data-need="admin" onclick="prepullStop(${jsq(pull.name)})">Stop</button>`, { className: "between prepull-note", attrs: 'style="margin-bottom:12px"' })}`).join("")}
    ${(d.pulls_finished || []).length ? `${UI.callout("ok", "", `Finished pre-pulling
      ${d.pulls_finished.map(pull => `<span class="mono">${esc(pull.image)}</span>`).join(", ")} — cleared away.`, { attrs: 'style="margin-bottom:12px"' })}` : ""}
    <div class="card flat pad0"><div class="tblwrap"><table data-sort="images" class="tbl stack imgtable"><thead><tr>
      <th>Image</th><th>Size</th><th>Cached on</th><th>Retention</th><th></th></tr></thead><tbody>
      ${imgs.map(i => {
        const missing = d.node_names.filter(n => !i.nodes.includes(n));
        const retained = i.retained_by || [];
        const retention = retained.length ? retained.slice(0, 3).map(imageKeeper).join(" ") +
          (retained.length > 3 ? ` ${UI.chip(`+${retained.length - 3}`, "neutral")}` : "") : UI.chip("unreferenced", "neutral");
        return `<tr><td class="mono small imgname">${esc(i.name)}</td>
        <td class="mono">${bytesText(i.size_mb * 1024 ** 2)}</td>
        <td>${i.nodes.map(n => UI.chip(n.replace("harvester-", ""), "ok")).join(" ")}
            ${missing.map(n => UI.chip(n.replace("harvester-", ""), "neutral", { tip: "Not cached on this host" })).join(" ")}
            ${missing.length ? "" : '<span class="dim xs">everywhere</span>'}</td>
        <td><div class="row" style="gap:5px">${retention}</div></td>
        <td data-actions>${actionBar([
          missing.length ? { label: "Pre-pull", run: `prepull(${jsq(i.name)})`, tip: "Pull it onto the hosts that do not have it" } : null,
          // Forget lets a kept Roll back image go, so it can be cleaned up.
          ...retained.filter(r => r.reason === "rollback").map(r => ({ label: `Forget ${r.workload}'s roll back`, run: `forgetRollback(${jsq(r.namespace)},${jsq(r.workload)})`,
            need: "admin", tip: `Stop keeping this image for ${r.workload}'s Roll back, so it can be cleaned up` })),
          !i.protected && !i.system && i.digest ? { label: "Clean up", run: `imageCleanupReview(${jsq(i.digest)})`, need: "admin", danger: true } : null],
          { shown: missing.length ? 1 : 0, label: `Actions for ${i.name}` })}</td></tr>`;
      }).join("") || `<tr><td colspan=5 class="empty">No cached images to show.</td></tr>`}
    </tbody></table></div></div>
    ${vmImagesSection(vm, d.node_names)}`);
}

/* Unused images, from every host at once: Clean all unused (reviewed) and
   the automatic clean-up, which the leader runs hourly when it is on. An
   update keeps one previous image for Roll back; the one before is unused. */
const imageUnused = d => (d.images || []).filter(i => i.digest && !i.protected && !i.system && (i.nodes || []).length);
function imageCleanupRow(d, auto) {
  const unused = imageUnused(d);
  const size = Math.round(unused.reduce((sum, i) => sum + i.size_mb * i.nodes.length, 0));
  const last = auto?.last;
  const state = auto?.error ? UI.chip("unknown", "warn", { tip: auto.error })
    : auto?.auto ? UI.chip(`on · ${auto.after_days} days`, "ok") : UI.chip("off", "neutral");
  const detail = auto?.error ? "The automatic clean-up's setting could not be read"
    : auto?.auto ? `Images nothing uses for ${auto.after_days} days are removed from every host, checked hourly${last
        ? ` · last: ${last.removed ? `removed ${last.removed} (${bytesText(last.freed_mb * 1024 ** 2)})` : "nothing due"} ${esc(fmtAgo(Date.now() / 1000 - last.at))}` : ""}`
    : "Images nothing uses stay until you clean them, or a host's image disk is 85% full";
  return `<div class="card flat" style="margin-bottom:14px">
    ${serviceRow(`${unused.length} unused image${unused.length === 1 ? "" : "s"}${unused.length ? ` · ${bytesText(size * 1024 ** 2)} across hosts` : ""}`, "",
      "Not running, not a stopped app's or a scheduled job's, not a Roll back copy",
      actionBar([{ label: "Clean all unused", run: "imageCleanAllReview()", need: "admin", danger: true, disabled: !unused.length }]))}
    ${serviceRow("Automatic clean-up", state, detail, actionBar([{ label: auto?.auto ? "Change" : "Turn on", run: "imageCleanupAuto()", need: "admin", disabled: !!auto?.error }]))}
  </div>`;
}
window.imageCleanupRow = imageCleanupRow;
window.imageCleanAllReview = async () => {
  modal("Clean all unused images", '<div class="empty"><span class="spin2"></span> Checking what nothing uses…</div>');
  let review;
  try { review = await api("/api/images/cleanup-unused/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); }
  catch (e) { $("#mbody").innerHTML = UI.callout("bad", "Could not check the images", `${esc(e.message)}. Nothing was removed.`) + UI.actions(UI.cancel("Close")); return; }
  STATE.data.imageCleanReview = review;
  if (!review.count) { $("#mbody").innerHTML = UI.lead("No image is unused: every cached image is running, a stopped app's or a scheduled job's, or kept for Roll back.") + UI.actions(UI.cancel("Close")); return; }
  $("#mbody").innerHTML = UI.lead(`Removes ${review.count} image${review.count === 1 ? "" : "s"} nothing uses from every host that holds them, freeing about <b>${bytesText(review.freed_mb * 1024 ** 2)}</b> across ${review.hosts.length} host${review.hosts.length === 1 ? "" : "s"}. Running images, stopped apps' and scheduled jobs' images, Roll back copies and Harvester's or Kubernetes' own images stay. An image removed here is pulled again if something needs it later.`)
    + (review.complete ? "" : UI.callout("warn", "Some hosts haven't been scanned yet", "Images on them that Kubernetes doesn't list aren't included."))
    + UI.table([{ label: "Image" }, { label: "Size" }, { label: "Hosts" }, { label: "Unused since" }],
      review.images.map(i => [`<span class="mono small">${esc(i.name)}</span>`, bytesText(i.size_mb * 1024 ** 2),
        esc(i.nodes.join(", ")), esc(fmtAgo(Date.now() / 1000 - i.unused_since))]))
    + UI.typedConfirm({ id: "cleanAllConfirm", expect: "CLEAN", button: "cleanAllGo" })
    + UI.actions(UI.cancel() + `<button id="cleanAllGo" class="btn danger" data-need="admin" disabled onclick="imageCleanAllApply()">Clean ${review.count} image${review.count === 1 ? "" : "s"}</button>`);
};
window.imageCleanAllApply = async () => {
  const review = STATE.data.imageCleanReview;
  if (!review || !typedConfirmMatches("cleanAllConfirm")) return;
  try {
    const r = await api("/api/images/cleanup-unused", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: review.token }) });
    toast(r.detail + (r.skipped?.length ? `; ${r.skipped.length} skipped, needed again` : ""), "ok");
    closeModal(); viewImages();
  } catch (e) { toast(e.message, "bad"); }
};
window.imageCleanupAuto = () => {
  const auto = STATE.data.imageCleanupAuto || {};
  modal("Automatic image clean-up", UI.lead("The leader checks every hour, and removes images nothing has used for this many days from every host that holds them. Running images, stopped apps' and scheduled jobs' images, Roll back copies and the platform's own images are never removed.")
    + `<label class="switch"><input type="checkbox" id="ica_on" ${auto.auto ? "checked" : ""}> Remove unused images automatically</label>
    <div class="f"><label for="ica_days">After this many days unused</label><input id="ica_days" type="number" min="1" max="365" value="${esc(auto.after_days || 14)}"></div>
    <p class="dim small">Days count from when Homestead first saw an image unused.</p>`
    + UI.actions(UI.cancel() + UI.button("Save", "imageCleanupAutoSave()", { kind: "pri" })));
};
window.imageCleanupAutoSave = async () => {
  const days = Math.round(+$("#ica_days").value);
  if (!(days >= 1 && days <= 365)) return toast("Choose 1 to 365 days", "bad");
  const on = !!$("#ica_on").checked;
  try {
    await settingsWrite({ image_cleanup: { auto: on, after_days: days } });
    toast(on ? `Unused images are removed after ${days} days` : "Automatic image clean-up is off", "ok");
    closeModal(); viewImages();
  } catch (e) { toast(e.message, "bad"); }
};

/* VM images: on Harvester each is downloaded once and kept as a Longhorn
   backing image. A disk made from it starts as a copy, and every node its
   disks run on holds a copy of the image - a cache as much as the container
   one above, and as easy to lose track of. */
function vmImagesSection(vm, nodeNames) {
  if (!vm) return "";
  if (vm.unread) return `<div class="ctitle" style="margin-top:24px">VM images</div>${UI.callout("warn", "Could not read the VM images", esc(vm.unread))}`;
  if (!vm.harvester) return `<div class="ctitle" style="margin-top:24px">VM images</div><div class="note">${esc(vm.note)}</div>`;
  const size = mb => bytesText(mb * 1024 ** 2);
  const q = STATE.q.toLowerCase();
  const rows = vm.images.filter(i => !q || i.display.toLowerCase().includes(q));
  const total = vm.images.reduce((sum, i) => sum + i.size_mb * Math.max(1, i.copies), 0);
  return `<div class="between" style="margin:24px 0 10px"><div><div class="ctitle" style="margin:0">VM images</div>
      <p class="dim small" style="margin:4px 0 0">${vm.images.length} image${vm.images.length === 1 ? "" : "s"} · ${size(Math.round(total))} across their copies on hosts ·
        a disk made from one keeps reading from it, so an image a disk came from stays</p></div></div>
    <div class="card flat pad0"><div class="tblwrap"><table data-sort="vmimages" class="tbl stack imgtable"><thead><tr>
      <th>Image</th><th>Size</th><th>Copies on</th><th>Disks made from it</th><th></th></tr></thead><tbody>
      ${rows.map(i => {
        const missing = (nodeNames || []).filter(n => !i.nodes.includes(n));
        const state = i.deleting ? UI.chip("deleting", "neutral")
          : i.state === "failed" ? UI.chip("download failed", "bad", { tip: i.message })
          : i.state === "downloading" ? UI.chip(`downloading ${i.progress}%`, "warn") : "";
        const used = i.used_by.length ? i.used_by.map(v => UI.chip(v.split("/").pop(), "ok")).join(" ")
          : i.disks.length ? UI.chip(`${i.disks.length} disk${i.disks.length === 1 ? "" : "s"}, no VM`, "neutral", { tip: i.disks.join(", ") })
          : UI.chip("unused", "neutral");
        return `<tr><td class="imgname"><b>${esc(i.display)}</b> ${state}
            <div class="dim xs mono">${esc(i.namespace)}${i.source ? ` · from ${esc(i.source)}` : ""}${i.virtual_size_gb ? ` · ${i.virtual_size_gb} GiB disk` : ""}</div></td>
          <td class="mono nowrap" data-sort="${i.size_mb}">${i.size_mb ? size(i.size_mb) : '<span class="dim">—</span>'}</td>
          <td>${i.nodes.map(n => UI.chip(n.replace("harvester-", ""), "ok")).join(" ")}
            ${missing.map(n => UI.chip(n.replace("harvester-", ""), "neutral", { tip: "Not cached on this host" })).join(" ")}</td>
          <td><div class="row" style="gap:5px">${used}</div></td>
          <td data-actions>${!i.disks.length && !i.deleting ? actionBar([{ label: "Delete", run: `vmImageDelete(${jsq(i.namespace)},${jsq(i.name)})`, need: "admin", danger: true }]) : ""}</td></tr>`;
      }).join("") || `<tr><td colspan=5 class="empty">No VM images: an image is kept here when a VM is made from a download address.</td></tr>`}
    </tbody></table></div></div>`;
}
window.vmImageDelete = (namespace, name) => {
  const i = (STATE.data.vmImages?.images || []).find(x => x.namespace === namespace && x.name === name);
  if (!i) return;
  modal(`Delete · ${i.display}`, `<p>The image is deleted, with its copies on ${i.nodes.length} host${i.nodes.length === 1 ? "" : "s"}
      (${esc(i.nodes.join(", ") || "none")}). No disk was made from it. A VM made from the same download address later downloads it again.</p>
    ${UI.typedConfirm({ id: "vmi_confirm", expect: i.display, button: "vmi_go" })}
    ${UI.actions(UI.cancel() + `<button class="btn danger" id="vmi_go" disabled onclick="vmImageDeleteGo(${jsq(namespace)},${jsq(name)})">Delete image</button>`)}`);
};
window.vmImageDeleteGo = async (namespace, name) => {
  if (!typedConfirmMatches("vmi_confirm")) return;
  try {
    const r = await api("/api/images/vm/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ namespace, name, confirm: $("#vmi_confirm").value.trim() }) });
    toast(r.detail, "ok"); closeModal(); resetPaint(); viewImages();
  } catch (e) { toast(e.message, "bad"); }
};
window.imageScan = async () => {
  try {
    const r = await api("/api/images/scan", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast(r.detail, "ok"); window.__imageScanAt = Date.now();
    setTimeout(() => { if (STATE.view === "images") viewImages(); }, 5000);
  } catch (e) { toast(e.message, "bad"); }
};
window.forgetRollback = async (namespace, name) => {
  if (!(await ask(`Stop keeping ${name}'s previous image? Roll back for its last update goes, and the image can be cleaned up.`, { title: "Forget the previous image", ok: "Forget it", danger: true }))) return;
  try {
    const r = await api("/api/images/forget-rollback", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ namespace, name }) });
    toast(r.detail, "ok"); resetPaint(); viewImages();
  } catch (e) { toast(e.message, "bad"); }
};
window.prepull = async image => {
  try {
    const result = await api("/api/images/prepull", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image }) });
    toast(result.message || "pre-pull started", result.skipped?.length ? "warn" : "ok");
    resetPaint(); viewImages();
  } catch (e) { toast(e.message, "bad"); }
};
/* Its pods are a DaemonSet's to replace, so stopping means deleting the set. */
window.prepullStop = async name => {
  try {
    const result = await api("/api/images/prepull/stop", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast(result.message || "pre-pull stopped", "ok");
    resetPaint(); viewImages();
  } catch (e) { toast(e.message, "bad"); }
};

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  IMAGE_REASONS, IMAGE_KEEPER_PAGES, imageKeeper, viewImages, imageUnused, imageCleanupRow, vmImagesSection
};
