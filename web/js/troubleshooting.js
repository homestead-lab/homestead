/* Opt-in diagnostics. Control identity comes from code and structure, never
   textContent, field values, URLs with queries, or a snapshot of the DOM. */
(() => {
  const storeKey = "homestead.diagnostic.recording";
  let active = null, queue = [], batch = 0, sending = null, stopped = false, trouble = "";
  let reports = [], current = null, format = "anonymised", interrupted = "", restored = false;
  let total = 0, previewGeneration = 0, stopping = false;
  const post = (path, data, keepalive = false) => api(`/api/diagnostics/${path}`, {
    method: "POST", keep: true, keepalive, headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  });
  const ignored = el => !el?.closest || el.closest("[data-diagnostic-ignore],.monaco-editor,.xterm,[contenteditable],input[type=password],#gate");
  function target(el) {
    const parts = [];
    for (let node = el; node?.tagName && parts.length < 6; node = node.parentElement) {
      const siblings = [...(node.parentElement?.children || [])].filter(x => x.tagName === node.tagName);
      parts.unshift(`${node.tagName.toLowerCase()}:${siblings.indexOf(node) + 1}`);
    }
    return parts.join("/").slice(0, 180);
  }
  function action(el) {
    const handler = el?.getAttribute?.("onclick") || el?.getAttribute?.("onchange") || "";
    return (handler.match(/\b[A-Za-z_$][\w$]*(?=\s*\()/g) || []).filter(name => !["if", "preventDefault", "stopPropagation", "closest"].includes(name))[0] || el?.tagName?.toLowerCase() || "control";
  }
  function note(kind, detail = {}) {
    if (!active || stopped || !window.can?.("admin")) return;
    if (total >= 10000 || queue.length >= 500) {
      trouble = "Recording limit reached. Stop to save the captured events.";
      stopped = true; banner(); return;
    }
    total++;
    queue.push({ kind, at: Math.round(performance.now() - active.localStarted), page: location.pathname, ...detail });
  }
  async function flush() {
    if (!active) return;
    if (sending) return sending;
    const recording = active, events = queue.slice(0, 100), number = batch + 1;
    sending = post("events", { id: recording.id, batch: number, events }).then(result => {
      if (active !== recording) return;
      queue.splice(0, events.length); batch = result.batch; trouble = "";
      if (result.truncated || result.status === "interrupted") { stopped = true; trouble = "Recording limit reached. Review the captured events."; }
    }).catch(error => { trouble = "Events are waiting to save. " + error.message; throw error; })
      .finally(() => { sending = null; banner(); });
    return sending;
  }
  function banner() {
    const host = $("#diagnosticRecorder"); if (!host) return;
    host.classList.toggle("hidden", !active && !interrupted);
    if (interrupted) {
      host.innerHTML = `<div><b>Recording interrupted</b><span>Saved events are available. Events not yet sent may be missing.</span></div><button class="btn sm" onclick="bugRecover()">Review</button>`;
    } else if (active) {
      const elapsed = Math.max(0, Math.floor((performance.now() - active.localStarted) / 1000));
      host.innerHTML = `<div><b>${stopped ? "Recording stopped" : "Recording UI actions"} · ${Math.floor(elapsed / 60)}:${String(elapsed % 60).padStart(2, "0")}</b><span>${esc(trouble || `${total} events · this tab · stops after 10 minutes`)}</span></div><button class="btn sm" onclick="bugStop()">${stopped ? "Save and review" : "Stop recording"}</button>`;
    }
  }
  window.HomesteadRecorder = {
    note,
    request(path, opts = {}) {
      if (!active || stopped || !path.startsWith("/api/") || path.startsWith("/api/diagnostics") || path.startsWith("/api/auth/")) return null;
      const id = [...crypto.getRandomValues(new Uint8Array(16))].map(value => value.toString(16).padStart(2, "0")).join("");
      const started = performance.now(), recording = active;
      const local = !opts?.headers?.["X-Homestead-Cluster"];
      note("request", { action: "started", path: path.split("?")[0], method: opts?.method || "GET", request: id });
      return { headers: local ? { "X-Homestead-Report": active.id, "X-Homestead-Request": id } : {},
        done(status, operation) {
          if (active !== recording) return;
          note("request", { action: "finished", path: path.split("?")[0], method: opts?.method || "GET", status,
            duration: Math.round(performance.now() - started), request: id,
            ...(typeof operation === "string" ? { operation: operation.slice(0, 100) } : {}) });
        } };
    },
  };
  for (const kind of ["click", "change", "focusin", "toggle"]) document.addEventListener(kind, event => {
    if (!active || ignored(event.target)) return;
    const el = event.target.closest("button,a,select,input,textarea,summary,details,[role=tab],[onclick],[onchange],.sortable,.iconbtn,[data-view]");
    if (!el) return;
    note(kind === "focusin" ? "focus" : kind, { target: target(el), action: action(el),
      ...(el.type === "checkbox" || el.type === "radio" ? { checked: el.checked } : {}),
      ...(el.tagName === "DETAILS" ? { expanded: el.open } : {}) });
  }, true);
  document.addEventListener("keydown", event => {
    if (ignored(event.target) || event.target.closest("input,textarea,select")) return;
    if (["Escape", "Enter", "Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key))
      note("key", { key: event.key, target: target(event.target) });
    else if ((event.ctrlKey || event.metaKey) && /^[a-z]$/i.test(event.key))
      note("key", { key: `${event.ctrlKey ? "Ctrl" : "Meta"}+${event.shiftKey ? "Shift+" : ""}${event.key.toUpperCase()}` });
  }, true);
  let scrollAt = 0;
  document.addEventListener("scroll", event => {
    if (!active || Date.now() - scrollAt < 400 || event.target !== document && ignored(event.target)) return;
    scrollAt = Date.now(); const el = event.target === document ? document.scrollingElement : event.target;
    note("scroll", { target: target(el), x: Math.round(el.scrollLeft || 0), y: Math.round(el.scrollTop || 0) });
  }, true);
  const viewport = () => note("viewport", { width: innerWidth, height: innerHeight,
    scale: window.visualViewport?.scale || devicePixelRatio, theme: document.documentElement.dataset.theme || "system" });
  window.addEventListener("resize", viewport);
  window.addEventListener("error", event => note("error", { message: String(event.message || "Resource failed to load").slice(0, 500), line: event.lineno || 0, column: event.colno || 0 }));
  window.addEventListener("unhandledrejection", event => note("rejection", { message: String(event.reason?.message || "Unhandled promise rejection").slice(0, 500) }));
  window.addEventListener("pagehide", () => {
    if (!active) return;
    note("lifecycle", { action: "page hidden or reloaded" });
    if (!sending) {
      // Keep the batch number unchanged until acknowledged; retries are idempotent.
      post("events", { id: active.id, batch: batch + 1, events: queue.slice(0, 100) }, true).catch(() => {});
    }
  });
  let uiState = "", uiTimer;
  new MutationObserver(() => {
    if (!active || uiTimer) return;
    uiTimer = setTimeout(() => {
      uiTimer = null;
      const state = { action: "visible UI state", expanded: !$("#modal")?.classList.contains("hidden"),
        message: `loading:${$$("#views .spin2,#mbody .spin2").length};errors:${$$("#views .bad,#mbody .ui-callout.bad").length};invalid:${$$("[aria-invalid=true]").length}` };
      const next = JSON.stringify(state); if (next !== uiState) { uiState = next; note("ui", state); }
    }, 300);
  }).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["class", "aria-invalid", "aria-busy", "disabled"] });

  setInterval(() => {
    if (!window.can?.("admin")) {
      if (active) { active = null; queue = []; interrupted = ""; try { sessionStorage.removeItem(storeKey); } catch (_) {} banner(); }
      return;
    }
    if (!restored) {
      restored = true;
      try { interrupted = sessionStorage.getItem(storeKey) || ""; } catch (_) { /* storage may be disabled */ }
      banner();
    }
    if (active && !stopped && performance.now() - active.localStarted >= 600000) { bugStop(); return; }
    if (active && !stopped) flush().catch(() => {});
    banner();
  }, 5000);

  const fullWarning = () => UI.callout("warn", "Full logs may contain sensitive information", "Original names, addresses and collected log text are retained. Keep this download private. GitHub always uses an anonymised draft.");
  const choices = (id, change = "") => UI.field("Download format", `<select id="${id}" aria-label="Download format" ${change ? `onchange="${change}"` : ""}><option value="anonymised">Anonymised · recommended for sharing</option><option value="full" ${format === "full" ? "selected" : ""}>Full · keep private</option></select>`);
  const wrap = html => `<div class="ui-stack" data-diagnostic-ignore>${html}</div>`;
  const fail = error => toast(error.message, "bad");
  window.troubleshootingCards = () => `<section class="card flat settings-wide" data-tab="troubleshooting"><div class="settings-card-head"><div><div class="ctitle">Bug reports</div><div class="csub">Record UI actions and diagnostics while you reproduce a problem.</div></div><button class="btn sm pri" data-need="admin" onclick="bugStart()">Record a bug</button></div>
    ${UI.guide("What gets recorded", "Clicks, navigation, focus, scrolling, shortcuts, UI states, request results and browser errors in this tab. Form values, password input, terminal contents and screen video are excluded. Original service logs may contain sensitive data. Saved reports expire after 24 hours.")}
    <div id="diagnosticReports">${can("admin") ? '<div class="empty small">Loading reports…</div>' : '<div class="empty small">An administrator can record bugs and download logs.</div>'}</div></section>
    <section class="card flat settings-wide" data-tab="troubleshooting"><div class="settings-card-head"><div><div class="ctitle">Logs package</div><div class="csub">Download anonymised or full diagnostics without recording a bug.</div></div><button class="btn sm" data-need="admin" onclick="bugPackage()">Download logs</button></div></section>`;
  window.troubleshootingPaint = async () => {
    const host = $("#diagnosticReports"); if (!host || !can("admin")) return;
    try {
      reports = await api("/api/diagnostics", { keep: true });
      if (!host.isConnected) return;
      host.innerHTML = UI.table([{ label: "Report" }, { label: "State" }, { label: "Actions" }], reports.map(row => [
        `<b>${esc(row.title)}</b><div class="dim xs">${esc(new Date(row.created * 1000).toLocaleString())} · ${row.events} events</div>`,
        esc(row.status), actionBar([{ label: "Review", run: `bugReview(${jsq(row.id)})` }, { label: "Delete", danger: true, run: `bugDelete(${jsq(row.id)})` }], { shown: 1 }),
      ]), { empty: "No saved reports. Record a bug or collect a logs package." });
    } catch (error) { host.textContent = "Could not load saved reports. " + error.message; }
  };
  window.bugStart = () => {
    if (active) return toast("A recording is already active in this tab. Use Stop recording to review it.");
    modal("Record a bug", wrap(UI.lead("Start recording, reproduce the problem, then stop and add a comment.") +
      UI.facts([["Scope", "This tab · this cluster"], ["Limit", "10 minutes · 10,000 events"]]) +
      UI.more("Privacy and limits", "Records control identities and UI state, not field values or video. Private originals expire after 24 hours. Switching clusters or reloading interrupts the recording; saved events remain available.") +
      UI.actions(UI.cancel() + UI.button("Start recording", "bugRecord(this)", { kind: "pri" }))));
  };
  window.bugRecord = async button => {
    if (active) return; button.disabled = true;
    try {
      active = await post("start", {}); queue = []; total = 0; batch = 0; stopped = false; trouble = ""; interrupted = ""; restored = true;
      active.localStarted = performance.now();
      try { sessionStorage.setItem(storeKey, active.id); } catch (_) { /* server copy remains */ }
      closeModal(); note("lifecycle", { action: "recording started" }); viewport(); banner(); troubleshootingPaint();
    } catch (error) { button.disabled = false; fail(error); }
  };
  window.bugStop = async () => {
    if (!active || stopping) return;
    stopping = true;
    if (!stopped) note("lifecycle", { action: "recording stopped" }); stopped = true; banner();
    try {
      await flush(); while (queue.length) await flush();
      const id = active.id; await post("stop", { id }); active = null;
      try { sessionStorage.removeItem(storeKey); } catch (_) { /* server copy remains */ }
      banner(); await bugDescribe(id); troubleshootingPaint();
    } catch (error) { trouble = "Could not save all events. Use Save and review to retry. " + error.message; banner(); }
    finally { stopping = false; }
  };
  window.bugRecover = async () => {
    try { const id = interrupted; await post("stop", { id }); interrupted = ""; sessionStorage.removeItem(storeKey); banner(); await bugDescribe(id); }
    catch (error) { interrupted = ""; sessionStorage.removeItem(storeKey); banner(); fail(error); }
  };
  window.bugDescribe = async id => {
    try {
      current = await api(`/api/diagnostics/report?id=${encodeURIComponent(id)}&format=full`, { keep: true });
      modal("Finish bug report", wrap(UI.lead("Recording stopped. Describe what happened and what you expected.") +
        UI.fields(UI.field("Summary", `<input id="bugTitle" aria-label="Summary" maxlength="120" value="${esc(current.title)}">`, { wide: true }),
          UI.field("Comment", `<textarea id="bugComment" aria-label="Comment" maxlength="4000" rows="4">${esc(current.comment)}</textarea>`, { wide: true })) +
        bugSources() + UI.more("Saved privately", "Your comment is saved with the report. Anonymised exports remove detected secrets and replace known identifiers. Review the exact draft before publishing anything.") +
        UI.actions(UI.button("Keep draft", "bugSaveDraft(this)") + UI.button("Prepare report", "bugPrepare(this)", { kind: "pri" }))));
    } catch (error) { fail(error); }
  };
  function bugSources() {
    return UI.more("Include diagnostics", `<label class="switch"><input type="checkbox" id="bugService" checked> Homestead service logs</label><label class="switch"><input type="checkbox" id="bugCluster" checked> Cluster events and health</label><label class="switch"><input type="checkbox" id="bugJobs" checked> Related job summaries</label>`);
  }
  window.bugSaveDraft = async button => {
    button.disabled = true;
    try { await post("draft", { id: current.id, title: $("#bugTitle").value, comment: $("#bugComment").value }); closeModal(); troubleshootingPaint(); }
    catch (error) { button.disabled = false; fail(error); }
  };
  window.bugPrepare = async button => {
    button.disabled = true; const id = current.id;
    try {
      await post("prepare", { id, title: $("#bugTitle")?.value || current.title, comment: $("#bugComment")?.value || current.comment,
        sources: [$("#bugService")?.checked && "service", $("#bugCluster")?.checked && "cluster", $("#bugJobs")?.checked && "jobs"].filter(Boolean),
        seconds: Number($("#bugRange")?.value || 900) });
      format = $("#bugPackageFormat")?.value || "anonymised";
      await bugReview(id, format); troubleshootingPaint();
    } catch (error) { button.disabled = false; fail(error); }
  };
  window.bugReview = async (id, selected = "anonymised") => {
    if (active?.id === id) return bugStop();
    const generation = ++previewGeneration; format = selected;
    $$("#mbody [data-diagnostic-ignore] button").forEach(button => { button.disabled = true; });
    try {
      const record = await api(`/api/diagnostics/report?id=${encodeURIComponent(id)}&format=${encodeURIComponent(format)}`, { keep: true });
      if (generation !== previewGeneration) return;
      current = record;
      if (record.status !== "ready") { await post("stop", { id }); return bugDescribe(id); }
      modal("Review bug report", wrap(UI.lead(esc(record.title)) + choices("bugFormat", "bugReviewFormat(this.value)") +
        (format === "full" ? fullWarning() : UI.more("Anonymisation", "Detected credentials are removed and known names and addresses replaced with consistent aliases. No alias mapping is included. Automatic checks cannot identify every secret in arbitrary text; review before sharing.")) +
        UI.facts([["Events", String(record.events.length)], ["Saved until", esc(new Date(record.expires * 1000).toLocaleString())]]) +
        UI.more("Report preview", `<pre class="diagnostic-preview">${esc(record.comment + "\n\n" + record.events.slice(0, 80).map(row => JSON.stringify(row)).join("\n"))}</pre><p class="dim xs">First 80 events. Downloads include all captured events and selected sources.</p>`) +
        UI.more("Collection results", UI.table([{ label: "Source" }, { label: "State" }], record.manifest.map(row => [esc(row.source), esc(row.state)]))) +
        (record.truncated ? '<p class="warn">The recording reached its limit. Some events are missing.</p>' : "") +
        UI.actions(UI.cancel() + UI.button("Download log", "bugDownload(false)") + UI.button("Download package", "bugDownload(true)") + UI.button("GitHub issue…", "bugIssue()", { kind: "pri" }))));
    } catch (error) {
      if (generation !== previewGeneration) return;
      modal("Report needs attention", wrap(UI.callout("bad", "Could not prepare this preview", esc(error.message)) +
        UI.actions(UI.cancel() + UI.button("Retry", `bugReview(${jsArg(id)})`) + UI.button("Review full log", `bugReview(${jsArg(id)},'full')`))));
    }
  };
  window.bugReviewFormat = selected => bugReview(current.id, selected);
  window.bugPackage = async () => {
    format = "anonymised";
    modal("Download logs package", wrap(UI.lead("Collect diagnostics without recording a bug. Choose an anonymised or full download.") + choices("bugPackageFormat", "bugPackageWarning(this.value)") +
      '<div id="bugFullWarning"></div>' + UI.field("Time range", '<select id="bugRange" aria-label="Time range"><option value="900">Last 15 minutes</option><option value="3600">Last hour</option><option value="86400">Last 24 hours</option></select>') +
      bugSources() + UI.more("Package contents", "Readable report, UI events when recorded, selected logs, platform health and a collection manifest. Missing or truncated sources are listed. All saved copies expire after 24 hours.") +
      UI.actions(UI.cancel() + UI.button("Prepare package", "bugPackagePrepare(this)", { kind: "pri" }))));
  };
  window.bugPackageWarning = value => { $("#bugFullWarning").innerHTML = value === "full" ? fullWarning() : ""; };
  window.bugPackagePrepare = async button => {
    button.disabled = true;
    try { current = await post("start", { package: true }); await bugPrepare(button); }
    catch (error) { button.disabled = false; fail(error); }
  };
  window.bugDownload = async packageFile => {
    try {
      const response = await fetch(`/api/diagnostics/download?id=${encodeURIComponent(current.id)}&format=${format}&package=${packageFile ? 1 : 0}`);
      if (!response.ok) throw new Error((await response.json()).error || "Download failed");
      const blob = await response.blob(), url = URL.createObjectURL(blob), link = document.createElement("a");
      link.href = url; link.download = `homestead-diagnostics-${current.id.slice(0, 8)}-${format}.${packageFile ? "zip" : "log"}`;
      document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 10000);
    } catch (error) { fail(error); }
  };
  window.bugIssue = async () => {
    try {
      const draft = await api(`/api/diagnostics/issue?id=${encodeURIComponent(current.id)}`, { keep: true });
      modal("Review public GitHub issue", wrap(UI.lead("Destination: wjcloudy/homestead") +
        UI.callout("warn", "This issue will be public", "Review the exact anonymised title and body below. Full logs are never included. Packages are not attached automatically.") +
        UI.field("Issue title", `<input aria-label="Issue title" value="${esc(draft.title)}" readonly>`) +
        `<pre class="diagnostic-preview">${esc(draft.body)}</pre>` +
        (draft.comment_shortened ? '<p class="dim xs">The comment was shortened to fit a GitHub draft link. The downloaded report keeps the full comment.</p>' : "") +
        UI.ack("bugShareAck", "I reviewed this draft for information I do not want to publish.", { onchange: "document.querySelector('#bugGitHub').disabled=!this.checked" }) +
        UI.actions(UI.button("Back", `bugReview(${jsArg(current.id)})`) + UI.button("Continue on GitHub", `window.open(${jsArg(draft.url)},'_blank','noopener,noreferrer')`, { kind: "pri", id: "bugGitHub", attrs: "disabled" }))));
    } catch (error) { fail(error); }
  };
  window.bugDelete = id => modal("Delete bug report", wrap(UI.lead("Deletes this saved report and its original logs from Homestead. Downloads already saved to your device are kept.") +
    UI.actions(UI.cancel() + UI.button("Delete report", `bugDeleteConfirm(${jsArg(id)})`, { kind: "danger" }))));
  window.bugDeleteConfirm = async id => {
    try { await post("delete", { id }); closeModal(); troubleshootingPaint(); }
    catch (error) { fail(error); }
  };
})();
