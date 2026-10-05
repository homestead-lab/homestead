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
  assert.match(html, /<ul><li>kept<\/li><\/ul>/, "the body is HTML the caller built");
  assert.match(UI.callout("nonsense", "x"), /ui-callout info/, "an unknown tone is information");
});

test("table cells carry their column's label for the phone layout", () => {
  const html = UI.table([{ label: "Host" }, { label: "Memory", className: "grow" }], [["node<1>", "81%"]]);
  assert.match(html, /<td class="" data-label="Host">node<1><\/td>/);
  assert.match(html, /data-label="Memory">81%/);
  assert.match(UI.table([{ label: "Host" }], [], { empty: "No hosts <yet>" }), /ui-empty">No hosts &lt;yet&gt;/);
});

test("steps mark what is done and what is current", () => {
  const html = UI.steps([{ title: "Copy" }, { title: "Verify" }, { title: "Switch" }], 1);
  assert.match(html, /<li class="done">/);
  assert.match(html, /<li class="current">/);
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


test("page section forms have an in-flow footer and return action without changing dialog dismissal",()=>{
 const sections=[{title:'Basics',html:'<input id="field">'}];
 const page=UI.sectionForm('create',sections,'<button>Review</button>',{page:true,cancelHtml:UI.button('Cancel',"go('workloads')")});
 assert.match(page,/section-page/);assert.match(page,/go\(/);assert.doesNotMatch(page,/closeModal\(/);
 assert.match(UI.sectionForm('dialog',sections,'<button>Save</button>'),/closeModal\(/);
});
