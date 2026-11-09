/* A VM's screen and serial port.

   Screen is VNC: noVNC (vendored, loaded only when a console opens) speaks
   RFB to KubeVirt through Homestead's proxy. Serial is the VM's first serial
   port in the same terminal view as container consoles - the one to use for
   a VM that boots without a display, or when the screen is stuck. */

const VMC = { rfb: null, socket: null, ns: "", name: "", kind: "vnc", keyboard: false };
// A phone or tablet: its on-screen keyboard only opens for a text field.
const VMC_TOUCH = () => !!window.matchMedia?.("(pointer: coarse)").matches;

function vmConsoleUrl(kind) {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return fleetSocketUrl(`${protocol}//${location.host}/api/vm/console?${new URLSearchParams({ ns: VMC.ns, vm: VMC.name, kind })}`);
}

function vmConsoleStop() {
  if (VMC.rfb) { try { VMC.rfb.disconnect(); } catch (e) { /* already gone */ } VMC.rfb = null; }
  if (VMC.socket) { try { VMC.socket.close(); } catch (e) { /* already gone */ } VMC.socket = null; }
  if (window.__consoleSocket && window.__consoleSocket === VMC.serial) window.__consoleSocket = null;
}
// core-modal.js closeModal() calls this when any dialog closes: the console's session ends with it.
window.vmConsoleDismiss = () => { if (VMC.rfb || VMC.socket) vmConsoleStop(); };

/* A VM with a GPU passed through and no virtual display adapter draws on
   the GPU's own outputs: the screen here stays black, so it says why. */
function vmConsoleDisplayNote(ns, name) {
  const vm = (STATE.data.vms || []).find(v => v.ns === ns && v.name === name);
  if (!vm || !vm.display || vm.display === "virtual") return "";
  return vm.display === "gpu"
    ? UI.callout("warn", "No virtual display: the picture is on the passed-through GPU",
      "This VM has a GPU passed through and no virtual display adapter, so Screen shows nothing. Use a monitor on the GPU, Remote Desktop or Serial, or turn the virtual display on under Edit → Display and devices.")
    : UI.callout("warn", "No virtual display", "This VM has no display adapter, so Screen shows nothing. Use Serial, or turn the virtual display on under Edit → Display and devices.");
}
window.vmConsoleDisplayNote = vmConsoleDisplayNote;

function vmConsoleState(text) { const el = $("#vmcState"); if (el) el.textContent = text; }

window.vmConsole = (ns, name, kind = "vnc") => {
  vmConsoleStop();
  Object.assign(VMC, { ns, name, kind, keyboard: false });
  modal("Console · " + name, `<div class="vmc-bar">
      <div class="seg" role="tablist" aria-label="Console">
        <button type="button" role="tab" aria-selected="${kind === "vnc"}" class="${kind === "vnc" ? "on" : ""}" onclick="vmConsole(${jsq(ns)},${jsq(name)},'vnc')">Screen</button>
        <button type="button" role="tab" aria-selected="${kind === "serial"}" class="${kind === "serial" ? "on" : ""}" onclick="vmConsole(${jsq(ns)},${jsq(name)},'serial')">Serial</button></div>
      <span class="dim xs" id="vmcState" aria-live="polite">connecting…</span>
      ${kind === "vnc" ? `<span class="vmc-tools">
        ${VMC_TOUCH() ? `<button class="btn sm" id="vmcKeyboard" onclick="vmConsoleKeyboard()" title="Open this device's keyboard to type into the VM">Keyboard</button>` : ""}
        <button class="btn sm" onclick="vmConsoleKeys()" title="Send Ctrl+Alt+Del to the VM">Ctrl+Alt+Del</button>
        <button class="btn sm" onclick="vmConsolePaste()" title="Paste text into the VM. It is typed key by key (Ctrl+Shift+V on the screen).">${icon("copy")}Paste</button>
        <select id="vmcLayout" class="sm" style="width:auto" aria-label="The VM's keyboard layout" onchange="vmKeyLayoutChanged(this.value)"
          title="The keyboard layout set inside the VM: pasted text is typed as the keys that make each character there">${Object.entries(VMC_LAYOUTS).map(([id, label]) =>
          `<option value="${id}" ${id === vmKeyLayout() ? "selected" : ""}>${label} keyboard</option>`).join("")}</select>
        <button class="btn sm" id="vmcCopy" hidden onclick="vmConsoleCopyGuest()" title="Copy what the VM last put on its clipboard (Ctrl+Shift+C)">Copy from VM</button>
        <button class="btn sm" onclick="vmConsoleFull()" title="Fill the screen">${icon("ext")}Full screen</button></span>`
      : `<span class="vmc-tools">
        <button class="btn sm" onclick="vmSerialCopy()" title="Copy the selected output, or all of it (Ctrl+Shift+C)">${icon("copy")}Copy</button>
        <button class="btn sm" onclick="vmSerialPaste()" title="Send the clipboard to the serial port (Ctrl+Shift+V)">Paste</button></span>`}
    </div>
    <div class="console-security">Operators only · The start and end of each session are recorded, but what is typed and shown is not.</div>
    ${kind === "vnc" ? vmConsoleDisplayNote(ns, name) : ""}
    ${kind === "vnc" ? `<div class="vmc-paste" id="vmcPaste" hidden>
        <textarea id="vmcPasteText" rows="3" spellcheck="false" autocomplete="off"
          placeholder="Paste here (Ctrl+V), then Type it: the VM has no shared clipboard, so it is typed key by key"></textarea>
        ${UI.actions(`<button class="btn sm pri" onclick="vmConsoleTypePasted()">Type into the VM</button>
          <label class="switch" style="margin:0"><input type="checkbox" id="vmcPasteEnter"> <span>Press Enter after</span></label>          <button data-dialog-dismiss="true" class="btn sm" onclick="vmConsolePasteClose()">Cancel</button>`)}</div>
      <div class="vmc-wrap" id="vmcWrap"><div class="vmc-screen" id="vmcScreen" tabindex="0"></div>
        <textarea class="vmc-keys" id="vmcKeys" aria-label="Type into the VM" autocapitalize="off" autocomplete="off"
          autocorrect="off" spellcheck="false" enterkeyhint="send"></textarea></div>`
      : `<pre class="consoleview" id="consoleView" tabindex="0" aria-label="Serial console output">Waiting for the serial port… press Enter below if it stays quiet: a login prompt only appears after a key.</pre>
        <div class="consoleinput"><textarea id="consoleInput" rows="1" spellcheck="false" autocomplete="off" placeholder="Type · Enter sends · Shift+Enter adds a line"></textarea>
        <button class="btn" onclick="consoleSend()">Send</button></div>`}`, true);
  // Keys typed into the VM are not edits to this dialog: closing it asks nothing.
  window.__modalGuard = () => "";
  // Closing the dialog ends the session (closeModal calls vmConsoleDismiss); nothing else would.
  if (kind === "vnc") vmConsoleScreen(); else vmConsoleSerial();
};

async function vmConsoleScreen() {
  let RFB;
  try { RFB = (await import(`${vendorBase()}/novnc/core/rfb.js`)).default; }
  catch (e) { return vmConsoleState("the VNC client could not load: " + e.message); }
  const target = $("#vmcScreen");
  if (!target) return;
  const rfb = new RFB(target, vmConsoleUrl("vnc"), { wsProtocols: ["binary"] });
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  // On a touch screen a tap would move focus to the screen and close the
  // keyboard; the keyboard's own field keeps it while it is open.
  rfb.focusOnClick = !VMC_TOUCH();
  VMC.rfb = rfb;
  vmConsoleKeyInput(rfb);
  rfb.addEventListener("connect", () => {
    if (VMC_TOUCH()) return vmConsoleState("connected · tap Keyboard to type");
    vmConsoleState("connected · click the screen to type"); rfb.focus({ preventScroll: true });
  });
  rfb.addEventListener("desktopname", event => vmConsoleState(`connected · ${event.detail.name}`));
  rfb.addEventListener("disconnect", event => {
    if (VMC.rfb !== rfb) return;
    vmConsoleState(event.detail.clean ? "disconnected" : "the connection dropped · check the VM is running and try Screen again");
  });
  rfb.addEventListener("securityfailure", event => vmConsoleState("refused: " + (event.detail.reason || "security failure")));
  // What the VM copies, when its display passes its clipboard on (QEMU does
  // with a clipboard agent in the guest; most VMs never send any).
  rfb.addEventListener("clipboard", event => {
    VMC.guestClip = String(event.detail.text || "");
    const button = $("#vmcCopy");
    if (button) button.hidden = !VMC.guestClip;
  });
  // Ctrl+Shift+V pastes and Ctrl+Shift+C copies, as in a terminal; plain
  // Ctrl+V and Ctrl+C still reach the VM. Caught before noVNC takes the key.
  target.addEventListener("keydown", event => {
    const key = event.key.toLowerCase(), mac = event.metaKey && !event.ctrlKey;
    if (!((event.ctrlKey && event.shiftKey) || mac) || !["v", "c"].includes(key)) return;
    event.preventDefault(); event.stopPropagation();
    if (key === "v") vmConsolePaste(); else vmConsoleCopyGuest();
  }, true);
}

function vmConsoleSerial() {
  const socket = new WebSocket(vmConsoleUrl("serial"));
  VMC.socket = VMC.serial = socket;
  window.__consoleSocket = socket;   // consoleSend and Ctrl+C reuse it
  socket.onopen = () => { vmConsoleState("Connected · serial port 1"); $("#consoleInput")?.focus(); };
  socket.onmessage = event => {
    let message;
    try { message = JSON.parse(event.data); } catch (_) { return; }
    // A terminal's colour and cursor codes read as noise in a plain view.
    if (message.type === "output") consoleWrite(String(message.data).replace(/\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\r(?!\n)/g, ""));
  };
  socket.onclose = () => { if (VMC.socket === socket) vmConsoleState("disconnected"); };
  socket.onerror = () => { if (VMC.socket === socket) vmConsoleState("Unavailable · Check that the VM is running and that the account has the operator role."); };
}

window.vmConsoleKeys = () => { if (VMC.rfb) { VMC.rfb.sendCtrlAltDel(); VMC.rfb.focus({ preventScroll: true }); } };
// The screen and the keyboard's field together, so typing works full screen.
window.vmConsoleFull = () => { const el = $("#vmcWrap"); if (el && el.requestFullscreen) el.requestFullscreen().catch(() => {}); };

/* ---------- a touch screen's keyboard ----------
   noVNC reads key presses from the screen, but a phone opens its keyboard
   only for a text field, and then reports words rather than keys: Android
   sends most of them as edits to the field, with no key code. So the field
   keeps a little text of its own, each edit is compared with what it held,
   and the difference is typed - Backspace for each character removed, the
   characters added - which works however the keyboard composes. */
const VMC_FILL = "  ";
const VMC_SPECIAL = { Enter: 0xff0d, Tab: 0xff09, Escape: 0xff1b, ArrowLeft: 0xff51, ArrowUp: 0xff52,
  ArrowRight: 0xff53, ArrowDown: 0xff54, Home: 0xff50, End: 0xff57, Delete: 0xffff, PageUp: 0xff55, PageDown: 0xff56 };
function vmConsoleKeyInput(rfb) {
  const field = $("#vmcKeys"), screen = $("#vmcScreen");
  if (!field) return;
  let held = VMC_FILL, composing = false;
  const reset = () => { field.value = held = VMC_FILL; field.setSelectionRange(VMC_FILL.length, VMC_FILL.length); };
  reset();
  const press = keysym => { if (VMC.rfb === rfb) rfb.sendKey(keysym, null); };
  field.addEventListener("compositionstart", () => { composing = true; });
  field.addEventListener("compositionend", () => { composing = false; reset(); });
  field.addEventListener("input", () => {
    const now = field.value;
    let same = 0;
    while (same < held.length && same < now.length && held[same] === now[same]) same++;
    for (let i = same; i < held.length; i++) press(0xff08);
    for (const ch of now.slice(same)) {
      for (const [keysym, code, down] of vmKeyStrokes(ch)) if (VMC.rfb === rfb) rfb.sendKey(keysym, code, down);
    }
    held = now;
    // Mid-word the keyboard is still editing its own text; leave it be.
    if (!composing) reset();
  });
  // Keys that edit nothing - Enter, arrows, Escape, from a tablet's keyboard.
  field.addEventListener("keydown", event => {
    const keysym = VMC_SPECIAL[event.key];
    if (!keysym || composing || event.isComposing) return;
    event.preventDefault();
    press(keysym);
  });
  field.addEventListener("blur", () => {
    // A tap on the screen clicks in the VM; the keyboard stays open for it.
    if (VMC.keyboard) setTimeout(() => { if (VMC.keyboard && document.activeElement !== field && $("#vmcKeys") === field) field.focus({ preventScroll: true }); }, 0);
  });
  screen?.addEventListener("touchend", () => { if (VMC.keyboard) field.focus({ preventScroll: true }); });
}
window.vmConsoleKeyboard = () => {
  const field = $("#vmcKeys"), button = $("#vmcKeyboard");
  if (!field) return;
  VMC.keyboard = !VMC.keyboard;
  if (button) button.classList.toggle("pri", VMC.keyboard);
  if (VMC.keyboard) { field.focus({ preventScroll: true }); vmConsoleState("Keyboard open · Typing goes to the VM"); }
  else { field.blur(); vmConsoleState("keyboard closed"); }
};

/* ---------- the screen: pasting is typing ----------
   A VM's display has no clipboard of its own to paste into, so pasted text
   is typed key by key. Where the page may read the clipboard (HTTPS), it is
   typed at once; elsewhere a box takes the paste first. */
const KEYSYM = { "\n": 0xff0d, "\t": 0xff09, "\b": 0xff08 };
function keysymOf(ch) {
  if (KEYSYM[ch]) return KEYSYM[ch];
  const code = ch.codePointAt(0);
  return code < 0x100 ? code : 0x01000000 + code;
}

/* Which key, and whether Shift, types each character on the VM's own
   keyboard layout. A character alone is turned into a key by QEMU's US
   keymap, without Shift: "A" came out as "a", and on a UK guest "\" as "#".
   So a character is sent as the key that types it there, Shift held round
   it; one the layout has no key for goes as the character, as before. */
const VMC_ROWS = [
  ["Backquote Digit1 Digit2 Digit3 Digit4 Digit5 Digit6 Digit7 Digit8 Digit9 Digit0 Minus Equal", "`1234567890-=", "~!@#$%^&*()_+"],
  ["KeyQ KeyW KeyE KeyR KeyT KeyY KeyU KeyI KeyO KeyP BracketLeft BracketRight Backslash", "qwertyuiop[]\\", "QWERTYUIOP{}|"],
  ["KeyA KeyS KeyD KeyF KeyG KeyH KeyJ KeyK KeyL Semicolon Quote", "asdfghjkl;'", "ASDFGHJKL:\""],
  ["KeyZ KeyX KeyC KeyV KeyB KeyN KeyM Comma Period Slash", "zxcvbnm,./", "ZXCVBNM<>?"]];
// A UK keyboard: the keys whose characters differ from a US one, and its
// extra key left of Z.
const VMC_UK = { Backquote: ["`", "¬"], Digit2: ["2", "\""], Digit3: ["3", "£"], Quote: ["'", "@"],
  Backslash: ["#", "~"], IntlBackslash: ["\\", "|"] };
const VMC_LAYOUTS = { us: "US", gb: "UK" };
function vmKeyMap(layout) {
  const map = { " ": ["Space", false], "\n": ["Enter", false], "\t": ["Tab", false], "\b": ["Backspace", false] };
  const keys = {};
  for (const [codes, plain, shifted] of VMC_ROWS) codes.split(" ").forEach((code, i) => { keys[code] = [plain[i], shifted[i]]; });
  if (layout === "gb") Object.assign(keys, VMC_UK);
  for (const [code, [plain, shifted]] of Object.entries(keys)) {
    map[plain] = [code, false];
    map[shifted] = [code, true];
  }
  return map;
}
/* The key events that type one character: [keysym, code, down]. */
function vmKeyStrokes(ch, layout = vmKeyLayout()) {
  const keysym = keysymOf(ch), key = vmKeyMap(layout)[ch];
  if (!key) return [[keysym, null, true], [keysym, null, false]];
  const [code, shift] = key, press = [[keysym, code, true], [keysym, code, false]];
  return shift ? [[0xffe1, "ShiftLeft", true], ...press, [0xffe1, "ShiftLeft", false]] : press;
}
/* The VM's keyboard layout, as last chosen here; a browser set to UK
   English starts on UK. */
function vmKeyLayout() {
  if (VMC_LAYOUTS[VMC.layout]) return VMC.layout;
  try {
    const saved = localStorage.getItem("homestead.vm-keyboard");
    if (VMC_LAYOUTS[saved]) return saved;
  } catch (e) { /* storage blocked: the default */ }
  return /^en-GB\b/i.test(navigator.language || "") ? "gb" : "us";
}
window.vmKeyLayoutChanged = value => {
  try { localStorage.setItem("homestead.vm-keyboard", value); } catch (e) { /* kept for this page only */ }
  VMC.layout = value;
};

/* Typed a little at a time, so a long paste does not flood the VM's
   keyboard and lose keys. */
function vmConsoleTypeText(text, enter = false) {
  const rfb = VMC.rfb;
  if (!rfb) return toast("The screen is not connected", "bad");
  const chars = [...String(text).replace(/\r\n?/g, "\n")];
  if (enter) chars.push("\n");
  if (!chars.length) return;
  const layout = vmKeyLayout();
  let at = 0;
  const step = () => {
    if (VMC.rfb !== rfb) return;
    for (const end = Math.min(chars.length, at + 40); at < end; at++) {
      for (const [keysym, code, down] of vmKeyStrokes(chars[at], layout)) rfb.sendKey(keysym, code, down);
    }
    if (at < chars.length) {
      vmConsoleState(`typing · ${at} of ${chars.length}`);
      setTimeout(step, 30);
    } else {
      vmConsoleState(`typed ${chars.length} key${chars.length === 1 ? "" : "s"}`);
      rfb.focus({ preventScroll: true });
    }
  };
  step();
}

window.vmConsolePaste = async () => {
  const text = await readClipboard();
  if (text) return vmConsoleTypeText(text);
  const panel = $("#vmcPaste");
  if (!panel) return;
  panel.hidden = false;
  $("#vmcPasteText").focus();
};
window.vmConsoleTypePasted = () => {
  const text = $("#vmcPasteText").value;
  if (!text) return toast("Paste something first", "bad");
  vmConsoleTypeText(text, $("#vmcPasteEnter").checked);
  vmConsolePasteClose();
};
window.vmConsolePasteClose = () => {
  const panel = $("#vmcPaste");
  if (panel) { panel.hidden = true; $("#vmcPasteText").value = ""; }
  VMC.rfb?.focus({ preventScroll: true });
};
window.vmConsoleCopyGuest = async () => {
  if (!VMC.guestClip) return toast("The VM has not shared anything to copy. For text, the Serial tab can be selected and copied.", "warn");
  toast(await copyText(VMC.guestClip) ? "Copied from the VM" : "The browser would not copy it", "ok");
};

/* ---------- serial: plain text both ways ---------- */
window.vmSerialCopy = async () => {
  const view = $("#consoleView");
  if (!view) return;
  const selected = consoleSelection();
  const text = selected || view.textContent;
  toast(await copyText(text) ? (selected ? "Selection copied" : "All the output copied") : "The browser would not copy it", "ok");
};
/* Straight to the port, as a terminal pastes: each line ends in Enter. */
window.vmSerialPaste = async () => {
  const socket = VMC.serial;
  if (!socket || socket.readyState !== WebSocket.OPEN) return toast("The serial port is not connected", "bad");
  const text = await readClipboard();
  if (text === null) {
    $("#consoleInput")?.focus();
    return toast("This page is not allowed to read the clipboard. Paste into the box below with Ctrl+V, then choose Send.", "warn");
  }
  socket.send(JSON.stringify({ type: "input", data: text.replace(/\r\n?|\n/g, "\r") }));
};
document.addEventListener("keydown", event => {
  if (!event.ctrlKey || !event.shiftKey || !$("#consoleView") || VMC.kind !== "serial") return;
  if (!event.target?.closest?.("#consoleView, #consoleInput")) return;
  const key = event.key.toLowerCase();
  if (key === "c") { event.preventDefault(); vmSerialCopy(); }
  else if (key === "v" && event.target.id !== "consoleInput") { event.preventDefault(); vmSerialPaste(); }
});
