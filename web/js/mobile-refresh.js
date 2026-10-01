/* The installed phone app keeps its document still. Refresh belongs to the
   content pane, with a button for people who do not use the gesture. */
(() => {
  const main = $(".main"), indicator = $("#pullRefresh"), button = $("#refresh");
  const formViews = new Set(["deploy", "vmimport", "imports", "setup"]);
  const controls = "input,textarea,select,button,a,summary,[contenteditable]:not([contenteditable='false']),.monaco-editor,.xterm,canvas,iframe,[role='slider'],[role='textbox']";
  const fields = "input,textarea,select,[contenteditable]:not([contenteditable='false'])";
  let gesture = null, running = false, hideTimer = null;
  const visible = el => el && el.getClientRects().length > 0;
  const editing = () => formViews.has(STATE.view) ||
    [...document.querySelectorAll("#views .monaco-editor,#views .xterm,#views [contenteditable]:not([contenteditable='false'])")].some(visible);
  const blocked = () => STATE.busy || running || STATE.settingsDirty?.size || editing() ||
    visible($("#modal:not(.hidden)")) || visible($("#gate:not(.hidden)")) || visible($(".askdlg")) ||
    document.body.classList.contains("navopen") || document.body.classList.contains("searching");
  const installedPhone = () => document.documentElement.dataset.display === "standalone" && matchMedia("(max-width:900px)").matches;
  const hide = () => { clearTimeout(hideTimer); indicator.hidden = true; };
  const show = (state, text) => {
    clearTimeout(hideTimer);
    indicator.style.top = `${$(".top").getBoundingClientRect().bottom + 10}px`;
    indicator.dataset.state = state;
    indicator.querySelector("span").textContent = text;
    indicator.hidden = false;
  };
  const cancel = () => { gesture = null; if (!running) hide(); };

  async function manualRefresh() {
    if (blocked()) {
      if (STATE.settingsDirty?.size) toast("Save or discard your settings before refreshing", "bad");
      else if (editing()) toast("Finish editing before refreshing this page", "bad");
      return false;
    }
    gesture = null;
    running = true;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    if (installedPhone()) show("loading", "Refreshing…");
    try {
      const updated = await window.refresh(true);
      if (updated && installedPhone()) {
        show("done", "Updated");
        hideTimer = setTimeout(hide, 900);
      } else hide();
      return updated;
    } catch (e) {
      hide(); toast(e.message || "Could not refresh this page", "bad"); return false;
    } finally {
      running = false;
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }
  window.manualRefresh = manualRefresh;
  button.onclick = manualRefresh;

  // A full reload is explicit. Settings use their existing discard check;
  // dialogs, editors and form pages can hold other work not saved yet.
  window.reloadApp = async () => {
    if (!(await settingsLeave())) return;
    if ((editing() || visible($("#modal:not(.hidden)"))) &&
        !(await ask("Reload the app? Unsaved work is lost and open consoles disconnect.", { ok: "Reload app" }))) return;
    window.location.reload();
  };
  $("#reloadApp").onclick = window.reloadApp;

  main.addEventListener("touchstart", event => {
    cancel();
    if (!installedPhone() || blocked() || event.touches.length !== 1 || main.scrollTop > 0) return;
    const target = event.target;
    if (!target.closest("#views") || target.closest(controls) || document.activeElement?.closest(fields)) return;
    // A table, list or editor keeps its own gestures, even at its top edge.
    for (let el = target; el && el !== main; el = el.parentElement) {
      const css = getComputedStyle(el);
      if ((/(auto|scroll)/.test(css.overflowY) && el.scrollHeight > el.clientHeight) ||
          (/(auto|scroll)/.test(css.overflowX) && el.scrollWidth > el.clientWidth)) return;
    }
    const touch = event.touches[0];
    gesture = { id: touch.identifier, x: touch.clientX, y: touch.clientY, token: window.NAV_TOKEN, ready: false };
  }, { passive: true });
  main.addEventListener("touchmove", event => {
    if (!gesture) return;
    if (event.touches.length !== 1 || blocked() || !installedPhone() || main.scrollTop > 0 || window.NAV_TOKEN !== gesture.token) return cancel();
    const touch = event.touches[0];
    if (touch.identifier !== gesture.id) return cancel();
    const dx = Math.abs(touch.clientX - gesture.x), dy = touch.clientY - gesture.y;
    if (dy < -8 || dx > Math.max(12, dy)) return cancel();
    if (dy < 12 || dy < dx * 1.25) { gesture.ready = false; hide(); return; }
    if (!event.cancelable) return cancel();
    event.preventDefault();
    gesture.ready = dy >= 84;
    show(gesture.ready ? "ready" : "pull", gesture.ready ? "Release to refresh" : "Pull to refresh");
  }, { passive: false });
  main.addEventListener("touchend", event => {
    if (!gesture) return;
    const ready = gesture.ready && !event.touches.length && !blocked() && installedPhone() &&
      main.scrollTop === 0 && window.NAV_TOKEN === gesture.token;
    cancel();
    if (ready) void manualRefresh();
  }, { passive: true });
  main.addEventListener("touchcancel", cancel, { passive: true });
  window.addEventListener("resize", cancel);
  document.addEventListener("visibilitychange", () => { if (document.hidden) cancel(); });
})();
