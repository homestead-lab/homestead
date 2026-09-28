/* An address field's IPAM button: any <input data-ipam> gets a small button
   inside its box that lists free addresses from Networking › IP addresses -
   not documented, not answering a scan, outside the DHCP range and the VIP
   pools - and puts the one picked in the field. data-ipam="multi" adds to a
   comma-separated list instead of replacing it. docs/design.md, "Address
   fields". */

let IPAM_FREE = null, IPAM_FREE_AT = 0;

async function ipamFree() {
  if (IPAM_FREE && Date.now() - IPAM_FREE_AT < 30000) return IPAM_FREE;
  IPAM_FREE = await api("/api/ipam/free", { keep: true });
  IPAM_FREE_AT = Date.now();
  return IPAM_FREE;
}

/* Give each new address field its button, once. */
function ipamEnhance(root = document) {
  $$("input[data-ipam]:not([data-ipam-ready])", root).forEach(input => {
    input.dataset.ipamReady = "1";
    const wrap = document.createElement("span");
    wrap.className = "ipam-field";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ipam-btn";
    button.setAttribute("aria-label", "Pick a free address from IP addresses");
    button.innerHTML = '<svg aria-hidden="true"><use href="#i-network"/></svg>';
    button.onclick = event => { event.preventDefault(); ipamPickFor(input, button); };
    wrap.appendChild(button);
  });
}
window.ipamEnhance = ipamEnhance;
new MutationObserver(records => {
  if (records.some(r => [...r.addedNodes].some(n => n.nodeType === 1 && (n.matches?.("input[data-ipam]") || n.querySelector?.("input[data-ipam]")))))
    ipamEnhance();
}).observe(document.documentElement, { childList: true, subtree: true });

function ipamPickClose() {
  $$(".ipam-pop").forEach(pop => pop.remove());
  document.removeEventListener("click", ipamPickOutside, true);
  document.removeEventListener("keydown", ipamPickKey, true);
}
function ipamPickOutside(event) { if (!event.target.closest(".ipam-pop, .ipam-btn")) ipamPickClose(); }
function ipamPickKey(event) { if (event.key === "Escape") { event.stopPropagation(); ipamPickClose(); } }

async function ipamPickFor(input, button) {
  if ($(".ipam-pop")) return ipamPickClose();
  const pop = document.createElement("div");
  pop.className = "ipam-pop";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", "Free addresses");
  pop.innerHTML = '<div class="ipam-pop-empty"><span class="spin2"></span> Reading free addresses…</div>';
  document.body.appendChild(pop);
  const place = () => {
    const box = input.getBoundingClientRect();
    const below = window.innerHeight - box.bottom > pop.offsetHeight + 12 || box.top < pop.offsetHeight + 12;
    pop.style.left = `${Math.round(Math.max(8, Math.min(box.left, window.innerWidth - pop.offsetWidth - 8)))}px`;
    pop.style.top = `${Math.round(below ? box.bottom + 6 : box.top - pop.offsetHeight - 6)}px`;
  };
  place();
  setTimeout(() => {
    document.addEventListener("click", ipamPickOutside, true);
    document.addEventListener("keydown", ipamPickKey, true);
  });
  let subnets;
  try { subnets = await ipamFree(); }
  catch (e) {
    pop.innerHTML = `<div class="ipam-pop-empty">${esc(e.message)}</div>`;
    return place();
  }
  if (!pop.isConnected) return;
  const multi = input.dataset.ipam === "multi";
  const taken = new Set(String(input.value || "").split(/[\s,]+/).filter(Boolean));
  const withFree = subnets.filter(s => (s.free || []).length);
  pop.innerHTML = `<div class="ipam-pop-head"><b>Free addresses</b>
      <span class="dim xs">Outside DHCP and the VIP pools, and not answering a scan</span></div>
    ${withFree.length ? withFree.map(s => `<div class="ipam-pop-subnet">
        <div class="ipam-pop-cidr">${esc(s.cidr)}${s.name ? ` · ${esc(s.name)}` : ""}</div>
        <div class="ipam-pop-list">${s.free.map(ip => `<button type="button" class="${taken.has(ip) ? "on" : ""}" data-ip="${esc(ip)}">${esc(ip)}</button>`).join("")}</div>
      </div>`).join("")
      : `<div class="ipam-pop-empty">${subnets.length ? "No free addresses in the subnets documented."
        : "No subnet is documented yet. Add yours under Networking › IP addresses to be offered free addresses here."}</div>`}`;
  $$(".ipam-pop-list button", pop).forEach(b => b.onclick = () => {
    const ip = b.dataset.ip;
    if (multi) {
      const list = String(input.value || "").split(/[\s,]+/).filter(Boolean);
      if (!list.includes(ip)) list.push(ip);
      input.value = list.join(", ");
    } else {
      input.value = ip;
    }
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    ipamPickClose();
    input.focus();
  });
  place();
}
window.ipamPickFor = ipamPickFor;
