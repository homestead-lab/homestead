/* Shared application components.

   Every dialog is built from these, so they all read the same way and all
   work on a phone. docs/design.md says when to use which; in short:

     UI.lead        the one or two sentences that open a dialog
     UI.callout     the one thing that needs attention - at most one per dialog
     UI.section     a titled group of content
     UI.facts       label and value pairs
     UI.checklist   checks and their results
     UI.steps       a numbered guide, or where a long job has got to
     UI.progress    a bar for work under way
     UI.meter       how full something is, now and after a change
     UI.table       rows of items; on a phone each row becomes a card
     UI.more        detail most people do not need, behind a disclosure
     UI.ack         the confirmation a risky action needs
     UI.fields      a grid of form fields, one column on a phone
     UI.field       one labelled form control, with help beneath
     UI.chip        a short status label
     UI.button      a button
     UI.actions     one footer: dismissal left, primary action last on the right
     UI.sectionForm section rail, phone selector, mounted panes and footer
     UI.masterDetail grouped list and a selected detail, stacked on phones

   and for pages:

     UI.pageHeader  page title, summary and actions
     UI.moduleHeader shared card title, summary and actions
     UI.settingsCard Settings topic, save scope and content
     UI.workspace   section navigation and responsive page content
     UI.stats       a row of stat cards: a figure each, two to a row on a phone
     UI.guide       how a page works, collapsed - in place of notes at its top

   Each returns HTML, escaped where it takes plain text: arguments named
   *Html are inserted as they are. */
const UI = (() => {
  const text = value => esc(value ?? "");
  const TONES = new Set(["info", "ok", "warn", "bad"]);
  const tone = value => (TONES.has(value) ? value : "info");
  const TONE_ICON = { info: "info", ok: "check", warn: "alert", bad: "alert" };
  const svg = name => `<svg class="ui-icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;

  /* The opening sentences: what this dialog does, plainly. */
  const lead = html => `<p class="ui-lead">${html}</p>`;

  /* One notice. tone: info | ok | warn | bad. Use warn and bad only for real
     risk; information that is merely useful is info, or goes in UI.more. */
  const callout = (kind, title, bodyHtml = "") => {
    const t = tone(kind);
    return `<div class="ui-callout ${t}" role="${t === "bad" || t === "warn" ? "alert" : "note"}">
      <span class="ui-callout-icon">${svg(TONE_ICON[t])}</span>
      <div>${title ? `<b>${text(title)}</b>` : ""}${bodyHtml ? `<div class="ui-callout-body">${bodyHtml}</div>` : ""}</div>
    </div>`;
  };

  const section = (title, bodyHtml, asideHtml = "") => `<section class="ui-section">
    ${title || asideHtml ? `<header><h4>${text(title)}</h4>${asideHtml ? `<div class="ui-section-aside">${asideHtml}</div>` : ""}</header>` : ""}
    ${bodyHtml}</section>`;

  /* [[label, valueHtml], ...] - values are HTML so they can carry chips. */
  const facts = pairs => `<dl class="ui-facts">${pairs.filter(Boolean).map(([label, valueHtml]) =>
    `<div><dt>${text(label)}</dt><dd>${valueHtml}</dd></div>`).join("")}</dl>`;

  /* [{ state: ok|warn|bad|todo|run|skip, title, detailHtml }] */
  const STATE_ICON = { ok: "check", warn: "alert", bad: "x", todo: "", run: "", skip: "" };
  const checklist = items => `<ul class="ui-checklist">${items.filter(Boolean).map(item => {
    const state = item.state || "todo";
    return `<li class="${state}"><span class="ui-check-mark">${state === "run" ? '<span class="spin2"></span>' : STATE_ICON[state] ? svg(STATE_ICON[state]) : ""}</span>
      <div><div class="ui-check-title">${text(item.title)}</div>${item.detailHtml ? `<div class="ui-check-detail">${item.detailHtml}</div>` : ""}</div></li>`;
  }).join("")}</ul>`;

  /* [{ title, detailHtml }] and the index of the current step; steps before it
     are done. current = -1 shows a plain numbered guide. */
  const steps = (list, current = -1) => `<ol class="ui-steps${current < 0 ? " guide" : ""}">${list.map((step, i) => {
    const state = current < 0 ? "" : i < current ? "done" : i === current ? "current" : "todo";
    return `<li class="${state}"><span class="ui-step-num">${state === "done" ? svg("check") : i + 1}</span>
      <div><div class="ui-step-title">${text(step.title)}</div>${step.detailHtml ? `<div class="ui-step-detail">${step.detailHtml}</div>` : ""}</div></li>`;
  }).join("")}</ol>`;

  /* A bar for work under way: value 0-100, or null while it is unknown. */
  const progress = (value, { label = "", detail = "", kind = "info" } = {}) => {
    const known = value !== null && value !== undefined && Number.isFinite(Number(value));
    const pct = known ? Math.max(0, Math.min(100, Number(value))) : 0;
    return `<div class="ui-progress ${tone(kind)}${known ? "" : " unknown"}">
      ${label || known ? `<div class="ui-progress-head"><span>${text(label)}</span>${known ? `<b>${Math.round(pct)}%</b>` : ""}</div>` : ""}
      <div class="ui-bar" role="progressbar" aria-label="${text(label || "Progress")}" ${known ? `aria-valuenow="${Math.round(pct)}"` : ""} aria-valuemin="0" aria-valuemax="100"><i style="width:${known ? pct : 35}%"></i></div>
      ${detail ? `<div class="ui-progress-detail">${text(detail)}</div>` : ""}</div>`;
  };

  /* How full something is: now, and after the change (the lighter part).
     Past warnAt the change turns amber; past 100 red. */
  const meter = ({ now = 0, after = null, warnAt = 85, label = "" } = {}) => {
    const clamp = v => Math.max(0, Math.min(100, Number(v) || 0));
    const peak = after === null ? now : after;
    const kind = peak >= 100 ? "bad" : peak >= warnAt ? "warn" : "ok";
    return `<div class="ui-meter ${kind}" role="meter" aria-valuenow="${Math.round(peak)}" aria-valuemin="0" aria-valuemax="100"${label ? ` aria-label="${text(label)}"` : ""}>
      <i class="now" style="width:${clamp(now)}%"></i>
      ${after !== null ? `<i class="after" style="left:${clamp(now)}%;width:${Math.max(0, clamp(after) - clamp(now))}%"></i>` : ""}
      <b class="mark" style="left:${clamp(warnAt)}%"></b></div>`;
  };

  /* columns: [{ label, className }]; rows: [[cellHtml, ...]]. Each cell
     carries its column's label, which a phone shows beside it. */
  const table = (columns, rows, { empty = "Nothing to show." } = {}) => rows.length
    ? `<div class="ui-table-wrap"><table class="ui-table"><thead><tr>${columns.map(c => `<th class="${c.className || ""}">${text(c.label)}</th>`).join("")}</tr></thead>
      <tbody>${rows.map(row => `<tr>${row.map((cell, i) => `<td class="${columns[i]?.className || ""}" data-label="${text(columns[i]?.label || "")}">${cell}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`
    : `<div class="ui-empty">${text(empty)}</div>`;

  const more = (summary, bodyHtml, open = false, key = summary) => `<details class="ui-more" data-disclosure="${text(key)}"${open ? " open" : ""}>
    <summary>${text(summary)}</summary><div class="ui-more-body">${bodyHtml}</div></details>`;

  /* The one checkbox a risky action needs. Keep the sentence short: what the
     person accepts, not everything that could happen. */
  const ack = (id, sentence, { onchange = "" } = {}) => `<label class="ui-ack">
    <input type="checkbox" id="${text(id)}"${onchange ? ` onchange="${text(onchange)}"` : ""}><span>${text(sentence)}</span></label>`;

  const fields = (...html) => `<div class="ui-fields">${html.join("")}</div>`;
  const field = (label, controlHtml, { help = "", tipHtml = "", wide = false } = {}) => `<div class="ui-field${wide ? " wide" : ""}">
    <label>${text(label)}${tipHtml}</label>${controlHtml}${help ? `<div class="ui-help">${text(help)}</div>` : ""}</div>`;

  const statusDot = kind => `<i class="ui-status-dot ${["ok","bad","warn","info","neutral"].includes(kind)?kind:"neutral"}" aria-hidden="true"></i>`;
  const insightList = rows => `<div class="ui-insight-list">${rows.map(r=>`<div class="ui-insight-row">${statusDot(r.tone)}<div><b>${text(r.title)}</b><small>${text(r.detail)}</small></div><div class="ui-insight-action">${chip(r.label,r.tone)}${r.actionsHtml || (r.onclick?button(r.action || "Review",r.onclick):"")}</div></div>`).join("")}</div>`;

  const chip = (label, kind = "") => `<span class="ui-chip ${TONES.has(kind) ? kind : ""}">${text(label)}</span>`;

  /* kind: pri | danger | "" ; attrs are extra attributes, already escaped. */
  const button = (label, onclick, { kind = "", icon: iconName = "", id = "", attrs = "", disabled = false } = {}) =>
    `<button type="button" class="btn${kind ? ` ${kind}` : ""}"${id ? ` id="${text(id)}"` : ""} onclick="${text(onclick)}"${disabled ? " disabled" : ""} ${attrs}>${iconName ? icon(iconName) : ""}${text(label)}</button>`;

  /* The dialog's buttons. Pass the primary (or dangerous) button last; extra
     buttons that are not the main choice go in `start`, on the left. */
  const actions = (buttonsHtml, startHtml = "", { className = "", attrs = "" } = {}) => {
    if (typeof startHtml !== "string") startHtml = "";     // HTML for the left, never a flag
    // Existing callers may include Cancel among their actions. Move only explicitly
    // marked dismissals, preserving their original handler and keyboard order.
    const dismissals = [];
    const endHtml = buttonsHtml.replace(/<button\b[^>]*\bdata-dialog-dismiss="true"[^>]*>[\s\S]*?<\/button>/g, html => { dismissals.push(html); return ""; });
    const start = dismissals.join("") + startHtml;
    return `<div class="ui-actions${className ? ` ${text(className)}` : ""}" ${attrs}>
      ${start ? `<div class="ui-actions-start">${start}</div>` : ""}<div class="ui-actions-end">${endHtml}</div></div>`;
  };
  const cancel = (label = "Cancel") => button(label, "closeModal()", { attrs: 'data-dialog-dismiss="true"' });

  /* [{ title, value, unit, sub, tone, wide, tipHtml }]: tone ok | warn | bad | info
     tints the card; wide spans a whole row. */
  const stats = cards => `<div class="ui-stats">${cards.filter(Boolean).map(c => {
    const glow = { ok: "glow g-ok", warn: "glow g-warn", bad: "glow g-bad", info: "glow g-info" }[c.tone] || "flat";
    return `<div class="card ${glow} ui-stat${c.wide ? " statwide" : ""}"><div class="ctitle">${text(c.title)}${c.tipHtml || ""}</div>
      ${c.value !== undefined ? `<div class="bignum">${text(c.value)}${c.unit ? `<span class="unit">${text(c.unit)}</span>` : ""}</div>` : ""}
      ${c.bodyHtml || ""}${c.sub ? `<div class="csub">${text(c.sub)}</div>` : ""}</div>`;
  }).join("")}</div>`;

  /* How a page works, for whoever needs it: closed until opened. */
  const guide = (summary, bodyHtml) => `<details class="ui-guide"><summary>${text(summary)}</summary><div class="ui-guide-body">${bodyHtml}</div></details>`;

  /* Page and module headings use explicit HTML slots, like section/facts.
     Escape data at the call site; IDs and option labels are plain text. */
  const pageHeader = (titleHtml, descriptionHtml = "", actionsHtml = "", { extraHtml = "", descriptionAttrs = "", actionsClass = "", mobileSummary = "show" } = {}) =>
    `<div class="phead"><div><h2>${titleHtml}</h2>${descriptionHtml || descriptionAttrs ? `<p${mobileSummary === "omit" ? ' class="page-summary-optional"' : ""} ${descriptionAttrs}>${descriptionHtml}</p>` : ""}${extraHtml}</div>${actionsHtml ? `<div class="row page-actions${actionsClass ? ` ${text(actionsClass)}` : ""}">${actionsHtml}</div>` : ""}</div>`;
  const moduleHeader = (titleHtml, descriptionHtml = "", actionsHtml = "", { extraHtml = "", actionsClass = "" } = {}) =>
    `<div class="settings-card-head"><div><div class="ctitle">${titleHtml}</div>${descriptionHtml ? `<div class="csub">${descriptionHtml}</div>` : ""}${extraHtml}</div>${actionsHtml ? `<div class="row module-actions${actionsClass ? ` ${text(actionsClass)}` : ""}">${actionsHtml}</div>` : ""}</div>`;
  const settingsCard = (bodyHtml, { tab, id = "", save = "", wide = true, hidden = false } = {}) =>
    `<section class="card flat${wide ? " settings-wide" : ""}" data-tab="${text(tab)}"${id ? ` id="${text(id)}"` : ""}${save ? ` data-save="${text(save)}"` : ""}${hidden ? " hidden" : ""}>${bodyHtml}</section>`;
  const settingsGrid = (bodyHtml, tab) => `<div class="settings-grid" data-tab="${text(tab)}">${bodyHtml}</div>`;
  const saveBar = ({ id, messageId, save, discard, hidden = true }) =>
    `<div class="savebar" id="${text(id)}"${hidden ? " hidden" : ""}><span id="${text(messageId)}" role="status"></span>${button("Discard", discard)}${button("Save", save, {kind:"pri"})}</div>`;
  const collectionHeader = (controlsHtml, summaryHtml) => `<div class="collection-mobile-head"><div class="collection-mobile-toolbar">${controlsHtml}</div><div class="collection-mobile-summary">${summaryHtml}</div></div>`;
  const workspace = (navigationHtml, bodyHtml, { id = "", open = false, backLabel = "", back = "", currentLabel = "", guide = false, pickerHtml = "", contentId = "" } = {}) =>
    `<div class="settings-layout${guide ? " setup-layout" : ""}"${id ? ` id="${text(id)}"` : ""}${guide ? "" : ` data-open="${open ? 1 : 0}"`}>${pickerHtml}${navigationHtml}<div class="settings-main"${contentId ? ` id="${text(contentId)}"` : ""}>${backLabel ? `<nav class="workspace-location" aria-label="Section navigation"><button type="button" class="settings-back" onclick="${text(back)}">‹ ${text(backLabel)}</button><span class="workspace-current" aria-current="location">${text(currentLabel)}</span></nav>` : ""}${bodyHtml}</div></div>`;
  const workspaceNav = (items, { label, selected, onSelect, guide = false } = {}) =>
    `<nav class="settings-nav${guide ? " setup-nav" : ""}"${guide ? "" : ' role="tablist" aria-orientation="vertical"'} aria-label="${text(label)}">${items.map((item, i) =>
      `${item.group && item.group !== items[i - 1]?.group ? `<div class="${guide ? "setup-chapter" : "settings-nav-group"}">${text(item.group)}</div>` : ""}<button type="button"${guide ? "" : ` role="tab" aria-selected="${item.key === selected}" tabindex="${item.key === selected ? 0 : -1}"`} data-${guide ? "step" : "tab"}="${text(item.key)}" class="${guide ? "setup-step" : ""}${item.key === selected ? " on" : ""}"${item.ariaLabel ? ` aria-label="${text(item.ariaLabel)}"` : ""} onclick="${text(`UI.navigateWorkspace(this, () => ${onSelect(item.key)})`)}">${item.markerHtml || ""}${item.icon ? `<svg class="settings-nav-icon" aria-hidden="true"><use href="#i-${text(item.icon)}"/></svg>` : ""}<span>${guide ? text(item.label) : `<b>${text(item.label)}</b>${item.descriptionHtml ? `<small>${item.descriptionHtml}</small>` : ""}`}</span>${guide ? "" : '<i aria-hidden="true">›</i>'}</button>`).join("")}</nav>`;
  const navigateWorkspace = async (button, run) => {
    const nav = button.closest(".settings-nav"), label = nav?.getAttribute("aria-label");
    const key = button.dataset.tab ?? button.dataset.step;
    const restoreFocus = document.activeElement === button;
    await run();
    if (!restoreFocus) return;
    const current = [...document.querySelectorAll(".settings-nav")].find(el => el.getAttribute("aria-label") === label);
    const selected = [...(current?.querySelectorAll("button.on") || [])].find(el => (el.dataset.tab ?? el.dataset.step) === key);
    if (selected?.getClientRects().length) selected.focus({preventScroll:true});
    else current?.closest(".settings-layout")?.querySelector(".settings-back")?.focus({preventScroll:true});
  };
  const selectWorkspace = (root, key, { gridSelector = "", paneSelector = "" } = {}) => {
    if (!root) return;
    root.dataset.open = key ? "1" : "0";
    if (!key) {
      root.querySelector('.settings-nav button.on')?.focus({preventScroll:true});
      return;
    }
    const current = root.querySelector('.workspace-current');
    if (current) current.textContent = [...root.querySelectorAll('.settings-nav [data-tab]')].find(item => item.dataset.tab === key)?.querySelector('b')?.textContent || '';
    if (gridSelector) { const grid = root.querySelector(gridSelector); if (grid) grid.dataset.tab = key; }
    if (paneSelector) root.querySelectorAll(paneSelector).forEach(pane => { pane.hidden = pane.dataset.pane !== key; });
    root.querySelectorAll('.settings-nav [data-tab]').forEach(item => {
      const selected = item.dataset.tab === key;
      item.classList.toggle("on", selected); item.setAttribute("aria-selected", String(selected)); item.tabIndex = selected ? 0 : -1;
    });
  };

  return { lead, callout, section, facts, checklist, steps, progress, meter, table, more, ack,
    fields, field, chip, button, actions, cancel, stats, guide,
    pageHeader, moduleHeader, settingsCard, settingsGrid, saveBar, collectionHeader, workspace, workspaceNav, selectWorkspace, navigateWorkspace,
    sectionForm: dialogSectionForm, sectionNavigation: dialogSectionNavigation,
    statusDot, insightList, selectSection: selectDialogSection, masterDetail: dialogMasterDetail };
})();
window.UI = UI;

/* Dialogs written before these components still have their buttons in a row
   somewhere in the body - sometimes with notes below them. The row of
   buttons that ends the dialog (Cancel, Close or a primary action) is moved
   to the end and styled as its actions, so every dialog closes the same way. */
function normaliseDialogActions(body) {
  if (!body) return;
  const footer = [...body.children].find(el => el.classList.contains("ui-actions"));
  if (footer && footer !== body.lastElementChild) body.appendChild(footer);
  if (body.querySelector(".ui-actions")) return;
  const rows = [...body.children].filter(el => el.matches(".row, .modalactions") && el.children.length
    && [...el.children].every(child => child.matches("button, .btn, a.btn"))
    && [...el.children].some(child => child.matches(".pri, .danger") || /^(cancel|close|done)$/i.test(child.textContent.trim())));
  const row = rows[rows.length - 1];
  if (!row) return;
  row.removeAttribute("style");
  row.classList.remove("row", "modalactions");
  row.classList.add("ui-actions", "legacy");
  body.appendChild(row);
}
window.normaliseDialogActions = normaliseDialogActions;

/* One explanation box at most. A dialog's first plain note stays where it
   is; any further ones fold into a single "How this works" at its foot.
   Warnings, errors, good news and notes with controls in them are left
   alone - they need to be seen, or used. */
function foldDialogNotes(body) {
  if (!body) return;
  const panes = body.querySelectorAll(".stepper-pane");
  if (panes.length) { panes.forEach(foldDialogNotes); return; }
  const plain = [...body.querySelectorAll(".note")].filter(note => !note.matches(".warn, .bad, .good, .crit, .dependency-danger")
    && !note.id && !note.hidden && !note.closest(".dialog-more, .ui-more, [hidden]")
    && !note.querySelector("button, input, select, textarea, a[onclick], .btn") && note.textContent.trim());
  if (plain.length < 2) return;
  let more = body.querySelector(":scope > details.dialog-more");
  if (!more) {
    more = document.createElement("details");
    more.className = "ui-more dialog-more";
    more.innerHTML = '<summary>How this works</summary><div class="ui-more-body"></div>';
    const actions = body.querySelector(":scope > .ui-actions, :scope .stepper-foot");
    if (actions) actions.parentNode.insertBefore(more, actions); else body.appendChild(more);
  }
  const into = more.querySelector(".ui-more-body");
  plain.slice(1).forEach(note => { const p = document.createElement("p"); p.innerHTML = note.innerHTML; into.appendChild(p); note.remove(); });
}
window.foldDialogNotes = foldDialogNotes;
// Dialogs that draw their body again later (a check finishing, a step
// moving on) get the same treatment.
if (typeof MutationObserver === "function" && typeof document.querySelector === "function") {
  const body = document.querySelector("#mbody");
  if (body) new MutationObserver(() => { normaliseDialogActions(body); foldDialogNotes(body); restoreDialogDisclosures(body); }).observe(body, { childList: true, subtree: true });
}

/* A page header's ⋯: the actions besides its main one. Each item is
   { label, run, icon?, need?, tip?, danger? }. run goes into the onclick
   attribute as it is: pass values through jsq(), as every handler does. Falsy
   items are left out, and
   with none left there is no menu. A header shows one main button and this
   beside it, so it fits on one line at any width. */
function menuItems(items) {
  return (items || []).filter(Boolean).map(item => `<button type="button"${item.need ? ` data-need="${esc(item.need)}"` : ""}${item.tip ? ` title="${esc(item.tip)}"` : ""}${item.danger ? ' class="danger"' : ""}
      onclick="this.closest('details').open=false;${item.run}">${item.icon ? icon(item.icon) : ""}${esc(item.label)}</button>`).join("");
}
function moreMenu(items, label = "More actions", summaryClass = "btn") {
  const rows = (items || []).filter(Boolean);
  if (!rows.length) return "";
  const dots = summaryClass === "btn";
  return `<details class="actionmenu headmenu"><summary class="${summaryClass}"${dots ? ` aria-label="${esc(label)}"` : ""}>${dots ? "⋯" : esc(label)}</summary>
    <div class="actionmenu-pop">${menuItems(rows)}</div></details>`;
}
/* Collection preferences share the action menu's placement, dismissal and
   role checks. Controls stay inside it rather than adding toolbar rows. */
function listOptions(controls, items) {
  return `<details class="actionmenu headmenu list-options"><summary class="btn" aria-label="List options"><span class="list-options-label">Options</span>${icon("sliders")}</summary>
    <div class="actionmenu-pop list-options-pop">${controls}${menuItems(items)}</div></details>`;
}
/* The top of a page: one line saying how things stand, and the numbers
   behind it a tap away. Items are HTML; the detail stays open across the
   page's own refreshes until closed. */
function summaryLine(id, items, detail, label = "Details") {
  const open = !!(STATE.sumOpen || {})[id];
  return `<div class="sumline">${(items || []).filter(Boolean).map(item => `<span class="sumitem">${item}</span>`).join("")}
    ${detail ? `<button type="button" class="sumtoggle" aria-expanded="${open}" aria-controls="sum-${id}" onclick="summaryToggle(${jsq(id)},this)">${esc(label)}<span class="chev" aria-hidden="true">▾</span></button>` : ""}</div>
    ${detail ? `<div class="sumdetail" id="sum-${id}"${open ? "" : " hidden"}>${detail}</div>` : ""}`;
}
function summaryToggle(id, button) {
  STATE.sumOpen = STATE.sumOpen || {};
  const open = !STATE.sumOpen[id];
  STATE.sumOpen[id] = open;
  const detail = document.getElementById(`sum-${id}`);
  if (detail) detail.hidden = !open;
  if (button) button.setAttribute("aria-expanded", String(open));
}
if (typeof window !== "undefined") { window.summaryLine = summaryLine; window.summaryToggle = summaryToggle; }

/* A card's or a row's buttons: the first `shown` as buttons, the rest in the
   same ⋯ menu the page headers use, so every card and row reads the same way
   and none runs onto a second line. Items as for moreMenu, plus pri. */
function actionBar(items, { shown = 2, label = "More actions", iconOnly = false } = {}) {
  const rows = (items || []).filter(Boolean);
  const button = item => `<button type="button" class="btn sm${iconOnly ? " iconic" : ""}${item.pri ? " pri" : ""}${item.danger ? " danger" : ""}"${item.need ? ` data-need="${esc(item.need)}"` : ""}${item.tip ? ` title="${esc(item.tip)}"` : ""}${item.ariaLabel ? ` aria-label="${esc(item.ariaLabel)}"` : ""}${item.disabled ? " disabled" : ""}
    onclick="${item.run}">${item.icon ? icon(item.icon) : ""}${iconOnly && item.icon ? `<span class="sr-only">${esc(item.label)}</span>` : esc(item.label)}</button>`;
  const rest = rows.slice(shown);
  return `<div class="actionbar">${rows.slice(0, shown).map(button).join("")}${rest.length ? `<details class="actionmenu"><summary class="btn sm" aria-label="${esc(label)}">⋯</summary>
    <div class="actionmenu-pop">${rest.map(item => `<button type="button"${item.need ? ` data-need="${esc(item.need)}"` : ""}${item.tip ? ` title="${esc(item.tip)}"` : ""}${item.danger ? ' class="danger"' : ""}${item.disabled ? " disabled" : ""}
      onclick="this.closest('details').open=false;${item.run}">${item.icon ? icon(item.icon) : ""}${esc(item.label)}</button>`).join("")}</div></details>` : ""}</div>`;
}
if (typeof window !== "undefined") window.actionBar = actionBar;

/* A setting: its label and one line of help on the left, its control on
   the right - a switch, a number and its unit, a choice, a short text. */
function settingRow(label, help, control) {
  return `<div class="srow"><div class="srow-l"><b>${label}</b>${help ? `<small>${help}</small>` : ""}</div><div class="srow-c">${control}</div></div>`;
}
/* Something Homestead runs or talks to: its name and state, what it is doing,
   and its buttons (an actionBar) - its settings open in a dialog. */
function serviceRow(name, state, detail, actions) {
  return `<div class="svcrow"><div class="svcrow-l"><b>${name}</b>${state ? ` ${state}` : ""}${detail ? `<small>${detail}</small>` : ""}</div>${actions || ""}</div>`;
}
if (typeof window !== "undefined") { window.settingRow = settingRow; window.serviceRow = serviceRow; }

/* Shared navigation for section forms and externally managed setup guides.
   Handlers are JavaScript, escaped here once; keys and titles are plain text. */
function dialogSectionNavigation(id, sections, { current = 0, always = false, onSelect = key => `UI.selectSection(${jsArg(id)},${jsArg(key)})`, onChange = `UI.selectSection(${jsArg(id)},this.value)`, guide = false } = {}) {
  const label = always ? "Section" : "Step";
  const keyOf = (section, i) => String(section.key ?? i);
  return `<label class="dialog-section-picker">${label}<select id="${esc(id)}_section" aria-label="${label}" onchange="${esc(onChange)}">${sections.map((section, i) => `<option value="${esc(keyOf(section, i))}"${i === current ? " selected" : ""}>${esc(section.title)}${always ? "" : ` · ${i + 1} of ${sections.length}`}</option>`).join("")}</select></label>
    <div class="stepper-head" ${guide ? 'aria-label="Setup steps"' : `role="tablist" aria-label="${always ? "Sections" : "Steps"}" aria-orientation="vertical"`}>${sections.map((section, i) => {
      const key = keyOf(section, i), selected = i === current;
      return `<button type="button" ${guide ? (selected ? 'aria-current="step"' : '') : `role="tab" id="${esc(id)}-tab-${esc(key)}" aria-controls="${esc(id)}-pane-${esc(key)}" aria-selected="${selected}" tabindex="${selected ? 0 : -1}"`} class="stepper-chip${selected ? " on" : ""}" data-i="${i}" data-key="${esc(key)}" onclick="${esc(onSelect(key))}">${esc(section.title)}</button>`;
    }).join("")}</div>`;
}

/* All panes stay mounted, preserving unsaved fields when moving between sections.
   Create forms advance with Next; editors expose their save/review on every section. */
function dialogSectionForm(id, sections, finishHtml, { always = false, noticeHtml = "", cancelHtml = UI.cancel(), page = false } = {}) {
  const list = sections.filter(Boolean);
  if (!list.length) throw new Error("A section form needs at least one section");
  return `<div class="stepper dialog-rail${always ? " section-editor" : ""}${page ? " section-page" : ""}" id="${esc(id)}" data-step="0">
    ${dialogSectionNavigation(id, list, { always })}
    ${list.map((section, i) => {
      const key = String(section.key ?? i);
      return `<div class="stepper-pane" role="tabpanel" aria-labelledby="${esc(id)}-tab-${esc(key)}" id="${esc(id)}-pane-${esc(key)}" data-i="${i}" data-key="${esc(key)}"${i ? " hidden" : ""}>${section.lead ? UI.lead(esc(section.lead)) : ""}${section.html}</div>`;
    }).join("")}
    ${noticeHtml ? `<div class="dialog-rail-notice">${noticeHtml}</div>` : ""}
    ${UI.actions((always ? finishHtml : `<span data-finish${list.length > 1 ? " hidden" : ""}>${finishHtml}</span>`) +
      (!always && list.length > 1 ? UI.button("Next", `stepGo(${jsArg(id)},1,true)`, {kind:"pri",attrs:"data-next"}) : ""),
      cancelHtml + (always ? "" : UI.button("Back", `stepGo(${jsArg(id)},-1,true)`, {attrs:"data-back hidden"})), {className:"stepper-foot"})}
  </div>`;
}
function selectDialogSection(id, key) {
  const root = document.getElementById(id);
  const panes = [...(root?.querySelectorAll(":scope > .stepper-pane") || [])];
  const index = panes.findIndex(pane => pane.dataset.key === String(key));
  if (index >= 0) stepGo(id, index);
}
// Compatibility for existing create/edit callers; layout lives in sectionForm.
function stepper(id, sections, finishHtml, options) {
  return UI.sectionForm(id, sections, finishHtml, options);
}

/* A grouped list and one selected detail, stacked on phones. Domain actions and
   polling stay with the caller; this owns navigation and disclosure structure. */
function dialogMasterDetail(groups, selectedKey, detailHtml, { label = "Items", detailLabel = "Selected item", onSelect } = {}) {
  const rows = items => items.map(item => `<button type="button"${String(item.key) === String(selectedKey) ? ' aria-current="true"' : ''} onclick="${esc(onSelect(item.key))}"><span>${item.tone ? UI.statusDot(item.tone) : ""}${esc(item.title)}</span>${item.detail ? `<small${item.attention ? ' class="needs-attention"' : ''}>${esc(item.detail)}</small>` : ""}</button>`).join("");
  return `<div class="dialog-rail dialog-master-detail"><nav class="dialog-master-nav" aria-label="${esc(label)}">${groups.filter(group => group.items.length).map(group =>
    group.collapsed ? UI.more(group.title, rows(group.items), group.items.some(item => String(item.key) === String(selectedKey)), group.key) : UI.section(group.title, rows(group.items))).join("")}</nav>
    <section class="dialog-master-content" aria-label="${esc(detailLabel)}">${detailHtml}</section></div>`;
}
function stepGo(id, to, relative) {
  const root = document.getElementById(id);
  if (!root) return;
  const panes = [...root.querySelectorAll(":scope > .stepper-pane")], chips = [...root.querySelectorAll(".stepper-chip")];
  const next = Math.max(0, Math.min(panes.length - 1, relative ? +root.dataset.step + to : to));
  root.dataset.step = next;
  panes.forEach((pane, i) => { pane.hidden = i !== next; });
  chips.forEach((chip, i) => { chip.classList.toggle("on", i === next); chip.setAttribute("aria-selected", String(i === next)); chip.tabIndex = i === next ? 0 : -1; });
  const last = next === panes.length - 1, nextButton = root.querySelector("[data-next]");
  const back = root.querySelector("[data-back]"); if (back) back.hidden = !next;
  const picker = root.querySelector(".dialog-section-picker select"); if (picker) picker.value = panes[next]?.dataset.key ?? String(next);
  if (nextButton) { nextButton.hidden = last; if (!last) nextButton.textContent = "Next"; }
  const finish = root.querySelector("[data-finish]");
  if (finish) finish.hidden = !last;
  const chip = chips[next], head = chip?.parentElement;
  if (head && head.scrollWidth > head.clientWidth) {
    const item = chip.getBoundingClientRect(), strip = head.getBoundingClientRect();
    if (item.left < strip.left) head.scrollLeft -= strip.left - item.left;
    else if (item.right > strip.right) head.scrollLeft += item.right - strip.right;
  }
  const body = root.closest("#mbody") || root.closest(".modalbox");
  if (body) { body.scrollTop = 0; const dialog = body.closest(".modalbox"); if (dialog) dialog.scrollTop = 0; }
}
if (typeof window !== "undefined") { window.stepper = stepper; window.stepGo = stepGo; }

/* A main button that opens a choice - ＋ Import and its kinds. */
const menuButton = (label, items) => moreMenu(items, label, "btn pri");
if (typeof window !== "undefined") { window.moreMenu = moreMenu; window.menuButton = menuButton; }

/* A "…" menu opens against the screen, not its row: below its button when
   there is room, above when not. A card's blur or overflow still clips a
   fixed box inside it - the card, not the screen, is what it is placed in -
   so an open menu moves to the page itself, in an open <details> of its own
   so its items' this.closest('details').open=false still closes it, and goes
   back when it closes. One open menu at a time; a click elsewhere, a scroll
   or Escape closes it. */
if (typeof document !== "undefined" && typeof document.addEventListener === "function"
    && typeof window !== "undefined" && typeof window.addEventListener === "function") {
  const place = (button, pop) => {
    const at = button.getBoundingClientRect();
    pop.style.position = "fixed";
    pop.style.bottom = "auto";
    pop.style.right = "auto";
    const width = pop.offsetWidth, height = pop.offsetHeight;
    const below = at.bottom + 6 + height <= window.innerHeight - 8;
    pop.style.top = `${below ? at.bottom + 6 : Math.max(8, at.top - 6 - height)}px`;
    pop.style.left = `${Math.max(8, Math.min(at.right - width, window.innerWidth - width - 8))}px`;
  };
  const lift = details => {
    const pop = details.querySelector(":scope > .actionmenu-pop"), button = details.querySelector("summary");
    if (!pop || !button) return;
    const shell = document.createElement("details");
    shell.className = "actionmenu-portal";
    shell.appendChild(document.createElement("summary"));
    shell.appendChild(pop);
    shell.open = true;
    shell.owner = details;
    details.shell = shell;
    document.body.appendChild(shell);
    place(button, pop);
  };
  const lower = details => {
    const shell = details.shell;
    if (!shell) return;
    details.shell = null;
    const pop = shell.querySelector(".actionmenu-pop");
    if (pop && details.isConnected) {
      ["position", "top", "left", "right", "bottom"].forEach(key => { pop.style[key] = ""; });
      details.appendChild(pop);
    }
    shell.remove();
    if (details.open) details.open = false;
  };
  window.closeActionMenu = details => lower(details.owner || details);
  // A page repaint can take a row away with its menu open.
  const sweep = () => document.querySelectorAll("details.actionmenu-portal").forEach(shell => {
    if (!shell.owner?.isConnected || !shell.owner.open) { if (shell.owner) shell.owner.shell = null; shell.remove(); }
  });
  document.addEventListener("click", event => {
    const summary = event.target.closest?.("#mbody details[data-disclosure] > summary");
    if (summary) dialogDisclosureState.set(dialogDisclosureKey(summary.parentElement), !summary.parentElement.open);
  });
  document.addEventListener("toggle", event => {
    const details = event.target;
    if (details.matches?.("details.actionmenu-portal")) {
      if (!details.open && details.owner) lower(details.owner);
      return;
    }
    if (!details.matches?.("details.actionmenu")) return;
    if (!details.open) return lower(details);
    document.querySelectorAll("details.actionmenu[open]").forEach(other => { if (other !== details) other.open = false; });
    sweep();
    if (!details.shell) lift(details);
  }, true);
  document.addEventListener("click", event => {
    const path = event.composedPath();
    document.querySelectorAll("details.actionmenu[open]").forEach(d => {
      // A control can redraw itself while this click is still bubbling
      // (sorting does). Its original event path still belongs to the menu.
      if (!d.contains(event.target) && !d.shell?.contains(event.target) && !path.includes(d) && !path.includes(d.shell)) d.open = false;
    });
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") document.querySelectorAll("details.actionmenu[open]").forEach(d => { d.open = false; });
  });
  window.addEventListener("scroll", event => {
    if (event.target?.closest?.(".actionmenu-portal")) return;
    document.querySelectorAll("details.actionmenu[open]").forEach(d => { d.open = false; });
  }, { passive: true, capture: true });
  setInterval(sweep, 1000);
}

/* A small comparison keeps items as columns, so the same metric lines up.
   Four columns fit on a phone; larger comparisons scroll within the matrix.
   Like UI.table, headings are text and values are already escaped HTML. */
function comparisonTable(columns, rows, caption) {
  return `<div class="tblwrap comparison-scroll" role="region" aria-label="${esc(caption)}"${columns.length > 4 ? ' tabindex="0"' : ""}><table class="tbl stack comparison-table" style="--comparison-columns:${columns.length}">
    <caption class="sr-only">${esc(caption)}</caption><thead><tr><th scope="col">Node</th>
    ${columns.map(c => `<th scope="col"${c.selected ? ' class="selected"' : ''}${c.attrs || ""}>${c.html}</th>`).join("")}</tr></thead>
    <tbody>${rows.map(r => `<tr${r.phone === false ? ' class="comparison-extra"' : ''}><th scope="row">${esc(r.label)}</th>
      ${r.values.map((v, i) => `<td${columns[i].selected ? ' class="selected"' : ''}${columns[i].attrs || ""}>${v}</td>`).join("")}</tr>`).join("")}</tbody>
    </table></div>`;
}

/* A disclosure changes how much of an item is shown; its actions remain in
   actionBar. A native button makes the whole title keyboard accessible. */
function collectionDisclosure({ label, bodyHtml, expanded, controls, run, id }) {
  return `<button type="button" class="collection-disclosure" id="${esc(id)}" aria-label="${esc(label)}"
    aria-expanded="${!!expanded}" aria-controls="${esc(controls)}" onclick="${run}">${bodyHtml}<span class="collection-chevron" aria-hidden="true">${icon("chevron-down")}</span></button>`;
}

/* Disclosures survive polling within a dialog. New dialogs reset this memory;
   the dialog stack keeps the original DOM and its disclosure state. */
let dialogDisclosureState = new Map();
const restoredDialogDisclosures = new WeakSet();
window.resetDialogDisclosures = () => { dialogDisclosureState = new Map(); };
function dialogDisclosureKey(detail) {
  return `${detail.closest("[data-operation], .stepper-pane")?.dataset.operation || detail.closest(".stepper-pane")?.id || ""}:${detail.dataset.disclosure}`;
}
function restoreDialogDisclosures(body) {
  body?.querySelectorAll("details[data-disclosure]").forEach(detail => {
    if (restoredDialogDisclosures.has(detail)) return;
    restoredDialogDisclosures.add(detail);
    const key = dialogDisclosureKey(detail);
    if (dialogDisclosureState.has(key)) detail.open = dialogDisclosureState.get(key);
  });
}
window.revealDialogField = field => {
  if (typeof field === "string") field = document.querySelector(field);
  if (!field) return;
  const pane = field.closest(".stepper-pane");
  if (pane) stepGo(pane.closest(".stepper").id, Number(pane.dataset.i));
  for (let parent = field.parentElement; parent; parent = parent.parentElement) {
    if (parent.matches("details")) parent.open = true;
  }
  field.scrollIntoView({ block: "nearest" }); field.focus();
};
if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
  document.addEventListener("click", event => {
    const summary = event.target.closest?.("#mbody details[data-disclosure] > summary");
    if (summary) dialogDisclosureState.set(dialogDisclosureKey(summary.parentElement), !summary.parentElement.open);
  });
  document.addEventListener("toggle", event => {
    const detail = event.target;
    if (detail.matches?.("#mbody details[data-disclosure]"))
      dialogDisclosureState.set(dialogDisclosureKey(detail), detail.open);
  }, true);
  document.addEventListener("keydown", event => {
    const tab = event.target.closest?.('.dialog-rail [role="tab"], .settings-nav [role="tab"]');
    if (!tab || !["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...tab.parentElement.querySelectorAll('[role="tab"]')], current = tabs.indexOf(tab);
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (current + (event.key === "ArrowDown" ? 1 : -1) + tabs.length) % tabs.length;
    tabs[next].focus(); tabs[next].click();
  });
  document.addEventListener("invalid", event => {
    if (event.target.closest?.("#mbody")) window.revealDialogField(event.target);
  }, true);
}
