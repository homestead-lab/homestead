/* A VM's screen and serial port.

   Screen is VNC: noVNC (vendored, loaded only when a console opens) speaks
   RFB to KubeVirt through Homestead's proxy. Serial is the VM's first serial
   port in the same terminal view as container consoles - the one to use for
   a VM that boots without a display, or when the screen is stuck. */

const VMC = { rfb: null, socket: null, watch: 0, ns: "", name: "", kind: "vnc" };

function vmConsoleUrl(kind) {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${location.host}/api/vm/console?${new URLSearchParams({ ns: VMC.ns, vm: VMC.name, kind })}`;
}

function vmConsoleStop() {
  if (VMC.rfb) { try { VMC.rfb.disconnect(); } catch (e) { /* already gone */ } VMC.rfb = null; }
  if (VMC.socket) { try { VMC.socket.close(); } catch (e) { /* already gone */ } VMC.socket = null; }
  if (window.__consoleSocket && window.__consoleSocket === VMC.serial) window.__consoleSocket = null;
}

function vmConsoleState(text) { const el = $("#vmcState"); if (el) el.textContent = text; }

window.vmConsole = (ns, name, kind = "vnc") => {
  vmConsoleStop();
  Object.assign(VMC, { ns, name, kind });
  modal("Console · " + name, `<div class="vmc-bar">
      <div class="seg" role="tablist">
        <button class="${kind === "vnc" ? "on" : ""}" onclick="vmConsole('${esc(ns)}','${esc(name)}','vnc')">Screen</button>
        <button class="${kind === "serial" ? "on" : ""}" onclick="vmConsole('${esc(ns)}','${esc(name)}','serial')">Serial</button></div>
      <span class="dim xs" id="vmcState">connecting…</span>
      ${kind === "vnc" ? `<span class="vmc-tools">
        <button class="btn sm" onclick="vmConsoleKeys()" title="Send Ctrl+Alt+Del to the VM">Ctrl+Alt+Del</button>
        <button class="btn sm" onclick="vmConsolePaste()" title="Paste text into the VM - it is typed key by key (Ctrl+Shift+V on the screen)">${icon("copy")}Paste</button>
        <button class="btn sm" id="vmcCopy" hidden onclick="vmConsoleCopyGuest()" title="Copy what the VM last put on its clipboard (Ctrl+Shift+C)">Copy from VM</button>
        <button class="btn sm" onclick="vmConsoleFull()" title="Fill the screen">${icon("ext")}Full screen</button></span>`
      : `<span class="vmc-tools">
        <button class="btn sm" onclick="vmSerialCopy()" title="Copy the selected output, or all of it (Ctrl+Shift+C)">${icon("copy")}Copy</button>
        <button class="btn sm" onclick="vmSerialPaste()" title="Send the clipboard to the serial port (Ctrl+Shift+V)">Paste</button></span>`}
    </div>
    <div class="console-security">Operator-only · session start and stop are audited; what is typed and shown is not recorded.</div>
    ${kind === "vnc" ? `<div class="vmc-paste" id="vmcPaste" hidden>
        <textarea id="vmcPasteText" rows="3" spellcheck="false" autocomplete="off"
          placeholder="Paste here (Ctrl+V), then Type it: the VM has no shared clipboard, so it is typed key by key"></textarea>
        <div class="row" style="gap:10px;margin-top:8px">
          <button class="btn sm pri" onclick="vmConsoleTypePasted()">Type into the VM</button>
          <label class="switch" style="margin:0"><input type="checkbox" id="vmcPasteEnter"> <span>Press Enter after</span></label>
          <button class="btn sm" onclick="vmConsolePasteClose()">Cancel</button></div></div>
      <div class="vmc-screen" id="vmcScreen" tabindex="0"></div>`
      : `<pre class="consoleview" id="consoleView" tabindex="0" aria-label="Serial console output">Waiting for the serial port… press Enter below if it stays quiet: a login prompt only appears after a key.</pre>
        <div class="consoleinput"><textarea id="consoleInput" rows="1" spellcheck="false" autocomplete="off" placeholder="Type · Enter sends · Shift+Enter adds a line"></textarea>
        <button class="btn" onclick="consoleSend()">Send</button></div>`}`, true);
  clearInterval(VMC.watch);
  // Closing the dialog ends the session; nothing else would.
  VMC.watch = setInterval(() => { if ($("#modal").classList.contains("hidden")) { clearInterval(VMC.watch); vmConsoleStop(); } }, 1000);
  if (kind === "vnc") vmConsoleScreen(); else vmConsoleSerial();
};

async function vmConsoleScreen() {
  let RFB;
  try { RFB = (await import("/vendor/novnc/core/rfb.js")).default; }
  catch (e) { return vmConsoleState("the VNC client could not load: " + e.message); }
  const target = $("#vmcScreen");
  if (!target) return;
  const rfb = new RFB(target, vmConsoleUrl("vnc"), { wsProtocols: ["binary"] });
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.focusOnClick = true;
  VMC.rfb = rfb;
  rfb.addEventListener("connect", () => { vmConsoleState("connected · click the screen to type"); rfb.focus({ preventScroll: true }); });
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
  socket.onopen = () => { vmConsoleState("connected · serial port 1"); $("#consoleInput")?.focus(); };
  socket.onmessage = event => {
    let message;
    try { message = JSON.parse(event.data); } catch (_) { return; }
    // A terminal's colour and cursor codes read as noise in a plain view.
    if (message.type === "output") consoleWrite(String(message.data).replace(/\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\r(?!\n)/g, ""));
  };
  socket.onclose = () => { if (VMC.socket === socket) vmConsoleState("disconnected"); };
  socket.onerror = () => { if (VMC.socket === socket) vmConsoleState("unavailable · check the VM is running and your role"); };
}

window.vmConsoleKeys = () => { if (VMC.rfb) { VMC.rfb.sendCtrlAltDel(); VMC.rfb.focus({ preventScroll: true }); } };
window.vmConsoleFull = () => { const el = $("#vmcScreen"); if (el && el.requestFullscreen) el.requestFullscreen().catch(() => {}); };

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

/* Typed a little at a time, so a long paste does not flood the VM's
   keyboard and lose keys. */
function vmConsoleTypeText(text, enter = false) {
  const rfb = VMC.rfb;
  if (!rfb) return toast("The screen is not connected", "bad");
  const keys = [...String(text).replace(/\r\n?/g, "\n")].map(keysymOf);
  if (enter) keys.push(0xff0d);
  if (!keys.length) return;
  let at = 0;
  const step = () => {
    if (VMC.rfb !== rfb) return;
    for (const end = Math.min(keys.length, at + 40); at < end; at++) rfb.sendKey(keys[at], null);
    if (at < keys.length) {
      vmConsoleState(`typing · ${at} of ${keys.length}`);
      setTimeout(step, 30);
    } else {
      vmConsoleState(`typed ${keys.length} key${keys.length === 1 ? "" : "s"}`);
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
    return toast("This page may not read the clipboard - paste into the box below with Ctrl+V, then Send", "warn");
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
