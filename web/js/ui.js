/* Dialog components.

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
     UI.actions     the dialog's buttons: last, right-aligned, primary last

   and for pages:

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
      <div class="ui-bar" role="progressbar" ${known ? `aria-valuenow="${Math.round(pct)}"` : ""} aria-valuemin="0" aria-valuemax="100"><i style="width:${known ? pct : 35}%"></i></div>
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

  const more = (summary, bodyHtml, open = false) => `<details class="ui-more"${open ? " open" : ""}>
    <summary>${text(summary)}</summary><div class="ui-more-body">${bodyHtml}</div></details>`;

  /* The one checkbox a risky action needs. Keep the sentence short: what the
     person accepts, not everything that could happen. */
  const ack = (id, sentence, { onchange = "" } = {}) => `<label class="ui-ack">
    <input type="checkbox" id="${text(id)}"${onchange ? ` onchange="${text(onchange)}"` : ""}><span>${text(sentence)}</span></label>`;

  const fields = (...html) => `<div class="ui-fields">${html.join("")}</div>`;
  const field = (label, controlHtml, { help = "", tipHtml = "", wide = false } = {}) => `<div class="ui-field${wide ? " wide" : ""}">
    <label>${text(label)}${tipHtml}</label>${controlHtml}${help ? `<div class="ui-help">${text(help)}</div>` : ""}</div>`;

  const chip = (label, kind = "") => `<span class="ui-chip ${TONES.has(kind) ? kind : ""}">${text(label)}</span>`;

  /* kind: pri | danger | "" ; attrs are extra attributes, already escaped. */
  const button = (label, onclick, { kind = "", icon: iconName = "", id = "", attrs = "", disabled = false } = {}) =>
    `<button type="button" class="btn${kind ? ` ${kind}` : ""}"${id ? ` id="${text(id)}"` : ""} onclick="${text(onclick)}"${disabled ? " disabled" : ""} ${attrs}>${iconName ? icon(iconName) : ""}${text(label)}</button>`;

  /* The dialog's buttons. Pass the primary (or dangerous) button last; extra
     buttons that are not the main choice go in `start`, on the left. */
  const actions = (buttonsHtml, startHtml = "") => `<div class="ui-actions">
    ${startHtml ? `<div class="ui-actions-start">${startHtml}</div>` : ""}<div class="ui-actions-end">${buttonsHtml}</div></div>`;
  const cancel = (label = "Cancel") => button(label, "closeModal()");

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

  return { lead, callout, section, facts, checklist, steps, progress, meter, table, more, ack,
    fields, field, chip, button, actions, cancel, stats, guide };
})();
window.UI = UI;

/* Dialogs written before these components still have their buttons in a row
   somewhere in the body - sometimes with notes below them. The row of
   buttons that ends the dialog (Cancel, Close or a primary action) is moved
   to the end and styled as its actions, so every dialog closes the same way. */
function normaliseDialogActions(body) {
  if (!body || body.querySelector(".ui-actions")) return;
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
// Dialogs that draw their body again later (a check finishing, a step
// moving on) get the same treatment.
if (typeof MutationObserver === "function" && typeof document.querySelector === "function") {
  const body = document.querySelector("#mbody");
  if (body) new MutationObserver(() => normaliseDialogActions(body)).observe(body, { childList: true });
}

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
  // A page repaint can take a row away with its menu open.
  const sweep = () => document.querySelectorAll("details.actionmenu-portal").forEach(shell => {
    if (!shell.owner?.isConnected || !shell.owner.open) { if (shell.owner) shell.owner.shell = null; shell.remove(); }
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
    document.querySelectorAll("details.actionmenu[open]").forEach(d => {
      if (!d.contains(event.target) && !d.shell?.contains(event.target)) d.open = false;
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
