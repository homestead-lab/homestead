// docs/design.md, enforced: the layout rules a reviewer would otherwise have
// to remember. Each failure names the file and line, the rule and the fix.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const WEB = path.join(__dirname, "..", "web", "js");
// demo.js is made-up data, not markup; ui.js defines the components.
const FILES = fs.readdirSync(WEB).filter(f => f.endsWith(".js") && !["demo.js", "ui.js"].includes(f))
  .map(f => ({ name: `web/js/${f}`, text: fs.readFileSync(path.join(WEB, f), "utf8") }));
const lineOf = (text, index) => text.slice(0, index).split("\n").length;

function offences(pattern, keep = () => true) {
  const found = [];
  for (const file of FILES) {
    for (const match of file.text.matchAll(pattern)) {
      if (keep(match, file)) found.push(`${file.name}:${lineOf(file.text, match.index)}`);
    }
  }
  return found;
}

test("a row's buttons are an actionBar, never loose buttons in a cell", () => {
  // design.md, Components: actionBar - "A card's or a row's buttons". A
  // button that edits a form table (removing a row being typed) says so with
  // data-form-row.
  const found = offences(/<td\b[^>]*>([\s\S]*?)<\/td>/g,
    match => /<button class="btn\b(?![^>]*\bdata-form-row\b)/.test(match[1]));
  assert.deepStrictEqual(found, [], "use actionBar([{ label, run, need, danger }]) in these cells");
});

test("a list of like things is a table, not service rows", () => {
  // design.md, Components: serviceRow - not for "A list of like items";
  // Collections: items with the same fields are a table marked stack.
  const found = [];
  for (const file of FILES) {
    const lines = file.text.split("\n");
    lines.forEach((line, i) => {
      // A list's map returning a service row per item. One service row in a
      // card per server, say, is a service row's own use.
      if (/(=>|\breturn)\s*serviceRow\(/.test(line) && /\.map\(/.test(lines.slice(Math.max(0, i - 3), i + 1).join("\n"))) {
        found.push(`${file.name}:${i + 1}`);
      }
    });
  }
  assert.deepStrictEqual(found, [], "a mapped list is a tbl stack table with an actionBar per row");
});

test("every table is a stacked table, so it reads on a phone", () => {
  // design.md, Collections: items with the same fields - "A table marked stack".
  const found = offences(/<table class="tbl(?![^"]*\bstack\b)[^"]*"/g);
  assert.deepStrictEqual(found, [], 'give these tables class="tbl stack"');
});

test("Settings uses shared module headings in ready and error states", () => {
  const settings = FILES.find(file => file.name === "web/js/views-settings.js");
  assert.doesNotMatch(settings.text, /class="(?:ctitle|csub)"/, "use UI.moduleHeader for Settings headings, including loading/error states");
});

test("the rules here are the ones design.md states", () => {
  // If a rule is changed here, design.md must say so too, and the other way round.
  const design = fs.readFileSync(path.join(__dirname, "..", "docs", "design.md"), "utf8");
  assert.match(design, /tests\/design-rules\.test\.js/, "design.md names this test");
  for (const phrase of ["actionBar", "tbl stack", "settings-card-head", "data-form-row", "UI.sectionForm", "UI.masterDetail", "UI.actions", "UI.pageHeader", "UI.moduleHeader", "UI.settingsCard", "UI.workspace"]) {
    assert.ok(design.includes(phrase), `design.md mentions ${phrase}`);
  }
});

test("dialog navigation and layout are authored only by shared components", () => {
  const found = offences(/<[a-z]+\b[^>]*\bclass="[^"]*\b(?:modalactions|ui-actions|dialog-rail|dialog-section-picker|stepper-head|stepper-pane|dialog-master-nav|dialog-master-content)\b[^"]*"/g);
  assert.deepStrictEqual(found, [], "use UI.actions, UI.sectionForm/stepper, UI.sectionNavigation or UI.masterDetail");
});

test("dialog dismissal rows use UI.actions", () => {
  const found = offences(/<div\b[^>]*class="row[^>]*>((?:(?!<div\b)[\s\S])*?)<\/div>/g,
    match => /<button\b[^>]*>\s*(?:Cancel|Close(?: browser)?|Done|Back(?: to [^<]*)?|Keep tracking)\s*<\/button>|onclick="(?:closeModal|closeFiles)\(\)"/.test(match[1]));
  assert.deepStrictEqual(found, [], "use UI.actions for footer buttons, with dismissal/back in its start slot");
});

test("page and Settings structure is authored only by the UI module", () => {
  const found = offences(/<[a-z]+\b[^>]*\bclass="[^"]*\b(?:phead|settings-card-head|settings-layout|settings-nav|settings-main|settings-grid|settings-back|savebar|collection-mobile-head|srow)\b[^"]*"[^>]*>/g,
    // paint's matcher inserts family tabs; it is a regex, not authored markup.
    (match,file) => !file.text.slice(match.index + match[0].length).startsWith("\\s*"));
  assert.deepStrictEqual(found, [], "use UI.pageHeader, UI.moduleHeader, UI.workspace, UI.settingsGrid, UI.saveBar, UI.collectionHeader or settingRow");
  assert.deepStrictEqual(offences(/<section\b[^>]*\bdata-tab="/g), [], "use UI.settingsCard to retain topic/save/permission hooks consistently");
});

/* design.md, Reviews and verbosity. These hold the line where older markup
   still exists: a count may go down as screens move to the components, never
   up. When you remove some, lower the number in the same change. */
const LEGACY = [
  [/class="note\b/g, 215, 'UI.callout for a notice, UI.lead or ui-help for prose'],
  [/class="sec"/g, 66, "UI.section"],
  [/deployCapacityHtml\(/g, 14, "startReview or capacityHostTable for a new review"],
  [/<label class="check"><input type="checkbox"/g, 7, "UI.ack for a risk acknowledgement"],
  [/I accept\b/g, 10, 'a short UI.ack sentence that names what is accepted ("Start it anyway")'],
];
test("older markup only shrinks", () => {
  for (const [pattern, limit, use] of LEGACY) {
    const found = offences(pattern);
    assert.ok(found.length <= limit, `${found.length} uses of ${pattern} (at most ${limit}); use ${use}. New: ${found.slice(-5).join(", ")}`);
  }
});

test("an acknowledgement is one short sentence", () => {
  // design.md, Reviews: "the one checkbox" - what is accepted, not everything that could happen.
  const found = offences(/UI\.ack\([^,]+,\s*"([^"]+)"/g, match => match[1].split(/\s+/).length > 16);
  assert.deepStrictEqual(found, [], "keep UI.ack sentences to 16 words or fewer; the consequence belongs in the callout");
});

test("start and update reviews use the shared review builders", () => {
  // design.md, Reviews: one host table and one start review, not a capacity
  // block written again for each dialog.
  const source = name => FILES.find(file => file.name === `web/js/${name}`).text;
  const between = (text, from, to) => text.slice(text.indexOf(from), text.indexOf(to, text.indexOf(from)));
  assert.match(between(source("views-workloads.js"), "window.wlScale", "window.wlScaleGo"), /startReview\(/, "Start on a container uses startReview");
  assert.match(between(source("views-vms.js"), "window.vmPowerReview", "window.vmPowerReviewReady"), /startReview\(/, "Start on a VM uses startReview");
  assert.match(between(source("views-workloads.js"), "async function reviewImageActions", "window.imageReviewReady"), /capacityHostTable\(/, "the image update review uses capacityHostTable");
  const outside = offences(/\b(?:capacityPlacementHtml|capacityHosts)\(/g, (_, file) => file.name !== "web/js/views-workloads.js");
  assert.deepStrictEqual(outside, [], "new reviews use startReview or capacityHostTable, not the older placement blocks");
});

test("design.md states the review and verbosity rules", () => {
  const design = fs.readFileSync(path.join(__dirname, "..", "docs", "design.md"), "utf8");
  for (const phrase of ["Verbosity budget", "startReview", "capacityHostTable", "UI.ack", "only shrinks", "says the same thing twice"]) {
    assert.ok(design.includes(phrase), `design.md mentions ${phrase}`);
  }
});

test("every Settings section has an icon from the menu's sprite", () => {
  // design.md, Settings: each section is listed with its icon, drawn like the main menu's.
  const settings = FILES.find(file => file.name === "web/js/views-settings.js").text;
  const html = fs.readFileSync(path.join(__dirname, "..", "web", "index.html"), "utf8");
  const block = settings.slice(settings.indexOf("const SETTINGS_SECTIONS = ["), settings.indexOf("];", settings.indexOf("const SETTINGS_SECTIONS = [")));
  const rows = [...block.matchAll(/^\s*\["([^"]+)",.*,\s*"([^"]+)"\],?\s*$/gm)];
  assert.ok(rows.length >= 9, "the Settings sections are found");
  for (const [, key, icon] of rows) assert.match(html, new RegExp(`<symbol id="i-${icon}"`), `${key}: icon i-${icon} is in the sprite`);
});

test("a field's label is text: its tip goes in tipHtml", () => {
  // UI.field escapes its label, so a tip written into it shows its markup -
  // '<span class="tip" ...>?</span>' on the screen.
  const found = offences(/UI\.field\(\s*`[^`]*\$\{tip\(/g);
  assert.deepStrictEqual(found, [], 'use UI.field("Label", control, { tipHtml: " " + tip("...") })');
});

test("a dialog's footer is given HTML on its left, never a flag", () => {
  // UI.actions(buttons, startHtml): a true or false there was printed after
  // the buttons - "Canceltrue".
  const found = offences(/UI\.actions\(/g, (match, file) => {
    let depth = 0, i = match.index + "UI.actions".length, args = 0, start = i + 1;
    const second = [];
    for (; i < file.text.length; i++) {
      const c = file.text[i];
      if ("([{".includes(c)) depth++;
      else if (")]}".includes(c)) { if (--depth === 0) break; }
      else if (c === "`") { i++; let t = 0; while (i < file.text.length && !(file.text[i] === "`" && !t)) { if (file.text[i] === "$" && file.text[i + 1] === "{") { t++; i++; } else if (file.text[i] === "}" && t) t--; i++; } }
      else if (c === '"' || c === "'") { const q = c; i++; while (file.text[i] !== q) { if (file.text[i] === "\\") i++; i++; } }
      else if (c === "," && depth === 1) { if (++args === 2) { second.push(file.text.slice(start, i)); break; } start = i + 1; }
    }
    if (args === 1) second.push(file.text.slice(start, i));       // the second argument ran to the closing bracket
    return second.length > 0 && /^\s*(true|false)\s*$/.test(second[0]);
  });
  assert.deepStrictEqual(found, [], "pass the extra buttons' HTML as UI.actions' second argument, or leave it out");
});
