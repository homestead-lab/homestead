# Design

Homestead's pages and dialogs are built from one set of components, laid out
the same way, and work on a phone. This page is the rulebook. The components
live in `web/js/ui.js` and at the end of `web/style.css`;
`scripts/audit_pages.mjs` and `scripts/audit_dialogs.mjs` check every page and
dialog against the rules that can be measured, in CI.

## Principles

These hold everywhere.

**Reuse before you build.** A list of items is a stacked table or a card grid,
a set of values is a facts panel, a row of figures is stat cards, a way of
working is a guide. Reach for the component; new markup needs a reason the
existing ones cannot meet, and then becomes a component itself.

**Colour means state.** Green is healthy, amber needs attention, red is an
error or something that cannot be undone. Two more colours carry meaning of
their own: blue marks what opens something - a port, a VIP, a link - and
purple the hardware a workload takes from its host (an iGPU, a Coral). Every
other tag, chip, avatar, role badge and progress bar is neutral, so a healthy
page is calm and the one red thing stands out. If everything is amber,
nothing is.

**One line of buttons.** A header, a card or a row keeps its buttons on one
line at any width: the main one or two as buttons, the rest in a `⋯` menu
beside them. Nothing wraps onto a second line of buttons.

**Summary first, detail on tap.** A page opens with what matters - one status
line - and keeps the tiles and breakdowns behind it, a tap away.

**Draw, don't describe.** Where a paragraph would explain a picture - what a
node holds, where a VIP leads, where each folder goes - draw it
(`diagrams.js`), from live data, in the README graphic's vocabulary: a node
is a column, a VIP a blue pill, an app an amber block and a VM an outlined
one, a volume green, and dashed for whatever is moving, empty or not there
yet. It redraws as the fields change.

**Say it once.** A warning appears once. A sentence that would be repeated on
every card ("saved for use...") is said once, above the cards, and each card
shows a chip.

**Numbers are shown, not described.** Capacity is a meter, a set of values a
facts panel, a list of hosts a table. "12.6 GiB of 15.6 GiB, 81%" is a meter.

**Explain out of the way.** How something works belongs in a guide
(`UI.guide`) on a page, in `UI.more` in a dialog, or in a `tip()`. It is never
a block of notes at the top of a page, pushing the content down.

**Keep sentences short.** A subtitle or lead is one or two sentences. A
paragraph that runs past three lines belongs in a guide or disclosure.

**Headings are sentence case.** Section headings (`.sec`, `UI.section`) and
labels read as text, not code: `Recurring jobs`, not `RECURRING JOBS`. Table
column headings are the one small upper-case label.

**Type.** Nothing smaller than 10px. Body text 12.5-14px; labels 11.5px.

## Pages

### The shape of a page

1. **Header** (`.phead`) - on the left, the page family's tabs when it has
   them (added by `paint()`), then a one-line subtitle; on the right, on the
   same line as the tabs, the actions: the cards/rows switch if the page has
   one, `moreMenu()` for everything secondary, and one main button (`pri`) -
   or `menuButton()` when the main action is a choice (＋ Import). Filters
   that belong to the list, such as Containers' group chips, share the
   subtitle's line.
   On a phone, Containers and Virtual machines keep their page tabs above one
   compact toolbar. Containers has a group picker, Options and Deploy; Virtual
   machines has an All VMs scope label, Options and New VM. The count and
   image-update or running state share one line below.
   `listOptions()` puts sorting, layout and platform visibility inside the
   standard action menu; preferences never add permanent toolbar rows. With
   no groups, a plain scope label replaces the picker.
2. **Summary** (`summaryLine()`) - one line that answers "is it well?", with
   its tiles (`UI.stats`, `.statgrid`) behind Details.
3. **Tabs** (`.seg`), if the page itself has views.
4. **Guide** (`UI.guide`) - collapsed: how this part of Homestead works, for
   whoever wants to know.
5. **One callout**, only when something needs doing now (a clash, a failure).
6. **Content** - sections with `.sec` headings or cards (`.card` with
   `.ctitle` and `.csub`), each holding one of the collections below.

### Finding a page

- **The sidebar has twelve entries** in four groups: Overview (Dashboard,
  Nodes, Portal), Apps (Containers, Virtual Machines, App Store), Storage
  (Volumes, Network Shares, Data Protection) and System (Networking, Cluster,
  Settings).
- **A page that belongs to another is its tab**, not a sidebar entry:
  Architecture of the Dashboard; Images, Schedules and Import of Containers
  (Deploy is reached from its ＋ Deploy); Import of Virtual machines; Helm of
  the App Store; Events and Resources of Cluster. A thing is imported on the
  page it ends up on: containers under Containers › Import, VMs under
  Virtual machines › Import, from the same Unraid servers. `router.js` records it as the route's `parent` and
  the family's `TABS`; the sidebar marks the parent, the breadcrumbs name
  it, and every page keeps its own address.
- **A phone has a bottom bar**: Home, Apps, VMs, Storage, and More, which
  opens the sidebar.
- **The top bar** is search, the bell, the gear (your own settings:
  Settings › You) and the account. The bell is everything in the background:
  a ring turns round it while jobs run, and its list opens with them -
  Running now, each with its progress - then Needs you: a new Homestead,
  container updates, failed image checks and failed jobs. All jobs opens every
  job with its log. Nothing floats over the page.
- **On a phone**, refresh replaces the gear in the top bar; Settings stays in
  the drawer and on the account button. Installed apps also refresh by pulling
  down at the top of the content pane. Refresh updates the current page;
  **Reload app** in the drawer reloads the whole app. Neither refresh action
  replaces unsaved settings, form pages, open dialogs or editors.

### Detail pages

Something with a lot to say about it - a node - is a page of its own, not a
dialog: `/nodes?node=<name>`, laid out as Settings is, a column of sections
each with its state under its name, one shown at a time (a list first on a
phone). A node's are Overview (its picture, uptime and facts), Workloads,
Storage, Hardware, Network and Host OS. A drive opens inside Storage.

### The setup guide

`/setup` (`welcome.js`) opens with an introduction, then chapters and their steps
down the side with each step's state, one step open. On a phone, a step picker
keeps the introduction and current step visible above the fold. A step is a two-line
lead, the live state (a `Diagram` where the idea is spatial - `quorum`,
`vip`, `copies`, `remote`, `node`), one main action and the rest folded under
"How this works". Whether a step is done is never stored: the server looks
(`setup_state()`), and only skips are kept (`homestead_setup.py`). Cluster
steps are an admin's; appearance, phone and notifications are everyone's.
The guide opens from its book icon on real clusters and in the demo. The icon
pulses within its top-bar button, respecting reduced motion, until the person
finishes the guide or chooses Don't show again. Guide completion and reminder
preferences are stored per user without marking cluster checks as passed.
The book button stays available for everyone after completion; Settings › Homestead
also opens the guide. There is no dashboard progress shortcut or probe step;
the node probe is installed automatically.
Each step names what is checked and its limits. Appearance is explicitly a
confirmation on this device and can be undone; skipping and Next never mark a
step done. Configuration visits retain a return bar naming the setup step, even
after reloading the page. Returning rechecks the facts.

### Settings

- **Nine sections, sorted by what you came to change.** Eight concern the
  cluster for everyone - Homestead, Updates, Monitoring, Hardware and
  storage, Linked clusters, Connections, Users and access, Troubleshooting - and **You** is
  yours and this browser's. A column beside them on a desktop; a list to tap
  into on a phone.
- **A card names its topic** (`data-tab`); `SETTINGS_SECTIONS` in
  `views-settings.js` and the rules in `style.css` say which topics a section
  shows, and `tests/settings-sections.test.js` keeps the two in step and
  every card in a section. `settingsTab()` takes a section or a topic.
- **Every card opens with the standard head**: `settings-card-head` holding
  the title (`ctitle`), one line under it (`csub`), and the card's buttons on
  the right (`btn sm`, the main one `pri`). No card brings its own header,
  padding or nested card - not even one moved in from another page.
- **Four kinds of setting, and nothing else**: a setting row
  (`settingRow()`), a threshold pair (`thresholdEditor()`), a service row
  (`serviceRow()` - something Homestead runs or talks to, its buttons an
  `actionBar()`), and lists of things (`tbl stack` with `actionBar()`).
  Status is a checklist behind a summary line. Several of one kind of thing -
  keys, classes, namespaces, users - is a list, never a column of service
  rows; a wide list keeps its columns few (fold related yes/no facts into one
  column of tags) so it fits the Settings column.
- **A section saves once.** A card whose fields save marks itself
  `data-save` ("app" for Homestead's own settings, sent as one request); a
  change shows the section's save bar - Save or Discard - and leaving the
  section or the page with changes asks first. Appearance applies at once and
  is not on it.
- **Many fields open a dialog**: MQTT, UniFi, the update window and the App
  Store catalogue are a service row or a setting row on the page, their fields
  in a standard dialog with its own Save.

### Collections

| Showing | Use | On a phone |
|---|---|---|
| Items with the same fields - volumes, events, services, images | A table marked `stack` (`tbl stack`, inside `.tblwrap`) | Each row is a card: the first cell its title, short values two to a line with their label above, long values and the actions across the card |
| Rich items with their own actions - nodes, workloads, jobs | A grid of cards (`grid` with `auto-fit`/`minmax`) | One card to a row |
| Short, uniform tiles - services, links | A grid of small tiles | Two to a row, never one tall tile each |
| A set of values - an endpoint, a bucket, a size | `.about-grid` on a page, `UI.facts` in a dialog | Two to a row, one panel |
| Headline figures | `UI.stats` | Two to a row |

A table marked `stack` needs nothing else: its cells are labelled from its
headings, and long ones are laid across the card automatically. Keep the
first column the item's name, and the last its buttons - always an
`actionBar()`, never buttons written into the cell. Every table is a `stack`
table, in a dialog as on a page.

A small comparison uses `comparisonTable()`: hosts are columns and metrics
are rows, with row and column headings for screen readers. It stays a matrix
on a phone, with four hosts visible at once. A larger fleet scrolls inside
the comparison, keeping the metric labels pinned; the page never scrolls
sideways. A single host uses a card. Uptime bars and pod dots stay visible.
Only short metrics remain in the phone matrix; selecting a host shows its
storage and facts beneath it. Full host names remain in accessible labels and in the selected host heading.

Compact container rows use `collectionDisclosure()` for their title. This
native button expands facts and runtime objects directly under the row;
`actionBar()` still holds the workload actions. Sorting moves the detail with
its parent, and refreshing keeps the disclosure state. A disclosure is a
view control, separate from a row's workload actions.

The form exception: a button that edits a form laid out as a table - the ✕
that removes a row being typed - is a plain button marked `data-form-row`.

### Phones

Pages are designed for a phone as much as a desktop:

- **The page never scrolls sideways.** A wide item table stacks; a long name
  wraps. A node comparison can scroll within its own labelled region.
- **Use the width.** Pair short things - stats, facts, tiles, short table
  values - two to a line. A short value alone on a full-width line, or a
  label at one edge and its value at the other, wastes the screen.
- **The important thing first.** State and figures before explanation;
  explanation collapsed.
- **Actions stay reachable** - in the header, or at the foot of each card.
- **The bottom bar is always there**, so a page never needs its own way back
  to the main pages.
- **Controls take two lines at most.** A page's header buttons, pills,
  filters and chips fit in two rows on a phone: shorten labels there
  (`<span class="hide-sm">`), make secondary buttons icons with an
  `aria-label`, let chip rows scroll sideways, and leave out controls a phone
  does not need (the cards/rows switch).
- **Card headings wrap, not squeeze.** A card's title and description keep a
  readable width; its buttons move under it rather than crushing it into a
  one-word column.

### Linked clusters

A list that All clusters can show - containers, VMs, nodes, volumes - marks
each row's outermost element with `clusterAttr(row)`, so every action and
dialog opened from the row goes to the row's cluster, and shows
`clusterTag(row)` beside its name. Both are empty unless All clusters is on.
A record kept by this cluster alone (an image update, say) is not shown on a
row where `remoteRow(row)` is true. See `docs/multi-cluster.md`.

### Address fields

A field that gives something an IP address - a VIP, a Service's address, a
VM's static address, backup storage - is marked `data-ipam` (or
`data-ipam="multi"` for a comma-separated list). It then gets a small button
inside its box that offers the free addresses from Networking › IP addresses
(`web/js/ipam-picker.js`), so nobody has to go and look one up. A field for
an address that already exists - a gateway, a broker, a NAS - is not marked.

### Density

Homestead is a working tool: show more at once, pad less.

- **Use the shared sizes.** Buttons are 32px tall (`.btn.sm` 28px), inputs
  34px, icon buttons 34px, pills and tags 3px by 10px. They live in the
  density block at the end of `web/style.css`. A page does not pad its own
  buttons or fields beyond them.
- **Phones are tighter still**: page padding 12px, card padding 13px, gaps
  10px, big figures 28px - but fields use 16px text there, or the phone zooms
  in while typing.
- **Two buttons, then `…`.** A card or row shows the action that fits its
  state and the one most used; the rest are in `…` (`actionBar()`, or
  `details.actionmenu` where a card builds its own). Delete always lives in
  `…`, never as a full button in a row. Container and VM cards share one
  shape: name and state, a row of facts, what it runs, then the buttons. An
  open `…` menu is lifted to the page (`ui.js`), so a card's blur or overflow
  never clips it; items close it with `this.closest('details').open=false`,
  which still works there.
- **A list is shorter than its cards.** A table a page offers as the
  "rows" layout is marked `compact` (`tbl stack compact`): on a phone each
  row is the name with its status beside it, then its figures inline with
  their labels - no labelled box per value, no meters. Mark the status cell
  `data-status`, the actions cell `data-actions`, and cells a phone can do
  without `data-sm-hide`.
- **Related figures share a line**: "20 GiB provisioned · 27.8 GiB
  footprint", not a line each.
- **Absence is a mark, not a pill.** A state that only says "nothing known
  yet" - an image not checked, a value not measured - is a small dashed
  `?` (`.tip.unchecked-tip`) with the reason on hover, and the page
  subtitle gives the count. Pills are for states that matter: running,
  failed, an update.
- **Rows line up.** Every row's actions end at the same right edge, and its
  second line starts under the name, not under the icon. A row with nothing
  to report - a stopped container - is one line; `0%` and `0 MB` are not
  figures worth a line.
- **Menus are placed against the screen.** `…` menus open below their button,
  or above when there is no room, and are never clipped by a table's scroll
  box; one is open at a time, and Escape, a scroll or a click elsewhere
  closes it.

## Dialogs

### The shape of a dialog

Top to bottom, leaving out what a dialog does not need:

1. **Title** - what the dialog is about: `Delete volume · media`,
   `Start frigate?`, `New share`. Sentence case, the object after a `·`.
2. **Lead** (`UI.lead`) - one or two sentences: what happens if you go ahead.
3. **One callout** (`UI.callout`) - the thing that needs attention, if there
   is one. Never more than one warning or error callout: gather everything
   into one, as a list.
4. **Content** - sections (`UI.section`) of facts, tables, checklists, steps
   or form fields.
5. **Details** (`UI.more`) - how something is calculated, background, the
   objects behind it.
6. **Confirmation** (`UI.ack`, or a typed name) - only for actions that are
   risky or cannot be undone.
7. **Actions** (`UI.actions`) - always last.

**Long forms go in steps** (`stepper()`): Edit container, Import, New VM
and New share. Numbered chips across the top, one pane at a time, Back and
Next at the foot. Every pane is drawn at once and only hidden, so the form's
save reads every field; an edit keeps its Save on every step.

Edit container uses Basics (workload and container names, images and autostart),
Hardware and access (resources, devices, privileges and ports), Environment values
(variables, managed references and startup configuration), Storage, Where it runs,
and Address. Each step groups its fields by container. Changing a name or image
updates that container's heading throughout the form; each field is rendered once.

**One explanation box at most**, and only for a risk. A field's explanation
is a `tip()` beside its label; what the whole dialog needs you to know is one
`UI.more("How this works", …)` at the foot. `foldDialogNotes()` enforces it
as a dialog draws: a second plain note and any after it fold into that
expand; warnings, errors and notes with controls stay.

**Buttons.** The actions bar is the last thing in the dialog, pinned to its
bottom edge while the content scrolls. Cancel (or Close) comes first and the
main action last, on the right; on a phone they stack full width, main action
on top. A button names what it does - `Delete volume`, `Start anyway` - not
`OK`. A destructive main action is `kind: "danger"`.

**Risky actions ask once, clearly**: one acknowledgement (`UI.ack`) or, when
it cannot be undone, the object's name typed - never both for the same risk.

**Phones.** Dialogs open as a sheet from the bottom of the screen, full width;
tables become cards and field grids one column on their own.

## Components

| Component | Use it for | Not for |
|---|---|---|
| `UI.lead(html)` | A dialog's opening sentences | Long explanations |
| `UI.callout(tone, title, html)` | The one thing that needs attention. `warn` and `bad` for real risk; `ok` a check passed; `info` a notice that must be seen | Merely useful information |
| `UI.section(title, html)` | A titled group in a dialog | A single line |
| `UI.facts([[label, value]])` | A handful of values in a dialog (`.about-grid` on a page) | Long text |
| `UI.table(columns, rows)` | Items with the same fields in a dialog (`tbl stack` on a page) | Two or three values |
| `UI.checklist(items)` | Checks and their results (`ok`, `warn`, `bad`, `run`, `todo`, `skip`) | Steps to follow |
| `UI.steps(list, current)` | A numbered guide (`current = -1`), or where a long job has got to | Unordered facts |
| `UI.progress(value, options)` | Work under way; `null` while its size is unknown | How full something is |
| `UI.meter({ now, after, warnAt })` | How full a host, disk or volume is, now and after a change | Progress over time |
| `UI.more(summary, html)` | Detail in a dialog most people do not need | Anything needed to decide |
| `UI.guide(summary, html)` | How a page works, collapsed | Something that needs doing now |
| `UI.stats(cards)` | A page's headline figures, behind a summary line | The first thing on a page |
| `summaryLine(id, items, detail, label)` | A page's one-line status, its tiles a tap away (open stays open across refreshes) | A list |
| `moreMenu(items)` | A header's secondary actions, behind `⋯` | The main action |
| `menuButton(label, items)` | A main action that is a choice - ＋ Import and its kinds | A single action |
| `actionBar(items, { shown })` | A card's or a row's buttons: `shown` (2) as buttons, the rest in `⋯` | A dialog's buttons |
| `stepper(id, steps, finish, { always })` | A long form, one step at a time | A short form |
| `Diagram.node(n)`, `Diagram.vip(ip, rows)`, `Diagram.mapping(rows)` | What a node holds, where a VIP leads, where each folder goes - drawn live | Decoration |
| `settingRow(label, help, control)` | One setting: label and help left, its control right | A form of many fields (use a dialog) |
| `serviceRow(name, state, detail, actions)` | Something Homestead runs or connects to, with its state and buttons | A list of like items |
| `UI.ack(id, sentence)` | The one checkbox a risky action needs | Settings |
| `UI.fields(...)` / `UI.field(label, control, { help })` | Forms: two columns on a desktop, one on a phone | - |
| `UI.chip(label, tone)` | A short status next to a name | Sentences |
| `UI.button(label, onclick, { kind })` / `UI.cancel()` | Buttons | - |
| `UI.actions(buttons, start)` | A dialog's buttons | Buttons that act on one row |

Menu and bar items are `{ label, run, icon, need, tip, danger, ariaLabel }`. `run` goes
into the `onclick` attribute as it is, so values in it go through `jsq()`
like any other handler's; `tests/handler-escaping.test.js` sends hostile
values through them.

Older markup - `.note`, `.sec`, `.row` and `.modalactions` in dialogs,
`.grid.statgrid` on pages - still works, styled to match. New and rewritten
work uses the components.

## Checking

### Rules the tests hold you to

`tests/design-rules.test.js` reads the web app's code and fails, naming the
file and line, when it finds:

- a button written into a table cell instead of an `actionBar()` (unless it
  is marked `data-form-row`);
- a list made by mapping items to `serviceRow()` - a list of like things is a
  `tbl stack` table;
- a table that is not `tbl stack`;
- a Settings card with a title but no `settings-card-head`.

It also checks this file still states those rules. Change a rule in both
places at once; never quiet the test for one screen.

With the demo running (`PORT=4173 WEBROOT=web python server/server.py`):

```bash
node scripts/audit_pages.mjs
```

```bash
node scripts/audit_dialogs.mjs
```

Each opens every page or dialog it knows at a desktop and a phone width,
saves it whole under `release-assets/pages/` or `release-assets/dialogs/`,
and fails when something runs off the screen, a page scrolls sideways, text
is smaller than 10px, or a page or dialog cannot be opened.
`node scripts/dialog_sheets.mjs mobile 4 4 pages` lays captures side by side
for review, and reading them is part of the check: the scripts catch what
can be measured, not clutter.

A new page is added to `scripts/audit_pages.mjs` and a new dialog to
`scripts/audit_dialogs.mjs`, with demo data in `web/js/demo.js` if it needs
any, so CI checks it from then on.
