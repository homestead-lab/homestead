/* Logos for apps that are already running, picked from the app store.

   wlLogo opens the picker for one app; the edit dialog's Find button shows the
   same tiles under its logo field; logoFixup lists every app with no logo and
   its best guess. A logo that matches the app's own image comes first: that is
   the same software, so nearly always the right picture. Saving changes only
   the Deployment's annotations, so nothing restarts. */
const LOGO = { ns: "", name: "", tiles: [], chosen: -1, timer: 0, field: "", seq: 0, back: null, missing: [] };

const logoQuery = (ns, name, q) =>
  `/api/logos?ns=${encodeURIComponent(ns)}&name=${encodeURIComponent(name)}${q === null ? "" : `&q=${encodeURIComponent(q)}`}`;

const logoMatchTag = match => match === "image"
  ? '<span class="pill slim ok" data-tip="This app store entry uses the same image as the app">image match</span>'
  : '<span class="pill slim neutral">name</span>';

function logoTilesHtml(tiles, chosen, onpick) {
  if (!tiles.length) return '<div class="empty">Nothing in the app store matches. Try another name, or paste a URL.</div>';
  return `<div class="logo-tiles" role="listbox" aria-label="Logos">${tiles.map((t, i) => `<button type="button" role="option"
    class="logo-tile${i === chosen ? " on" : ""}" aria-selected="${i === chosen}" onclick="${onpick}(${i})">
    ${appAvatar(t.name, t.icon, "av-logo")}<span class="logo-name">${esc(t.name)}</span>${logoMatchTag(t.match)}</button>`).join("")}</div>`;
}

async function logoLoad(q, paintTo, onpick) {
  const seq = ++LOGO.seq;
  $(paintTo).innerHTML = '<div class="empty"><span class="spin2"></span> Searching the app store…</div>';
  try {
    const found = await api(logoQuery(LOGO.ns, LOGO.name, q));
    if (seq !== LOGO.seq || !$(paintTo)) return;
    LOGO.tiles = found.tiles || [];
    LOGO.chosen = LOGO.tiles[0]?.match === "image" && q === null ? 0 : -1;
    $(paintTo).innerHTML = logoTilesHtml(LOGO.tiles, LOGO.chosen, onpick);
  } catch (e) {
    if (seq === LOGO.seq && $(paintTo)) $(paintTo).innerHTML = UI.callout("bad", "The app store could not be searched", esc(e.message));
  }
  logoSyncSave();
}

function logoSyncSave() {
  const go = $("#lg_go");
  if (go) go.disabled = LOGO.chosen < 0 && !($("#lg_url")?.value || "").trim();
}

/* One app: the picker as its own dialog. back reopens what sent you here. */
window.wlLogo = (ns, name, back = null) => {
  const w = (STATE.data.wl || []).find(x => x.ns === ns && x.name === name) || {};
  Object.assign(LOGO, { ns, name, tiles: [], chosen: -1, back });
  modal(`Logo · ${name}`, `<div class="ui-stack">
    ${UI.lead("Pick a logo from the app store. Homestead keeps its own copy, so it stays if the store drops the app. Nothing restarts.")}
    <div class="f"><label for="lg_q">Search the app store</label><input id="lg_q" type="search" value="${esc(name)}" autocomplete="off"
      oninput="logoSearchSoon()"></div>
    <div id="lg_tiles"></div>
    ${UI.more("Use an address instead", `<div class="f"><label for="lg_url">Image or site address</label><input id="lg_url" type="url" placeholder="https://…/logo.png or https://example.com" oninput="logoSyncSave()">
      <div class="ui-help">A site's own logo is used if it is an SVG or at least 64 px.</div></div>`)}
    ${UI.actions((back ? UI.button("Back", "logoBack()") : UI.cancel())
      + (w.icon ? UI.button("Remove logo", "logoSave(true)", { attrs: 'data-need="operator"' }) : "")
      + UI.button("Use this logo", "logoSave()", { kind: "pri", id: "lg_go", disabled: true, attrs: 'data-need="operator"' }))}</div>`);
  logoLoad(null, "#lg_tiles", "logoPick");
};

window.logoSearchSoon = (field = "#lg_q", paintTo = "#lg_tiles", onpick = "logoPick") => {
  clearTimeout(LOGO.timer);
  LOGO.timer = setTimeout(() => logoLoad(($(field)?.value || "").trim(), paintTo, onpick), 300);
};

window.logoPick = i => {
  LOGO.chosen = i;
  $$("#lg_tiles .logo-tile").forEach((b, j) => { b.classList.toggle("on", j === i); b.setAttribute("aria-selected", String(j === i)); });
  logoSyncSave();
};

window.logoBack = () => { const back = LOGO.back; LOGO.back = null; back ? back() : closeModal(); };

async function logoPost(items) {
  return api("/api/workloads/logo", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items }) });
}

window.logoSave = async (remove = false) => {
  const url = ($("#lg_url")?.value || "").trim();
  const icon = remove ? "" : url || LOGO.tiles[LOGO.chosen]?.icon || "";
  if (!remove && !icon) return toast("Choose a logo first", "bad");
  const go = $("#lg_go");
  if (go) go.disabled = true;
  try {
    const result = await logoPost([{ ns: LOGO.ns, name: LOGO.name, icon }]);
    toast(result.detail, result.ok ? "ok" : "bad");
    await refreshWorkloadsAfterLogo();
    logoBack();
  } catch (e) { toast(e.message, "bad"); if (go) go.disabled = false; }
};

async function refreshWorkloadsAfterLogo() {
  try { STATE.data.wl = await api("/api/workloads"); } catch (e) { /* the next refresh shows it */ }
  if (typeof renderWorkloads === "function" && STATE.view === "workloads") renderWorkloads();
}

/* In the edit dialog: the same tiles under its logo field, filling it in. */
window.logoFindInline = (ns, name, field) => {
  Object.assign(LOGO, { ns, name, field, tiles: [], chosen: -1 });
  const box = $(`#${field}_find`);
  if (!box) return;
  if (box.dataset.open) { box.innerHTML = ""; delete box.dataset.open; return; }
  box.dataset.open = "1";
  box.innerHTML = `<div class="logo-inline"><input id="${field}_q" type="search" aria-label="Search the app store" value="${esc(name)}"
    oninput="logoSearchSoon(${jsq(`#${field}_q`)},${jsq(`#${field}_tiles`)},${jsq("logoPickInline")})"><div id="${field}_tiles"></div></div>`;
  logoLoad(null, `#${field}_tiles`, "logoPickInline");
};

window.logoPickInline = i => {
  const t = LOGO.tiles[i], input = $(`#${LOGO.field}`);
  if (!t || !input) return;
  input.value = t.icon;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  $$(`#${LOGO.field}_tiles .logo-tile`).forEach((b, j) => { b.classList.toggle("on", j === i); b.setAttribute("aria-selected", String(j === i)); });
};

/* Every app with no logo, with a best guess beside each. Image matches are
   ticked; name matches are offered unticked; the rest are chosen one by one. */
window.logoCanHave = w => !w.icon && !w.has_logo && !w.logo_skipped && !w.platform && !w.self && !w.homestead
  && !w.managed_smb && !w.managed_nfs && !w.site;

window.logoFixup = async () => {
  modal("Apps without a logo", '<div class="empty"><span class="spin2"></span> Matching apps with the app store…</div>');
  let found;
  try { found = await api("/api/logos/missing"); }
  catch (e) { $("#mbody").innerHTML = UI.callout("bad", "The app store could not be read", esc(e.message)) + UI.actions(UI.cancel("Close")); return; }
  LOGO.missing = found.apps || [];
  const rows = LOGO.missing;
  if (!rows.length) { $("#mbody").innerHTML = UI.callout("ok", "Every app has a logo") + UI.actions(UI.cancel("Close")); return; }
  const images = rows.filter(r => r.suggestion?.match === "image").length, named = rows.filter(r => r.suggestion?.match === "name").length;
  const parts = [images && `${images} match their image`, named && `${named} by name only`, rows.length - images - named && `${rows.length - images - named} not found`].filter(Boolean);
  $("#mbody").innerHTML = `<div class="ui-stack">
    ${UI.lead(esc(`${rows.length} app${rows.length === 1 ? "" : "s"} have no logo: ${parts.join(", ")}. Tick the ones to use; image matches are ticked for you.`))}
    <div class="logo-fix">${rows.map((r, i) => {
      const s = r.suggestion;
      return `<div class="logo-fix-row">
        <label class="logo-fix-pick">${s ? `<input type="checkbox" data-i="${i}" ${s.match === "image" ? "checked" : ""} onchange="logoFixCount()">` : '<span class="logo-fix-nobox" aria-hidden="true"></span>'}
          ${appAvatar(r.name, "")}<span class="logo-fix-name"><b>${esc(r.name)}</b><span class="dim xs mono">${esc(r.images[0] || r.ns)}</span></span></label>
        <span class="logo-fix-guess">${s ? `${appAvatar(s.name, s.icon)}<span class="logo-fix-name"><b>${esc(s.name)}</b>${logoMatchTag(s.match)}</span>` : '<span class="dim small">No match</span>'}</span>
        <span class="logo-fix-acts">${UI.button("Choose…", `wlLogo(${jsArg(r.ns)},${jsArg(r.name)},logoFixup)`, { attrs: 'data-need="operator"' })}
          ${UI.button("No logo", `logoSkip(${i})`, { attrs: `data-need="operator" title="Leave ${esc(r.name)} without a logo and stop counting it"` })}</span></div>`;
    }).join("")}</div>
    ${UI.actions(UI.cancel("Close") + UI.button("Use ticked logos", "logoFixApply()", { kind: "pri", id: "lg_fix_go", attrs: 'data-need="operator"' }))}</div>`;
  logoFixCount();
};

window.logoFixCount = () => {
  const n = $$("#mbody .logo-fix input:checked").length, go = $("#lg_fix_go");
  if (go) { go.textContent = n ? `Use ${n} logo${n === 1 ? "" : "s"}` : "Use ticked logos"; go.disabled = !n; }
};

window.logoFixApply = async () => {
  const items = $$("#mbody .logo-fix input:checked").map(box => LOGO.missing[+box.dataset.i])
    .filter(r => r?.suggestion).map(r => ({ ns: r.ns, name: r.name, icon: r.suggestion.icon }));
  if (!items.length) return;
  const go = $("#lg_fix_go");
  if (go) { go.disabled = true; go.textContent = "Saving…"; }
  try {
    const result = await logoPost(items);
    toast(result.detail, result.ok ? "ok" : "bad");
    await refreshWorkloadsAfterLogo();
    logoFixup();
  } catch (e) { toast(e.message, "bad"); logoFixCount(); }
};

window.logoSkip = async i => {
  const r = LOGO.missing[i];
  if (!r) return;
  try {
    const result = await logoPost([{ ns: r.ns, name: r.name, skip: true }]);
    toast(result.detail, "ok");
    await refreshWorkloadsAfterLogo();
    logoFixup();
  } catch (e) { toast(e.message, "bad"); }
};

