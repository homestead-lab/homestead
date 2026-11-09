/* Homestead Containers: the interactive container console (#911). One of the scripts views-workloads.js was split into; they share the page's globals and load in the order index.html lists them. */
/* ---------------- interactive container console ---------------- */
window.wlConsole = (ns, name, fromRoute = false) => {
  const workload = (STATE.data.wl || []).find(x => x.ns === ns && x.name === name);
  if (!workload) return toast("App details are not available yet", "bad");
  const pods = (workload.pods || []).filter(p => p.phase === "Running" && (p.containers || []).some(c => c.kind === "app"));
  if (!fromRoute && window.setModalRoute) setModalRoute({ panel: "console", ns, workload: name }, name + " console");
  window.__consoleWorkload = workload;
  modal("Console · " + name, `<div class="consolebar">
      <div class="f"><label>Pod</label><select id="consolePod" onchange="consolePodChanged()">
        ${pods.map(p => `<option value="${esc(p.name)}">${esc(p.name)} · ${esc(p.node || "unscheduled")}</option>`).join("")}
      </select></div>
      <div class="f"><label>Container</label><select id="consoleContainer"></select></div>
      <div class="f"><label>Shell ${tip("Automatic tries /bin/sh, /bin/bash, then /bin/ash. Select one explicitly if the image uses a known shell.")}</label><select id="consoleShell">
        <option value="auto">automatic</option><option value="/bin/sh">/bin/sh</option><option value="/bin/bash">/bin/bash</option><option value="/bin/ash">/bin/ash</option>
      </select></div>
      <button class="btn pri" id="consoleConnect" onclick="consoleConnect()">Connect</button>
    </div>
    ${pods.length ? `<div class="console-security">Operators only · The start and end of each session are recorded, but commands and output are not.</div>
      <div class="consolestate" id="consoleState">not connected · choose a pod and container, then connect</div>
      <div class="nodeterm" id="consoleTerm" aria-label="Container terminal"></div>`
      : `<div class="empty"><b>No running pod with an application container</b><br><span class="dim">Start the app and wait until its pod is running.</span></div>`}`, true);
  // What is typed into the shell is not an edit to this dialog: closing it asks nothing.
  window.__modalGuard = () => "";
  if (pods.length) consolePodChanged();
};

window.consolePodChanged = () => {
  const podName = $("#consolePod")?.value;
  const pod = (window.__consoleWorkload?.pods || []).find(p => p.name === podName);
  const select = $("#consoleContainer");
  if (!select) return;
  select.innerHTML = (pod?.containers || []).filter(c => c.kind === "app")
    .map(c => `<option value="${esc(c.name)}">${esc(c.name)} · ${esc(c.state || "unknown")}</option>`).join("");
};

function consoleWrite(data, stream = "stdout") {
  const view = $("#consoleView");
  if (!view) return;
  if (view.dataset.empty !== "0") { view.textContent = ""; view.dataset.empty = "0"; }
  const span = document.createElement("span");
  span.className = stream === "stderr" ? "console-stderr" : "";
  span.textContent = data;
  view.appendChild(span);
  if (view.textContent.length > 250000) view.removeChild(view.firstChild);
  view.scrollTop = view.scrollHeight;
}

function consoleSize() {
  const view = $("#consoleView");
  if (!view) return { cols: 80, rows: 24 };
  return { cols: Math.max(20, Math.floor(view.clientWidth / 8.2)), rows: Math.max(5, Math.floor(view.clientHeight / 18)) };
}

/* A container's shell in a real terminal, as a host's (nodeShell): what the
   shell sends to drive the screen - colours, bracketed paste, a prompt
   redrawn - is drawn, not printed, and keys go straight to it. */
window.consoleConnect = async (attempt = 0) => {
  if (window.__consoleSocket) window.__consoleSocket.close();
  const pod = $("#consolePod")?.value, container = $("#consoleContainer")?.value;
  if (!pod || !container) return toast("Choose a running pod and container", "bad");
  const shells = ["/bin/sh", "/bin/bash", "/bin/ash"];
  const selected = $("#consoleShell").value;
  const shell = selected === "auto" ? shells[Math.min(attempt, shells.length - 1)] : selected;
  const ns = window.__consoleWorkload.ns;
  const state = text => { const el = $("#consoleState"); if (el) el.textContent = text; };
  state(`connecting · ${shell}`);
  $("#consoleConnect").textContent = "Reconnect";
  try { await loadXterm(); } catch (e) { return state(e.message); }
  const host = $("#consoleTerm");
  if (!host) return;
  let term = window.__consoleTerm;
  if (!term || !host.contains(term.element)) {
    if (term) { try { term.dispose(); } catch (_) { /* gone with its dialog */ } }
    const css = getComputedStyle(document.documentElement);
    term = new Terminal({ cursorBlink: true, fontSize: 13, scrollback: 5000, convertEol: false,
      fontFamily: css.getPropertyValue("--mono").trim() || "ui-monospace, Menlo, Consolas, monospace",
      theme: { background: "#0b0b0d", foreground: "#e8e8ea" } });
    term.__fit = new FitAddon.FitAddon();
    term.loadAddon(term.__fit);
    term.open(host);
    // Ctrl+C with text selected copies it, as in a terminal; only without a
    // selection does it reach the shell as an interrupt.
    term.attachCustomKeyEventHandler(event => {
      if (event.type === "keydown" && event.ctrlKey && !event.shiftKey && event.key.toLowerCase() === "c" && term.hasSelection()) {
        navigator.clipboard?.writeText(term.getSelection()).catch(() => {});
        return false;
      }
      return true;
    });
    term.onData(data => {
      const socket = window.__consoleSocket;
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "input", data }));
    });
    window.__consoleTerm = term;
  }
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const query = new URLSearchParams({ ns, pod, container, shell });
  const socket = new WebSocket(fleetSocketUrl(`${protocol}//${location.host}/api/console?${query}`));
  window.__consoleSocket = socket;
  window.__consoleAttempt = attempt;
  const resize = () => {
    try { term.__fit.fit(); } catch (_) { /* not laid out yet */ }
    if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  };
  socket.onopen = () => { state(`connected · ${shell}`); resize(); term.focus(); };
  socket.onmessage = event => {
    let message;
    try { message = JSON.parse(event.data); } catch (_) { return; }
    if (message.type === "output") term.write(message.data);
    else if (message.type === "error") {
      const canFallback = selected === "auto" && attempt < shells.length - 1 && /not found|executable|no such file/i.test(message.data || "");
      if (canFallback) { term.write(`\r\n\x1b[33m${shell} unavailable. Trying ${shells[attempt + 1]}…\x1b[0m\r\n`); return consoleConnect(attempt + 1); }
      term.write(`\r\n\x1b[31m${message.data || "Console error"}\x1b[0m\r\n`);
    } else if (message.type === "disconnected") state(`disconnected · ${message.reason || "session ended"}`);
  };
  socket.onclose = () => { if (window.__consoleSocket === socket) state("disconnected · use Reconnect to try again"); };
  socket.onerror = () => { if (window.__consoleSocket === socket) state("connection unavailable · check role and pod state"); };
  if (window.__consoleResize) window.__consoleResize.disconnect();
  window.__consoleResize = new ResizeObserver(resize);
  window.__consoleResize.observe(host);
};

/* Text selected in the console's input or its output, if any. */
function consoleSelection() {
  const input = $("#consoleInput");
  if (input && document.activeElement === input && input.selectionStart !== input.selectionEnd) {
    return input.value.slice(input.selectionStart, input.selectionEnd);
  }
  const selection = window.getSelection(), view = $("#consoleView");
  return selection && view && !selection.isCollapsed && view.contains(selection.anchorNode) ? selection.toString() : "";
}
window.consoleSend = () => {
  const input = $("#consoleInput"), socket = window.__consoleSocket;
  if (!input || !socket || socket.readyState !== WebSocket.OPEN) return toast("Connect the console first", "bad");
  socket.send(JSON.stringify({ type: "input", data: input.value + "\n" }));
  input.value = "";
};
document.addEventListener("keydown", event => {
  if (event.target?.id !== "consoleInput") return;
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); consoleSend(); }
  // Ctrl+C with text selected copies it, as in a terminal; only without a
  // selection does it interrupt.
  else if (event.key.toLowerCase() === "c" && event.ctrlKey && !event.shiftKey && !consoleSelection()
      && window.__consoleSocket?.readyState === WebSocket.OPEN) {
    event.preventDefault(); window.__consoleSocket.send(JSON.stringify({ type: "input", data: "\u0003" }));
  }
});


// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  consoleWrite, consoleSize, consoleSelection
};
