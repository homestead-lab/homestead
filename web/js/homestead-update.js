/* Homestead's own updates: its release, and the helpers it runs beside it
   (the SMB and NFS servers, the object store moves use). They are not apps -
   the Containers page hides them with the platform - so they are updated
   from here: a button on the top bar that appears when one is waiting, the
   dialog it opens, and a card under Settings › About. The update itself is
   the same reviewed rollout an app gets (reviewImageActions), Homestead last. */
const HOMESTEAD_PARTS = { self: "Homestead", smb: "SMB server", nfs: "NFS server", objectstore: "Object store" };
const HOMESTEAD_RELEASES = "https://github.com/wjcloudy/homestead/releases";

function homesteadUpdates() {
  const report = STATE.data.imageUpdates;
  const parts = HomesteadUpdateState.homesteadWorkloads(report);
  const self = parts.find(w => w.homestead === "self");
  const release = (self?.images || []).find(i => i.available && i.candidate_tag)?.candidate_tag || "";
  return { report, parts, self, release, waiting: parts.filter(w => w.available),
    failed: parts.filter(w => (w.images || []).some(i => i.error)) };
}

function paintHomesteadNotice() {
  const notice = $("#homesteadNotice");
  if (notice) {
    const { waiting, release } = homesteadUpdates();
    notice.classList.toggle("hidden", !waiting.length);
    const words = release ? `Homestead ${release} is available`
      : `${waiting.length} Homestead helper update${waiting.length === 1 ? "" : "s"}`;
    notice.title = words;
    notice.setAttribute("aria-label", words);
    const label = $("#homesteadNoticeText");
    if (label) label.textContent = release ? `v${release}` : "Helpers";
  }
  homesteadUpdateCardPaint();
}
window.paintHomesteadNotice = paintHomesteadNotice;

/* One line per part: what it runs and what it would move to. */
function homesteadPartRows(parts) {
  return parts.map(w => {
    const running = (w.images || []).map(i => imageVersion(i.source || i.deployed).tag).filter(Boolean).join(", ");
    const failure = (w.images || []).find(i => i.error)?.error;
    const state = w.available ? `<span class="pill warn">${esc(updateVersions(w) || "update available")}</span>`
      : failure ? `<span class="pill crit" data-tip="${esc(failure)}">check failed</span>`
      : w.unchecked ? '<span class="pill neutral">not checked yet</span>' : '<span class="pill ok">current</span>';
    return `<div class="hs-part"><div><b>${esc(HOMESTEAD_PARTS[w.homestead] || w.name)}</b>
        <span class="dim xs mono">${esc(w.name)}${running ? ` · ${esc(running)}` : ""}</span></div>${state}</div>`;
  }).join("");
}

function homesteadUpdateBody(inCard = false) {
  const { report, parts, release, waiting, failed } = homesteadUpdates();
  if (!report) return '<div class="empty small"><span class="spin2"></span> Asking the registries…</div>';
  const checked = checkedAgo();
  const head = release
    ? `<div class="hs-release"><span class="dim xs">NEW RELEASE</span><b>Homestead ${esc(release)}</b>
        <span class="dim small">You run v${esc(HOMESTEAD_VERSION)} · <a href="${safeHref(`${HOMESTEAD_RELEASES}/tag/v${release}`)}" target="_blank" rel="noopener">what's new ${icon("ext")}</a></span></div>`
    : `<div class="hs-release current"><span class="dim xs">RELEASE</span><b>Homestead v${esc(HOMESTEAD_VERSION)}</b>
        <span class="dim small">${waiting.length ? "Current; a helper has an update" : "Up to date"}${checked ? ` · ${esc(checked)}` : ""} · <a href="${safeHref(HOMESTEAD_RELEASES)}" target="_blank" rel="noopener">releases ${icon("ext")}</a></span></div>`;
  return `${head}
    ${parts.length ? `<div class="hs-parts">${homesteadPartRows(parts)}</div>` : ""}
    ${failed.length ? '<p class="dim small">A part whose check failed is compared with its registry again on the next check.</p>' : ""}
    ${waiting.length ? `<p class="dim small">${inCard ? "" : "Each part restarts while it changes, Homestead last; this page reconnects when it is back. "}Nothing changes until you review and accept it.</p>` : ""}
    <div class="row hs-actions">${waiting.length ? `<button class="btn pri" data-need="operator" onclick="homesteadUpdateReview()">${release ? `Update to ${esc(release)}` : `Update ${waiting.length === 1 ? "helper" : "helpers"}`}</button>` : ""}
      <button class="btn" onclick="homesteadUpdateCheck(this)">↻ Check now</button></div>`;
}

window.homesteadUpdateDialog = async () => {
  modal("Homestead updates", `<div class="ui-stack hs-update" id="hsUpdateBody">${homesteadUpdateBody()}</div>`);
  if (!STATE.data.imageUpdates) {
    await loadImageUpdates(false, true);
    const body = $("#hsUpdateBody");
    if (body) body.innerHTML = homesteadUpdateBody();
  }
  if (window.applyRole) applyRole();
};

window.homesteadUpdateReview = () => {
  const { waiting } = homesteadUpdates();
  if (!waiting.length) return toast("Homestead is up to date", "ok");
  return reviewImageActions(waiting.map(w => ({ ns: w.ns, name: w.name })));
};

window.homesteadUpdateCheck = async button => {
  if (button) { button.disabled = true; button.textContent = "Checking…"; }
  const report = await loadImageUpdates(true, true);
  if (report) {
    const { release, waiting } = homesteadUpdates();
    toast(release ? `Homestead ${release} is available` : waiting.length ? "A Homestead helper has an update" : "Homestead is up to date", "ok");
  } else if (button) { button.disabled = false; button.textContent = "↻ Check now"; }
  const body = $("#hsUpdateBody");
  if (body) { body.innerHTML = homesteadUpdateBody(); if (window.applyRole) applyRole(); }
  homesteadUpdateCardPaint();
};

/* Settings › About: always there, current or not. */
function homesteadUpdateCardPaint() {
  const card = $("#homesteadUpdateCard");
  if (!card) return;
  card.innerHTML = `<div class="ctitle">Homestead updates</div>
    <div class="csub">Homestead's release and the helpers it runs are updated here, not with your apps
      ${tip("The SMB and NFS servers and the object store moves use are part of Homestead: hidden with the platform on the Containers page, and updated from here. How updates are approved is under Settings › Updates.")}</div>
    <div class="ui-stack">${homesteadUpdateBody(true)}</div>`;
  if (window.applyRole) applyRole();
}
window.homesteadUpdateCardPaint = homesteadUpdateCardPaint;

/* Where each part of Homestead is looked after, from its row on Containers. */
window.homesteadPartManage = part => {
  if (part === "objectstore") { settingsTab("fleet"); return go("settings"); }
  if (part === "smb" || part === "nfs") return go("shares");
  settingsTab("about");
  go("settings");
};

const homesteadNoticeButton = $("#homesteadNotice");
if (homesteadNoticeButton) {
  homesteadNoticeButton.onclick = () => homesteadUpdateDialog();
  homesteadNoticeButton.onkeydown = event => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); homesteadUpdateDialog(); }
  };
}
