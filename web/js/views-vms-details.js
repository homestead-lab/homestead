/* Homestead Virtual machines: a VM's problem in words, the page's summary
   line and a row's status, its details sidebar and its hover cards (design.md
   "Details sidebar", "Hover cards"). A classic script, loaded after
   views-vms.js, whose helpers it uses. */
// A VM's key: its namespace and name, and its cluster's in the view of every cluster.
const vmKey = v => `${v.site?.id ? `${v.site.id}/` : ""}${v.ns}/${v.name}`;
const vmByKey = key => (STATE.data.vms || []).find(v => vmKey(v) === key) || null;
// Stopped is said as the guest would: shut down.
const vmStateWord = status => status === "Stopped" ? "Shut down" : status || "Unknown";
// 4Gi as people read it: 4 GiB.
const vmMemoryWords = s => String(s || "").replace(/(\d)\s*Gi$/, "$1 GiB").replace(/(\d)\s*Mi$/, "$1 MiB") || "—";
const vmSentence = text => { const s = String(text || "").trim(); return s ? `${s[0].toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? "" : "."}` : ""; };
// win11 and ubuntu-test; a, b and c.
const vmNames = names => names.length < 2 ? names.join("") : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;

/* What is wrong with a VM, if anything: {tone, chip, word, line}. The word
   finishes "<name> …" in the summary; the line says why, beside the chip.
   A VM that is shut down is not a problem: the summary says so in words. */
function vmProblem(v) {
  const stuck = (v.filling || []).find(f => f.stuck);
  // Kubernetes' own state (ErrorUnschedulable) is in the hover card; the chip says it in words.
  if (v.problem) return { tone: "bad", chip: v.running ? "Problem" : "Cannot start", word: v.running ? "has a problem" : "cannot start", line: vmSentence(v.problem) };
  if (stuck) return { tone: "warn", chip: "Stuck", word: "is stuck preparing a disk", line: `${VM_FILL_WORDS[stuck.phase] || stuck.phase} ${stuck.claim} is not progressing.` };
  const tone = vmTone(v.status);
  if (tone === "bad") return { tone, chip: v.status, word: "has a problem", line: `Its state is ${v.status}.` };
  if (tone === "warn") return { tone, chip: v.status, word: "needs attention", line: `Its state is ${v.status}.` };
  return null;
}

/* The page's one line (design.md "Summary line"): how many VMs run, each
   one in trouble as a link to its details, and the ones shut down by name. */
function vmSummary(rows, { q = "" } = {}) {
  const running = rows.filter(v => v.status === "Running").length, off = rows.filter(v => v.status === "Stopped");
  const troubled = rows.map(v => [v, vmProblem(v)]).filter(([, p]) => p).sort((a, b) => (a[1].tone === "bad" ? 0 : 1) - (b[1].tone === "bad" ? 0 : 1));
  const scope = q ? ` matching “${esc(q)}”` : "";
  const head = !rows.length ? `No VMs${scope}.`
    : running === rows.length ? `${rows.length === 1 ? "The VM" : `All ${rows.length} VMs`}${scope} ${rows.length === 1 ? "is" : "are"} running.`
    : `${running} of ${rows.length} VM${rows.length === 1 ? "" : "s"}${scope} ${running === 1 ? "is" : "are"} running.`;
  const links = [...troubled.slice(0, 3).map(([v, p]) => ({ text: `${v.name} ${p.word}`, tone: p.tone, run: `openDetails('vm',${jsq(vmKey(v))})` })),
    troubled.length > 3 && { text: `${troubled.length - 3} more`, note: true }];
  const platform = STATE.platform?.harvester === false ? `KubeVirt on ${platformName(STATE.platform)}${STATE.platform.cdi ? "" : ", without CDI"}` : "";
  return UI.summary({ head: `<b>${head}</b>`, parts: [...links,
    off.length && { text: `${off.length > 3 ? `${off.length} VMs` : vmNames(off.map(v => v.name))} ${off.length === 1 ? "is" : "are"} shut down.`, note: true },
    platform && { text: platform, note: true }], narrow: { parts: links } });
}

/* A row's status: quiet words when all is well, a chip and the reason when
   not, then its monitoring and schedule tags and what it is waiting for. */
function vmStatusWords(v) {
  const p = vmProblem(v), fill = (v.filling || [])[0];
  const tags = `${answerTag(v, true)}${scheduleTag(v)}`;
  const under = v.restart_required ? UI.reason("Restart it to apply the changes.", { under: true })
    : fill && !p ? UI.reason(esc(`${VM_FILL_WORDS[fill.phase] || fill.phase}${fill.progress != null ? ` · ${fill.progress.toFixed(0)}%` : ""}`), { under: true }) : "";
  return p ? `${UI.chip(p.chip, p.tone, { size: "slim" })}${tags}${UI.reason(esc(p.line), { under: true })}${under}`
    : `<span class="vm-state dim">${esc(vmStateWord(v.status))}</span>${tags}${under}`;
}

/* A VM's actions, in groups: what it is used for, what changes it, what
   removes it. The power actions are those its state allows. */
function vmActionItems(v) {
  const ns = jsq(v.ns), name = jsq(v.name), run = a => `vmPower(${ns},${name},${jsq(a)})`;
  const item = (label, runJs, iconName, options = {}) => ({ label, run: runJs, icon: iconName, ariaLabel: `${label} ${v.name}`, ...options });
  const can = a => (v.actions || []).includes(a);
  const power = a => VM_ACTIONS[a][2];
  return [
    can("console") && item("Open console", `vmConsole(${ns},${name})`, "console", { need: "operator", group: "Use", tip: "The VM's screen and keyboard." }),
    item("Open VM page", `vmOpen(${ns},${name})`, "list", { group: "Use", tip: "Every detail: hardware, display, disks, network and events." }),
    item("Monitoring", `vmMonitoring(${ns},${name})`, "pulse", { group: "Use" }),
    can("start") && item("Start VM", run("start"), "play", { need: "operator", group: "Change", tip: power("start") }),
    can("unpause") && item("Resume", run("unpause"), "play", { need: "operator", group: "Change", tip: power("unpause") }),
    can("restart") && item("Restart VM", run("restart"), "restart", { need: "operator", group: "Change", tip: power("restart") }),
    can("stop") && item(v.stop_retries ? "Stop retries" : "Shut down", run("stop"), "power", { need: "operator", group: "Change",
      tip: v.stop_retries ? "Stop retrying the start, and keep the VM off until it is started." : power("stop") }),
    can("pause") && item("Pause", run("pause"), "pause", { need: "operator", group: "Change", tip: power("pause") }),
    item("Edit settings", `vmEdit(${ns},${name})`, "edit", { need: "operator", group: "Change" }),
    item("Schedule", `vmSchedule(${ns},${name})`, "clock", { need: "operator", group: "Change" }),
    item("Logo", `vmLogo(${ns},${name})`, "logo", { need: "operator", group: "Change" }),
    can("migrate") && item("Move to another host", `vmMove(${ns},${name})`, "move", { need: "operator", group: "Change" }),
    FLEET.view?.linked && item("Move to cluster", `moveToCluster('vm',${name},${jsq(v.site?.handle || "")},'move',${ns})`, "move", { need: "admin", group: "Change", tip: "Move this VM and its disks to another linked cluster." }),
    FLEET.view?.linked && item("Copy to cluster", `moveToCluster('vm',${name},${jsq(v.site?.handle || "")},'copy',${ns})`, "copy", { need: "admin", group: "Change", tip: "Create a stopped copy of this VM on another linked cluster and keep the original." }),
    can("force-stop") && item("Force off", run("force-stop"), "plug", { need: "operator", danger: true, group: "Remove", tip: power("force-stop") }),
    item("Delete VM…", `vmDelete(${ns},${name})`, "trash", { need: "admin", danger: true, group: "Remove" }),
  ].filter(Boolean);
}
/* The one labelled action that fits its state: the console while it runs,
   Start when it is shut down, Resume when paused, and Edit settings for one
   that cannot start, since its settings are what keep it off. */
function vmMainAction(v, items = vmActionItems(v)) {
  return items.find(i => i.label === "Open console") || items.find(i => ["Start VM", "Resume", "Stop retries"].includes(i.label))
    || (vmProblem(v) && items.find(i => i.label === "Edit settings")) || null;
}

/* The sidebar's reads (detailsData): the VM's own record - disks, events,
   guest and hardware - which the list leaves out. */
const vmDetailRead = v => () => api(`/api/vm?ns=${encodeURIComponent(v.ns)}&name=${encodeURIComponent(v.name)}`, { keep: true });
const vmDetailSlot = (v, render) => detailsSlot(detailsData("vm", vmDetailRead(v)), render, { reading: "Reading the VM…", failed: "The VM could not be read" });

/* Overview (the mockup): what is wrong first, then its address, size,
   memory, disk activity and host. */
function vmOverview(key) {
  const v = vmByKey(key);
  if (!v) return "";
  const p = vmProblem(v), u = v.running ? v.usage : null, ips = v.ips?.length ? v.ips : v.ip ? [v.ip] : [];
  return [p && UI.callout(p.tone, `${v.name} ${p.word}`, esc(p.line)), vmFilling(v),
    v.restart_required && UI.callout("info", "Some changes apply after a restart", "Restart VM applies them.")].filter(Boolean).join("")
    + UI.facts([
      ["Address", ips.length ? `<span class="mono">${esc(ips[0])}</span>${ips.length > 1 ? ` <span class="dim xs">and ${ips.length - 1} more</span>` : ""}${v.network ? `<div class="dim xs">${esc(v.network)}</div>` : ""}`
        : `<span class="dim">${v.running ? "Not reported yet" : "None while shut down"}</span>`],
      ["Size", esc(`${v.cores} vCPU${v.cores === 1 ? "" : "s"}, ${vmMemoryWords(v.memory)} memory`)],
      v.running && ["Memory", u?.mem != null ? esc(`${vmBytes(u.mem)} of ${vmMemoryWords(v.memory)} used`) : '<span class="dim">Not measured yet</span>'],
      v.running && ["Disk activity", u?.read_bps != null ? esc(`${vmRate(u.read_bps)} read, ${vmRate(u.write_bps)} written`) : '<span class="dim">Not measured yet</span>'],
      ["Host", v.node ? esc(v.node) : `<span class="dim">${v.running ? "Not placed yet" : "None while shut down"}</span>`],
    ], { icons: false });
}

/* Disks: each disk and the volume behind it. */
function vmDisksTab(key) {
  const v = vmByKey(key);
  if (!v) return "";
  return vmDetailSlot(v, d => UI.list((d.disks || []).map(disk => ({ title: disk.name, detail: [disk.kind, disk.bus, disk.size].filter(Boolean).join(" · "),
    noteHtml: disk.claim ? `<span class="dim xs mono">${esc(disk.claim)}${disk.storage_class ? ` · ${esc(disk.storage_class)}` : ""}</span>` : "",
    chipHtml: disk.boot ? UI.chip(`Boot ${disk.boot}`, "neutral", { size: "slim" }) : "" })), { empty: "This VM has no disks." }))
    + `<div class="details-foot">${actionBar([{ label: "Open VM page", run: `vmOpen(${jsq(v.ns)},${jsq(v.name)})`, plain: true, tip: "Every detail: hardware, display, disks, network and events." }], { shown: 1 })}</div>`;
}

/* Activity: its web check over the last day and what Kubernetes said about it. */
function vmActivityTab(key) {
  const v = vmByKey(key);
  if (!v) return "";
  return (answerOf(v, true) ? answerCardRow(v, true) : "")
    + vmDetailSlot(v, d => UI.activity((d.events || []).map(e => ({ at: e.last, title: eventWords(e.reason), detail: vmSentence(e.message), tone: e.type === "Warning" ? "warn" : "" })),
      { empty: "Nothing has been recorded for this VM lately." }))
    + '<p class="dim xs">Kubernetes keeps its events for about an hour.</p>';
}

/* Settings: what the VM is, and the changes that can be made to it. */
function vmSettingsTab(key) {
  const v = vmByKey(key);
  if (!v) return "";
  const change = vmActionItems(v).filter(i => ["Edit settings", "Schedule", "Logo"].includes(i.label)).map(i => ({ ...i, plain: true }));
  return vmDetailSlot(v, d => UI.facts([
    ["CPU", esc(`${d.cores ?? v.cores} vCPU${(d.cores ?? v.cores) === 1 ? "" : "s"}`)],
    ["Memory", esc(vmMemoryWords(d.memory || v.memory))],
    ["Guest OS", esc(d.guest?.prettyName || d.os || v.os || "Unknown")],
    d.guest?.kernelRelease && ["Kernel", `<span class="mono xs">${esc(d.guest.kernelRelease)}</span>`],
    ["Live migration", d.migratable ? "Possible" : "Not possible"],
    ["Run strategy", esc(d.run_strategy || v.run_strategy || "—")],
    ["Network", esc((d.nics || []).map(n => n.network).filter(Boolean).join(", ") || v.network || "—")],
    ["Namespace", esc(v.ns)],
    ["Created", esc(fmtDay(d.created) || "—")],
  ])) + (change.length ? `<div class="details-foot">${actionBar(change, { shown: 3 })}</div>` : "");
}

if (typeof DETAIL_KINDS === "object") DETAIL_KINDS.vm = {
  head: key => {
    const v = vmByKey(key);
    return v && { title: v.name, sub: [v.os, v.node || vmStateWord(v.status)].filter(Boolean).join(" · "), iconHtml: vmAvatar(v),
      cluster: v.site && typeof fleetAll === "function" && fleetAll() ? v.site.id : "" };
  },
  // The console or Start first, then Restart, then Edit settings; the rest in ⋯.
  actions: key => {
    const v = vmByKey(key);
    if (!v) return [];
    const items = vmActionItems(v), main = vmMainAction(v, items);
    const lead = [main, items.find(i => i.label === "Restart VM"), items.find(i => i.label === "Edit settings")].filter((i, n, all) => i && all.indexOf(i) === n);
    return [...lead, ...items.filter(i => !lead.includes(i))];
  },
  tabs: [
    { id: "overview", label: "Overview", html: vmOverview },
    { id: "disks", label: "Disks", html: vmDisksTab },
    { id: "activity", label: "Activity", html: vmActivityTab },
    { id: "settings", label: "Settings", html: vmSettingsTab },
  ],
};

if (typeof HOVER_CARDS === "object") {
  /* CPU and memory's card: what it uses now, and its disk activity. */
  HOVER_CARDS["vm-usage"] = key => {
    const v = vmByKey(key);
    if (!v) return "";
    const u = v.running ? v.usage : null;
    return UI.card({ title: `What ${v.name} uses`, chipHtml: UI.chip(vmStateWord(v.status), v.running ? "ok" : "neutral", { size: "slim" }),
      chartHtml: u?.mem_pct != null ? UI.meter({ now: u.mem_pct, warnAt: 90, label: `${v.name} memory used` }) : "",
      rows: [
        ["CPU", u?.cpu_pct != null ? esc(`${u.cpu_pct}% of ${v.cores} vCPU${v.cores === 1 ? "" : "s"}`) : esc(`${v.cores} vCPU${v.cores === 1 ? "" : "s"}`), "chip"],
        ["Memory", u?.mem != null ? esc(`${vmBytes(u.mem)} of ${vmMemoryWords(v.memory)}`) : esc(vmMemoryWords(v.memory)), "memory"],
        u?.read_bps != null && ["Disk activity", esc(`${vmRate(u.read_bps)} read, ${vmRate(u.write_bps)} written`), "disk"],
        v.node && ["Host", esc(v.node), "node"],
      ], link: { label: "Open details", onclick: `openDetails('vm',${JSON.stringify(key)})` } });
  };
  /* The address's card: every address, its network and where it runs. */
  HOVER_CARDS["vm-address"] = key => {
    const v = vmByKey(key);
    if (!v) return "";
    const ips = v.ips?.length ? v.ips : v.ip ? [v.ip] : [];
    return UI.card({ title: ips[0] || `${v.name} has no address`, rows: [
      ips.length > 1 && ["Other addresses", `<span class="mono">${ips.slice(1).map(esc).join(", ")}</span>`, "network"],
      ["Network", esc(v.network || "The cluster network"), "network"],
      ["Host", esc(v.node || (v.running ? "Not placed yet" : "None while shut down")), "node"],
      !ips.length && ["Why", v.running ? "The guest has not reported one. It does when it runs the QEMU guest agent." : "A VM that is shut down has no address.", "info"],
    ], link: { label: "Open details", onclick: `openDetails('vm',${JSON.stringify(key)})` } });
  };
}

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  vmKey, vmByKey, vmStateWord, vmMemoryWords, vmSentence, vmNames, vmProblem, vmSummary, vmStatusWords, vmActionItems, vmMainAction,
  vmDetailRead, vmDetailSlot, vmOverview, vmDisksTab, vmActivityTab, vmSettingsTab,
};
