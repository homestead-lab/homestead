/* The first-run checklist: after the first administrator is made, the few
   settings a new cluster wants - an address for Homestead and apps, the node
   probe, backups, OS updates - each with whether it is done and one button
   to do it. Shown to an admin until one of them says it is done; Settings
   opens it again. */

window.welcomeCheck = async (force = false) => {
  if (typeof ROLE !== "undefined" && ROLE !== "admin") return;
  let w;
  try { w = await api("/api/welcome"); } catch (e) { if (force) toast(e.message, "bad"); return; }
  if (!w.show && !force) return;
  STATE.data.welcome = w;
  const s = w.steps || {};
  const vipOk = ["kube-vip", "metallb"].includes(w.load_balancer);
  const step = (done, title, body, action) => `<li class="welcome-step${done ? " done" : ""}">
    <span class="welcome-mark" aria-hidden="true">${done ? "✓" : ""}</span>
    <div><b>${title}</b>${done ? ` ${UI.chip("done", "ok")}` : ""}<div class="small dim">${body}</div>${!done && action ? `<div class="welcome-act">${action}</div>` : ""}</div></li>`;
  const address = s.address || {};
  modal("Welcome to Homestead", `<div class="ui-stack">
    ${UI.lead("A few settings make a new cluster ready for everyday use. Each can be done now or later; this list is in Settings too.")}
    <ol class="welcome-steps">
      ${step(address.done, "An address for Homestead and apps",
        address.done ? `Homestead is at <span class="mono">${esc(address.url)}</span>, and apps share that VIP on their own ports.`
          : w.harvester ? "Harvester gave Homestead its address when it was installed."
          : vipOk ? "A VIP moves to another node if one goes down; the nodes' own addresses do not. Reserve one address outside your router's DHCP range: Homestead, its backup storage and shares go on it, and new apps share it."
          : "kube-vip is not running yet, so only the nodes' own addresses are available. Install it under Settings → Hardware and storage → Add-ons, then come back.",
        vipOk ? (address.vips ? UI.button("Put Homestead on a VIP", "welcomeGo('address')", { kind: "pri" }) : UI.button("Add a VIP", "welcomeGo('vip')", { kind: "pri" })) : "")}
      ${step(s.probe?.done, "Node probe", "Temperatures, drive health, each host's devices and interfaces. It runs a small privileged pod on every node.",
        UI.button("Install node probe", "welcomeGo('probe')"))}
      ${step(s.backups?.done, "Backups", "Somewhere for Longhorn to copy volumes to: the built-in backup storage, or an S3 or NFS target you have.",
        UI.button("Set up backups", "welcomeGo('backups')"))}
      ${s.updates?.applies ? step(s.updates?.done, "OS updates", "Update every host one at a time, in a weekly window, restarting through a drain when an update needs it.",
        UI.button("Choose a window", "welcomeGo('updates')")) : ""}
    </ol>
    ${UI.actions(UI.button("Later", "closeModal()") + UI.button("Done - don't show again", "welcomeDone()", { kind: "pri" }))}</div>`);
};

window.welcomeGo = async what => {
  closeModal();
  if (what === "vip") { go("network"); setTimeout(() => window.vipAdd && vipAdd(), 800); }
  else if (what === "address") { go("network"); setTimeout(() => window.selfAddressMove && selfAddressMove(), 800); }
  else if (what === "probe") window.probeInstallConfirm && probeInstallConfirm();
  else if (what === "backups") { go("protect"); setTimeout(() => window.objectStoreSetup && objectStoreSetup(), 600); }
  else if (what === "updates") window.osUpdates && osUpdates();
};

window.welcomeDone = async () => {
  try { await api("/api/welcome/done", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); }
  catch (e) { toast(e.message, "bad"); return; }
  closeModal();
  toast("Setup checklist closed; it is in Settings if you want it again", "ok");
};
