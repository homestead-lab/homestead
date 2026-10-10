/* A volume's files, as a file manager: two panes, each on a volume, with
   what a file manager does - folders, upload, download (a folder as a zip),
   rename, delete, permissions and owners - and copy or move to the other
   pane, between volumes too. On a phone the panes are two tabs.

   Each volume is read through a short-lived helper pod that mounts it
   (homestead_files.py). It goes a minute after it was last used: working
   here keeps it, and the next action after a pause starts it again. Closing
   stops every helper this opened and waits until each has gone. */

const FM = { panes: [], active: 0, open: false, volumes: [], lastSeen: 0, editing: null, busy: "" };
const FM_IDLE = 60;
const FM_CHUNK = 3 * 1024 * 1024;

function fmHeaders() {
  const headers = { "Content-Type": "application/json" };
  const cluster = window.FLEET?.target || window.FLEET?.view?.self;
  if (cluster) headers["X-Homestead-Cluster"] = cluster;
  return headers;
}
const fmPost = (path, body) => api(path, { method: "POST", headers: fmHeaders(), body: JSON.stringify(body) });
const fmGet = path => api(path, { keep: true, headers: fmHeaders() });
const fmQuery = (pane, extra = {}) => new URLSearchParams({ namespace: pane.namespace, pvc: pane.pvc, ...extra }).toString();
const fmJoin = (folder, name) => (folder ? `${folder}/${name}` : name);
const fmOther = () => FM.panes[1 - FM.active];

/* rwxr-xr-x from 755. */
function fmMode(octal) {
  const digits = String(octal || "").padStart(3, "0").slice(-3);
  return [...digits].map(d => ["r", "w", "x"].map((c, i) => (+d & (4 >> i) ? c : "-")).join("")).join("");
}
function fmWhen(seconds) {
  if (!seconds) return "";
  const d = new Date(seconds * 1000);
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: d.getFullYear() === new Date().getFullYear() ? undefined : "numeric" })
    + " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

window.fileManager = async (namespace, pvc, attached) => {
  if (window.filesDismiss) await window.filesDismiss();
  modal(`Files · ${pvc}`, `<div class="empty"><span class="spin2"></span>opening ${esc(pvc)}</div>`, true, "volume-files");
  if (attached && !(await ask(`${pvc} is attached to a running workload.\n\nA ReadWriteOnce volume mounts in one place, so its files can only be opened once the workload is stopped. Continue anyway?`))) {
    closeModal();
    return;
  }
  Object.assign(FM, { open: true, active: 0, editing: null, busy: "", lastSeen: Date.now(),
    panes: [{ namespace, pvc, path: "", listing: null, selected: new Set(), state: "starting" },
            { namespace, pvc, path: "", listing: null, selected: new Set(), state: "starting" }] });
  $("#mbody").innerHTML = `<div class="fm" id="fm">${fmShellHtml()}</div>`;
  fmBindActivity();
  api("/api/volumes", { keep: true }).then(rows => {
    FM.volumes = (rows || []).filter(v => v.pvc_name || v.name).map(v => ({ namespace: v.namespace || "lab", pvc: v.pvc_name || v.name, attached: v.state === "attached" }));
    fmPaint();
  }).catch(() => {});
  await Promise.all([fmLoad(0), fmLoad(1)]);
};

function fmShellHtml() {
  return `<div class="fm-tabs" role="tablist">${[0, 1].map(i => `<button type="button" role="tab" id="fm_tab${i}" onclick="fmFocus(${i})"></button>`).join("")}</div>
    <div class="fm-panes">${[0, 1].map(i => `<section class="fm-pane" id="fm_pane${i}" onpointerdown="fmFocus(${i}, true)"></section>`).join("")}</div>
    <div class="fm-panel" id="fm_panel" hidden></div>
    <div class="fm-actions" id="fm_actions"></div>
    <div class="fm-foot"><span class="dim xs" id="fm_status">Each volume is opened by a small helper that stops a minute after you stop using it; the next action starts it again.</span>
      <button class="btn" data-dialog-dismiss="true" onclick="fmClose()">Close</button></div>`;
}

async function fmLoad(index, path) {
  const pane = FM.panes[index];
  if (!pane) return;
  if (path !== undefined) pane.path = path;
  pane.state = pane.listing ? "loading" : "starting";
  fmPaintPane(index);
  try {
    const listing = await fmGet(`/api/files/entries?${fmQuery(pane, { path: pane.path })}`);
    if (!FM.open || FM.panes[index] !== pane) return;
    Object.assign(pane, { listing, path: listing.path || "", state: "ready", error: "" });
    pane.selected = new Set([...pane.selected].filter(name => listing.entries.some(e => e.name === name)));
  } catch (e) {
    if (!FM.open || FM.panes[index] !== pane) return;
    Object.assign(pane, { state: "error", error: e.message });
  }
  fmPaint();
}

function fmPaint() {
  if (!$("#fm")) return;
  [0, 1].forEach(fmPaintPane);
  fmPaintActions();
  if (window.applyRole) applyRole();
}

function fmVolumeOptions(pane) {
  const known = FM.volumes.some(v => v.namespace === pane.namespace && v.pvc === pane.pvc)
    ? FM.volumes : [{ namespace: pane.namespace, pvc: pane.pvc }, ...FM.volumes];
  return known.map(v => `<option value="${esc(v.namespace + "/" + v.pvc)}" ${v.namespace === pane.namespace && v.pvc === pane.pvc ? "selected" : ""}>${esc(v.pvc)}${v.namespace !== "lab" ? ` · ${esc(v.namespace)}` : ""}${v.attached ? " · in use" : ""}</option>`).join("");
}

function fmPaintPane(index) {
  const pane = FM.panes[index], host = $(`#fm_pane${index}`), tab = $(`#fm_tab${index}`);
  if (!pane || !host) return;
  host.classList.toggle("active", FM.active === index);
  if (tab) {
    tab.classList.toggle("on", FM.active === index);
    tab.setAttribute("aria-selected", String(FM.active === index));
    tab.textContent = `${index ? "Right" : "Left"} · ${pane.pvc}`;
  }
  const parts = (pane.path || "").split("/").filter(Boolean);
  const crumbs = [`<button type="button" class="linkish" onclick="fmLoad(${index}, '')">${esc(pane.pvc)}</button>`,
    ...parts.map((part, i) => `<span class="dim">/</span><button type="button" class="linkish" onclick="fmLoad(${index}, ${jsq(parts.slice(0, i + 1).join("/"))})">${esc(part)}</button>`)].join("");
  const entries = pane.listing?.entries || [];
  const all = entries.length && entries.every(e => pane.selected.has(e.name));
  const body = pane.state === "error"
    ? `<div class="fm-empty"><b>${esc(pane.error)}</b><br>${UI.button("Try again", `fmLoad(${index})`)}</div>`
    : !pane.listing
    ? `<div class="fm-empty"><span class="spin2"></span> opening ${esc(pane.pvc)}…</div>`
    : `<div class="fm-list" role="grid" aria-label="Files in ${esc(pane.pvc)}">
        <div class="fm-row fm-headrow"><label class="fm-check"><input type="checkbox" ${all ? "checked" : ""} aria-label="Select all" onchange="fmSelectAll(${index}, this.checked)"></label>
          <span>Name</span><span class="fm-size">Size</span><span class="fm-perm">Permissions</span><span class="fm-own">Owner</span><span class="fm-when">Modified</span></div>
        ${pane.path ? `<div class="fm-row fm-up" onclick="fmLoad(${index}, ${jsq(parts.slice(0, -1).join("/"))})"><span></span><span class="fm-name">↩ ..</span><span class="dim xs">up one level</span></div>` : ""}
        ${entries.map(e => `<div class="fm-row ${pane.selected.has(e.name) ? "on" : ""}" data-name="${esc(e.name)}"
            onclick="fmClick(event, ${index}, ${jsq(e.name)})" ondblclick="fmOpen(${index}, ${jsq(e.name)})">
          <label class="fm-check" onclick="event.stopPropagation()"><input type="checkbox" ${pane.selected.has(e.name) ? "checked" : ""} aria-label="Select ${esc(e.name)}" onchange="fmToggle(${index}, ${jsq(e.name)}, this.checked)"></label>
          <span class="fm-name"><span class="fm-icon">${e.kind === "dir" ? "▸" : e.kind === "link" ? "↪" : "·"}</span>${e.kind === "dir"
            ? `<button type="button" class="linkish" onclick="event.stopPropagation();fmOpen(${index}, ${jsq(e.name)})">${esc(e.name)}</button>` : `<span>${esc(e.name)}</span>`}</span>
          <span class="fm-size dim xs">${e.kind === "dir" ? "folder" : fileSize(e.size)}</span>
          <span class="fm-perm mono xs" title="${esc(e.mode)}">${fmMode(e.mode)}</span>
          <span class="fm-own xs">${esc(e.user)}:${esc(e.group)}</span>
          <span class="fm-when dim xs">${fmWhen(e.modified)}</span></div>`).join("") || '<div class="fm-empty dim">this folder is empty</div>'}
      </div>${pane.listing.truncated ? '<div class="dim xs">Only the first 500 entries are listed.</div>' : ""}`;
  host.innerHTML = `<div class="fm-panehead">
      <select aria-label="Volume in this pane" onchange="fmVolume(${index}, this.value)">${fmVolumeOptions(pane)}</select>
      <span class="fm-state ${pane.state}">${pane.state === "ready" ? "" : pane.state === "error" ? "" : pane.state === "stopped" ? "helper stopped · it starts again when you act" : "working…"}</span>
      <div class="fm-tools">
        <button type="button" class="iconbtn" title="New folder" aria-label="New folder" data-need="admin" onclick="fmNewFolder(${index})">${icon("plus")}</button>
        <label class="iconbtn" title="Upload files" aria-label="Upload files" data-need="admin">${icon("import")}<input type="file" multiple hidden onchange="fmUpload(${index}, this)"></label>
        <button type="button" class="iconbtn" title="Refresh" aria-label="Refresh this folder" onclick="fmLoad(${index})">${icon("refresh")}</button>
      </div></div>
    <div class="filecrumbs">${crumbs}</div>${body}`;
}

function fmSelected(index = FM.active) {
  const pane = FM.panes[index];
  return (pane.listing?.entries || []).filter(e => pane.selected.has(e.name));
}

function fmPaintActions() {
  const host = $("#fm_actions");
  if (!host) return;
  const pane = FM.panes[FM.active], picked = fmSelected(), other = fmOther();
  const one = picked.length === 1 ? picked[0] : null;
  const across = other && (other.namespace !== pane.namespace || other.pvc !== pane.pvc || other.path !== pane.path);
  const to = other ? `${other.pvc}/${other.path}`.replace(/\/$/, "") : "";
  host.innerHTML = picked.length ? `<span class="fm-count">${picked.length} selected</span>
      ${UI.button(picked.length === 1 && one.kind === "file" ? "Download" : "Download zip", "fmDownload()")}
      ${one && one.editable ? UI.button("Edit", `fmEdit(${FM.active}, ${jsArg(one.name)})`, { attrs: 'data-need="admin"' }) : ""}
      ${one ? UI.button("Rename", "fmRename()", { attrs: 'data-need="admin"' }) : ""}
      ${UI.button("Permissions", "fmPermissions()", { attrs: 'data-need="admin"' })}
      ${across ? UI.button(`Copy → ${to}`, "fmTransfer(false)", { attrs: 'data-need="admin"' }) : ""}
      ${across ? UI.button(`Move → ${to}`, "fmTransfer(true)", { attrs: 'data-need="admin"' }) : ""}
      ${UI.button("Delete", "fmDelete()", { kind: "danger", attrs: 'data-need="admin"' })}
      <button type="button" class="btn quiet" onclick="fmSelectAll(${FM.active}, false)">Clear</button>`
    : `<span class="dim xs">${FM.busy ? esc(FM.busy) : "Select files to download, change or copy them to the other pane. Double-click a folder to open it, a text file to edit it."}</span>`;
}

window.fmFocus = (index, quiet = false) => {
  if (FM.active === index) return;
  FM.active = index;
  [0, 1].forEach(i => { $(`#fm_pane${i}`)?.classList.toggle("active", i === index); $(`#fm_tab${i}`)?.classList.toggle("on", i === index); });
  if (!quiet) fmPaint(); else fmPaintActions();
};
window.fmVolume = (index, value) => {
  const [namespace, ...rest] = String(value).split("/");
  Object.assign(FM.panes[index], { namespace, pvc: rest.join("/"), path: "", listing: null, selected: new Set() });
  fmLoad(index);
};
window.fmToggle = (index, name, on) => {
  const pane = FM.panes[index];
  on ? pane.selected.add(name) : pane.selected.delete(name);
  fmFocus(index, true);
  fmPaintPane(index); fmPaintActions();
};
window.fmSelectAll = (index, on) => {
  const pane = FM.panes[index];
  pane.selected = new Set(on ? (pane.listing?.entries || []).map(e => e.name) : []);
  fmFocus(index, true);
  fmPaintPane(index); fmPaintActions();
};
window.fmClick = (event, index, name) => {
  const pane = FM.panes[index];
  if (event.ctrlKey || event.metaKey) return fmToggle(index, name, !pane.selected.has(name));
  pane.selected = new Set([name]);
  fmFocus(index, true);
  fmPaintPane(index); fmPaintActions();
};
window.fmOpen = (index, name) => {
  const pane = FM.panes[index], entry = (pane.listing?.entries || []).find(e => e.name === name);
  if (!entry) return;
  if (entry.kind === "dir") { pane.selected = new Set(); return fmLoad(index, fmJoin(pane.path, name)); }
  if (entry.editable) return fmEdit(index, name);
};

/* ---- the operations */
async function fmDo(label, work, refresh = [FM.active]) {
  FM.busy = label; fmPaintActions();
  try { await work(); }
  catch (e) { toast(e.message, "bad"); }
  finally {
    FM.busy = "";
    await Promise.all([...new Set(refresh)].map(i => fmLoad(i)));
  }
}
const fmTarget = () => { const pane = FM.panes[FM.active]; return { namespace: pane.namespace, pvc: pane.pvc }; };
const fmPaths = () => { const pane = FM.panes[FM.active]; return fmSelected().map(e => fmJoin(pane.path, e.name)); };
const fmSameVolume = (a, b) => a.namespace === b.namespace && a.pvc === b.pvc;
const fmBoth = () => [FM.active, ...(fmSameVolume(FM.panes[0], FM.panes[1]) ? [1 - FM.active] : [])];

window.fmNewFolder = async index => {
  fmFocus(index, true);
  const pane = FM.panes[index];
  fmPanel(`<div class="f"><label>New folder in ${esc(pane.pvc)}/${esc(pane.path)}</label><input id="fm_name" placeholder="Folder name" autocomplete="off"></div>
    ${UI.actions(`${UI.button("Create", "fmPanelGo()", { kind: "pri" })}${UI.button("Cancel", "fmPanel()")}`)}`, async () => {
    const name = $("#fm_name").value.trim();
    await fmPost("/api/files/folder", { ...fmTarget(), path: pane.path, name });
  });
};
window.fmRename = () => {
  const [entry] = fmSelected(), pane = FM.panes[FM.active];
  fmPanel(`<div class="f"><label>Rename ${esc(entry.name)}</label><input id="fm_name" value="${esc(entry.name)}" autocomplete="off"></div>
    ${UI.actions(`${UI.button("Rename", "fmPanelGo()", { kind: "pri" })}${UI.button("Cancel", "fmPanel()")}`)}`, async () => {
    await fmPost("/api/files/rename", { ...fmTarget(), path: fmJoin(pane.path, entry.name), name: $("#fm_name").value.trim() });
    pane.selected = new Set([$("#fm_name").value.trim()]);
  }, fmBoth());
  const input = $("#fm_name"); input?.setSelectionRange(0, entry.name.lastIndexOf(".") > 0 ? entry.name.lastIndexOf(".") : entry.name.length);
};
window.fmDelete = async () => {
  const picked = fmSelected();
  const what = picked.length === 1 ? picked[0].name : `${picked.length} entries`;
  const folders = picked.filter(e => e.kind === "dir").length;
  if (!(await ask(`Delete ${what}${folders ? `, with everything in ${folders === 1 ? "that folder" : "those folders"}` : ""}? This cannot be undone.`, { title: "Delete", ok: "Delete", danger: true }))) return;
  const paths = fmPaths();
  fmDo(`Deleting ${what}…`, async () => {
    await fmPost("/api/files/delete", { ...fmTarget(), paths });
    FM.panes[FM.active].selected = new Set();
  }, fmBoth());
};

/* Permissions: octal and the nine boxes kept in step, and an owner. */
window.fmPermissions = () => {
  const picked = fmSelected(), first = picked[0];
  const mode = String(first.mode || "644").padStart(3, "0").slice(-3);
  const boxes = ["Owner", "Group", "Others"].map((who, i) => `<div class="fm-permrow"><span>${who}</span>${["read", "write", "run"].map((what, j) =>
    `<label class="fm-bit"><input type="checkbox" data-bit="${i}${j}" ${+mode[i] & (4 >> j) ? "checked" : ""} onchange="fmModeFromBoxes()"> ${what}</label>`).join("")}</div>`).join("");
  const folders = picked.some(e => e.kind === "dir");
  fmPanel(`<div class="fm-perms">
      ${UI.section(`Permissions${picked.length > 1 ? ` · ${picked.length} entries` : ` · ${esc(first.name)}`}`, `${boxes}
        <div class="f"><label>As a number</label><input id="fm_mode" class="mono" value="${mode}" inputmode="numeric" maxlength="4" oninput="fmBoxesFromMode()"></div>`)}
      ${UI.section("Owner", `<div class="f2"><div class="f"><label>User (UID)</label><input id="fm_uid" type="number" min="0" value="${first.uid ?? ""}"></div>
          <div class="f"><label>Group (GID)</label><input id="fm_gid" type="number" min="0" value="${first.gid ?? ""}"></div></div>
        <div class="dim xs">Now ${esc(first.user)}:${esc(first.group)}. Containers usually run as a numbered user; Fix ownership on the volume finds the one its workload uses.</div>`)}</div>
    ${folders ? `<label class="switch"><input type="checkbox" id="fm_recursive"> <span>Also everything inside the folders</span></label>` : ""}
    ${UI.actions(`${UI.button("Apply", "fmPanelGo()", { kind: "pri" })}${UI.button("Cancel", "fmPanel()")}`)}`, async () => {
    const recursive = !!$("#fm_recursive")?.checked, paths = fmPaths();
    const newMode = $("#fm_mode").value.trim(), uid = $("#fm_uid").value.trim(), gid = $("#fm_gid").value.trim();
    if (newMode !== mode || recursive) await fmPost("/api/files/mode", { ...fmTarget(), paths, mode: newMode, recursive });
    if (uid !== String(first.uid ?? "") || gid !== String(first.gid ?? "") || (recursive && uid)) {
      await fmPost("/api/files/owner", { ...fmTarget(), paths, uid, gid, recursive });
    }
  }, fmBoth());
};
window.fmModeFromBoxes = () => {
  const digits = [0, 1, 2].map(i => [0, 1, 2].reduce((sum, j) => sum + ($(`#fm_panel [data-bit="${i}${j}"]`)?.checked ? 4 >> j : 0), 0));
  const input = $("#fm_mode"), special = input.value.length === 4 ? input.value[0] : "";
  input.value = special + digits.join("");
};
window.fmBoxesFromMode = () => {
  const value = $("#fm_mode").value.trim();
  if (!/^[0-7]{3,4}$/.test(value)) return;
  const mode = value.slice(-3);
  [0, 1, 2].forEach(i => [0, 1, 2].forEach(j => { const box = $(`#fm_panel [data-bit="${i}${j}"]`); if (box) box.checked = !!(+mode[i] & (4 >> j)); }));
};

/* A small form under the panes, for the operations that ask something. */
let FM_PANEL = null;
window.fmPanel = (html, go, refresh) => {
  const host = $("#fm_panel");
  if (!host) return;
  FM_PANEL = html ? { go, refresh } : null;
  host.hidden = !html;
  host.innerHTML = html || "";
  host.querySelector("input")?.focus();
  host.onkeydown = event => { if (event.key === "Enter" && event.target.tagName === "INPUT") { event.preventDefault(); fmPanelGo(); } };
};
window.fmPanelGo = () => {
  const panel = FM_PANEL;
  if (!panel) return;
  fmDo("Working…", async () => { await panel.go(); fmPanel(); }, panel.refresh || [FM.active]);
};

/* Download: a file as it is, a folder or several as a zip, through the
   browser's own download so a large one never sits in this page. */
window.fmDownload = () => {
  const pane = FM.panes[FM.active], query = new URLSearchParams({ namespace: pane.namespace, pvc: pane.pvc });
  fmPaths().forEach(path => query.append("path", path));
  const cluster = window.FLEET?.target || window.FLEET?.view?.self;
  if (cluster) query.set("hs_cluster", cluster);
  const link = Object.assign(document.createElement("a"), { href: `/api/files/download?${query}`, download: "" });
  document.body.appendChild(link); link.click(); link.remove();
  toast("Download started; a folder is zipped as it downloads", "ok");
};

/* Upload: each file in pieces, in order; it appears only once whole. */
window.fmUpload = async (index, input) => {
  const files = [...(input.files || [])];
  input.value = "";
  if (!files.length) return;
  fmFocus(index, true);
  const pane = FM.panes[index], target = { namespace: pane.namespace, pvc: pane.pvc }, folder = pane.path;
  const taken = new Set((pane.listing?.entries || []).map(e => e.name));
  const clashes = files.filter(f => taken.has(f.name)).map(f => f.name);
  if (clashes.length && !(await ask(`${clashes.join(", ")} ${clashes.length === 1 ? "is" : "are"} already here. Replace ${clashes.length === 1 ? "it" : "them"}?`, { title: "Replace files", ok: "Replace" }))) return;
  const total = files.reduce((sum, f) => sum + f.size, 0);
  let sent = 0;
  fmDo("Uploading…", async () => {
    for (const file of files) {
      let token = "", offset = 0;
      do {
        const piece = file.slice(offset, offset + FM_CHUNK);
        const data = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
          reader.onerror = () => reject(new Error(`${file.name} could not be read`));
          reader.readAsDataURL(piece);
        });
        const final = offset + piece.size >= file.size;
        const answer = await fmPost("/api/files/upload", { ...target, path: folder, name: file.name, data, offset, final, token });
        token = answer.token; offset = answer.received; sent += piece.size;
        FM.busy = `Uploading ${file.name} · ${Math.round(sent * 100 / Math.max(1, total))}%`; fmPaintActions();
        fmTouch(true);
      } while (offset < file.size);
    }
    toast(`Uploaded ${files.length === 1 ? files[0].name : files.length + " files"}`, "ok");
  }, [index, ...(fmSameVolume(FM.panes[0], FM.panes[1]) ? [1 - index] : [])]);
};

/* Copy or move to the other pane's folder, between volumes too. */
window.fmTransfer = async move => {
  const from = { ...fmTarget(), path: FM.panes[FM.active].path }, other = fmOther();
  const to = { namespace: other.namespace, pvc: other.pvc, path: other.path };
  const paths = fmPaths(), what = paths.length === 1 ? paths[0].split("/").pop() : `${paths.length} entries`;
  if (move && !(await ask(`Move ${what} to ${to.pvc}/${to.path}? ${fmSameVolume(from, to) ? "" : "It is copied there first; the originals are removed only once the copy is complete."}`, { title: "Move", ok: "Move" }))) return;
  fmDo(`${move ? "Moving" : "Copying"} ${what}…`, async () => {
    const answer = await fmPost("/api/files/transfer", { from, to, paths, move });
    if (answer.done) return;
    let job = answer.job;
    while (job.status === "running") {
      await new Promise(resolve => setTimeout(resolve, 1500));
      if (!FM.open) return;
      job = await fmGet(`/api/files/transfer?id=${encodeURIComponent(job.id)}`);
      FM.busy = `${move ? "Moving" : "Copying"} ${what} to ${to.pvc} · ${fileSize(job.bytes)}`; fmPaintActions();
      fmTouch(true);
    }
    if (job.status === "failed") throw new Error(job.error || "the copy did not finish");
    toast(`${move ? "Moved" : "Copied"} ${what} to ${to.pvc}`, "ok");
    FM.panes[FM.active].selected = new Set();
  }, [0, 1]);
};

/* Edit a text file in place, in the manager; Back returns to the panes. */
window.fmEdit = async (index, name) => {
  const pane = FM.panes[index], path = fmJoin(pane.path, name);
  let file;
  try { file = await fmGet(`/api/files/read?${fmQuery(pane, { path })}`); }
  catch (e) { return toast(e.message, "bad"); }
  FM.editing = { index, path, revision: file.revision, dirty: false, editor: null };
  $("#fm").innerHTML = `<div class="filecrumbs"><b>${esc(pane.pvc)}/${esc(path)}</b></div>
    <div class="between fileeditbar"><span class="dim xs">${fileSize(file.size)} · saving keeps the previous contents as <span class="mono">${esc(name)}.homestead-bak</span></span><span class="dim xs" id="fm_editstate"></span></div>
    <div id="fm_editor" class="fileeditor"></div>
    ${UI.actions(`<button class="btn pri" id="fm_save" data-need="admin" onclick="fmSave()">Save</button><button class="btn" onclick="fmEditDone()">Back to files</button>`)}`;
  try {
    FM.editing.editor = await mountEditor($("#fm_editor"), file.content, path);
    FM.editing.editor.onDidChangeModelContent(() => { FM.editing.dirty = true; const s = $("#fm_editstate"); if (s) s.textContent = "unsaved changes"; });
  } catch (_) {
    $("#fm_editor").innerHTML = `<textarea id="fm_plain" class="mono fileeditor-plain" spellcheck="false"></textarea>`;
    $("#fm_plain").value = file.content;
    $("#fm_plain").oninput = () => { FM.editing.dirty = true; };
  }
  if (window.applyRole) applyRole();
};
window.fmSave = async (ignoreSyntax = false) => {
  const editing = FM.editing, pane = FM.panes[editing.index];
  const content = editing.editor ? editing.editor.getValue() : $("#fm_plain").value;
  try {
    const result = await fmPost("/api/files/write", { namespace: pane.namespace, pvc: pane.pvc, path: editing.path, content, revision: editing.revision, ignore_syntax: ignoreSyntax });
    Object.assign(editing, { revision: result.revision, dirty: false });
    const state = $("#fm_editstate"); if (state) state.textContent = `saved ${new Date().toLocaleTimeString()}`;
    toast(result.message || "saved", "ok");
  } catch (e) {
    if (/^(JSON is invalid|YAML cannot)/.test(e.message) && (await ask(`${e.message}\n\nSave it anyway?`))) return fmSave(true);
    toast(e.message, "bad");
  }
};
window.fmEditDone = async () => {
  if (FM.editing?.dirty && !(await ask("Discard unsaved changes?"))) return;
  try { FM.editing?.editor?.getModel()?.dispose(); FM.editing?.editor?.dispose(); } catch (_) { /* already gone */ }
  const index = FM.editing?.index ?? 0;
  FM.editing = null;
  $("#fm").innerHTML = fmShellHtml();
  fmLoad(index);
  fmPaint();
};

/* Working here keeps the helpers; a minute without it lets them go. */
function fmTouch(force = false) {
  if (!FM.open) return;
  const now = Date.now();
  if (!force && now - FM.lastSeen < 15000) return;
  FM.lastSeen = now;
  const seen = new Set();
  FM.panes.forEach((pane, index) => {
    const key = `${pane.namespace}/${pane.pvc}`;
    if (seen.has(key) || pane.state === "error") return;
    seen.add(key);
    fmPost("/api/files/keepalive", { namespace: pane.namespace, pvc: pane.pvc }).then(answer => {
      if (!answer.running && pane.state === "ready") { pane.state = "stopped"; fmPaintPane(index); }
    }).catch(() => {});
  });
}
function fmBindActivity() {
  const host = $("#fm");
  if (!host || host.dataset.bound) return;
  host.dataset.bound = "1";
  ["pointerdown", "keydown", "wheel", "scroll", "touchstart"].forEach(kind => host.addEventListener(kind, () => fmTouch(), { passive: true, capture: true }));
}

/* Closing stops every helper this opened, and says when they have gone. */
async function fmRelease(keepalive = false) {
  if (!FM.open) return;
  FM.open = false;
  const seen = new Map();
  FM.panes.forEach(pane => seen.set(`${pane.namespace}/${pane.pvc}`, pane));
  const results = await Promise.all([...seen.values()].map(pane =>
    api("/api/files/close", { method: "POST", headers: fmHeaders(), keepalive, body: JSON.stringify({ namespace: pane.namespace, pvc: pane.pvc }) })
      .then(answer => answer.stopped).catch(() => false)));
  if (!keepalive) {
    const left = results.filter(stopped => !stopped).length;
    toast(left ? `Files closed; ${left === 1 ? "a helper is" : `${left} helpers are`} still stopping and will be gone within a minute` : "Files closed; the volume is free for its workload", left ? "warn" : "ok");
  }
}
window.fmClose = async () => {
  if (FM.editing?.dirty && !(await ask("Discard unsaved changes?"))) return;
  const closing = fmRelease();
  closeModal();
  await closing;
};
// Every way out of the dialog - the ×, Escape, another page - closes it too.
const fmLegacyDismiss = window.filesDismiss;
window.filesDismiss = (keepalive = false) => Promise.all([fmRelease(keepalive), fmLegacyDismiss ? fmLegacyDismiss(keepalive) : null]);
window.addEventListener?.("pagehide", () => { if (FM.open) fmRelease(true); });
