"use strict";
// The dialog components: what they put on the page, and that plain text
// given to them is escaped.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const context = { window: {}, console, document: {},
  esc: value => String(value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
  icon: name => `<svg data-icon="${name}"></svg>` };
vm.createContext(context);
context.jsArg = s => JSON.stringify(String(s ?? "")); context.jsq = s => (context.esc || String)(context.jsArg(s));
vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), context);
const UI = context.window.UI;

test("a callout escapes its title and keeps its tone", () => {
  const html = UI.callout("warn", "<b>tight</b>", "<ul><li>kept</li></ul>");
  assert.match(html, /ui-callout warn/);
  assert.match(html, /&lt;b&gt;tight&lt;\/b&gt;/);
  assert.match(html, /<div class="ui-notice-line ui-notice-whole"><b class="ui-notice-head">&lt;b&gt;tight&lt;\/b&gt;<\/b><span class="ui-notice-first ui-issue-short">kept<\/span><\/div>/, "one short concern is read whole beside its heading");
  assert.doesNotMatch(html, /<details/, "with nothing to open");
  assert.match(UI.callout("info", "x", "<ul><li>kept</li></ul>"), /<ul><li>kept<\/li><\/ul>/, "information is the HTML the caller built");
  assert.match(UI.callout("nonsense", "x"), /ui-callout info/, "an unknown tone is information");
});

/* A warning or an error is a summary bar: its heading and first short line,
   opening to every concern in full (design.md, Notices). */
const RECREATE = "Recreate stops all old pods before replacements start; all containers in this workload will be unavailable during the restart";
test("several warnings are counted in a bar that opens to each in full", () => {
  assert.deepEqual([...UI.splitIssue(RECREATE)], ["Recreate stops all old pods before replacements start.", "All containers in this workload will be unavailable during the restart"]);
  const html = UI.callout("warn", "Check first", `<ul class="ui-list"><li>${RECREATE}</li><li>Short and whole</li></ul>`);
  assert.match(html, /<details class="ui-notice" data-disclosure="notice:2 things to check"><summary><span class="ui-notice-line"><b class="ui-notice-head">2 things to check<\/b><span class="ui-notice-first">Recreate stops all old pods before replacements start\.<\/span>/,
    "the generic title becomes a count, and its state survives a repaint");
  assert.match(html, /<li class="ui-issue"><span class="ui-issue-short">Recreate stops all old pods before replacements start\.<\/span><span class="ui-issue-more"> All containers/);
  assert.match(html, /ui-issue-short">Short and whole<\/span><\/li>/);
  assert.equal((html.match(/<details/g) || []).length, 1, "one bar for the notice, not one per line");
});

test("a title of its own stays the heading; one with nothing more to say opens nothing", () => {
  assert.doesNotMatch(UI.callout("bad", "Could not read storage", "Longhorn is not answering"), /<details|ui-issue-more/);
  assert.match(UI.callout("warn", "Installed by its tag", `<ul><li>${RECREATE}</li></ul>`), /<b class="ui-notice-head">Installed by its tag<\/b><span class="ui-notice-first">Recreate stops/);
  const long = "x".repeat(UI.SHORT_CHARS + 20);
  assert.deepEqual([...UI.splitIssue(long)], [long, ""]);
});

test("an error with no title is its own heading, its reason inside", () => {
  const html = UI.callout("bad", "", `lost response. Some VM, disk or Secret changes may already be saved. No request was repeated and no Restart was sent.`);
  // A first sentence under 20 characters says too little alone: the next one joins it.
  assert.match(html, /<b class="ui-notice-head">lost response\. Some VM, disk or Secret changes may already be saved\.<\/b><\/span><span class="ui-notice-chev"/);
  assert.match(html, /<p class="ui-issue">No request was repeated and no Restart was sent\.<\/p>/);
});

test("the short line breaks only outside markup, entities and abbreviations", () => {
  const html = '<span class="mono">a.b; c</span> was asked to stop, e.g. by a person, then left alone for a long while. It is still stopped';
  const [short, more] = UI.splitIssue(html);
  assert.equal(short, '<span class="mono">a.b; c</span> was asked to stop, e.g. by a person, then left alone for a long while.');
  assert.equal(more, "It is still stopped");
  assert.deepEqual([...UI.splitIssue("Tom &amp; Jerry share one volume that every host has to mount at the same time; move one of them first")],
    ["Tom &amp; Jerry share one volume that every host has to mount at the same time.", "Move one of them first"]);
});

test("a long title keeps its first sentence as the heading and the rest inside", () => {
  const html = UI.callout("warn", "This app is set to wait for its host to come back; this overrides that once, for this restart only, and then it waits again");
  assert.match(html, /<b class="ui-notice-head">This app is set to wait for its host to come back\.<\/b>[\s\S]*<p class="ui-issue">This overrides/);
});

test("the buttons that fix it stay outside the bar", () => {
  const html = UI.notices([["bad", "One"], ["bad", "Two"]], "This copy cannot start", { actions: '<button class="btn">Release</button>' });
  assert.match(html, /<\/details><div class="ui-callout-actions"><button class="btn">Release<\/button><\/div>/);
});

test("a body with controls of its own is left whole, with nothing hidden", () => {
  const body = '<p>Pick one</p><button class="btn">Retry</button>';
  assert.ok(UI.callout("bad", "Stopped", body).includes(body));
});

test("notices take a short line and its detail from the server as they are", () => {
  const html = UI.notices([["warn", { short: "Restarts the app", more: "Recreate stops the old pod first <b>" }]], "Check first");
  assert.match(html, /<b class="ui-notice-head">1 thing to check<\/b><span class="ui-notice-first">Restarts the app<\/span>/);
  assert.match(html, /ui-issue-short">Restarts the app<\/span><span class="ui-issue-more"> Recreate stops the old pod first &lt;b&gt;/);
});

test("table cells carry their column's label for the phone layout", () => {
  const html = UI.table([{ label: "Host" }, { label: "Memory", className: "grow" }], [["node<1>", "81%"]]);
  assert.match(html, /<td class="" data-label="Host">node<1><\/td>/);
  assert.match(html, /data-label="Memory">81%/);
  assert.match(UI.table([{ label: "Host" }], [], { empty: "No hosts <yet>" }), /ui-empty">No hosts &lt;yet&gt;/);
});

test("a UI.table is a stack table, so it folds on a phone like every other list (#506)", () => {
  const html = UI.table([{ label: "Host" }], [["a"]]);
  assert.match(html, /<table class="ui-table stack">/, "the stack labeller and phone layout apply to it");
});

test("steps mark what is done and what is current", () => {
  const html = UI.steps([{ title: "Copy" }, { title: "Verify" }, { title: "Switch" }], 1);
  assert.match(html, /<li class="done">/);
  assert.match(html, /<li class="current" aria-current="step">/);
  assert.match(html, /class="ui-track"/, "progress steps are a track");
  assert.match(html, /<li class="todo">/);
  assert.match(UI.steps([{ title: "Read" }]), /ui-steps guide/, "no current step is a plain guide");
});

test("a meter turns amber past its warning mark and red past full", () => {
  assert.match(UI.meter({ now: 50, after: 60, warnAt: 85 }), /ui-meter ok/);
  assert.match(UI.meter({ now: 80, after: 91, warnAt: 88 }), /ui-meter warn/);
  assert.match(UI.meter({ now: 95, after: 104 }), /ui-meter bad/);
  assert.match(UI.meter({ now: 95, after: 104 }), /width:5%/, "the change is drawn only up to the edge");
  const down = UI.meter({ now: 70, after: 45 });
  assert.match(down, /class="now" style="width:45%"/, "a host giving work up shows what it keeps");
  assert.match(down, /class="freed" style="left:45%;width:25%"/, "and the part it gives up");
  assert.doesNotMatch(down, /class="after"/);
});

test("progress without a value is shown as under way", () => {
  assert.match(UI.progress(null, { label: "Copying" }), /ui-progress info unknown/);
  assert.match(UI.progress(42.4, { label: "Copying" }), /<b>42%<\/b>/);
  assert.match(UI.progress(130), /width:100%/);
});

test("actions put their buttons in the bar, extra buttons at the start", () => {
  const html = UI.actions(UI.cancel() + UI.button("Delete <it>", "go()", { kind: "danger", id: "x" }), UI.button("Help", "help()"));
  assert.match(html, /class="ui-actions"/);
  assert.match(html, /ui-actions-start">.*Help/s);
  assert.match(html, /class="btn danger" id="x" onclick="go\(\)"/);
  assert.match(html, /Delete &lt;it&gt;/);
});

test("the acknowledgement is one checkbox with an escaped sentence", () => {
  const html = UI.ack("ok_box", "I accept <risk>");
  assert.match(html, /<input type="checkbox" id="ok_box">/);
  assert.match(html, /I accept &lt;risk&gt;/);
});

test("stat cards carry their figure, unit and tone", () => {
  const html = UI.stats([{ title: "Free <space>", value: 906, unit: "GB", sub: "of 1392", tone: "ok" }, null, { title: "Wide", wide: true }]);
  assert.match(html, /class="ui-stats"/);
  assert.match(html, /card glow g-ok ui-stat/);
  assert.match(html, /Free &lt;space&gt;/);
  assert.match(html, /<div class="bignum">906<span class="unit">GB<\/span><\/div>/);
  assert.match(html, /ui-stat statwide/, "a wide card spans the row");
  assert.equal((html.match(/ui-stat[" ]/g) || []).length, 2, "empty entries are left out");
});

test("a guide is closed until opened", () => {
  const html = UI.guide("How <this> works", "<p>kept</p>");
  assert.match(html, /^<details class="ui-guide"><summary>How &lt;this&gt; works<\/summary>/);
  assert.doesNotMatch(html, / open/);
});

test("section forms share keyed desktop/mobile navigation and one footer", () => {
  const html = UI.sectionForm("edit", [{key:"general",title:"General",html:'<input id="name">'}, false,
    {key:"network",title:"Network",html:'<input id="address">'}], UI.button("Save", "save()"), {always:true});
  assert.equal((html.match(/class="ui-actions /g) || []).length, 1);
  assert.match(html, /aria-controls="edit-pane-network"/);
  assert.match(html, /id="edit-pane-network" data-i="1" data-key="network" hidden/);
  assert.match(html, /<option value="network">Network<\/option>/);
  assert.match(html, /onchange="UI.selectSection\(&quot;edit&quot;,this.value\)"/);
  assert.doesNotMatch(html, /data-next/);
  assert.throws(() => UI.sectionForm("empty", [], ""), /at least one section/);
});

/* A section the form cannot set yet (#544): Deploy's Placement. */
const LATER_SECTIONS = [{key:"a",title:"A",lead:"Lead",html:"x"},{key:"p",title:"Placement",html:"y",later:"Set after deploying"},{key:"s",title:"Summary",html:"z"}];
test("a section set later is greyed and says when", () => {
  const html = UI.sectionForm("f", LATER_SECTIONS, "<b>Go</b>");
  const tab = html.match(/<button[^>]*id="f-tab-p"[^>]*>[^]*?<\/button>/)[0];
  assert.match(tab, /class="stepper-chip later"/);
  assert.doesNotMatch(tab, /aria-disabled/, "it still opens, to say why, so it is not announced as disabled");
  assert.match(tab, /data-later/);
  assert.match(tab, /aria-label="Placement, set after deploying"/);
  assert.match(tab, />Placement<small>Set after deploying<\/small><\/button>/);
  assert.match(html, /<option value="p">Placement · set after deploying<\/option>/);
  assert.match(html, /id="f-pane-p" data-i="1" data-key="p" data-later hidden>/);
  assert.doesNotMatch(html.match(/<button[^>]*id="f-tab-s"[^>]*>/)[0], /later|aria-disabled/);
  assert.match(UI.sectionForm("g", [{key:"q",title:"Q",html:"",later:"<i>soon</i>"}], ""), /<small>&lt;i&gt;soon&lt;\/i&gt;<\/small>/, "escaped");
});

test("a section without later is unchanged", () => {
  const today = "<div class=\"stepper dialog-rail\" id=\"f\" data-step=\"0\">\n    <label class=\"dialog-section-picker\">Step<select id=\"f_section\" aria-label=\"Step\" onchange=\"UI.selectSection(&quot;f&quot;,this.value)\"><option value=\"a\" selected>A · 1 of 3</option><option value=\"p\">Placement · 2 of 3</option><option value=\"s\">Summary · 3 of 3</option></select></label>\n    <div class=\"stepper-head\" role=\"tablist\" aria-label=\"Steps\" aria-orientation=\"vertical\"><button type=\"button\" role=\"tab\" id=\"f-tab-a\" aria-controls=\"f-pane-a\" aria-selected=\"true\" tabindex=\"0\" class=\"stepper-chip on\" data-i=\"0\" data-key=\"a\" onclick=\"UI.selectSection(&quot;f&quot;,&quot;a&quot;)\">A</button><button type=\"button\" role=\"tab\" id=\"f-tab-p\" aria-controls=\"f-pane-p\" aria-selected=\"false\" tabindex=\"-1\" class=\"stepper-chip\" data-i=\"1\" data-key=\"p\" onclick=\"UI.selectSection(&quot;f&quot;,&quot;p&quot;)\">Placement</button><button type=\"button\" role=\"tab\" id=\"f-tab-s\" aria-controls=\"f-pane-s\" aria-selected=\"false\" tabindex=\"-1\" class=\"stepper-chip\" data-i=\"2\" data-key=\"s\" onclick=\"UI.selectSection(&quot;f&quot;,&quot;s&quot;)\">Summary</button></div>\n    <div class=\"stepper-pane\" role=\"tabpanel\" aria-labelledby=\"f-tab-a\" id=\"f-pane-a\" data-i=\"0\" data-key=\"a\"><p class=\"ui-lead\">Lead</p>x</div><div class=\"stepper-pane\" role=\"tabpanel\" aria-labelledby=\"f-tab-p\" id=\"f-pane-p\" data-i=\"1\" data-key=\"p\" hidden>y</div><div class=\"stepper-pane\" role=\"tabpanel\" aria-labelledby=\"f-tab-s\" id=\"f-pane-s\" data-i=\"2\" data-key=\"s\" hidden>z</div>\n    \n    <div class=\"ui-actions stepper-foot\" >\n      <div class=\"ui-actions-start\"><button type=\"button\" class=\"btn\" onclick=\"closeModal()\" data-dialog-dismiss=\"true\">Cancel</button><button type=\"button\" class=\"btn\" onclick=\"stepGo(&quot;f&quot;,-1,true)\" data-dialog-dismiss=\"true\" data-back hidden>Back</button></div><div class=\"ui-actions-end\"><span data-finish hidden><b>Go</b></span><button type=\"button\" class=\"btn pri\" onclick=\"stepGo(&quot;f&quot;,1,true)\" data-next>Next</button></div></div>\n  </div>";
  assert.equal(UI.sectionForm("f", LATER_SECTIONS.map(({ later, ...section }) => section), "<b>Go</b>"), today);
});

/* stepGo against a stand-in for the form: three panes, the middle one later. */
function laterForm() {
  const attrs = (dataset = {}) => ({ dataset, hidden: false, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; },
    classList: { toggle() {} }, tabIndex: 0 });
  const panes = [attrs({ key: "a" }), attrs({ key: "p", later: "" }), attrs({ key: "s" })];
  const chips = panes.map(() => attrs());
  const root = { dataset: { step: "0" }, closest: () => null,
    querySelectorAll: sel => sel.includes("stepper-pane") ? panes : chips,
    querySelector: () => null };
  context.document.getElementById = () => root;
  return root;
}
test("Next and Back step over a section set later; choosing it still opens it", () => {
  const root = laterForm();
  context.stepGo("f", 1, true);
  assert.equal(+root.dataset.step, 2, "Next from A lands on Summary");
  context.stepGo("f", -1, true);
  assert.equal(+root.dataset.step, 0, "Back from Summary lands on A");
  context.stepGo("f", 1);
  assert.equal(+root.dataset.step, 1, "a tab or the picker still opens it");
  // A relative step of 0 from a later section stays put rather than looping.
  vm.runInContext('stepGo("f", 0, true)', context, { timeout: 500 });
  assert.equal(+root.dataset.step, 1);
});

test("dismissal precedes the main action without losing a custom handler or gate", () => {
  const html = UI.actions(UI.button("Delete", "remove()", {kind:"danger",disabled:true,attrs:'data-need="admin"'}) +
    '<button data-dialog-dismiss="true" onclick="modalBack()">Back</button>');
  assert.ok(html.indexOf('modalBack()') < html.indexOf('remove()'));
  assert.match(html, /ui-actions-start[^]*Back<\/button><\/div>/);
  assert.match(html, /disabled data-need="admin"/);
});

test("master detail keeps stable disclosure keys and escapes labels and selection handlers", () => {
  const key = 'job"<&\\', title = '<img onerror=alert(1)>';
  const html = UI.masterDetail([{key:"Completed", title:"Completed · 1", collapsed:true,
    items:[{key,title,detail:"<done>"}]}], key, '<p>Selected content</p>', {onSelect:id => `pick(${JSON.stringify(id)})`});
  assert.match(html, /data-disclosure="Completed" open/);
  assert.match(html, /aria-current="true"/);
  assert.match(html, /&lt;img onerror=alert\(1\)&gt;/);
  assert.match(html, /&lt;done&gt;/);
  const encoded = html.match(/onclick="([^"]*)"/)[1];
  const decoded = encoded.replace(/&(amp|lt|gt|quot|#39);/g, (_,entity) => ({amp:"&",lt:"<",gt:">",quot:'"',"#39":"'"}[entity]));
  let received;
  new Function("pick", decoded)(value => {received=value;});
  assert.equal(received,key);
});

test("page and module headers share action placement and explicit HTML slots", () => {
  const page = UI.pageHeader("Hosts", "<b>3</b> ready", '<button id="create">Add</button>', {extraHtml:'<span id="filter">Group</span>',descriptionAttrs:'id="count"'});
  assert.match(page, /<h2>Hosts<\/h2><p id="count"><b>3<\/b> ready<\/p>/);
  assert.match(page, /class="row page-actions"><button id="create">/);
  assert.ok(page.indexOf('id="filter"') < page.indexOf('page-actions'));
  const module = UI.moduleHeader("Updates", "Releases and hosts", '<button disabled data-need="admin">Check</button>');
  assert.match(module, /settings-card-head/);
  assert.match(module, /module-actions"><button disabled data-need="admin">/);
});

test("settings wrappers retain routing, save scope, loading identity and visibility", () => {
  const card = UI.settingsCard('<div>Loading</div>', {tab:'updates',id:'host"updates',save:'app',hidden:true});
  assert.match(card, /data-tab="updates" id="host&quot;updates" data-save="app" hidden/);
  assert.match(UI.settingsGrid(card,'updates'), /class="settings-grid" data-tab="updates"/);
  assert.match(UI.saveBar({id:'save',messageId:'message',save:'save(this)',discard:'discard()'}), /id="save" hidden/);
  assert.match(UI.saveBar({id:'save',messageId:'message',save:'save(this)',discard:'discard()'}), /id="message" role="status"/);
});

test("workspace navigation escapes labels and handlers and groups items once", () => {
  const key = 'quoted"<&';
  const html = UI.workspaceNav([{key,label:'<Unsafe>',group:'Cluster'}, {key:'other',label:'Other',group:'Cluster'}],
    {label:'Settings',selected:key,onSelect:id => `choose(${JSON.stringify(id)})`});
  assert.equal((html.match(/settings-nav-group/g)||[]).length,1);
  assert.match(html, /&lt;Unsafe&gt;/);
  assert.match(html, /aria-selected="true" tabindex="0"/);
  const encoded=html.match(/onclick="([^"]*)"/)[1];
  const decoded=encoded.replace(/&(amp|lt|gt|quot|#39);/g,(_,e)=>({amp:'&',lt:'<',gt:'>',quot:'"','#39':"'"}[e]));
  let result;new Function('choose','UI',decoded)(value=>{result=value;},{navigateWorkspace:(_button,run)=>run()});assert.equal(result,key);
  assert.match(UI.workspace(html,'<input id="kept">',{open:false,backLabel:'Back',back:'goBack()'}), /data-open="0"/);
  assert.match(UI.workspace(html,'<input id="kept">',{open:true}), /data-open="1"/);
});
test("a guide's navigation marks the open step as the current one, and only that one", () => {
  const html = UI.workspaceNav([{key:'intro',label:'Start here'}, {key:'lan',label:'LAN networks'}],
    {label:'Setup steps',selected:'lan',guide:true,onSelect:id => `setupOpen(${JSON.stringify(id)})`});
  assert.equal((html.match(/aria-current="step"/g)||[]).length,1);
  assert.match(html, /<button[^>]*aria-current="step"[^>]*>[\s\S]*?LAN networks/);
});


test("page section forms have an in-flow footer and return action without changing dialog dismissal",()=>{
 const sections=[{title:'Basics',html:'<input id="field">'}];
 const page=UI.sectionForm('create',sections,'<button>Review</button>',{page:true,cancelHtml:UI.button('Cancel',"go('workloads')")});
 assert.match(page,/section-page/);assert.match(page,/go\(/);assert.doesNotMatch(page,/closeModal\(/);
 assert.match(UI.sectionForm('dialog',sections,'<button>Save</button>'),/closeModal\(/);
});

// #543: UI.chip is the one way to write a small coloured label.
test("a chip's default markup is unchanged", () => {
  assert.equal(UI.chip("x", "warn"), '<span class="ui-chip warn">x</span>');
  assert.equal(UI.chip("x"), '<span class="ui-chip ">x</span>');
});

test("a chip knows the whole tone set", () => {
  for (const tone of ["ok", "warn", "bad", "info", "hw", "link", "neutral"]) {
    assert.equal(UI.chip("x", tone), `<span class="ui-chip ${tone}">x</span>`);
  }
  assert.equal(UI.chip("x", "nonsense"), '<span class="ui-chip ">x</span>', "an unknown tone is the plain chip");
});

test("older tone names map onto the set", () => {
  assert.match(UI.chip("x", "med"), /class="ui-chip warn"/);
  assert.match(UI.chip("x", "crit"), /class="ui-chip bad"/);
  assert.match(UI.chip("x", "low"), /class="ui-chip neutral"/);
});

test("slim is a size", () => {
  assert.match(UI.chip("x", "ok", { size: "slim" }), /class="ui-chip ok slim"/);
  assert.equal(UI.chip("x", "ok", { size: "huge" }), '<span class="ui-chip ok">x</span>', "an unknown size adds nothing");
});

test("a chip's tip is escaped", () => {
  assert.match(UI.chip("x", "", { tip: 'a"<b>' }), /data-tip="a&quot;&lt;b&gt;"/);
});

test("a chip's tip works by keyboard focus too (design.md: tooltips)", () => {
  const html = UI.chip("api", "warn", { tip: "late <2m>" });
  assert.match(html, /tabindex="0"/);
  assert.match(html, /aria-label="api: late &lt;2m&gt;"/);
  assert.doesNotMatch(UI.chip("api", "warn"), /tabindex|aria-label/, "a chip with no tip is not a stop for the keyboard");
});

test("only the set and its own aliases are tones", () => {
  for (const tone of ["constructor", "toString", "__proto__", "hasOwnProperty"]) {
    assert.equal(UI.chip("x", tone), '<span class="ui-chip ">x</span>', tone);
  }
});

test("a chip's label is text", () => {
  assert.match(UI.chip("<b>"), /&lt;b&gt;/);
});
