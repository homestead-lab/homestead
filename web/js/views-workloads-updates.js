/* Homestead Containers: the image update scan, the update review and the rollout watch (#911). One of the scripts views-workloads.js was split into; they share the page's globals and load in the order index.html lists them. */
/* A scan asks a registry about every workload in turn, so it is neither
   instant nor evenly paced. The server counts as it goes; this reads that
   count so the wait shows its work instead of going quiet. */
window.checkImageUpdates = async () => {
  const button = typeof event !== "undefined" ? event.currentTarget : null;
  if (button) { button.disabled = true; button.textContent = "Checking…"; }
  const host = $("#scanprogress");
  if (host) host.innerHTML = '<span class="spin2"></span> asking the registries…';
  let watching = true;
  const watch = async () => {
    while (watching) {
      await new Promise(r => setTimeout(r, 500));
      if (!watching) break;
      const at = await api("/api/image-updates/scan-progress", { keep: true }).catch(e => ({ unread: e.message }));
      // The check goes on; the bar says this read failed, until the next one answers.
      if (at?.unread) {
        const bar = $("#scanprogress");
        if (bar && !bar.querySelector(".scan-unread"))
          bar.insertAdjacentHTML("beforeend", ` <span class="scan-unread">· could not read the progress just now: ${esc(at.unread)}</span>`);
        continue;
      }
      if (!at.total) continue;
      const done = Math.min(at.done, at.total);
      if (button && at.running) button.textContent = `Checking ${done}/${at.total}…`;
      const bar = $("#scanprogress");
      if (!bar) continue;
      bar.innerHTML = at.running
        ? `<span class="spin2"></span> checked ${done} of ${at.total}`
          + (at.current ? ` · ${esc(at.current)}` : "")
          + (at.updates ? ` · <b>${at.updates} update${at.updates === 1 ? "" : "s"} so far</b>` : "")
        : "";
    }
  };
  watch();
  try {
    const report = await loadImageUpdates(true, false);
    if (!report) return;      // the failure has been said already
    const updates = HomesteadUpdateState.availableWorkloads(report).length;
    const errors = report?.errors || 0;
    toast(updates ? `${updates} update${updates === 1 ? "" : "s"} available${errors ? `; ${errors} image${errors === 1 ? "" : "s"} could not be checked` : ""}`
      : errors ? `no updates found; ${errors} image${errors === 1 ? "" : "s"} could not be checked`
      : "every image is up to date", updates ? "ok" : errors ? "warn" : "ok");
    if (report && STATE.view === "workloads") renderWorkloads();
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    watching = false;
    if (button) { button.disabled = false; button.innerHTML = '↻ Check<span class="hide-sm"> images</span>'; }
    const bar = $("#scanprogress");
    if (bar) bar.innerHTML = "";
  }
};

/* An image reference short enough to read: its last path part, and the start
   of a digest. The whole reference stays in the title and in the details. */
function shortImage(ref) {
  const text = String(ref || "");
  const [name, digest] = text.split("@");
  const tail = name.split("/").pop() || name;
  return digest ? `${tail} · ${digest.replace(/^sha256:/, "").slice(0, 12)}` : tail;
}

/* The part of an image that changes: a tag, or the start of a digest. */
function imageVersion(ref, tag = "") {
  const [name, digest] = String(ref || "").split("@");
  const tail = name.split("/").pop() || name;
  // A tag in the reference, else the release Homestead tracks for it; an
  // image pinned only to its digest has none to show.
  const own = tail.includes(":") ? tail.slice(tail.lastIndexOf(":") + 1) : digest ? "" : "latest";
  return { tag: tag || own, short: digest ? digest.replace(/^sha256:/, "").slice(0, 7) : "" };
}
/* What a person reads for an image change: the release when it changes
   (2.8.200 → 2.8.205), and the start of the digest only when the release
   stays the same - latest → latest is a different build, not no change. */
function imageChangeWords(before, after, beforeTag = "", afterTag = "") {
  const was = imageVersion(before, beforeTag), now = imageVersion(after, afterTag);
  if (was.tag && now.tag && was.tag !== now.tag) return [was.tag, now.tag];
  const word = v => (v.tag && v.short ? `${v.tag} · ${v.short}` : v.tag || v.short || "?");
  return [word(was), word(now)];
}
window.imageChangeWords = imageChangeWords;

/* Lead with the constraint someone can resolve; estimates stay in Details. */
function imagePlacementBlocker(plan) {
  const hosts = plan.candidates || [];
  if (hosts.length && hosts.every(host => !host.eligible && (host.reasons || []).includes("cordoned"))) {
    return hosts.length === 1 ? "The only host is cordoned. Uncordon it before updating."
      : "All hosts are cordoned. Uncordon a suitable host before updating.";
  }
  if (hosts.length && hosts.every(host => !host.eligible && (host.reasons || []).some(reason => reason.startsWith("node is ")))) {
    return hosts.length === 1 ? "The only host is not ready. Restore its health before updating."
      : "No host is ready. Restore a host's health before updating.";
  }
  if (plan.rollout?.start_blocked) return "A replacement pod cannot fit while the current pods are running. Free capacity or review the rollout policy in Details.";
  return "No host can accept this update. Review the host, storage and placement requirements in Details.";
}
/* The same concern for several updates is said once. A blocked review leads
   only with blockers; warnings for otherwise eligible apps remain in Details.
   A note about what an update does not change - a missing memory limit, a
   host folder or a volume it already mounts, the restart every update makes
   (the lead says it restarts) - goes in Details, not in the warning, so an
   ordinary update asks for no tick. */
const UNCHANGED_BY_UPDATE = new RegExp([
  /^No memory limit is set/, /^hostPath contents are node-local/, /^Recreate stops all old pods before replacements start/,
  /^PVC \S+ uses local storage without verified node affinity/, /^PVC \S+'s volume topology could not be verified/,
  /^ephemeral PVC provisioning and capacity need scheduler review/,
].map(re => re.source).join("|"));
const updateConcerns = plan => capacityNotes(plan).concerns.filter(text => !UNCHANGED_BY_UPDATE.test(text));
function groupedConcerns(rows) {
  const byText = new Map();
  const blocked = rows.some(row => row.preview.capacity.blocked);
  rows.forEach(({config, preview}) => {
    const messages = blocked ? (preview.capacity.blocked ? [imagePlacementBlocker(preview.capacity)] : [])
      : updateConcerns(preview.capacity);
    const who = config.clusterName ? `${config.clusterName} · ${config.name}` : config.name;
    new Set(messages).forEach(text => {
      if (!byText.has(text)) byText.set(text, []);
      if (!byText.get(text).includes(who)) byText.get(text).push(who);
    });
  });
  return [...byText].map(([text, names]) => rows.length > 1
    ? `${text.replace(/\.$/, "")} - ${names.length === rows.length ? `all ${rows.length} updates` : names.join(", ")}` : text);
}

/* An update can be for a linked cluster (the Homestead updates dialog
   updates them too): its requests carry that cluster, and its rollout is
   keyed by it, since the same app can run on two clusters. */
const clusterHeaders = item => {
  const cluster = item?.cluster || window.FLEET?.view?.self;
  return cluster ? { "X-Homestead-Cluster": cluster } : {};
};
const rolloutKey = item => `${item?.cluster ? `${item.cluster}|` : ""}${updateKey(item.ns, item.name)}`;
const rolloutBody = config => { const { cluster, clusterName, part, ...rest } = config; return rest; };
// Homestead replacing itself: its API is away for a minute, which is not a failure.
const restartsHomestead = item => item?.part === "self" || (!item?.part && item?.name === "homestead");
/* Whether someone else changed the workload while this rollout was watched:
   replaced, or its pod template no longer this rollout's - stamp or images.
   A change outside the template leaves the pods as they are: Homestead,
   starting after its own update, sets its rollout strategy to what its data
   volume allows, and that raised the generation and stopped the queue. */
const rolloutChanged = (accepted, state) => state.uid !== accepted.uid || (state.generation !== accepted.generation
  && (!accepted.rollout_at || state.rollout_at !== accepted.rollout_at
      || JSON.stringify(state.images || {}) !== JSON.stringify(accepted.images || {})));
window.rolloutChanged = rolloutChanged;

/* Review image updates or a rollback (#909), on the shared review flow: it keeps the sequence, the busy flag
   and the one-shot send. Sending starts the rollout queue below, which runs on its own: it is not the flow's
   to hold, so another review can be asked for while it runs (and stops it, by replacing its dialog). */
let IMAGE_REVIEW_OPEN = null, IMAGE_QUEUE_TOKEN = null, IMAGE_QUEUE_RUNNING = null;
const IMAGE_REVIEW = UI.reviewFlow({
  loading: input => {
    IMAGE_REVIEW_OPEN = input;
    const count = input.items.length;
    modal(input.action === "rollback" ? "Review rollback" : count > 1 ? `Update ${count} apps` : "Review image update",
      '<div id="imageReviewLoading" class="empty"><span class="spin2"></span> Checking exact images and rollout capacity…</div>', true);
  },
  live: () => !!$("#imageReviewLoading"),
  preview: async input => {
    const { items, action } = input;
    const rows = [];
    for (const item of HomesteadUpdateState.orderApply(items)) {
      const config = {ns: item.ns, name: item.name, action, approved: true,
        ...(item.cluster ? {cluster: item.cluster, clusterName: item.clusterName || item.cluster} : {}),
        ...(item.part ? {part: item.part} : {}), ...(item.version ? {version: item.version} : {})};
      let preview;
      try {
        preview = await api("/api/image-updates/preview", {method: "POST",
          headers: {"Content-Type": "application/json", ...clusterHeaders(config)}, body: JSON.stringify(rolloutBody(config))});
      } catch (error) {
        if (action !== "rollback" && /no image update is currently available/i.test(error.message || "")) {
          await loadImageUpdates(false, true);
          error.imageAlreadyCurrent = true;
        }
        throw error;
      }
      if (IMAGE_REVIEW_OPEN !== input || !$("#imageReviewLoading")) throw new Error("This review was replaced");
      if (!preview.capacity || !preview.capacity_token || !Array.isArray(preview.images)) throw new Error("Image capacity review unavailable; no update was started");
      rows.push({config, preview});
    }
    if (!rows.length) throw new Error("Select at least one image change");
    return rows;
  },
  render: (rows, { items, action }) => {
    const rollback = action === "rollback";
    const blocked = rows.some(row => row.preview.capacity.blocked);
    const many = rows.length > 1;
    const concerns = groupedConcerns(rows);
    // One line per app: its name, and what its image moves from and to -
    // once, when every container moves between the same two images.
    const changeWords = i => imageChangeWords(i.before, i.after, i.before_tag, i.after_tag);
    const apps = rows.map(({config, preview}) => {
      const flagged = preview.capacity.blocked || updateConcerns(preview.capacity).length;
      const words = preview.images.map(changeWords), same = words.every(w => w.join() === words[0]?.join());
      const change = (same ? [[null, words[0]]] : preview.images.map((i, n) => [i.container, words[n]]))
        .filter(([, w]) => w).map(([container, [was, now]]) => `<span class="upd-change">${container ? `${esc(container)} ` : ""}<code>${esc(was)}</code> → <code>${esc(now)}</code></span>`).join("");
      return `<li><span class="upd-name">${flagged ? `<span class="upd-flag ${preview.capacity.blocked ? "bad" : "warn"}" title="See the notes above">!</span>` : ""}<b>${esc(config.name)}</b> <span class="dim">${config.clusterName ? `${esc(config.clusterName)} · ` : ""}${esc(config.ns)}</span></span>${change}</li>`;
    }).join("");
    // Details: per app, the exact images and where it can run; notes shared
    // by every app, once.
    const short = digest => String(digest || "").replace(/@sha256:([0-9a-f]{12})[0-9a-f]+$/, "@sha256:$1…");
    const notes = [...new Set(rows.flatMap(({preview}) => [...capacityNotes(preview.capacity).caveats,
      ...capacityNotes(preview.capacity).concerns.filter(text => UNCHANGED_BY_UPDATE.test(text))]))];
    const images = preview => [...new Map(preview.images.map(i => [`${i.before}|${i.after}`, i])).values()];
    const details = rows.map(({config, preview}) => UI.section(many ? `${config.clusterName ? `${config.clusterName} · ` : ""}${config.ns}/${config.name}` : "",
      UI.facts(images(preview).flatMap(i => [[`Image ${rollback ? "back to" : "now"}`, `<code title="${esc(rollback ? i.after : i.before)}">${esc(short(rollback ? i.after : i.before))}</code>`],
        [rollback ? "From" : "New", `<code title="${esc(rollback ? i.before : i.after)}">${esc(short(rollback ? i.before : i.after))}</code>`]]))
      + capacityHostTable(preview.capacity))).join("");
    // One app names itself and its change at the top; several are listed.
    const one = !many && rows[0], oneWords = one && one.preview.images.map(changeWords).filter(Boolean);
    const sameChange = oneWords && oneWords.length && oneWords.every(w => w.join() === oneWords[0].join());
    const unread = rows.filter(r => r.preview.unread).map(({config, preview}) => ["warn", {
      short: `${config.name} is installed by its tag`,
      more: `Homestead could not read the registry (${preview.unread}), so the cluster pulls ${config.version} by its tag. If the tag is missing, the old pod keeps serving.`}]);
    $("#mbody").innerHTML = `<div class="update-review">${UI.dialog({ shape: "review",
      subject: one ? { kind: "App", name: one.config.name, where: `${one.config.clusterName ? `${one.config.clusterName} · ` : ""}${one.config.ns}`,
        change: sameChange ? oneWords[0] : null }
        : { kind: "Apps", name: rows.length > 3 ? `${rows.slice(0, 3).map(r => r.config.name).join(", ")} and ${rows.length - 3} more` : rows.map(r => r.config.name).join(", ") },
      lead: `${many ? "Apps update one at a time, with Homestead last. " : ""}${items.some(restartsHomestead) ? "Homestead will be briefly unavailable while it restarts. " : ""}${many ? "Each app restarts" : "The app restarts"} to ${rollback ? "return to its previous image" : "use the new image"}.`,
      issues: [...concerns.map(c => [blocked ? "bad" : "warn", c]), ...unread],
      issuesTitle: blocked ? "Update blocked" : "",
      body: `${one && sameChange ? "" : `<ul class="upd-apps">${apps}</ul>`}${rollback ? "" : '<div id="updNotes"></div>'}`,
      details: `${details}
        ${notes.length ? `<ul class="ui-list">${notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
        <p class="ui-help">Each app is checked again before it updates. A failure stops the rest, and so does closing this.</p>`,
      ack: { id: "imageCapacityApprove", sentence: concerns.length || unread.length ? "Update despite the warnings" : many ? "Restart them now" : "Restart it now", onchange: "imageReviewReady()" },
      actions: { primary: { label: rollback ? "Start rollback" : many ? `Update ${rows.length}` : "Update", onclick: "imageReviewedApply()", id: "imageCapacityApply", disabled: true } },
    })}</div>`;
    // What is in the update: read beside the review, never waited for.
    if (!rollback && window.updateNotesInto) updateNotesInto($("#updNotes"), rows.map(r => r.config));
  },
  failed: error => {
    if (error.imageAlreadyCurrent) {
      $("#mbody").innerHTML = UI.callout("", "The selected update is no longer available.",
        "No update was started by this review. Open Image updates to see what remains.")
        + UI.actions(UI.cancel("Close") + UI.button("Image updates", "imageUpdateCenter()"));
      return;
    }
    $("#mbody").innerHTML = UI.callout("bad", "Review could not finish.", `${esc(error.message)}. Nothing was changed. Close this dialog and review again.`) + UI.actions(UI.cancel("Close"));
  },
  blocked: rows => rows.some(row => row.preview.capacity.blocked), ack: "imageCapacityApprove", button: "imageCapacityApply",
  apply: async rows => {
    // Arm the reload only when this Homestead's own rollout starts, and hold
    // version notices until the whole queue has finished, including both pods
    // of a redundant Homestead. An unrelated early response cannot end it.
    try { sessionStorage.removeItem("homestead.selfUpdate"); } catch (e) { /* no session storage */ }
    const queue = IMAGE_QUEUE_TOKEN = {};
    const items = rows.map(r => r.config);
    const states = Object.fromEntries(items.map(item => [rolloutKey(item), {phase: "queued", ready: 0, desired: 1}]));
    const failures = [];
    modal("Reviewed image rollouts", '<div id="imageQueue"></div>', true);
    const active = () => queue === IMAGE_QUEUE_TOKEN && $("#imageQueue") && !$("#modal").classList.contains("hidden");
    window.homesteadUpdateQueueActive = active;
    delete window.__homesteadQueuedVersion;
    const paint = () => {if (active()) paintRollout($("#imageQueue"), batchUpdateMarkup(items, states, failures, false, true));};
    // Runs the queue from `start`; a failure stops it there, and Skip and
    // continue (#295) picks it up again after the failed app.
    const run = async start => {
      IMAGE_QUEUE = null;
      let complete = false;
      try {
        for (let index = start; index < rows.length; index++) {
          const {config, preview} = rows[index], key = rolloutKey(config);
          if (!active()) return;
          let job = "";
          try {
            if (restartsHomestead(config) && !config.cluster && config.action !== "rollback") {
              try { sessionStorage.setItem("homestead.selfUpdate", String(Date.now())); } catch (e) { /* the bar offers Reload */ }
            }
            const result = await api(config.action === "rollback" ? "/api/image-updates/rollback" : "/api/image-updates/apply",
              {method: "POST", headers: {"Content-Type": "application/json", ...clusterHeaders(config)},
               body: JSON.stringify({...rolloutBody(config), capacity_token: preview.capacity_token, confirm_capacity: true})});
            job = result.operation?.id || "";
            states[key] = result; paint();
            // Never start the next workload until this exact accepted generation is ready.
            if (!result.uid || !Number.isInteger(result.generation)) throw new Error("Rollout identity unavailable; check its job before continuing");
            const deadline = Date.now() + 15 * 60 * 1000;
            let misses = 0;
            while (true) {
              if (!active()) return;
              let state;
              try {
                state = await api(`/api/image-updates/progress?ns=${encodeURIComponent(config.ns)}&name=${encodeURIComponent(config.name)}`,
                  {timeout: 10000, headers: clusterHeaders(config)});
                misses = 0;
              } catch (error) {
                // Homestead is replacing itself - here, or on a linked cluster the
                // relay cannot reach for that minute: wait for it to answer again.
                // For any other app, no answer at all (a dropped connection, a
                // proxy giving up on a long pull, a gateway error) says nothing
                // about the rollout: ask again, and stop only once it has been
                // gone a while. An answer that is an error still stops the queue.
                const unanswered = !error.status || error.status >= 502;
                misses += 1;
                if (Date.now() > deadline || !(restartsHomestead(config) || (unanswered && misses <= PROGRESS_MISSES))) {
                  throw unanswered && !restartsHomestead(config) ? new Error(`Homestead stopped answering while ${config.name} updated (${error.message})`) : error;
                }
                states[key] = {...(states[key] || {}), phase: restartsHomestead(config) ? "restarting" : "reconnecting"}; paint();
                await new Promise(resolve => setTimeout(resolve, 3000));
                continue;
              }
              states[key] = state; paint();
              if (rolloutChanged(result, state)) throw new Error("App changed during monitoring; review remaining updates again");
              if (state.phase === "failed") throw new Error("Rollout failed");
              if (state.phase === "ready") { refreshAfterRollout(); break; }
              if (Date.now() > deadline) throw new Error("Monitoring timed out; check its job before continuing");
              await new Promise(resolve => setTimeout(resolve, 2000));
            }
          } catch (error) {
            // Already on the newest image - updated elsewhere since the review,
            // by GitOps for one: nothing to do, and no reason to stop the rest.
            if (!job && /no image update is currently available/i.test(error.message || "")) {
              states[key] = {phase: "up to date", ready: 0, desired: 0}; paint();
              continue;
            }
            failures.push({...config, job, error: `${asSentence(error.message)} No automatic retry: ${job ? "check its job" : "review it again"} before trying it again.`});
            const rest = rows.slice(index + 1);
            for (const remaining of rest) states[rolloutKey(remaining.config)] = {phase: "not started", ready: 0, desired: 1};
            IMAGE_QUEUE = rest.length ? {skip: () => run(index + 1)} : null;
            paint();
            return;
          }
        }
        paint();
        complete = true;
      } finally {
        if (!complete && window.homesteadUpdateQueueActive === active) {
          try { sessionStorage.removeItem("homestead.selfUpdate"); } catch (e) { /* no session storage */ }
        }
        // Keep the hold while Skip and continue is offered. Closing the dialog
        // makes active() false, so an abandoned queue never blocks later notices.
        if (window.homesteadUpdateQueueActive === active && (complete || !IMAGE_QUEUE || !active())) {
          delete window.homesteadUpdateQueueActive;
          const served = window.__homesteadQueuedVersion;
          delete window.__homesteadQueuedVersion;
          if (served && window.homesteadUpdated) window.homesteadUpdated(served);
        }
      }
    };
    paint();
    // The queue runs on after the review is used up, so a new review is not held back by it.
    IMAGE_QUEUE_RUNNING = run(0);
  },
});
function reviewImageActions(items, action = "update") { return IMAGE_REVIEW.open({ items, action }); }
window.imageReviewReady = () => IMAGE_REVIEW.ready();
window.imageUpdateReview = (ns, name) => reviewImageActions([{ns, name}]);
window.imageUpdateApply = () => imageReviewedApply();
window.imageReviewedApply = async () => {
  if (!imageReviewReady()) return toast("Review and acknowledge the image and capacity changes first", "bad");
  IMAGE_QUEUE_RUNNING = null;
  await IMAGE_REVIEW.apply();
  await IMAGE_QUEUE_RUNNING;
};
let IMAGE_QUEUE = null;     // the stopped queue, while Skip and continue can resume it
// Progress reads in a row with no answer before the queue stops: about a
// minute and a half, with each read's own 10 s timeout and 3 s pause.
const PROGRESS_MISSES = 20;

/* After a rollout: the list and the waiting updates as they are now, so the
   page shows the new image without a refresh. The server has re-asked about
   this app; the page repaints when its dialog is closed, or at once if not. */
async function refreshAfterRollout() {
  try {
    const [wl] = await Promise.all([api("/api/workloads"), new Promise(r => setTimeout(r, 1500)).then(() => loadImageUpdates(false, true))]);
    STATE.data.wl = wl;
  } catch (e) { return; }
  if (STATE.view !== "workloads") return;
  if ($("#modal").classList.contains("hidden")) renderWorkloads();
  else window.__repaintAfterModal = () => { if (STATE.view === "workloads") renderWorkloads(); };
}
window.refreshAfterRollout = refreshAfterRollout;
window.imageQueueSkip = () => IMAGE_QUEUE?.skip();
// A rollout's job from the queue, opened over it: Back returns to the queue.
window.imageQueueJob = id => { pushModal(); jobsDialog(); selectJob(id); };
const asSentence = text => {
  const words = String(text || "Rollout failed").trim();
  return words.charAt(0).toUpperCase() + words.slice(1) + (/[.!?]$/.test(words) ? "" : ".");
};

// Polling updates status without closing details the reader has opened.
function paintRollout(host, html) {
  if (!host) return;
  const open=[...(host.querySelectorAll?.("details[open][data-disclosure]") || [])].map(el=>el.dataset.disclosure);
  host.innerHTML=html;
  for(const el of host.querySelectorAll?.("details[data-disclosure]") || [])if(open.includes(el.dataset.disclosure))el.open=true;
}

function batchUpdateMarkup(items, states, startFailures = [], reconnecting = false, queueMode = false) {
  const failureMap = Object.fromEntries(startFailures.map(item => [rolloutKey(item), item]));
  const phaseOf = item => states[rolloutKey(item)]?.phase;
  const ready = items.filter(item => phaseOf(item) === "ready" && !failureMap[rolloutKey(item)]);
  const current = items.filter(item => phaseOf(item) === "up to date");
  const failedItems = items.filter(item => failureMap[rolloutKey(item)] || phaseOf(item) === "failed");
  const notStarted = items.filter(item => phaseOf(item) === "not started");
  const done = ready.length + current.length;
  const pending = items.filter(item => !ready.includes(item) && !current.includes(item));
  const failed = failedItems.length > 0;
  const running = pending.find(item=>{const s=states[rolloutKey(item)];return s && !["queued","not started","failed"].includes(s.phase) && !failureMap[rolloutKey(item)];});
  // One count, the same everywhere: the header and the bar agree (#295).
  const counts = [[ready.length, "updated"], [current.length, "already up to date"], [failedItems.length, "failed"], [notStarted.length, "not started"]]
    .filter(([n]) => n).map(([n, words]) => `${n} ${words}`).join(" · ");
  const row = item => {
      const key = rolloutKey(item), state = states[key], startError = failureMap[key];
      const phase = startError ? "failed" : state?.phase || "starting";
      const tone = phase === "ready" || phase === "up to date" ? "ok" : phase === "failed" ? "bad" : phase === "not started" ? "neutral" : "warn";
      // The ready count can be the old pod's: what the new one waits for says more.
      const waiting = !["ready", "up to date"].includes(phase) && (state?.pods || []).find(pod => pod.blocked);
      const actions = startError && queueMode ? [startError.job ? UI.button("Open job", `imageQueueJob(${jsArg(startError.job)})`) : "",
        IMAGE_QUEUE && notStarted.length ? UI.button(`Skip and continue with ${notStarted.length} more`, "imageQueueSkip()") : ""].join("") : "";
      return `<div><span><b>${esc(item.name)}</b><small>${item.clusterName ? `${esc(item.clusterName)} · ` : ""}${esc(item.ns)}${state && state.desired ? ` · ${state.ready || 0}/${state.desired} ready` : ""}</small></span>
        ${UI.chip(phase, tone)}${startError ? `<div class="updateerror">${esc(startError.error)}</div>` : ""}
        ${actions ? `<div class="row" style="gap:8px;margin-top:6px;flex-wrap:wrap">${actions}</div>` : ""}
        ${waiting ? `<div class="dim xs">New pod waiting${waiting.node ? ` on ${esc(waiting.node)}` : ""}: ${esc(waiting.blocked)}</div>` : ""}</div>`;
    };
  const left = notStarted.length;
  const closing = !queueMode ? ""
    : left ? `<p class="ui-help">Closing ends the queue: ${left === 1 ? "the app not started keeps its" : `the ${left} apps not started keep their`} image. Started updates carry on in Jobs.</p>`
    : done + failedItems.length < items.length ? '<p class="ui-help">Closing stops the apps still waiting. Started updates carry on in Jobs.</p>' : "";
  // One track across the apps: the one updating fills its segment as it goes
  // through its stages; its stage and detail are said under the track.
  const live = running && states[rolloutKey(running)], st = live && rolloutStages(live);
  const at = running ? items.indexOf(running) : failed ? items.length : items.findIndex(item => pending.includes(item));
  const list = items.map(item => {
    const key = rolloutKey(item), phase = failureMap[key] ? "failed" : states[key]?.phase;
    return { title: item.name, state: phase === "failed" ? "failed" : ["ready", "up to date"].includes(phase) ? "done" : item === running ? "current" : "todo",
      detailHtml: item === running && st ? `${esc(st.list[Math.min(st.current, 2)].title)}${st.list[Math.min(st.current, 2)].detailHtml ? `: ${st.list[Math.min(st.current, 2)].detailHtml}` : ""}` : "" };
  });
  const status = failed ? [queueMode ? (IMAGE_QUEUE ? "queue stopped" : "queue ended") : "needs attention", "warn"]
    : done === items.length ? ["finished", "ok"] : reconnecting ? ["reconnecting", "warn"] : ["monitoring", "ok"];
  return `<div class="batch-rollout">${UI.dialog({ shape: "watch",
    subject: { name: items.length > 3 ? `${items.slice(0, 3).map(i => i.name).join(", ")} and ${items.length - 3} more` : items.map(i => i.name).join(", "),
      where: `${items.length} app${items.length === 1 ? "" : "s"}${counts ? ` · ${counts}` : ""}`, status },
    lead: `${done} of ${items.length} done. Updates run one app at a time${queueMode ? "; a failure stops the ones after it" : ""}.`,
    issues: reconnecting ? [["warn", { short: "Some updates could not be checked", more: "Showing last-known progress. Reconnecting automatically." }]] : [],
    steps: { list, current: at < 0 ? items.length : at, unit: "App" }, progress: { value: st ? st.value : null },
    body: `${(() => {
      // A row for each app that needs looking at; the ones simply waiting are one line.
      const rest = pending.filter(item => item !== running);
      const waiting = rest.filter(item => !failureMap[rolloutKey(item)] && ["queued", "starting", undefined].includes(states[rolloutKey(item)]?.phase));
      const shown = rest.filter(item => !waiting.includes(item));
      return `${shown.length ? `<div class="batch-rollout-list">${shown.map(row).join("")}</div>` : ""}`
        + `${waiting.length ? `<p class="ui-help batch-next"><b>Up next</b> ${waiting.map(item => esc(item.name)).join(", ")}</p>` : ""}`;
    })()}
      ${running && live?.pods?.length ? UI.more(`Pods of ${running.name}`, UI.insightList(live.pods.map(p => ({ title: p.name, detail: [p.node || "Scheduling", p.blocked, p.waiting?.[0]?.reason].filter(Boolean).join(" · "),
        label: p.pull?.state === "pulling" ? "Pulling image" : p.phase, tone: p.blocked ? "warn" : p.phase === "Running" ? "ok" : "info" }))), false, "Pods") : ""}
      ${done ? UI.more(`Done · ${done}`, `<div class="batch-rollout-list">${[...ready, ...current].map(row).join("")}</div>`, false, "Updated") : ""}
      ${closing}`,
    actions: { dismiss: queueMode ? done === items.length ? "Done" : "Close queue" : "Monitor in background" } })}</div>`;
}

window.imageUpdateBatchApply = () => imageReviewedApply();

window.monitorImageRollouts = (items, startFailures = [], initialStates = {}, allItems = items) => {
  window.__updateTimer?.stop?.();
  const states = { ...initialStates };
  let misses = 0;
  const poll = async signal => {
    if ($("#modal").classList.contains("hidden")) return false;
    const results = await Promise.all(items.map(async item => {
      try {
        const state = await api(`/api/image-updates/progress?ns=${encodeURIComponent(item.ns)}&name=${encodeURIComponent(item.name)}`, { keep: true, signal });
        return { item, state };
      } catch (error) { return { item, error }; }
    }));
    if (UI.pollCancelled(signal)) return false;
    const successful = results.filter(result => result.state);
    misses = results.some(result=>result.error) ? misses + 1 : 0;
    successful.forEach(result => { states[rolloutKey(result.item)] = result.state; });
    if ($("#mbody")) paintRollout($("#mbody"), batchUpdateMarkup(allItems, states, startFailures, misses > 0));
    if (window.applyRole) window.applyRole();
    const terminal = items.every(item => ["ready", "failed"].includes(states[rolloutKey(item)]?.phase));
    if (terminal && !misses) {
      window.__updateTimer = null;
      setTimeout(() => loadImageUpdates(true, true), 1000);
      return false;
    }
  };
  window.__updateTimer = UI.poll(poll, 2000);
};

function pullElapsed(seconds) {
  const value = Math.max(0, Math.round(seconds || 0));
  return value < 60 ? `${value}s` : `${Math.floor(value / 60)}m ${String(value % 60).padStart(2, "0")}s`;
}

/* Kubernetes reports no byte progress for an image pull — the kubelet only
   says it started and, later, how long it took — so this says which node is
   fetching and for how long rather than drawing a percentage it cannot know. */
function pullDetail(s) {
  const pull = s.pull || {};
  if (pull.state === "pulling") {
    // containerd's own count of the layers fetched, against the registry's sizes.
    const amount = pull.total_bytes ? ` · ${pull.percent || 0}% of ${pullSize(pull.total_bytes)}` : "";
    return `Fetching ${pull.image || "the image"}${pull.node ? ` on ${pull.node}` : ""}${amount} · ${pullElapsed(pull.seconds)} so far`;
  }
  if (pull.state === "failed") return pull.detail || "The image could not be pulled";
  if (pull.state === "pulled" && s.updated < s.desired) {
    return `Pulled${pull.took ? ` in ${pull.took}` : ""}; starting the container`;
  }
  return `${s.updated} replacement pod${s.updated === 1 ? "" : "s"} created`;
}

function pullSize(b) { return bytesText(b); }

/* One rollout as a track of its three stages; a download of known size fills
   the stage it is on. One measure, never a second bar beside it. */
function rolloutStages(s) {
  const ready = s.phase === "ready", failed = s.phase === "failed", pull = s.pull || {};
  const accepted = s.observed_generation >= s.generation;
  const pulling = pull.state === "pulling";
  const pulled = s.updated >= s.desired && !pulling && pull.state !== "failed";
  const current = ready ? 3 : !accepted ? 0 : !pulled ? 1 : 2;
  const desired = s.desired ?? 0;
  const list = [
    { title: "Change accepted" },
    { title: pulling ? "Downloading image" : "Image and replacement pods",
      detailHtml: esc(pull.state === "failed" ? pull.detail || "Image download failed" : pulling ? pullDetail(s) : `${s.updated || 0}/${desired || "?"} replacement pods created`),
      state: pull.state === "failed" ? "failed" : undefined },
    { title: "Readiness checks", detailHtml: esc(`${s.ready || 0}/${desired || "?"} pods ready; completion waits for the accepted update.`),
      state: failed && current === 2 ? "failed" : undefined },
  ];
  // A failed rollout stopped at the stage it was on.
  if (failed && current < 3) list[current].state = "failed";
  // A percentage only for bytes known to be fetched: a pod count is not a
  // measure, and old pods still count as ready.
  const value = current === 1 && pulling && pull.total_bytes ? pull.percent || 0 : null;
  return { list, current, value, ready, failed };
}
function rolloutProgress(s) {
  const st = rolloutStages(s);
  return UI.dialog({ shape: "watch",
    steps: { list: st.list, current: st.current }, progress: { value: st.value },
    issues: [...(st.failed ? [["bad", "Review the failure details before retrying."]] : []), ...(s.problems || []).map(p => ["bad", p])], issuesTitle: st.failed ? "Update needs attention" : "",
    body: s.pods?.length ? UI.more("Pod details", UI.insightList(s.pods.map(p => ({ title: p.name, detail: [p.node || "Scheduling", p.blocked, p.waiting?.[0]?.reason].filter(Boolean).join(" · "),
      label: p.pull?.state === "pulling" ? "Pulling image" : p.phase, tone: p.blocked ? "warn" : p.phase === "Running" ? "ok" : "info" })))) : "",
    actions: { dismiss: false } });
}
function rolloutMarkup(s) {
  const st = rolloutStages(s);
  return UI.dialog({ shape: "watch",
    subject: { kind: "App", name: s.name || "", where: s.ns || "", status: st.ready ? ["updated", "ok"] : st.failed ? ["needs attention", "bad"] : ["updating", "info"] },
    steps: { list: st.list, current: st.current }, progress: { value: st.value },
    issues: [...(st.failed ? [["bad", "Review the failure details before retrying."]] : []), ...(s.problems || []).map(p => ["bad", p])], issuesTitle: st.failed ? "Update needs attention" : "",
    body: s.pods?.length ? UI.more("Pod details", UI.insightList(s.pods.map(p => ({ title: p.name, detail: [p.node || "Scheduling", p.blocked, p.waiting?.[0]?.reason].filter(Boolean).join(" · "),
      label: p.pull?.state === "pulling" ? "Pulling image" : p.phase, tone: p.blocked ? "warn" : p.phase === "Running" ? "ok" : "info" })))) : "",
    actions: { dismiss: st.ready ? false : "Monitor in background",
      start: s.can_rollback ? UI.button("Review rollback", `imageRollback(${jsArg(s.ns)},${jsArg(s.name)})`, { kind: st.failed ? "danger" : "", attrs: 'data-need="operator"' }) : "",
      primary: st.ready ? { label: "Done", onclick: "closeModal();go('workloads')" } : null } });
}

window.monitorImageRollout = (ns, name) => {
  window.__updateTimer?.stop?.();
  modal("Rollout · " + name, '<div class="empty"><span class="spin2"></span> waiting for Kubernetes…</div>', true);
  let misses = 0, lastState = null;
  const poll = async signal => {
    if ($("#modal").classList.contains("hidden")) return false;
    try {
      const s = await api(`/api/image-updates/progress?ns=${encodeURIComponent(ns)}&name=${encodeURIComponent(name)}`, { keep: true, signal });
      misses = 0; lastState = s; paintRollout($("#mbody"),rolloutMarkup(s));
      if (window.applyRole) window.applyRole();
      if (s.phase === "ready" || s.phase === "failed") {
        window.__updateTimer = null;
        setTimeout(() => loadImageUpdates(true, true), 1000);
        return false;
      }
    } catch (e) {
      if (UI.pollCancelled(signal)) return false;
      misses++;
      paintRollout($("#mbody"), UI.callout("warn","Connection lost","Retrying automatically. The update may still be running; showing the last known progress.") + (lastState ? rolloutMarkup(lastState) : UI.actions(UI.cancel("Monitor in background"))));
    }
  };
  window.__updateTimer = UI.poll(poll, 2000);
};

window.imageRollback = (ns, name) => reviewImageActions([{ns, name}], "rollback");
window.imageRollbackApply = () => imageReviewedApply();
window.wlLogs = (ns, pod, workload = "", fromRoute = false) => {
  if (!fromRoute && window.setModalRoute) setModalRoute({ panel: "logs", ns, workload: workload || pod }, (workload || pod) + " logs");
  if (!pod) return modal("Logs unavailable", '<div class="empty"><b>No running pod</b><br><span class="dim small">Start the container and wait for Kubernetes to create a pod.</span></div>');
  const workloadRow = (STATE.data.wl || []).find(x => x.ns === ns && x.name === workload);
  openLogs(workload || pod, `/api/logs?ns=${encodeURIComponent(ns)}&pod=${encodeURIComponent(pod)}`,
    workloadRow?.pods || null, ns);
};
window.jobLogs = (ns, job) => openLogs(job, `/api/logs?ns=${encodeURIComponent(ns)}&job=${encodeURIComponent(job)}`);


// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  shortImage, imageVersion, imageChangeWords, imagePlacementBlocker, UNCHANGED_BY_UPDATE, updateConcerns,
  groupedConcerns, clusterHeaders, rolloutKey, rolloutBody, restartsHomestead, rolloutChanged,
  IMAGE_REVIEW_OPEN, IMAGE_REVIEW, reviewImageActions, IMAGE_QUEUE, PROGRESS_MISSES, refreshAfterRollout,
  asSentence, paintRollout, batchUpdateMarkup, pullElapsed, pullDetail, pullSize, rolloutStages, rolloutProgress, rolloutMarkup
};
