/* Homestead's own configuration: backed up to a file sealed with a
   passphrase, and restored part by part. The server keeps neither the file
   nor the passphrase. Settings › About. */

function configCardHtml() {
  return `${UI.moduleHeader(`Configuration backup`, `Settings, users, VIPs, IP addresses, shares and the rest, in a file encrypted with a passphrase you choose.
        Workloads and their data are not in it: volume backups under Data protection hold those.`, `${UI.button("Restore", "configRestore()", { attrs: 'data-need="admin"' })}
        ${UI.button("Back up", "configBackup()", { kind: "pri", attrs: 'data-need="admin"' })}`)}`;
}
window.configCardHtml = configCardHtml;

const CONFIG_STATE_WORDS = { same: ["same as now", ""], differs: ["differs from now", "warn"], empty: ["nothing in it", ""],
  unknown: ["not known here", "bad"] };

function configPartRow(part, { checked, disabled, chip = "", note = "" }) {
  return `<li><label class="cfg-part${disabled ? " off" : ""}" title="${esc(part.detail || "")}">
    <input type="checkbox" data-part="${esc(part.id)}" ${checked ? "checked" : ""} ${disabled ? "disabled" : ""} onchange="configPartsChanged()">
    <span class="cfg-part-text"><b>${esc(part.label)}</b>${chip}<span class="ui-help">${esc(part.detail || "")}</span>
      ${note ? `<span class="cfg-part-note">${esc(note)}</span>` : ""}</span></label></li>`;
}

const configChosen = () => $$(".cfg-parts input[data-part]:checked").map(x => x.dataset.part);

window.configPartsChanged = () => {
  const count = configChosen().length;
  const go = $("#cfgGo");
  if (go) {
    go.disabled = !count;
    go.textContent = go.dataset.verb + (window.__configRestore ? ` ${count} part${count === 1 ? "" : "s"}` : "");
  }
  const warn = $("#cfgUsersWarn");
  if (warn) warn.hidden = !configChosen().includes("users");
};

/* ---------------------------------------------------------------- backup */
window.configBackup = async () => {
  window.__configRestore = null;
  modal("Back up configuration", '<div class="empty"><span class="spin2"></span> Reading what is set up…</div>', true);
  let parts;
  try { parts = await api("/api/config/parts"); }
  catch (e) { $("#mbody").innerHTML = UI.callout("bad", "Could not read the configuration.", esc(e.message)) + UI.actions(UI.cancel("Close")); return; }
  $("#mbody").innerHTML = `<div class="ui-stack">
    ${UI.lead("Saves the parts you choose to a file on this device, encrypted with a passphrase. Keep the passphrase: without it the file cannot be read, by anyone.")}
    <ul class="cfg-parts compact">${parts.map(p => configPartRow(p, { checked: p.present, disabled: !p.present,
      chip: p.present ? "" : ` ${UI.chip("nothing set up")}` })).join("")}</ul>
    ${UI.fields(
      UI.field("Passphrase", '<input id="cfgPass" type="password" autocomplete="new-password" minlength="8">', { help: "At least 8 characters." }),
      UI.field("Passphrase again", '<input id="cfgPass2" type="password" autocomplete="new-password">'))}
    ${UI.more("What the file holds", `<p>The chosen parts as Homestead keeps them, password hashes and keys included, sealed so only
      the passphrase opens it and any change to it is noticed. Linked clusters are not in it: after a restore, link them again.</p>`)}
    ${UI.actions(UI.cancel() + UI.button("Download backup", "configBackupGo()", { kind: "pri", id: "cfgGo", attrs: 'data-verb="Download backup"' }))}
  </div>`;
};

window.configBackupGo = async () => {
  const parts = configChosen(), pass = $("#cfgPass").value;
  if (!parts.length) return toast("Choose at least one part", "bad");
  if (pass.length < 8) return toast("The passphrase needs at least 8 characters", "bad");
  if (pass !== $("#cfgPass2").value) return toast("The two passphrases differ", "bad");
  const go = $("#cfgGo");
  go.disabled = true; go.textContent = "Sealing…";
  try {
    const doc = await api("/api/config/backup", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parts, passphrase: pass }) });
    const name = `homestead-config-${(doc.site || "homestead").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}-${(doc.created || "").slice(0, 10)}.json`;
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" }));
    link.download = name;
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 5000);
    toast(`Saved ${name}`, "ok");
    closeModal();
  } catch (e) {
    toast(e.message, "bad");
    go.disabled = false; go.textContent = "Download backup";
  }
};

/* ---------------------------------------------------------------- restore */
window.configRestore = () => {
  window.__configRestore = null;
  modal("Restore configuration", `<div class="ui-stack">
    ${UI.lead("Choose a configuration backup and its passphrase. Nothing changes until you pick the parts to restore.")}
    ${UI.fields(
      UI.field("Backup file", '<input id="cfgFile" type="file" accept=".json,application/json">', { wide: true }),
      UI.field("Passphrase", '<input id="cfgPass" type="password" autocomplete="current-password">', { wide: true }))}
    ${UI.actions(UI.cancel() + UI.button("Read backup", "configRestoreRead()", { kind: "pri", id: "cfgRead" }))}
  </div>`);
};

window.configRestoreRead = async () => {
  const file = $("#cfgFile").files?.[0], pass = $("#cfgPass").value;
  if (!file) return toast("Choose the backup file", "bad");
  let doc;
  try { doc = JSON.parse(await file.text()); }
  catch (e) { return toast("That file is not a Homestead configuration backup", "bad"); }
  const read = $("#cfgRead");
  read.disabled = true; read.textContent = "Reading…";
  try {
    const result = await api("/api/config/inspect", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: doc, passphrase: pass }) });
    window.__configRestore = { doc, pass };
    configRestoreParts(result);
  } catch (e) {
    toast(e.message, "bad");
    read.disabled = false; read.textContent = "Read backup";
  }
};

/* The parts a backup holds, each against what is here now. */
window.configRestoreParts = result => {
  window.__configRestore ||= { doc: null, pass: "" };
  const made = result.created ? new Date(result.created).toLocaleString() : "";
  childModal("Restore configuration", `<div class="ui-stack">
    ${UI.facts([["From", esc(result.site || "a Homestead")], ["Made", esc(made || "—")], ["With", `Homestead v${esc(result.homestead || "?")}`]])}
    <ul class="cfg-parts">${result.parts.map(p => {
      const [word, tone] = CONFIG_STATE_WORDS[p.state] || CONFIG_STATE_WORDS.unknown;
      return configPartRow(p, { checked: p.restorable && p.default !== false && p.state === "differs", disabled: !p.restorable,
        chip: ` ${UI.chip(word, tone)}`, note: p.caution || "" });
    }).join("")}</ul>
    <div id="cfgUsersWarn" hidden>${UI.callout("warn", "Restoring users signs everyone out.", "Every account and password becomes the backup's. Sign in again with one of those.")}</div>
    ${UI.more("What a restore does", `<p>Each chosen part is put back as the backup holds it; parts left unticked stay as they are,
      and nothing is deleted. Settings apply straight away; shares, MQTT and IP addresses within a minute. Linked clusters are not in backups.</p>`)}
    ${UI.actions(UI.button("Back", "modalBack()") + UI.button("Restore", "configRestoreGo()", { kind: "pri", id: "cfgGo", attrs: 'data-verb="Restore"' }))}
  </div>`);
  configPartsChanged();
};

window.configRestoreGo = async () => {
  const parts = configChosen(), state = window.__configRestore;
  if (!parts.length || !state?.doc) return;
  if (parts.includes("users") && !(await ask("Restore users and roles?" + String.fromCharCode(10, 10)
      + "Every account and password becomes the backup's, and everyone - you too - signs in again with those."))) return;
  const go = $("#cfgGo");
  go.disabled = true; go.textContent = "Restoring…";
  try {
    const result = await api("/api/config/restore", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: state.doc, passphrase: state.pass, parts }) });
    window.__configRestore = null;
    toast(result.detail, "ok");
    closeModal();
    if (parts.includes("users")) setTimeout(() => window.location.reload(), 1200);
    else { resetPaint(); configRefreshSettings(); }
  } catch (e) {
    toast(e.message, "bad");
    go.disabled = false; configPartsChanged();
  }
};

function configRefreshSettings() {
  if (STATE.view === "settings" && window.viewSettings) viewSettings();
}
