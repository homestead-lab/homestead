/* Homestead core: privilege fields and the logo check. A classic script, loaded right after core.js (#911). */
/* ---------------- privileges ----------------
   What a container may do to its host beyond the defaults: a VPN's tunnel
   device and routes, further capabilities, or everything (privileged). */
function privilegeFields(prefix, p = {}) {
  const caps = (p.cap_add || []).filter(c => !(p.tun && c === "NET_ADMIN"));
  return `<div class="priv-fields">
    <label class="switch"><input type="checkbox" id="${prefix}_tun" ${p.tun ? "checked" : ""}> VPN tunnel
      ${tip("Mounts /dev/net/tun and grants NET_ADMIN, so a VPN client can open its tunnel and add routes. Fixes \"RTNETLINK answers: Operation not permitted\" in transmission-openvpn, gluetun and similar apps, without making the container privileged.")}</label>
    <label class="switch"><input type="checkbox" id="${prefix}_priv" ${p.privileged ? "checked" : ""}> Privileged
      ${tip("Everything the host allows, as Unraid's Privileged does. A last resort: the container can reach every device and kernel setting on the host.")}</label>
    <div class="f"><label>Extra capabilities ${tip("Linux capabilities by name, separated by spaces or commas, for example NET_ADMIN SYS_TIME. This matches --cap-add in Unraid.")}</label>
      <input id="${prefix}_caps" class="mono" value="${esc(caps.join(" "))}" placeholder="none"></div></div>`;
}
window.privilegeFields = privilegeFields;
function readPrivileges(prefix) {
  if (!$(`#${prefix}_tun`)) return null;
  return { tun: $(`#${prefix}_tun`).checked, privileged: $(`#${prefix}_priv`).checked,
    cap_add: $(`#${prefix}_caps`).value.split(/[\s,]+/).map(c => c.trim().toUpperCase()).filter(Boolean) };
}
window.readPrivileges = readPrivileges;

/* A logo past Homestead's 256 KiB limit - an App Store picture can be - is
   drawn smaller here, where the browser can, and that copy is kept; the
   field then holds Homestead's own reference. One that fits is kept as it is.
   Python's standard library cannot resize a picture (homestead_icons.py). */
const LOGO_SIDE = 256;
async function logoReady(selector) {
  const input = $(selector);
  const url = (input?.value || "").trim();
  if (!/^https?:\/\//i.test(url)) return;
  const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const kept = await post("/api/logo-fit/keep", { url });
  if (kept.icon) { input.value = kept.icon; return; }
  const image = new Image();
  image.src = `/api/logo-fit/source?url=${encodeURIComponent(url)}`;
  try { await image.decode(); } catch { throw new Error("The logo is too large to keep, and this browser could not read it to make it smaller."); }
  const scale = Math.min(1, LOGO_SIDE / Math.max(image.naturalWidth, image.naturalHeight, 1));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
  canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
  const draw = canvas.getContext("2d");
  draw.imageSmoothingQuality = "high";
  draw.drawImage(image, 0, 0, canvas.width, canvas.height);
  const encode = type => new Promise(resolve => canvas.toBlob(resolve, type, 0.9));
  let smaller = await encode("image/png");
  if (!smaller || smaller.size > 250 * 1024) smaller = await encode("image/webp");
  const data = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.onerror = () => reject(new Error("the smaller logo could not be read"));
    reader.readAsDataURL(smaller);
  });
  input.value = (await post("/api/logo-fit/resized", { data })).icon;
  toast(`The logo was large, so Homestead keeps a copy ${canvas.width}×${canvas.height} px`, "ok");
}
window.logoReady = logoReady;

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  privilegeFields, readPrivileges, LOGO_SIDE, logoReady
};
