/* Homestead core: toasts, the dialog stack, ask() and askText(). A classic script, loaded right after core.js (#911). */
/* ---------------- toast / modal ---------------- */
/* A toast is announced (#486): a failure as an alert, which stays until it
   is dismissed or a newer failure replaces it, so a person who looked away
   still sees why; anything else politely, for a few seconds. */
function toast(msg, kind = "") {
  liftToasts();
  const box = $("#toast");
  if (kind === "bad") [...box.children].filter(old => old.dataset.stays).forEach(old => old.remove());
  const d = document.createElement("div");
  d.className = "tst " + kind;
  if (kind === "bad") {
    d.setAttribute("role", "alert");
    d.dataset.stays = "1";
    const text = document.createElement("span"); text.textContent = msg;
    const close = document.createElement("button");
    close.setAttribute("type", "button"); close.setAttribute("aria-label", "Dismiss"); close.className = "tst-close"; close.textContent = "×";
    close.addEventListener("click", () => d.remove());
    // It stays while the person looks away, and goes at their next action:
    // toasts sit where a dialog's main button is, so it must not stay on top.
    const leave = event => {
      if (box.contains(event.target)) return;
      d.remove();
      document.removeEventListener("pointerdown", leave, true);
      document.removeEventListener("keydown", leave, true);
    };
    document.addEventListener("pointerdown", leave, true);
    document.addEventListener("keydown", leave, true);
    d.appendChild(text); d.appendChild(close);
  } else {
    d.setAttribute("role", "status"); d.textContent = msg;
    setTimeout(() => d.remove(), 4200);
  }
  box.appendChild(d);
}
/* Toasts and the job tray share the bottom-right corner, so toasts stand on
   top of the tray - however tall it is, open or closed - rather than over it. */
function liftToasts() {
  const tray = $("#jobTray");
  const height = tray && !tray.classList.contains("hidden") ? tray.getBoundingClientRect().height : 0;
  document.documentElement.style.setProperty("--tray-lift", height ? `${Math.ceil(height) + 8}px` : "4px");
}
if (window.ResizeObserver && $("#jobTray")) {
  new ResizeObserver(liftToasts).observe($("#jobTray"));
  new MutationObserver(liftToasts).observe($("#jobTray"), { attributes: true, attributeFilter: ["class"] });
}
function modal(t, h, wide, contextClass = "") {
  const opening = !modalIsOpen(), retitled = $("#mtitle").textContent !== t;
  if (opening) MODAL_OPENER = document.activeElement;
  if (contextClass !== "volume-files" && window.fileManagerDismiss) window.fileManagerDismiss();
  window.resetDialogDisclosures?.();
  window.__modalGuard = null;   // each modal decides for itself what is at stake
  MODAL_EDITS = new Map();
  $("#mtitle").textContent = t;
  $("#mbody").innerHTML = h;
  enhanceActions($("#mbody"));
  if (window.normaliseDialogActions) normaliseDialogActions($("#mbody"));
  $(".modalbox").classList.toggle("wide", !!wide);
  $(".modalbox").classList.toggle("node-detail-modal", contextClass === "node-detail-modal");
  $(".modalbox").dataset.context = contextClass || "";
  $("#modal").classList.toggle("node-detail-view", contextClass === "node-detail-modal");
  $("#modal").classList.remove("hidden");
  paintModalBack();
  modalHeadHeight();
  if (opening || retitled) focusModal();
}

/* Focus in a dialog (#486): opening one moves focus into it - its first
   field, or its title on a touch screen, where a field would raise the
   keyboard - Tab stays inside it, and closing gives focus back to what
   opened it. */
let MODAL_OPENER = null;
const MODAL_FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';
function focusModal() {
  const touch = window.matchMedia?.("(pointer: coarse)").matches;
  const field = touch ? null : [...$("#mbody").querySelectorAll("input:not([type=hidden]):not([disabled]), select:not([disabled]), textarea:not([disabled])")]
    .find(el => el.offsetParent);
  (field || $("#mtitle")).focus({ preventScroll: true });
}
const restoreFocus = el => { if (el?.isConnected && typeof el.focus === "function") el.focus({ preventScroll: true }); };
document.addEventListener("keydown", event => {
  if (event.key !== "Tab" || !modalIsOpen() || document.querySelector(".askdlg")) return;
  const box = $(".modalbox"), items = [...box.querySelectorAll(MODAL_FOCUSABLE)].filter(el => el.offsetParent);
  if (!items.length) return;
  const first = items[0], last = items.at(-1), here = document.activeElement;
  if (!box.contains(here)) { event.preventDefault(); (event.shiftKey ? last : first).focus(); }
  else if (event.shiftKey && (here === first || here === $("#mtitle"))) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && here === last) { event.preventDefault(); first.focus(); }
});
/* What a person has changed in the open dialog (#459). A field's value is
   noted when a person first reaches for it - after any code has filled it
   in - so only their own edits count, and a field put back as it was is not
   a change. A change with nothing noted before it (autofill) counts. */
let MODAL_EDITS = new Map();
const MODAL_UNKNOWN = Symbol("unknown");
function fieldValue(field) {
  if (field.type === "checkbox" || field.type === "radio") return field.checked;
  if (field.multiple) return [...field.selectedOptions].map(option => option.value).join("\n");
  return field.value;
}
function noteModalField(event) {
  const field = event.target;
  if (!event.isTrusted || !field?.matches?.("input, textarea, select") || MODAL_EDITS.has(field)) return;
  const changed = event.type === "input" || event.type === "change";
  const toggle = field.type === "checkbox" || field.type === "radio";
  MODAL_EDITS.set(field, !changed ? fieldValue(field) : toggle ? !field.checked : MODAL_UNKNOWN);
}
const modalChanged = () => [...MODAL_EDITS].some(([field, start]) => fieldValue(field) !== start);
if ($("#mbody")) for (const type of ["focusin", "pointerdown", "keydown", "input", "change"]) $("#mbody").addEventListener(type, noteModalField, true);

/* A dialog's section rail sticks below its header, not under it (#374): the
   header's height, which a long title can change, is kept as --modalhead-h. */
function modalHeadHeight() {
  const box = $(".modalbox"), head = box?.querySelector(".modalhead");
  if (box && head) box.style.setProperty("--modalhead-h", `${head.offsetHeight}px`);
}
if (typeof window !== "undefined" && typeof window.addEventListener === "function") window.addEventListener("resize", () => modalHeadHeight());

/* Dialogs opened from dialogs - a drive from its host, a feature from the
   hardware list - sit on top of the one they came from. Closing one goes back
   a level, with the dialog underneath as it was left: scrolled, half-filled,
   its address restored. Code that finishes a job calls closeModal, which
   closes the whole stack. */
const MODAL_STACK = [];
const modalIsOpen = () => !$("#modal").classList.contains("hidden");

function pushModal(onReturn) {
  if (!modalIsOpen()) return false;
  const body = $("#mbody"), fragment = document.createDocumentFragment();
  const scroll = $(".modalbox").scrollTop;     // the box scrolls, not its body
  while (body.firstChild) fragment.appendChild(body.firstChild);
  MODAL_STACK.push({ title: $("#mtitle").textContent, fragment, scroll, focus: document.activeElement,
    wide: $(".modalbox").classList.contains("wide"), context: $(".modalbox").dataset.context || "",
    guard: window.__modalGuard, edits: MODAL_EDITS, url: window.location.pathname + window.location.search,
    route: STATE.modalRoute, detail: STATE.modalDetail, onReturn });
  return true;
}

/* Opens a dialog on top of the current one; with none open, it is a dialog.
   The same title again is the same dialog redrawing, not a new level. */
function childModal(t, h, wide, contextClass = "") {
  const deeper = modalIsOpen() && $("#mtitle").textContent !== t;
  if (deeper) pushModal();
  modal(t, h, wide, contextClass);
  if (deeper) $(".modalbox").scrollTop = 0;
}

function modalBack() {
  if (window.fileManagerDismiss) window.fileManagerDismiss();
  if (window.snapshotFilesDismiss) window.snapshotFilesDismiss();
  const back = MODAL_STACK.pop();
  if (!back) return closeModal();
  modal(back.title, "", back.wide, back.context);
  $("#mbody").replaceChildren(back.fragment);
  $(".modalbox").scrollTop = back.scroll;
  restoreFocus(back.focus);
  window.__modalGuard = back.guard;
  MODAL_EDITS = back.edits;
  if (back.url !== window.location.pathname + window.location.search) {
    window.history.replaceState(window.history.state, "", back.url);
  }
  STATE.modalRoute = back.route;
  STATE.modalDetail = back.detail;
  if (window.renderBreadcrumb) renderBreadcrumb(STATE.view, STATE.modalDetail);
  if (back.onReturn) back.onReturn();
}

function paintModalBack() {
  const button = $("#mback");
  if (!button) return;
  const under = MODAL_STACK[MODAL_STACK.length - 1];
  button.hidden = !under;
  if (under) {
    button.querySelector("span").textContent = under.title;
    button.title = `Back to ${under.title}`;
  }
}
window.childModal = childModal;

/* Some pages come as cards or as rows. Which is the reader's choice, kept per
   browser and per page; nothing about it is worth a round trip. */
/* Lists start as rows; nodes compare their metrics in columns. A choice
   made with the switch is kept. */
const LAYOUT_DEFAULT = { containers: "rows", vms: "rows", nodes: "rows" };
function viewLayout(page) {
  const fallback = LAYOUT_DEFAULT[page] || "cards";
  try {
    const chosen = localStorage.getItem(`homestead.layout.${page}`);
    return chosen === "rows" || chosen === "cards" ? chosen : fallback;
  } catch (e) { return fallback; }
}
/* The one control for a page's layout (#360): no menu entry repeats it.
   On phones it folds away where the page's list options carry a Layout
   choice instead; phone: true keeps it where nothing else does. */
function layoutSwitch(page, redraw, { phone = false } = {}) {
  const layout = viewLayout(page);
  const option = (value, label, iconName) => `<button class="${layout === value ? "on" : ""}" title="${label}"
    aria-label="Show as ${label.toLowerCase()}" aria-pressed="${layout === value}"
    onclick="setViewLayout(${jsq(page)},${jsq(redraw)},${jsq(value)})">${icon(iconName)}</button>`;
  return `<div class="seg iconseg${phone ? " phone-keep" : ""}" role="group" aria-label="Layout">${option("cards", "Cards", "dash")}${option("rows", page === "nodes" ? "Comparison" : "Rows", "list")}</div>`;
}
window.setViewLayout = (page, redraw, layout, {preservePaint = false} = {}) => {
  try { localStorage.setItem(`homestead.layout.${page}`, layout); } catch (e) { /* this visit only */ }
  if (!preservePaint) resetPaint();
  if (typeof window[redraw] === "function") window[redraw]();
};
window.modalBack = modalBack;
// The dialog header's back button (#906: a listener, not an inline handler).
globalThis.document?.getElementById?.("mback")?.addEventListener?.("click", () => modalBack());
window.pushModal = pushModal;
/* Homestead's own confirm and prompt, in place of the browser's: a browser
   can silence its dialogs for a page, and an installed app window may not
   show them at all - then a button seemed to do nothing. These open above
   any dialog already open, leave it as it is, and resolve to the answer:
   ask() to true or false, askText() to the text or null. Escape cancels,
   Enter answers. */
function askDialog(message, { title = "", ok = "", cancel = "", danger = false, input = null } = {}) {
  return new Promise(resolve => {
    const layer = document.createElement("div");
    layer.className = "askdlg";
    layer.setAttribute("role", "dialog");
    layer.setAttribute("aria-modal", "true");
    const risky = !!danger;
    layer.innerHTML = `<div class="askbox">${title ? `<h3>${esc(title)}</h3>` : ""}<p class="askmsg"></p>
      ${input ? `<input class="askin mono" autocomplete="off">` : ""}
      ${UI.actions(`<button data-dialog-dismiss="true" class="btn" data-a="no">${esc(cancel || "Cancel")}</button>
        <button class="btn ${risky ? "danger" : "pri"}" data-a="yes">${esc(ok || (input ? "OK" : "Continue"))}</button>`, "", {className:'askbtns'})}</div>`;
    layer.querySelector(".askmsg").textContent = String(message);
    const field = layer.querySelector(".askin");
    if (field) {
      field.value = input.value || "";
      field.placeholder = input.placeholder || "";
    }
    const before = document.activeElement;
    const done = answer => {
      document.removeEventListener("keydown", key, true);
      layer.remove();
      if (before?.isConnected && typeof before.focus === "function") before.focus({ preventScroll: true });
      resolve(input ? (answer ? field.value : null) : answer);
    };
    const key = event => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); done(false); }
      else if (event.key === "Enter" && (field || document.activeElement?.closest?.(".askdlg"))) { event.preventDefault(); event.stopPropagation(); done(true); }
    };
    layer.addEventListener("click", event => {
      const a = event.target.closest("[data-a]")?.dataset.a;
      if (a) done(a === "yes");
      else if (event.target === layer) done(false);
    });
    document.addEventListener("keydown", key, true);
    document.body.appendChild(layer);
    (field || layer.querySelector('[data-a="yes"]')).focus();
  });
}
window.ask = (message, options = {}) => askDialog(message, options);
window.askText = (message, value = "", options = {}) => askDialog(message, { ...options, input: { value, placeholder: options.placeholder } });

/* The X and Escape are the only ways a person dismisses a modal, and either can
   land on unsaved work, so both ask a modal that has something at stake first:
   one whose own guard says so, or, without one, one with changes typed in it.
   Code that closes a modal after finishing its job calls closeModal directly. */
async function dismissModal() {
  const guard = window.__modalGuard;
  if (typeof guard === "function") {
    const question = guard();
    if (question && !(await ask(question, { title: "Unsaved changes", ok: "Discard changes", danger: true }))) return;
  } else if (modalIsOpen() && modalChanged()) {
    const discard = await ask(`Discard your changes to ${$("#mtitle").textContent}?`,
      { title: "Unsaved changes", ok: "Discard changes", cancel: "Keep editing", danger: true });
    if (!discard) return;
  }
  if (MODAL_STACK.length) return modalBack();
  closeModal();
}
function closeModal(updateRoute = true) {
  if (window.fileManagerDismiss) window.fileManagerDismiss();
  if (window.snapshotFilesDismiss) window.snapshotFilesDismiss();
  if (window.selfDataClose) window.selfDataClose();
  if (window.vmConsoleDismiss) window.vmConsoleDismiss();
  window.__modalGuard = null;
  MODAL_EDITS = new Map();
  MODAL_STACK.length = 0;
  paintModalBack();
  $("#modal").classList.add("hidden");
  const opener = MODAL_OPENER; MODAL_OPENER = null; restoreFocus(opener);
  // Its polls (logs, a job, a rollout) stop, and the read each has running is cancelled (#983).
  window.UI?.stopPolls?.();
  window.__logTimer = null; window.__updateTimer = null; window.__reclassTimer = null;
  if (window.__consoleSocket) { window.__consoleSocket.close(); window.__consoleSocket = null; }
  if (window.__consoleResize) { window.__consoleResize.disconnect(); window.__consoleResize = null; }
  // A node's terminal: closing it ends the session, and its helper goes.
  if (window.__nodeSocket) { window.__nodeSocket.close(); window.__nodeSocket = null; }
  if (window.__nodeTermResize) { window.__nodeTermResize.disconnect(); window.__nodeTermResize = null; }
  if (window.__nodeTerm) { try { window.__nodeTerm.dispose(); } catch (_) { /* already gone */ } window.__nodeTerm = null; }
  if (updateRoute && window.clearModalRoute) window.clearModalRoute();
  // Something a dialog did changed the page behind it (a rollout finished): show it now.
  if (window.__repaintAfterModal) { const repaint = window.__repaintAfterModal; window.__repaintAfterModal = null; try { repaint(); } catch (_) { /* the next refresh shows it */ } }
}

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  toast, liftToasts, modal, MODAL_OPENER, MODAL_FOCUSABLE, focusModal, restoreFocus, MODAL_EDITS, MODAL_UNKNOWN,
  fieldValue, noteModalField, modalChanged, modalHeadHeight, MODAL_STACK, modalIsOpen, pushModal, childModal,
  modalBack, paintModalBack, LAYOUT_DEFAULT, viewLayout, layoutSwitch, askDialog, dismissModal, closeModal
};
