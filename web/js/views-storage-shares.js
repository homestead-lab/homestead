/* Homestead views: the Shares page. Split from views-storage.js (#911); a classic script. */
/* ---------------- shares ---------------- */
async function viewShares() {
  let sh;
  try { sh = await api("/api/shares"); }
  catch (e) {
    paint(`${UI.pageHeader(`Network shares`, ``, ``)}
      ${UI.callout("bad", "", `The shares could not be loaded. ${esc(e.message)} <button class="btn sm" onclick="viewShares()">Try again</button>`)}`);
    return;
  }
  STATE.data.shares = sh;
  const smb = await api("/api/shares/server").catch(error => ({ error: error.message }));
  const nfs = await api("/api/shares/nfs/server").catch(error => ({ error: error.message }));
  STATE.data.samba = smb;
  STATE.data.nfs = nfs;
  STATE.data.sambaInstalled = !!smb.installed;
  // SMB users belong on the SMB server's card, with their count (#370).
  // Unread is not "no SMB users yet" (#620): the card says so.
  const smbUsersRead = can("admin") ? await api("/api/shares/users").catch(e => ({ unread: e.message })) : null;
  const smbUserList = Array.isArray(smbUsersRead) ? smbUsersRead : null;
  const smbUsersUnread = smbUsersRead?.unread || "";
  if (smbUserList) STATE.data.smbUsers = smbUserList;
  const ip = smb.address || "address not assigned yet";
  paint(`${UI.pageHeader(`Network shares`, `SMB shares, and optional NFSv4 exports, stored on Longhorn volumes`, `<button class="btn pri" data-need="admin" onclick="newShare()">＋ New share</button>`)}
  <div class="card" style="margin-bottom:14px"><div class="between"><div><div class="ctitle">SMB server · ${esc(smb.name || "homestead-smb")}</div>
    <div class="dim small">${smb.error ? `Status unavailable. ${esc(smb.error)}` : !smb.installed ? "Not installed · Creating the first share installs it" :
      `${smb.enabled ? `${smb.ready || 0}/${smb.desired || 1} ready` : "Stopped"}${smb.address ? ` · \\\\${esc(smb.address)}` : " · waiting for an address"} · ${smb.served_shares?.length ?? 0} of ${sh.length} shares served${smb.in_sync ? "" : " · out of sync"}`}</div></div>
    ${can("admin") && !smb.error ? `<div class="row">${smb.installed && (!smb.in_sync || Object.keys(smb.recovery_failures || {}).length) ? '<button class="btn sm" onclick="repairSamba(this)">Repair SMB server</button>' : ""}${smbUserList ? `<button class="btn sm" onclick="smbUsers()">SMB users · ${smbUserList.length}</button>` : smbUsersUnread ? '<button class="btn sm" onclick="smbUsers()">SMB users</button>' : ""}<button class="btn sm quiet" onclick="settingsTab('cluster');go('settings')">Manage add-on</button></div>` : ""}</div>
    ${smbUsersUnread && !smb.error ? `<div class="dim small" style="margin-top:8px">The SMB users could not be read. ${esc(smbUsersUnread)}</div>` : ""}
    ${smb.partial ? `${UI.callout("warn", { html: `Partly serving · ${plural(smb.offline_shares.length, "share")} offline` }, `Shares whose storage is unavailable are left out for now, so the other shares can start. No volumes, passwords or share settings were deleted.<ul>${smb.offline_shares.map(s => `<li><b>${esc(s.name)}</b> · ${esc(s.pvc)}: ${esc(s.reason)}</li>`).join("")}</ul>When the storage has been available for two minutes, Homestead adds the shares again. This restarts the SMB server and briefly interrupts connections.`)}` : ""}
    ${smbUserList && !smbUserList.length ? `<div class="dim small" style="margin-top:8px">There are no SMB users yet. Private shares need one, but guest shares do not. <a class="linkish" onclick="smbUserEdit('')">Add a user</a></div>` : ""}
    ${smb.recovery_warning ? `${UI.callout("warn", "", `${esc(smb.recovery_warning)}`)}` : ""}
    ${Object.keys(smb.recovery_failures || {}).length ? `${UI.callout("bad", "", `Shares are no longer restored automatically. ${Object.entries(smb.recovery_failures).map(([pvc, reason]) => `${esc(pvc)}: ${esc(reason)}`).join(" ")} The shares that work were restored. After fixing the problem, choose Repair SMB server.`)}` : ""}
    ${Object.keys(smb.recovery_pending || {}).length ? `${UI.callout("warn", "", `Checking that the storage stays available: ${Object.entries(smb.recovery_pending).map(([pvc, p]) => `${esc(pvc)} (${p.action === "restore" ? "waiting to add back" : "waiting to leave out"})`).join(", ")}.`)}` : ""}
    ${UI.more("How the SMB server is managed", `<p>Homestead manages the SMB server, its image and its volumes from the list of shares. Turn the server on or off, or remove it, under Settings → Hardware and storage → Add-ons. Volumes are kept. A volume that has been unavailable for one minute is left out for now. It is added back when it has been available again for two minutes. Both changes restart the SMB server. Shares still need their storage and a working cluster to recover.</p>`)}</div>
  <div class="card" style="margin-bottom:14px"><div class="between"><div><div class="ctitle">NFSv4 server · ${esc(nfs.name || "homestead-nfs")}</div>
    <div class="dim small">${nfs.error ? `Status unavailable. ${esc(nfs.error)}` : !nfs.installed ? "Not installed" :
      `${nfs.enabled ? `${nfs.ready || 0}/${nfs.desired || 1} ready` : "Stopped"}${nfs.address ? ` · ${esc(nfs.address)}:/<share>` : " · waiting for an address"}`} · ${(nfs.exports || []).length} exports</div></div>
    <button class="btn sm quiet" onclick="settingsTab('cluster');go('settings')">Manage add-on</button></div>
    ${nfsRecovery(nfs, false, "<p>The NFS server is optional and separate from the SMB server. Only the chosen shared volumes are exported, and only to their allowed client networks. The SMB server and the volumes are not affected.</p>")}</div>
  <div class="card flat pad0"><div class="tblwrap sharetable"><table data-sort="shares" class="tbl stack">
    <thead><tr><th>Share</th><th>Storage</th><th>Size</th><th>Access</th><th>Windows path</th><th></th></tr></thead><tbody>
    ${sh.map(s => `<tr><td class="shareidentity"><div class="row" style="gap:9px"><div class="av n2">${esc(s.name.slice(0, 2).toUpperCase())}</div>
      <div><b>${esc(s.name)}</b>${(smb.offline_shares || []).some(row => row.name === s.name) ? ` ${UI.chip("Offline · storage unavailable", "bad")}` : ""}<div class="dim xs">${esc(s.created || "")}</div></div></div></td>
      <td class="small mono" data-label="Storage"><span class="sharevol">${esc(s.pvc || "—")}${s.sub_path ? `<span class="dim">/${esc(s.sub_path)}</span>` : ""}</span>
        ${s.owned === false ? UI.chip("shared volume", "neutral") : ""}</td>
      <td class="mono" data-label="Size">${s.size_gb ? s.size_gb + " GiB" : "—"}</td>
      <td data-label="Access"><span>${s.public ? UI.chip("guest", "warn", { tip: "No sign-in. Windows 11 refuses guest shares until two of its settings are changed. See Allow guest access when creating or editing a share." }) : (can("admin") ? `<button type="button" class="pill low pillbtn" data-tip="Private. Signs in as ${esc(s.user)}. Opens SMB users." onclick="smbUsers()">${esc(s.user)}</button>` : UI.chip(s.user, "neutral"))}
        ${s.read_only ? UI.chip("read only", "neutral") : UI.chip("read/write", "neutral")}
        ${shareBinChip(s)}
        ${s.nfs_clients ? UI.chip("NFS", "info", { size: "slim", tip: `NFS ${s.nfs_read_only === false ? "read/write" : "read only"} for ${s.nfs_clients}` }) : ""}</span></td>
      <td class="small muted mono" data-label="Windows path">${smb.address ? `\\\\${esc(ip)}\\${esc(s.name)}` : "Waiting for an address"}</td>
      <td data-actions>${actionBar([
        { label: "Edit", icon: "edit", run: `editShare(${jsq(s.name)})`, need: "admin", tip: "Make this share larger or change who can use it" },
        { label: "NFS", run: `nfsExport(${jsq(s.name)})`, need: "admin", tip: "Set up this share's NFSv4 export and its allowed clients" },
        { label: "Remove", icon: "trash", run: `rmShare(${jsq(s.name)})`, need: "admin", danger: true }])}</td></tr>`).join("")
      || `<tr><td colspan=6 class="empty">No shares yet. Create one with ＋ New share.</td></tr>`}</tbody></table></div></div>`);
}
window.nfsExport = name => {
  const share = (STATE.data.shares || []).find(row => row.name === name);
  if (!share) return toast("The share details are no longer available. Refresh and try again.", "bad");
  const rwx = (share.access_modes || []).includes("ReadWriteMany");
  modal(`NFS export · ${name}`, `<p>Export this share from the optional NFSv4 server to one client address or network. Leave the field blank to remove its NFS export. SMB access does not change.</p>
    ${rwx ? "" : `${UI.callout("warn", "", `This share may not be on a shared volume. NFS needs a shared (ReadWriteMany) volume, so that the separate NFS server can use the data. This is checked before saving.`)}`}
    <div class="f"><label>Allowed IPv4 client or CIDR ${tip("For example, 192.0.2.42 or 192.0.2.0/24. An export open to every address is not allowed.")}</label>
      <input id="nfs_clients" value="${esc(share.nfs_clients || "")}" placeholder="192.0.2.0/24" autocomplete="off"></div>
    <label class="switch"><input type="checkbox" id="nfs_ro" ${share.nfs_read_only !== false ? "checked" : ""}> Read only</label>
    <div class="dim xs" style="margin-top:8px">NFSv4 clients mount ${esc(STATE.data.nfs?.address || "<server-ip>")}:/${esc(name)} on TCP port 2049. The server and its virtual IP are turned on under Settings → Hardware and storage → Add-ons. Exporting a Longhorn shared volume adds a second NFS layer.</div>
    ${UI.actions(`<button class="btn pri" onclick="nfsExportSave(${jsq(name)},this)">Save export</button><button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
};
window.nfsExportSave = async (name, button) => {
  const clients = $("#nfs_clients")?.value.trim() || "";
  if (button) button.disabled = true;
  try {
    const result = await api("/api/shares/nfs", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, clients, read_only: !!$("#nfs_ro")?.checked }) });
    closeModal(); toast(result.detail, "ok"); viewShares();
  } catch (e) { toast(e.message, "bad"); if (button) button.disabled = false; }
};
window.repairSamba = async button => {
  if (button) { button.disabled = true; button.textContent = "Repairing…"; }
  try {
    const result = await api("/api/shares/repair", { method: "POST", headers: { "Content-Type": "application/json" },
      body: "{}" });
    toast(result.detail, "ok"); viewShares();
  } catch (e) { toast(e.message, "bad"); if (button) { button.disabled = false; button.textContent = "Repair SMB server"; } }
};
window.smbUsers = async () => {
  modal("SMB users", '<div class="empty">Loading the users…</div>', true, "smb-access");
  try {
    const users = await api("/api/shares/users");
    STATE.data.smbUsers = users;
    $("#mbody").innerHTML = UI.lead("Each SMB user has one password, used for all of that user's private shares.")
      + UI.table([{ label: "User" }, { label: "Shares" }, { label: "Actions" }], users.map(user => [esc(user.user),
        esc(user.shares.join(", ") || "No shares"),
        UI.button("Change password", `smbUserEdit(${jsArg(user.user)})`, { small: true })
        + (!user.shares.length ? UI.button("Remove", `smbUserRemove(${jsArg(user.user)})`, { small: true }) : "")]))
      + UI.more("About SMB users", "These accounts are separate from Homestead accounts. Saved passwords are never shown. A user can be removed once none of the private shares uses it. Unused users stay available for new shares.")
      + UI.actions(UI.cancel("Close") + UI.button("Add user", "smbUserEdit()", { kind: "pri" }));
  } catch (error) {
    $("#mbody").innerHTML = UI.callout("bad", "The SMB users could not be loaded", esc(error.message))
      + UI.actions(UI.cancel("Close") + UI.button("Try again", "smbUsers()", { kind: "pri" }));
  }
};
window.smbUserEdit = (name = "") => {
  const user = (STATE.data.smbUsers || []).find(row => row.user === name);
  modal(name ? `Change SMB password · ${esc(name)}` : "Add SMB user",
    UI.lead(name ? "Set one password for every share that uses this account." : "Save an SMB account to choose when creating private shares.")
    + (user?.shares.length ? UI.callout("warn", "SMB connections restart", `This changes access to ${esc(user.shares.join(", "))}. Clients must connect again with the new password.`) : "")
    + UI.fields(UI.field("Username", `<input id="su_user" value="${esc(name)}" ${name ? "readonly" : ""} autocomplete="username" maxlength="32">`)
      + UI.field("New password", '<input id="su_password" type="password" autocomplete="new-password">'))
    + UI.actions(UI.button("Back", "smbUsers()", { attrs: 'data-dialog-dismiss="true"' }) + UI.button(name ? "Change password" : "Add user", `smbUserSave(${jsArg(name ? "password" : "create")},this)`, { kind: "pri" })), false, "smb-access");
};
window.smbUserSave = async (action, button) => {
  const user = $("#su_user").value.trim(), password = $("#su_password").value;
  if (!/^[A-Za-z0-9_.-]{1,32}$/.test(user)) return toast("Enter a valid SMB username", "bad");
  if (!password) return toast("A password is required", "bad");
  button.disabled = true;
  try {
    const result = await api("/api/shares/users", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user, password, action }) });
    toast(result.message, "ok"); if (window.noteOperation) noteOperation(result.operation);
    await smbUsers();
  } catch (error) { button.disabled = false; toast(error.message, "bad"); }
};
window.smbUserRemove = name => {
  modal(`Remove SMB user · ${name}`, UI.lead("Remove this unused account and its saved password. Shares and their data are kept.")
    + UI.actions(UI.button("Back", "smbUsers()", { attrs: 'data-dialog-dismiss="true"' }) + UI.button("Remove user", `smbUserDelete(${jsArg(name)},this)`, { kind: "danger" })), false, "smb-access");
};
window.smbUserDelete = async (user, button) => {
  button.disabled = true;
  try {
    const result = await api("/api/shares/users/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user }) });
    toast(result.message, "ok"); await smbUsers();
  } catch (error) { button.disabled = false; toast(error.message, "bad"); }
};
window.newShare = async () => {
  modal("New share", `<div class="empty"><span class="spin2"></span>Loading the volumes</div>`, true, "smb-access");
  let options, users;
  try {
    [options, users] = await Promise.all([api("/api/shares/options"), api("/api/shares/users")]);
  } catch (error) {
    $("#mbody").innerHTML = UI.callout("bad", "The share options could not be loaded", esc(error.message))
      + UI.actions(UI.cancel() + UI.button("Try again", "newShare()", { kind: "pri" }));
    return;
  }
  STATE.data.smbUsers = users;
  const existing = users.filter(user => user.has_password);
  const shareWhat = `
    <div class="f"><label>Share name ${tip("Windows shows this name after the server address. Inside the SMB server, the share is at /shares/<name>.")}</label>
      <input type="text" id="sh_name" placeholder="media" autocomplete="off"></div>
    <div class="sec">Storage ${tip("A new Longhorn volume, or a folder in an existing volume.")}</div>
    <div id="sh_storage"></div>
    ${UI.more("Sharing an existing volume", "A volume can also be used by an app. A single-host volume (RWO) is attached to one host at a time, so the SMB server and that app must run on the same host.")}
`;
  const shareAccess = `
    ${UI.section("SMB access", `<div class="f"><label>SMB user</label><select id="sh_identity" onchange="shareAccountHint()">
      ${existing.map(user => `<option value="${esc(user.user)}">${esc(user.user)} · existing user</option>`).join("")}
      <option value="">Add a new user…</option></select><span class="ui-help">Existing users keep their saved password. Passwords are managed under SMB users.</span></div>
      <div class="f2" id="sh_new_account"><div class="f"><label>New username</label><input type="text" id="sh_user" value="${existing.length ? "" : "lab"}" autocomplete="username"></div>
      <div class="f"><label>Password</label><input type="password" id="sh_pass" autocomplete="new-password"></div></div>`)}
    ${options.samba_installed ? "" : `<div class="sec">SMB server address</div>
      <p class="ui-help">The SMB server is not installed yet, so creating this share installs it. Choose the address that Windows uses to reach it (\\\\address\\share).</p>
      <div class="f" id="sh_smb"><span class="dim xs"><span class="spin2"></span></span></div>`}
    <label class="switch"><input type="checkbox" id="sh_pub" onchange="shareAccountHint()"> Allow guest access</label>
    <div id="sh_guest_note" class="hidden">${shareGuestWindowsNote()}</div>
    <label class="switch"><input type="checkbox" id="sh_ro"> Read only</label>
    ${shareBinFields("sh", { recycle: true, recycle_days: 30 })}
    <p class="ui-help">Creating a share restarts the SMB server, so open connections drop briefly.</p>`;
  $("#mbody").innerHTML = stepper("sh_steps", [{ title: "Share and storage", html: shareWhat }, { title: "Access", html: shareAccess }],
    UI.button("Create share", "mkShare(this)", { kind: "pri", id: "sh_go", attrs: 'data-need="admin"' }));
  const host = createVolumePicker($("#sh_storage"), {
    pvcs: () => options.pvcs || [],
    storageClasses: () => options.storage_classes || [],
    sharedStorageClasses: () => options.shared_storage_classes || options.storage_classes || [],
    classFacts: () => options.storage_class_facts || {},
    targetNode: () => options.node || "",
    kinds: ["new-rwo", "new-rwx", "existing"],
    pathLabel: "Folder inside the volume",
    pathPlaceholder: "whole volume",
    newSourceHelp: "Creates a Longhorn volume for this share. Leave the name blank to use share-<share name>.",
    removable: false,
    readOnlyToggle: false,
    // The share's folder is the path field above; a second one would be ignored.
    folders: false,
  });
  renderVolumeRows(host, [{ kind: "new-rwo", path: "", source: "", size_gb: 10,
    label: "Share storage" }]);
  if (!options.samba_installed) vipChoices().then(choices => {
    const own = (choices.own || []).find(v => v.free);
    const smb = $("#sh_smb");
    if (smb) smb.innerHTML = vipPicker("sh_smb", own ? own.ip : (choices.free[0] || ""), choices);
  }).catch(e => {
    const smb = $("#sh_smb");
    if (smb) { smb.dataset.unread = e.message; smb.innerHTML = UI.callout("bad", "No address is available for the SMB server", esc(e.message)); }
  });
  shareAccountHint();
};
window.shareAccountHint = () => {
  const existing = !!$("#sh_identity")?.value, guest = !!$("#sh_pub")?.checked;
  $("#sh_guest_note")?.classList.toggle("hidden", !guest);
  $("#sh_new_account")?.classList.toggle("hidden", existing || guest);
  if ($("#sh_identity")) $("#sh_identity").disabled = guest;
  if ($("#sh_pass")) { $("#sh_pass").disabled = existing || guest; if (existing || guest) $("#sh_pass").value = ""; }
};
window.mkShare = async button => {
  const name = $("#sh_name").value.trim();
  if (!/^[a-z0-9-]{2,30}$/.test(name)) return toast("Use only lowercase letters, numbers and dashes", "bad");
  const selected = $("#sh_identity").value;
  const account = selected || $("#sh_user").value.trim();
  const known = (STATE.data.smbUsers || []).some(row => row.user === selected && row.has_password);
  if (!$("#sh_pub").checked && !$("#sh_pass").value && !known) {
    return toast("A password is required for a private share", "bad");
  }
  const storage = readVolumeRows($("#sh_storage"))[0] || {};
  const folder = String(storage.path || "").trim().replace(/^\/+|\/+$/g, "");
  if (folder && !/^[A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)*$/.test(folder)) {
    return toast("The folder must be a path inside the volume, such as media/photos", "bad");
  }
  const reusing = storage.kind === "existing";
  if (reusing && !storage.source) return toast("Choose the volume this share should use", "bad");
  if (shareBinBody("sh").recycle_days === null) return toast(`Keep deleted files for 0 to ${SHARE_BIN_MAX_DAYS} days`, "bad");
  const body = { name, user: account, password: selected || $("#sh_pub").checked ? "" : $("#sh_pass").value,
    account_mode: selected ? "existing" : "new",
    public: $("#sh_pub").checked, read_only: $("#sh_ro").checked, sub_path: folder, ...shareBinBody("sh"),
    samba_ip: ($("#sh_smb_lb_ip")?.value || "").trim() };
  if ($("#sh_smb")?.dataset.unread) return toast($("#sh_smb").dataset.unread, "bad");
  if ($("#sh_smb") && !body.samba_ip) return toast("Choose the address for the SMB server. If the list is empty, add virtual IPs under Networking.", "bad");
  if (reusing) body.pvc = storage.source;
  // A new volume's name is the one to create, not one to look up.
  else Object.assign(body, { new_name: storage.source || "", size_gb: storage.size_gb,
    storage_class: storage.storage_class, access_mode: storage.access_mode });
  if (button) { button.disabled = true; button.textContent = "Creating…"; }
  try {
    const result = await api("/api/shares", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    (result.warnings || []).forEach(warning => toast(warning, "warn"));
    toast(`Share "${name}" created`, "ok"); closeModal(); resetPaint(); viewShares();
  } catch (e) { if (button) { button.disabled = false; button.textContent = "Create share"; } toast(e.message, "bad"); }
};
/* A share's recycle bin: files deleted over SMB wait in .deleted at the
   share's root, for so many days or until emptied. */
const SHARE_BIN_MAX_DAYS = 3650;
const shareBinChip = s => (s.read_only || !s.recycle ? "" : UI.chip(s.recycle_days ? `bin ${s.recycle_days}d` : "bin", s.recycle_days ? "neutral" : "warn",
  { size: "slim", tip: s.recycle_days ? `Files deleted over SMB are kept in .deleted for ${s.recycle_days} days, then removed`
    : "Files deleted over SMB are kept in .deleted until it is emptied, so they keep using space. Edit the share to set how many days they are kept." }));
function shareBinFields(prefix, s) {
  const on = s.recycle !== false;
  return UI.section("Recycle bin", `<label class="switch"><input type="checkbox" id="${prefix}_bin" ${on ? "checked" : ""} onchange="shareBinHint(${jsq(prefix)})">
      Keep deleted files ${tip("Files deleted over SMB are moved to a hidden .deleted folder at the top of the share instead of being removed at once, so a mistake can be undone. They use space there until they are removed. Apps that change files directly, not over SMB, are not affected.")}</label>
    <div class="f ${on ? "" : "hidden"}" id="${prefix}_bin_keep"><label>Remove them after (days) ${tip("Counted from when each file was deleted. 0 keeps them until the bin is emptied manually. For files already in the bin when a limit is first set, their last modified date is used.")}</label>
      <input type="number" id="${prefix}_bin_days" min="0" max="${SHARE_BIN_MAX_DAYS}" step="1" value="${esc(on ? (s.recycle_days ?? 30) : 30)}"></div>
    ${prefix === "she" && !s.read_only ? `<div id="she_bin_size" class="dim small"><span class="spin2"></span> Reading the recycle bin</div>
    <div id="she_bin_off" class="hidden"></div>` : ""}`);
}
window.shareBinHint = prefix => {
  const on = !!$(`#${prefix}_bin`)?.checked;
  $(`#${prefix}_bin_keep`)?.classList.toggle("hidden", !on);
  const off = $("#she_bin_off");
  if (prefix !== "she" || !off) return;
  const held = STATE.shareBin && STATE.shareBin.files;
  off.classList.toggle("hidden", on || !held);
  off.innerHTML = on || !held ? "" : `${UI.callout("warn", "", `Turning the bin off stops deleted files from being moved there. The files already in it stay: ${esc(shareBinHolds())}.
    <label class="switch"><input type="checkbox" id="she_bin_empty"> Empty the bin when saving · the files are deleted permanently</label>`)}`;
};
const shareBinHolds = () => {
  const bin = STATE.shareBin || {};
  return `${bin.gb ?? 0} GiB in ${plural(bin.files || 0, "file")}`;
};
async function shareBinLoad(name) {
  STATE.shareBin = null;
  if (!$("#she_bin_size")) return;
  try {
    const bin = await api(`/api/shares/recycle?name=${encodeURIComponent(name)}`);
    STATE.shareBin = bin;
    if (!$("#she_bin_size")) return;
    $("#she_bin_size").innerHTML = bin.files
      ? `The bin holds ${esc(shareBinHolds())} in <span class="mono">${esc(bin.folder)}</span>.
         <button type="button" class="btn sm" data-need="admin" onclick="emptyShareBin(${jsq(name)},this)">Empty now</button>`
      : "The bin is empty.";
    shareBinHint("she");
  } catch (e) {
    if ($("#she_bin_size")) $("#she_bin_size").textContent = `The recycle bin could not be read. ${e.message}`;
  }
}
window.emptyShareBin = async (name, button) => {
  if (!(await ask(`Empty the recycle bin of "${name}"?\n\nEverything in it is deleted permanently: ${shareBinHolds()}.`,
    { title: "Empty recycle bin", ok: "Empty bin", danger: true }))) return;
  if (button) { button.disabled = true; button.textContent = "Emptying…"; }
  try {
    const result = await api("/api/shares/recycle/empty", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast(result.detail, "ok");
    shareBinLoad(name);
  } catch (e) { if (button) { button.disabled = false; button.textContent = "Empty now"; } toast(e.message, "bad"); }
};
function shareBinBody(prefix) {
  const recycle = !!$(`#${prefix}_bin`)?.checked;
  const raw = $(`#${prefix}_bin_days`)?.value ?? "30";
  const days = raw === "" ? 0 : Number(raw);
  const valid = Number.isInteger(days) && days >= 0 && days <= SHARE_BIN_MAX_DAYS;
  return { recycle, ...(recycle ? { recycle_days: valid ? days : null } : {}),
    ...(prefix === "she" && !recycle && $("#she_bin_empty")?.checked ? { empty_recycle: true } : {}) };
}
const shareAccountSiblings = share => (STATE.data.shares || [])
  .filter(row => !row.public && row.user === share.user && row.name !== share.name)
  .map(row => row.name);

window.shareAccessToggle = () => {
  const guest = $("#she_access")?.value === "guest";
  $("#she_private")?.classList.toggle("hidden", guest);
  $("#she_guest_note")?.classList.toggle("hidden", !guest);
};
window.editShare = name => {
  const s = (STATE.data.shares || []).find(row => row.name === name);
  if (!s) return toast("The share details are no longer available. Refresh and try again.", "bad");
  modal(`Edit share · ${s.name}`, `
    <div class="callout"><b>\\\\${esc(STATE.data.samba?.address || "address not assigned yet")}\\${esc(s.name)}</b><br>
      ${s.owned === false ? `Files come from <span class="mono">${esc(s.pvc || "")}${s.sub_path ? "/" + esc(s.sub_path) : ""}</span>, a volume this share uses but does not own.` : "The volume can be made larger but not smaller."}
      Changing only the size keeps the SMB server running. Changing access briefly disconnects open connections.</div>
    <div class="f2" style="margin-top:14px"><div class="f"><label>Size (GiB) ${tip("Longhorn volumes can be made larger while in use. They cannot be made smaller once they hold data.")}</label>
      <input type="number" id="she_size" min="${esc(s.size_gb || 1)}" value="${esc(s.size_gb || 1)}" ${s.owned === false ? "disabled" : ""}>
      ${s.owned === false ? `<span class="dim xs">Change the size of <span class="mono">${esc(s.pvc || "its volume")}</span> on the Volumes page, because this share uses another volume.</span>` : ""}</div>
      <div class="f"><label>Access</label><select id="she_access" onchange="shareAccessToggle()">
        <option value="private" ${s.public ? "" : "selected"}>Private · username and password</option>
        <option value="guest" ${s.public ? "selected" : ""}>Guest · no sign-in</option></select></div></div>
    <div id="she_guest_note" class="${s.public ? "" : "hidden"}">${shareGuestWindowsNote()}</div>
    <div id="she_private" class="${s.public ? "hidden" : ""}"><div class="f"><label>Username</label>
      <input type="text" id="she_user" value="${esc(s.user || "lab")}" autocomplete="username"></div>
      <div class="f"><label>New password ${tip("Leave blank to keep the current password. Passwords are stored in a Kubernetes Secret and are never sent back to the browser.")}</label>
        <input type="password" id="she_pass" autocomplete="new-password" placeholder="${s.has_password ? "Leave blank to keep the current password" : "Required for private access"}">
        ${shareAccountSiblings(s).length ? `<span class="dim xs">${esc(s.user)} is also used by ${esc(shareAccountSiblings(s).join(", "))}. Each account has one password, so a new password applies to those shares too.</span>` : ""}</div></div>
    <label class="switch"><input type="checkbox" id="she_ro" ${s.read_only ? "checked" : ""}> Read only · clients can view and download files but cannot change them</label>
    ${shareBinFields("she", s)}
    ${UI.actions(`<button class="btn pri" data-need="admin" onclick="saveShareEdit(${jsq(s.name)},this)">${icon("edit")}Save changes</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
  shareBinLoad(s.name);
};
window.saveShareEdit = async (name, button) => {
  const original = (STATE.data.shares || []).find(row => row.name === name);
  if (!original) return toast("The share details are no longer available", "bad");
  const size = +$("#she_size").value || +(original.size_gb || 1);
  const publicAccess = $("#she_access").value === "guest";
  const password = $("#she_pass")?.value || "";
  if (original.owned !== false && (!Number.isInteger(size) || size < +(original.size_gb || 1))) {
    return toast(`Size must be at least ${original.size_gb || 1} GiB`, "bad");
  }
  if (!publicAccess && !original.has_password && !password) return toast("Set a password before turning on private access", "bad");
  const bin = shareBinBody("she");
  if (bin.recycle_days === null) return toast(`Keep deleted files for 0 to ${SHARE_BIN_MAX_DAYS} days`, "bad");
  if (bin.empty_recycle && !(await ask(`Empty the recycle bin of "${name}" when saving?\n\nEverything in it is deleted permanently: ${shareBinHolds()}.`,
    { title: "Empty recycle bin", ok: "Empty and save", danger: true }))) return;
  if (button) { button.disabled = true; button.textContent = "Saving…"; }
  try {
    const result = await api("/api/shares/edit", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, size_gb: size, user: $("#she_user")?.value.trim() || original.user || "lab",
        password, public: publicAccess, read_only: $("#she_ro").checked, ...bin }) });
    toast(result.message || `Share "${name}" updated`, "ok"); closeModal(); resetPaint(); viewShares();
  } catch (e) { if (button) { button.disabled = false; button.innerHTML = `${icon("edit")}Save changes`; } toast(e.message, "bad"); }
};
window.rmShare = async name => {
  if (!(await ask(`Remove the share "${name}"?\n\nThe volume and its data are kept. The SMB server restarts and stops using the volume, unless another share uses it. Other apps may still use the volume.`, { title: "Remove share", ok: "Remove share", danger: true }))) return;
  try {
    const result = await api("/api/shares/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast(result.message || `Share "${name}" removed`, "ok");
    if (window.noteOperation) noteOperation(result.operation);
    resetPaint(); viewShares();
  } catch (e) { toast(e.message, "bad"); }
};

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  viewShares, SHARE_BIN_MAX_DAYS, shareBinChip, shareBinFields, shareBinHolds, shareBinLoad, shareBinBody,
  shareAccountSiblings
};
