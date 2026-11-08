/* Apps that git applies: Flux or Argo CD, or an administrator's mark that
   something else does (#734). A change made here lasts until that tool next
   applies the app, so a review that changes one says so first. managed is
   what the server's reviews answer with: {} or {by, source, detected}. */
function gitopsNote(managed, { rename = false, yaml = "" } = {}) {
  if (!managed?.by) return "";
  const by = esc(managed.by), where = managed.source ? ` (${esc(managed.source)})` : "";
  const body = managed.detected
    ? `${by} applies this app${where}. This change lasts until ${by} next applies it. To keep it, change it in git.`
    : `Marked as updated by ${by}. A change made here can be undone there. To keep it, change it there too.`;
  const tick = rename ? `<label class="switch"><input type="checkbox" id="editManagedConfirm"> ${by} will bring back the old name</label>` : "";
  return UI.callout("warn", rename ? `${managed.detected ? by : "Something else"} applies this app` : "", `<p>${body}</p>${rename
    ? `<p>A rename creates the app with its new name and removes the old one. ${by} then restores the old one, so there are two copies.</p>${tick}` : ""}${
    gitopsCopyButton(yaml)}`);
}

/* A change made here, as YAML to commit where git keeps the app (#735): the
   server's review sends only the fields that changed, under the app's kind,
   name and namespace. Deploy's review (#920) sends a new app whole, or the
   patch that adds a container. The text is kept here, not in the onclick. */
const GITOPS_COPIES = [];
function gitopsCopyButton(yaml, whole = false) {
  if (!yaml) return "";
  return UI.button("Copy as YAML for git", `gitopsCopy(${GITOPS_COPIES.push({ yaml, whole }) - 1})`, { kind: "sm" });
}
window.gitopsCopy = async index => {
  const copy = GITOPS_COPIES[index] || {};
  const done = await copyText(copy.yaml || "");
  toast(!done ? "Copying did not work here: select the YAML and copy it"
    : copy.whole ? "Copied. Commit it as a file of its own, where the apps are kept in git."
    : "Copied. Paste it where git keeps this app: a kustomize patch, or the same fields in its manifest", done ? "ok" : "bad");
};
/* Save text as a file in the browser: a link with download, as the config
   backup and the address list do. */
function downloadText(name, text, type = "text/plain") {
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([text], { type }));
  link.download = name;
  document.body.appendChild(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 5000);
}
const GITOPS_DOWNLOADS = [];
window.gitopsDownload = index => {
  const file = GITOPS_DOWNLOADS[index];
  if (file) downloadText(file.name, file.yaml, "application/yaml");
};
/* scope: "edit", the fields an edit sets; "app", a new app whole; "added",
   the patch that adds a container to an app. file names the download. */
const GITOPS_SCOPES = {
  edit: "Only the fields this change sets. Services and volumes are not in it.",
  app: "The whole app in one file: its Deployment, Service and new volume claims, without server fields or Homestead's bookkeeping.",
  added: "Only what adding this container changes in the app, as a patch, and any volume claim it creates.",
};
/* What Deploy adds when it runs that the file cannot hold (the server's
   deploy_adds: [{what, certain}]), named only when it applies; "" for none. */
function gitopsDeployAdds(adds = []) {
  const sure = adds.filter(a => a.certain).map(a => a.what), may = adds.filter(a => !a.certain).map(a => a.what);
  if (!sure.length && !may.length) return "";
  const lead = sure.length ? `Homestead also adds at deploy time: ${sure.join("; ")}${may.length ? `, and may add ${may.join("; ")}` : ""}.`
    : `At deploy time Homestead may add ${may.join("; ")}.`;
  return `<p class="ui-help">${esc(`${lead} Applying this file elsewhere does not add ${sure.length + may.length > 1 ? "them" : "it"}.`)}</p>`;
}
function gitopsYamlBlock(yaml, secret = {}, { scope = "edit", file = "", adds = [] } = {}) {
  if (!yaml) return "";
  const saved = scope === "edit" ? "saved" : "deployed", save = scope === "edit" ? "save" : "deploy";
  // The app's own Secret exists only once Homestead has saved the app, never in git.
  const needs = secret?.name ? `<p class="ui-help">It refers to Secret <span class="mono">${esc(secret.name)}</span> (keys ${
    (secret.keys || []).map(key => `<span class="mono">${esc(key)}</span>`).join(", ")}), not to the values. That Secret exists only once
    Homestead has ${saved} the app. In git, create it first (for example with SOPS), or ${save} here first.</p>` : "";
  const download = file ? UI.button(`Download ${file}`, `gitopsDownload(${GITOPS_DOWNLOADS.push({ name: file, yaml }) - 1})`, { kind: "sm" }) : "";
  return UI.more("YAML for git", `<p class="ui-help">${GITOPS_SCOPES[scope] || GITOPS_SCOPES.edit}
    Values Homestead keeps in the app's Secret, and values with a password or token in them, are left out; still check it
    before committing it to a public repository.</p>${needs}
    <pre class="mono small agent-raw">${esc(yaml)}</pre>${gitopsDeployAdds(adds)}${gitopsCopyButton(yaml, scope === "app")}${download}`);
}
window.downloadText = downloadText;
window.gitopsCopyButton = gitopsCopyButton;
window.gitopsYamlBlock = gitopsYamlBlock;
window.gitopsNote = gitopsNote;

/* Is Flux applying it? (#742) /api/gitops, read once a minute on the server:
   each Kustomization and HelmRelease, and the apps each applies. */
async function gitopsLoad() {
  STATE.data.gitops = await api("/api/gitops").catch(error => ({ error: error.message }));
  return STATE.data.gitops;
}
const gitopsAgo = iso => { const at = Date.parse(iso || ""); return at ? fmtAgo(Math.max(1, Math.round((Date.now() - at) / 1000))) : ""; };
function gitopsState(row) {
  return row.suspended ? "Suspended" : row.ready === true ? "Ready" : row.ready === false ? "Not ready" : "Applying";
}
/* An app's details: what applies it and how that is going. "" for an app
   Flux does not apply, or before the read has come back. */
function gitopsDetail(w) {
  const report = STATE.data.gitops;
  if (report?.error) {
    // Unread is not fine: an app the update check says Flux applies says so.
    const owner = (STATE.data.imageUpdateMap || {})[`${w.ns}/${w.name}`]?.managed;
    return owner?.by === "Flux" && !window.remoteRow?.(w) ? `<div class="wl-detail-gitops"><span>Flux</span><b>${esc(owner.source || "Flux")}</b>
      <div class="updateerror">Whether Flux is applying it could not be read: ${esc(report.error)}</div></div>` : "";
  }
  const row = gitopsRow(w);
  if (!row) return "";
  const bits = [gitopsState(row), row.revision, row.tried && `tried ${gitopsAgo(row.tried)}`,
    gitopsWaiting(row) && `Reconcile asked ${gitopsAgo(row.requested)}, waiting for Flux`].filter(Boolean).map(esc).join(" · ");
  return `<div class="wl-detail-gitops"><span>Flux</span><b>${esc(row.id)}</b><div class="dim xs">${bits}</div>${
    row.ready === false && !row.suspended ? `<div class="updateerror">${esc(row.message || "Flux says it is not ready")}</div>` : ""}</div>`;
}
/* The Kustomization or HelmRelease that applies this app, from the last read. */
function gitopsRow(w) {
  const report = STATE.data.gitops, id = report?.apps?.[`${w.ns}/${w.name}`];
  if (!id || report.error || window.remoteRow?.(w)) return null;
  return (report.objects || []).find(r => r.id === id) || null;
}
const gitopsWaiting = row => !!row.requested && row.requested !== row.handled;

/* Reconcile now (#742): an admin asks Flux to apply the app again, as
   `flux reconcile` does (without --with-source: Flux applies the revision it
   last fetched), then follows the shared read until Flux has taken the
   request (the server reads every 5 s for two minutes after it). */
const GITOPS_FOLLOW = { wait: ms => new Promise(done => setTimeout(done, ms)), every: 5000, tries: 30 };
// The latest request per app: an older follow stops once a newer one starts.
const GITOPS_FOLLOWING = {};
// Taken: Flux handled this request, or a later one (another Reconcile now, flux reconcile).
const gitopsTaken = (row, requested) => !!row.handled
  && (row.handled === requested || Date.parse(row.handled) >= Date.parse(requested));
function gitopsReconcileItem(w) {
  const row = gitopsRow(w);
  if (!row || row.suspended) return null;
  return { label: "Reconcile now", run: `gitopsReconcile(${jsq(w.ns)},${jsq(w.name)})`, icon: "refresh", ariaLabel: `Reconcile now ${w.name}`,
    need: "admin", tip: `Ask Flux to apply ${row.id} again now` };
}
/* How it is going, and Flux's message when it is not Ready: the review and its result. */
function gitopsFacts(row, apps = []) {
  return UI.facts([["Applied by", esc(row.id)], ["State", esc([gitopsState(row), row.revision].filter(Boolean).join(" · "))],
    apps.length && ["Apps", String(apps.length)]])
    + (row.ready === false && !row.suspended ? UI.callout("warn", "Flux says", `<p>${esc(row.message || "not ready")}</p>`) : "")
    + (apps.length ? UI.more(`Apps it applies · ${apps.length}`, `<p>${apps.map(esc).join(", ")}</p>`) : "");
}
// Marks the dialog as this app's Reconcile now, so a follow paints only its own dialog.
const gitopsMark = key => `<span hidden data-gitops-follow="${esc(key)}"></span>`;
function gitopsReconcile(ns, name) {
  const row = gitopsRow({ ns, name });
  if (!row) return toast(`Flux does not apply ${ns}/${name}`, "bad");
  const apps = Object.entries(STATE.data.gitops.apps || {}).filter(([, id]) => id === row.id).map(([key]) => key);
  modal(`Reconcile now · ${name}`, gitopsMark(`${ns}/${name}`)
    + UI.lead(esc(`Flux applies ${row.id} again now, from the revision it last fetched from git.`))
    + gitopsFacts(row, apps)
    + UI.actions(UI.cancel() + UI.button("Reconcile now", `gitopsReconcileGo(this,${jsArg(ns)},${jsArg(name)})`, { kind: "pri" })));
}
async function gitopsReconcileGo(button, ns, name) {
  const key = `${ns}/${name}`, title = `Reconcile now · ${name}`, close = UI.actions(UI.cancel("Close"));
  const mine = GITOPS_FOLLOWING[key] = (GITOPS_FOLLOWING[key] || 0) + 1;
  const current = () => GITOPS_FOLLOWING[key] === mine;
  // The dialog follows the request while it is open; closed, the result is a message.
  const paint = html => current() && modalIsOpen() && $("#mbody")?.querySelector(`[data-gitops-follow="${key}"]`)
    ? (modal(title, gitopsMark(key) + html), true) : false;
  const say = (html, text, tone) => { if (current() && !paint(html + close)) toast(text, tone); };
  button.disabled = true;
  let asked;
  try {
    asked = await api("/api/gitops/reconcile", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ns, name }) });
  } catch (error) { button.disabled = false; return toast(error.message, "bad"); }
  paint(UI.lead(esc(`Asked Flux to apply ${asked.id}.`)) + UI.progress(null, { label: "Waiting for Flux to take the request" }) + close);
  let unread = "";
  for (let tries = 0; tries < GITOPS_FOLLOW.tries && current(); tries++) {
    const report = await gitopsLoad();
    if (!current()) return;
    unread = report?.error || "";
    const now = !unread && (report.objects || []).find(r => r.id === asked.id);
    if (unread) {
      paint(UI.lead(esc(`Asked Flux to apply ${asked.id}.`)) + UI.callout("warn", "", `<p>Flux's status could not be read: ${esc(unread)}</p>`)
        + UI.progress(null, { label: "Trying again" }) + close);
    } else if (now && gitopsTaken(now, asked.requested) && now.ready !== null && now.ready !== undefined) {
      if (STATE.view === "workloads" && !modalIsOpen()) window.renderWorkloads?.();
      const failed = now.ready === false, said = failed ? `Flux tried ${now.id}: it is not ready` : `Flux applied ${now.id}`;
      return say(UI.lead(esc(`${said}.`)) + gitopsFacts(now),
        failed ? `${said}: ${now.message || "no message"}` : `${said}${now.revision ? `: ${now.revision}` : ""}`, failed ? "bad" : "ok");
    }
    await GITOPS_FOLLOW.wait(GITOPS_FOLLOW.every);
  }
  if (!current()) return;
  const late = unread ? `Flux's status could not be read: ${unread}. Whether Flux took the request is not known.`
    : `Flux has not taken the request yet. ${name}'s details show when it does.`;
  say(UI.lead(esc(late)), late, "warn");
}

window.GITOPS_FOLLOW = GITOPS_FOLLOW;
window.gitopsReconcileItem = gitopsReconcileItem;
window.gitopsReconcile = gitopsReconcile;
window.gitopsReconcileGo = gitopsReconcileGo;
/* The Cluster page: one GitOps line, and what is not Ready with Flux's message. */
async function clusterGitopsPaint() {
  const report = await gitopsLoad(), box = $("#clusterGitops");
  if (!box) return;
  if (report?.error) {
    box.innerHTML = UI.callout("warn", "GitOps", `<p>Flux's Kustomizations and HelmReleases could not be read: ${esc(report.error)}</p>`);
    return;
  }
  if (!(report?.objects || []).length) { box.innerHTML = ""; return; }
  const failing = report.objects.filter(row => row.ready === false && !row.suspended);
  box.innerHTML = UI.callout(failing.length ? "warn" : "info", `GitOps: ${report.summary}`, failing.length
    ? `<ul class="ui-list">${failing.map(row => `<li><b>${esc(row.id)}</b> · not ready${row.since ? ` (${esc(gitopsAgo(row.since))})` : ""}: ${esc(row.message || "no message")}</li>`).join("")}</ul>` : "");
}
window.gitopsLoad = gitopsLoad;
window.gitopsDetail = gitopsDetail;
window.clusterGitopsPaint = clusterGitopsPaint;
