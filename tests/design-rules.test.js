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
