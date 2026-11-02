/* Homestead core: the configurable hardware list. A classic script, loaded right after core.js (#911). */
/* ---------------- configurable hardware ---------------- */
async function loadHardwareFeatures(force = false) {
  if (!force && Array.isArray(STATE.data.hardwareFeatures)) return STATE.data.hardwareFeatures;
  STATE.data.hardwareFeatures = await api("/api/hardware/features");
  return STATE.data.hardwareFeatures;
}
function hardwareDef(id) {
  return (STATE.data.hardwareFeatures || []).find(x => x.id === id) ||
    { id, name: id.replace(/[_-]+/g, " "), host_path: "" };
}
const hardwareName = id => hardwareDef(id).name;
const hardwareTags = ids => (ids || []).map(id => {
  const f = hardwareDef(id);
  return UI.chip(f.name, "hw", { tip: f.host_path || "Configured hardware feature" });
}).join(" "); // a space, as .tag's margin gave, where the row has no gap
function hardwareChoices(cls, selected = []) {
  const chosen = new Set(selected || []);
  const nodes = STATE.data.nodes || STATE.data.ov?.nodes || [];
  const choices = (STATE.data.hardwareFeatures || []).map(f => {
    const hosts = nodes.filter(n => (n.hardware || {})[f.id]).map(n => n.name);
    return `<label class="switch hwchoice">
    <input type="checkbox" class="${esc(cls)}" data-hwid="${esc(f.id)}" ${chosen.has(f.id) ? "checked" : ""}>
    <span><b>${esc(f.name)}</b>${f.builtin ? ` ${UI.chip("built in", "neutral")}` : ""}
    ${UI.chip(`${f.host_path} → ${f.container_path || f.host_path}`, "hw")}
    ${f.usb_ids?.length ? `<span class="dim xs"> USB ${esc(f.usb_ids.join(", "))}</span>` : ""}
    <span class="dim xs hw-hosts">${hosts.length ? `${hosts.length} host${hosts.length === 1 ? "" : "s"}: ${esc(hosts.map(x => x.replace("harvester-", "")).join(", "))}` : "no eligible host detected"}</span></span></label>`;
  }).join("");
  return choices + `<button class="btn sm hw-manage" type="button" onclick="openHardwareManager(${jsq(cls)})">Browse host devices / add mapping</button>`;
}
const selectedHardware = cls => $$(`.${cls}:checked`).map(x => x.dataset.hwid);
window.openHardwareManager = cls => {
  if ($("#mtitle").textContent !== "Hardware features") {
    // Coming back, the editor's choices are redrawn: a feature may have been added.
    const selected = selectedHardware(cls);
    pushModal(() => {
      const host = $("#mbody .hwchoices");
      if (host) host.innerHTML = hardwareChoices(cls, selected);
    });
  }
  hardwareFeatureSettings();
};
window.hardwareManagerBack = () => modalBack();

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  loadHardwareFeatures, hardwareDef, hardwareName, hardwareTags, hardwareChoices, selectedHardware
};
