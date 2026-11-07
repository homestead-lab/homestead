/* Homestead core: the shared tooltip, row actions and table sorting. A classic script, loaded right after core.js (#911). */
/* ---------------- shared, viewport-safe tooltips ---------------- */
let tooltipOwner = null;
function tooltipTarget(node) {
  return node instanceof Element ? node.closest("[data-tip],[title]") : null;
}
function hideTooltip(owner) {
  if (owner && owner !== tooltipOwner) return;
  const bubble = $("#uiTooltip");
  if (bubble) bubble.hidden = true;
  if (tooltipOwner) tooltipOwner.removeAttribute("aria-describedby");
  tooltipOwner = null;
}
function showTooltip(owner) {
  if (!owner) return;
  const nativeTitle = owner.getAttribute("title");
  if (nativeTitle && !owner.dataset.tip) owner.dataset.tip = nativeTitle;
  if (nativeTitle) owner.removeAttribute("title");
  const message = owner.dataset.tip;
  if (!message) return;

  let bubble = $("#uiTooltip");
  if (!bubble) {
    bubble = document.createElement("div");
    bubble.id = "uiTooltip";
    bubble.className = "uitooltip";
    bubble.setAttribute("role", "tooltip");
    document.body.appendChild(bubble);
  }
  hideTooltip();
  tooltipOwner = owner;
  owner.setAttribute("aria-describedby", "uiTooltip");
  if (!owner.getAttribute("aria-label") && owner.matches(".iconbtn")) owner.setAttribute("aria-label", message);
  bubble.textContent = message;
  bubble.hidden = false;

  const rect = owner.getBoundingClientRect();
  const gap = 9;
  const margin = 10;
  const width = bubble.offsetWidth;
  const height = bubble.offsetHeight;
  const left = Math.max(margin, Math.min(rect.left + rect.width / 2 - width / 2, window.innerWidth - width - margin));
  const above = rect.top - height - gap;
  const top = above >= margin ? above : Math.min(window.innerHeight - height - margin, rect.bottom + gap);
  bubble.style.left = Math.round(left) + "px";
  bubble.style.top = Math.round(Math.max(margin, top)) + "px";
}
// A repaint or a new page can remove the element a tooltip belongs to, and no
// browser then says the pointer left it; Firefox does not say focus left it
// either. So the tooltip goes as soon as anything notices its element is gone (#834).
function dropOrphanTooltip() {
  if (tooltipOwner && !tooltipOwner.isConnected) hideTooltip();
}
window.dropOrphanTooltip = dropOrphanTooltip;
document.addEventListener("pointerover", e => {
  dropOrphanTooltip();
  const owner = tooltipTarget(e.target);
  if (owner && !owner.contains(e.relatedTarget)) showTooltip(owner);
});
document.addEventListener("pointermove", dropOrphanTooltip, { passive: true });
document.addEventListener("pointerout", e => {
  dropOrphanTooltip();
  if (tooltipOwner && tooltipOwner.contains(e.target) && !tooltipOwner.contains(e.relatedTarget)) hideTooltip(tooltipOwner);
});
document.addEventListener("focusin", e => showTooltip(tooltipTarget(e.target)));
document.addEventListener("focusout", e => {
  if (tooltipOwner && tooltipOwner.contains(e.target)) hideTooltip(tooltipOwner);
});
window.addEventListener("scroll", () => hideTooltip(), true);
window.addEventListener("resize", () => hideTooltip());

const ACTION_ICONS = [
  [/^logs?\b/, "log"], [/^console\b/, "console"], [/^edit\b/, "edit"], [/^(move|migrate)\b/, "move"],
  [/^(restart|reboot)\b/, "restart"], [/^(delete|remove)\b/, "trash"],
  [/^start\b/, "play"], [/^(stop|shut down)\b/, "stop"],
  [/^rollback\b/, "rollback"], [/^update\b/, "update"],
];
function enhanceActions(root = document) {
  labelStackTables(root);
  sortTables(root);
  $$("button.btn", root).forEach(button => {
    if ($(".btnicon", button)) return;
    const label = button.textContent.trim().replace(/^[＋↻←]+\s*/, "").toLowerCase();
    const match = ACTION_ICONS.find(([pattern]) => pattern.test(label));
    if (match) button.insertAdjacentHTML("afterbegin", icon(match[1]));
  });
}
window.enhanceActions = enhanceActions;

/* A table marked "stack" turns each row into a small card on a phone, and each
   cell needs its column's name to make sense on its own there. The headings
   already say it, so each cell is labelled from them rather than by hand. */
function labelStackTables(root = document) {
  $$("table.stack", root).forEach(table => {
    const heads = $$("thead th", table).map(th => th.textContent.trim());
    const rows = $$("tbody tr", table).map(row => {
      let column = 0;
      return [...row.children].map(cell => {
        const span = +cell.getAttribute("colspan") || 1, at = column;
        column += span;
        return [cell, span, at];
      });
    });
    // On a phone cells sit two to a line; a long one - a message, a path -
    // takes the whole line rather than wrapping to a word a line. Decided
    // for the column, not the cell, so every card in the list is laid out
    // alike: one long value would otherwise move its row's fields around.
    const wide = new Set();
    // Its words as they read: chips side by side are separate words, not one.
    const words = cell => { const parts = [], walk = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT); let n;
      while ((n = walk.nextNode())) if (n.textContent.trim()) parts.push(n.textContent.trim());
      return parts.join(" "); };
    rows.flat().forEach(([cell, span, at]) => {
      const text = words(cell);
      if (span === 1 && at > 0 && (text.length > 34 || (text.length > 18 && !/\s/.test(text)))) wide.add(at);
    });
    rows.flat().forEach(([cell, span, at]) => {
      if (span === 1 && !cell.hasAttribute("data-label")) cell.dataset.label = heads[at] || "";
      if (span === 1 && wide.has(at)) cell.dataset.wide = "";
    });
    // A card's actions - a Remove, a Snapshots, a "..." - sit beside its
    // title on a phone rather than on a line of their own, when they fit:
    // the title keeps their width clear (--acts), estimated from the labels.
    rows.forEach(cells => {
      const last = cells[cells.length - 1];
      if (!last || cells.length < 2) return;
      const [cell, span] = last;
      if (span !== 1 || cell.dataset.label !== "") return;
      const controls = [...cell.querySelectorAll("button, a.btn, .btn, details.actionmenu > summary")].filter(c => !c.closest(".actionmenu-pop"));
      const loose = [...cell.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
      if (!controls.length || controls.length > 3 || loose) return;
      const width = controls.reduce((sum, c) => {
        const text = c.textContent.trim().replace(/^[＋↻←]+\s*/, "");
        return sum + (text ? text.length * 7.2 + 28 : 34) + (c.querySelector("svg") || /^(delete|remove|edit|logs?|console|move|restart|start|stop|update|rollback)/i.test(text) ? 18 : 0);
      }, 0) + (controls.length - 1) * 8;
      if (width > 200) return;
      cell.dataset.top = "";
      cell.parentElement.style.setProperty("--acts", `${Math.ceil(width)}px`);
    });
  });
}

/* A table marked data-sort="<name>" sorts by whichever heading the reader
   clicks, and remembers it per table in this browser. Rows are put in order as
   each render is built, before it is compared with the page, so a live refresh
   keeps the reader's order instead of shuffling back. A cell's data-sort gives
   the value to compare when its text is not it (a ratio, a size in bytes). */
const SORT_UNITS = { "": 1, "%": 1, b: 1, kb: 1e3, mb: 1e6, gb: 1e9, tb: 1e12, kib: 1024, mib: 1024 ** 2,
  gib: 1024 ** 3, tib: 1024 ** 4 };
function sortState(name) {
  try { return JSON.parse(localStorage.getItem(`homestead.sort.${name}`) || "null"); } catch (e) { return null; }
}
function sortValue(cell) {
  if (!cell) return null;
  const given = cell.dataset.sort;
  const text = (given ?? cell.textContent).trim();
  if (!text || text === "—") return null;
  if (given !== undefined && given !== "" && Number.isFinite(+given)) return +given;
  const size = text.match(/^(-?\d+(?:[.,]\d+)?)\s*(%|[kmgt]i?b|b)?(?:\s|$)/i);
  if (size) return parseFloat(size[1].replace(",", ".")) * (SORT_UNITS[(size[2] || "").toLowerCase()] || 1);
  return text.toLowerCase();
}
function sortRows(table) {
  const state = sortState(table.dataset.sort);
  const heads = $$("thead th", table);
  heads.forEach((th, index) => {
    if (!th.textContent.trim() || th.hasAttribute("data-nosort")) return;
    th.classList.add("sortable");
    th.dataset.col = index;
    th.setAttribute("aria-sort", state && state.col === index ? (state.dir > 0 ? "ascending" : "descending") : "none");
  });
  sortBar(table, heads, state);
  if (!state || !heads[state.col]) return;
  // A table in sections (a heading body, then its rows) sorts each on its own.
  [...table.tBodies].filter(body => !body.classList.contains("grouphead")).forEach(body => sortBody(body, heads, state));
}
function firstDataRow(table) {
  const width = $$("thead th", table).length;
  for (const body of table.tBodies) for (const row of body.rows) if (row.cells.length === width) return row;
  return null;
}
function sortBody(body, heads, state) {
  const rows = [...body.rows];
  const keyed = rows.filter(row => !row.dataset.detailFor && row.cells.length === heads.length);
  const rest = rows.filter(row => row.cells.length !== heads.length && !row.dataset.detailFor);
  const details = new Map(rows.filter(row => row.dataset.detailFor).map(row => [row.dataset.detailFor, row]));
  keyed.map((row, at) => ({ row, at, value: sortValue(row.cells[state.col]) }))
    .sort((a, b) => {
      // Empty cells go last whichever way the column is sorted.
      if (a.value === null || b.value === null) return (a.value === null) - (b.value === null) || a.at - b.at;
      const order = typeof a.value === "number" && typeof b.value === "number" ? a.value - b.value
        : String(a.value).localeCompare(String(b.value), undefined, { numeric: true });
      return order * state.dir || a.at - b.at;
    })
    .concat(rest.map(row => ({ row })))
    .forEach(({ row }) => {
      body.appendChild(row);
      const detail = details.get(row.dataset.rowKey);
      if (detail) body.appendChild(detail);
    });
}
/* On a phone a stacked table has no headings to click, so it gets a menu. */
function sortBar(table, heads, state) {
  if (!table.classList.contains("stack")) return;
  const wrap = table.closest(".card") || table.parentElement;
  // A Settings card is a grid item its section shows or hides, so the menu
  // goes inside the card, above the table, and is hidden with it.
  const anchor = wrap.parentElement?.classList.contains("settings-grid") ? (table.closest(".tblwrap") || table) : wrap;
  const collection = table.closest("[data-collection]");
  const target = table.dataset.sortControls;
  const selector = target ? `[data-sort-controls="${CSS.escape(target)}"].sortbar` : "";
  // A collection can put the standard sort controls inside its Options
  // menu. An open menu is portalled out of the collection into the page.
  let bar = target && (collection?.querySelector(selector) ||
    (collection?.isConnected && document.querySelector(`.actionmenu-portal ${selector}`)));
  bar ||= anchor.previousElementSibling;
  if (!bar || !bar.classList.contains("sortbar")) {
    bar = document.createElement("div");
    bar.className = "sortbar";
    anchor.before(bar);
  }
  const options = heads.map((th, index) => th.classList.contains("sortable")
    ? `<option value="${index}" ${state && state.col === index ? "selected" : ""}>${esc(th.textContent.replace(/[↑↓]/g, "").trim())}</option>` : "").join("");
  bar.innerHTML = `<label>Sort by <select data-sort-for="${esc(table.dataset.sort)}">
      <option value="" ${state ? "" : "selected"}>as listed</option>${options}</select></label>
    ${state ? `<button class="btn sm" data-sort-flip="${esc(table.dataset.sort)}">${state.dir > 0 ? "↑ ascending" : "↓ descending"}</button>` : ""}`;
}
function saveSort(name, state) {
  try {
    if (state) localStorage.setItem(`homestead.sort.${name}`, JSON.stringify(state));
    else localStorage.removeItem(`homestead.sort.${name}`);
  } catch (e) { /* this visit only */ }
  const table = document.querySelector(`table[data-sort="${CSS.escape(name)}"]`);
  if (table) { if (!state) refresh(true); else sortRows(table); }
}
document.addEventListener("change", event => {
  const select = event.target.closest("select[data-sort-for]");
  if (!select) return;
  const col = select.value === "" ? null : +select.value;
  const table = document.querySelector(`table[data-sort="${CSS.escape(select.dataset.sortFor)}"]`);
  const numeric = col !== null && table && typeof sortValue(firstDataRow(table)?.cells[col]) === "number";
  saveSort(select.dataset.sortFor, col === null ? null : { col, dir: numeric ? -1 : 1 });
});
document.addEventListener("click", event => {
  const flip = event.target.closest("[data-sort-flip]");
  if (!flip) return;
  const current = sortState(flip.dataset.sortFlip);
  if (current) saveSort(flip.dataset.sortFlip, { col: current.col, dir: -current.dir });
});
function sortTables(root = document) {
  $$("table[data-sort]", root).forEach(sortRows);
}
document.addEventListener("click", event => {
  const th = event.target.closest("table[data-sort] th.sortable");
  if (!th) return;
  const table = th.closest("table");
  const col = +th.dataset.col, current = sortState(table.dataset.sort);
  // Numbers read best biggest first; names from A.
  const firstRow = firstDataRow(table);
  const numeric = typeof sortValue(firstRow?.cells[col]) === "number";
  const dir = current && current.col === col ? -current.dir : numeric ? -1 : 1;
  saveSort(table.dataset.sort, { col, dir });
});

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  tooltipOwner, tooltipTarget, hideTooltip, showTooltip, dropOrphanTooltip, ACTION_ICONS, enhanceActions,
  labelStackTables, SORT_UNITS, sortState, sortValue, sortRows, firstDataRow, sortBody, sortBar, saveSort,
  sortTables
};
