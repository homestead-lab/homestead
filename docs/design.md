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

1. **Header** (`UI.pageHeader`) - on the left, the page family's tabs when it has
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

Default storage offers one-, two- and three-replica recipes and SSD/HDD tag
recipes. Each opens the editable class form before any write. Matching existing
classes can be made default; conflicting names get a fresh name. Placement
warnings come from disk inventory, and absent tags remain selected for review.
Suggestions never change the current default merely by opening the guide.
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
  into on a phone. Each section has an icon from the main menu's sprite
  (`index.html`), drawn the same way - a 24px grid of 1.7 strokes - dimmed until
  it is the section open. The setup guide's steps have none.
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

### Suggested values

A text field that suggests values - an existing group, a namespace, a
section - names a `<datalist>` with `list=` as usual. `ui.js` shows those
values in a list of its own instead of the browser's popup: under the field
and its width, inside the dialog, above the field when there is no room
below, with the arrow keys, Enter and Escape. Fill or change the
`<datalist>`; nothing else is needed.

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

### Section rail standard

Use the same opaque shell, concise header and bottom action bar for every
dialog. Long forms use a section rail on the left; phones replace that rail
with a labelled section selector above the content. Short confirmations and
reviews need no rail. Do not add empty sections to make a small dialog larger.

Top to bottom, leaving out what the dialog does not need:

1. **Title** — action and object, such as `Edit · frigate` or `Delete volume · media`.
2. **Lead** (`UI.lead`) — one sentence describing the decision or next action.
3. **Attention** (`UI.callout`) — blockers, consequences and required action stay
   visible. Combine related warnings into one notice.
4. **Content** — the current section's fields, a concise review, or current job status.
5. **Optional detail** (`UI.more`) — named disclosures such as `Optional settings`,
   `Details` on a review, or `After the request`.
6. **Confirmation** (`UI.ack`, or a typed name) — an existing required risk check.
7. **Actions** (`UI.actions`) — one footer, always after the content.

### Navigation and buttons

`stepper(id, steps, finish, { always: true })` is a section editor: navigate
directly, with **Cancel** on the left and **Review changes** on the right on every
section. Do not add Next beside Review changes. A sequential creation form uses
the same rail with Back, Next and a final Review action. Visiting a section does
not mean its fields are valid and must never give it a completion tick.

Render each field once and hide inactive panes. Switching sections must preserve
values, added items and staged removals. Container edit groups Basics, Hardware
and access, Environment values, Storage, Placement, Address and Monitoring by container.
The full-page container deploy form also uses `UI.sectionForm`: Basics, Hardware
and access, Environment values, Storage, Address, Additional containers and Summary.
Use Back/Next and one final Review deployment action; the review still checks capacity
and any restart acknowledgement. Page forms use `page: true` for an in-flow footer and supply `cancelHtml` to return to their
collection, while dialogs keep their dismiss action. Keep the live summary in its
section, without a second deploy button. Storage/network explanations are collapsed;
blocking template requirements and shared-lifecycle impact stay visible. Collapse
optional appearance fields. Use `.f2.compact-fields` for short related controls
(such as namespace/pod copies or CPU/memory) that fit side by side on phones. Adding a
container reveals its section, and validation reveals and focuses invalid fields.

Adding a container is distinct from increasing pod copies. Remove stages a
removal with Undo; review keeps persistent data and explains the rollout.

Keep the footer reachable while content scrolls. Cancel or Close goes left; the
main action goes right and names the result (`Review changes`, `Update`,
`Delete volume`). The destructive action uses `kind: "danger"`. On phones keep
the same order, with wrapping only when needed and at least 44px touch targets.
Long tables become cards; form columns stack. The section selector uses a 16px
font and an explicit label. Do not squeeze the desktop rail onto a phone.

### Professional, succinct copy

Use sentence case and direct verbs. Aim for one lead sentence and one short
instruction per field. Remove conversational filler, repeated consequences,
implementation narration and promises that checks cannot guarantee. A long
dialog may contain many objects; judge explanatory copy separately from names,
rows and field labels.

Use `tip()` beside a field label for a short definition or uncommon setting.
Tooltips must work by keyboard focus and touch as well as hover. Use a named
`UI.more` disclosure for multi-paragraph guidance, commands or diagnostic output.
Use rail sections when the user must complete several tasks. Never hide a
blocker, destructive consequence, recovery instruction or required consent in
a tooltip. Keep examples concise and use neutral, professional wording.

### Show the decision; disclose the explanation

Keep essential inputs, affected objects, interruption/data-loss consequences,
blocking errors and required acknowledgements visible. Put uncommon settings,
long identifiers, capacity calculations and logs behind descriptive summaries.
Prefer a meaningful count or value (`Completed · 4`) over a generic `Details`.
Avoid repeating the same consequence in the lead, a callout and an acknowledgement.

Disclosures preserve their state while a dialog polls. Different job records
have separate disclosure state. Invalid fields must reveal their section and
any collapsed ancestors before receiving focus (`revealDialogField`). Keep
required warnings outside disclosures so a previous collapsed state cannot hide
a new blocker. Legacy `foldDialogNotes` works within each section; new content
uses explicitly named `UI.more` disclosures instead of relying on automatic folding.

Risky actions ask once for each distinct consequence. Preserve existing server
review tokens, role checks, confirmations and recovery guards when restyling.
Do not turn a visual simplification into weaker approval or automatic retry.

### Reviews

A review is the dialog before something happens: starting an app or VM, an
update, a reboot, a delete. They share one shape, so a person learns it once:

1. **One sentence** says what happens to what, and where - `Starts frigate on
   one of 2 hosts.`, `Reboots k3s-1.` Placement facts (pinned, preferred, the
   only host that can run it, a restart policy that changes) belong in that
   sentence or as a chip on the host, not in a box of their own.
2. **One notice** (`UI.callout`), only when there is something to act on or
   accept: `Check first` for warnings, `Can't start` / `Update blocked` for
   blockers. Say each concern once, however many apps, containers or checks
   raised it, naming the apps it affects. A note that the action does not
   change - a container that already had no memory limit, say - is not a
   warning; it goes in Details.
3. **The objects**, once each: one row per app with one change, not one per
   container when they all move between the same two images.
4. **Where it can run**: `capacityHostTable(plan)` - each host with the memory
   it would be left with, and the reason a host is out. Start a container or a
   VM with `startReview(plan, { what, name, extra, ackId, onAck, details })`,
   which composes all of this; never write a launch-host box or a second host
   table.
5. **Details** (`UI.more`): requests, reservations, estimates, caveats, exact
   digests (shortened, the whole one on hover), recovery images.
6. **One acknowledgement** (`UI.ack`), only when there is a warning to accept
   or an interruption the action always causes. One short sentence - at most
   16 words - naming what is accepted (`Start it anyway`, `Restart it now`,
   `Update despite the warnings`). The consequence is already in the notice.
7. **The button names the result**: `Start`, `Restart`, `Resume`, `Update 2`,
   `Reboot host` - not `Start reviewed VM` or `Apply`.

A review with several kinds of things to look at - a reboot's apps, volume
copies, disruption budgets and local storage - uses the section rail with a
**Summary** first (lead, a facts row of counts, the one notice) and a section
per kind, each titled with its count (`Volume copies · 2`). Sections with
nothing in them are left out. Typed confirmations and acknowledgements go in
the rail's `noticeHtml`, so they stay in view whichever section is open.

An action list - Host actions, say - is one line per action: what it does in
a few words, and its button on the right. Offer the action the state allows
(Cordon or Uncordon, not both). Lists that belong to the object - its apps,
its quorum - are sections beside the actions, not boxes under them.

Restyling a review never weakens it: keep review tokens, one-shot approval,
blocked states that cannot be forced, typed names and recovery guards.

### Verbosity budget

What a dialog shows before any Details is opened is its budget, and
`scripts/audit_dialogs.mjs` fails CI when a dialog:

- shows more than **300 visible words** - or **200** for a review or
  confirmation (a name with review, start, update, reboot, shutdown, power,
  delete, remove or confirm);
- shows more than **one notice** (`UI.callout`, or an older `.note.warn` /
  `.note.bad`): combine them;
- **says the same thing twice**: the same sentence of 40 characters or more in
  two places.

Words count names and rows as well as prose, so the budgets sit just above
today's longest dialogs: they stop a dialog growing, they do not make a long
list wrong. When a dialog nears its budget, move explanations, identifiers and
arithmetic into `UI.more`, or split it into sections - never raise the budget
for one screen.

### Progress, container updates and Jobs

Show current phase and the next useful action first. A percentage must represent
a reported measurement; unknown progress uses `UI.progress(null)` or a status
without a bar. Navigation is not progress. Lost contact shows last known state
and explicitly leaves completion unverified. Failed and blocked states remain
visible; diagnostic output can collapse.

Container update review shows the apps and image changes, restart impact and
warnings, as a review (above). Exact digests and capacity collapse under Details.
The update button stays disabled until the restart is acknowledged. During a queue, keep
failed or waiting workloads and their reason visible. **Close queue** explicitly
stops unstarted updates; submitted rollouts continue in Jobs. Never imply that
the browser-managed queue continues after closing it.

Jobs uses a list on the left and selected job detail on the right. On phones the
list sits above the detail. Keep selection through polling. Failed jobs belong
under **Needs attention**, active jobs under **Running**, and successful/cancelled
history under **Completed**. A job's **Log** and **Dismiss** buttons sit at the start of
its button row, its own actions (Open, Carry on, Cancel, recovery) at the end -
never behind a collapse. **Clear finished** removes every record the server
says may go, failed ones included; protected recovery records stay.

## Shared page and Settings structure

`web/js/ui.js` owns page and module layout. Pages provide titles, content, state
and handlers through shared components. Do not write custom header, Settings card,
sidebar, mobile collection toolbar or save-bar markup in feature modules.

- `UI.pageHeader(titleHtml, descriptionHtml, actionsHtml, options)` places the
  subtitle and filters on the left and actions on the right. `paint()` adds the
  page family's tabs. Keep the primary action last; use `moreMenu` for secondary
  choices. Dynamic values in HTML slots must be escaped.
- `UI.moduleHeader(titleHtml, descriptionHtml, actionsHtml)` gives Settings,
  host details, Cluster and Setup the same heading and action placement. Its
  action group wraps below the heading when space is limited.
- `UI.settingsCard(bodyHtml, {tab, id, save, wide, hidden})` retains a module's
  topic, loading identity, save scope and visibility. Reuse the same content
  renderer wherever a module appears; do not copy its markup into another page.
- `UI.settingsGrid(bodyHtml, tab)` groups cards for a selected Settings section.
  `UI.saveBar` owns the sticky Discard/Save layout and live status message.
  Callers retain dirty tracking, validation, permission checks and save behavior.
- `UI.workspace` and `UI.workspaceNav` provide the shared sidebar/content layout
  used by Settings, host details and Setup. `UI.selectWorkspace` updates section
  selection without replacing the fields. On phones, Settings and host details
  show their section list first with a consistent Back button; Setup keeps its
  step picker. Keyboard navigation uses Up, Down, Home and End.
- `UI.collectionHeader` places mobile container/VM controls above their summary.
  Continue using `settingRow`, `serviceRow`, `actionBar`, `summaryLine`, `UI.stats`
  and `UI.table` for their existing purposes.

Spacing comes from `--page-gap`, `--module-gap`, `--workspace-gap` and
`--workspace-rail` in `web/style.css`. Adjust the shared rule instead of adding
page-specific spacing overrides. Keep loading, blocked, empty and ready states
in the same module shell so controls do not change location as data arrives.

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
| `UI.more(summary, html, open, key)` | Detail in a dialog most people do not need | Anything needed to decide |
| `UI.guide(summary, html)` | How a page works, collapsed | Something that needs doing now |
| `UI.stats(cards)` | A page's headline figures, behind a summary line | The first thing on a page |
| `summaryLine(id, items, detail, label)` | A page's one-line status, its tiles a tap away (open stays open across refreshes) | A list |
| `moreMenu(items)` | A header's secondary actions, behind `⋯` | The main action |
| `menuButton(label, items)` | A main action that is a choice - ＋ Import and its kinds | A single action |
| `actionBar(items, { shown })` | A card's or a row's buttons: `shown` (2) as buttons, the rest in `⋯` | A dialog's buttons |
| `UI.sectionForm(id, sections, finish, { always, noticeHtml })` / `stepper(id, steps, finish, { always })` | Section rail and phone selector; `always` for direct editing | A short form |
| `revealDialogField(field)` | Reveal a field’s section and disclosures before focusing an error | Showing unrelated help |
| `Diagram.node(n)`, `Diagram.vip(ip, rows)`, `Diagram.mapping(rows)` | What a node holds, where a VIP leads, where each folder goes - drawn live | Decoration |
| `settingRow(label, help, control)` | One setting: label and help left, its control right | A form of many fields (use a dialog) |
| `serviceRow(name, state, detail, actions)` | Something Homestead runs or connects to, with its state and buttons | A list of like items |
| `UI.ack(id, sentence)` | The one checkbox a risky action needs - 16 words at most | Settings |
| `startReview(plan, options)` | The review before a container or VM starts: sentence, notice, hosts, Details, acknowledgement | Writing a capacity box again |
| `capacityHostTable(plan)` | Each host with its memory after a change, and why a host is out | A launch-host box |
| `UI.fields(...)` / `UI.field(label, control, { help })` | Forms: two columns on a desktop, one on a phone | - |
| `UI.chip(label, tone)` | A short status next to a name | Sentences |
| `UI.button(label, onclick, { kind })` / `UI.cancel()` | Buttons | - |
| `UI.masterDetail(groups, selectedKey, detailHtml, options)` | Grouped navigation and a selected detail, such as Jobs | Form steps |
| `UI.sectionNavigation(id, sections, options)` | Externally managed setup guides; section forms include this automatically | Hand-written navigation |
| `UI.actions(buttons, start, { className, attrs })` | A dialog's buttons | Buttons that act on one row |

Menu and bar items are `{ label, run, icon, need, tip, danger, ariaLabel }`. `run` goes
into the `onclick` attribute as it is, so values in it go through `jsq()`
like any other handler's; `tests/handler-escaping.test.js` sends hostile
values through them.

Dialog structure is owned by `web/js/ui.js`. Feature code supplies section data,
content and action handlers; it does not write rail, navigation or footer markup.
Use stable section keys when sections are conditional. All panes stay mounted so
navigation preserves unsaved values. The shared controller owns desktop tabs,
keyboard focus, the phone selector and revealing fields with validation errors.

Use `UI.actions` for every dialog footer. Pass Cancel/Back on the left and the
primary action last on the right. `UI.cancel()` carries a dismissal marker; the
component also moves marked dismissals out of an existing action string. Preserve
permission gates, disabled states and handlers when migrating markup. Inline
controls that edit a row remain an `actionBar` or a form row.

Jobs uses `UI.masterDetail`; its caller owns selection, refresh and recovery
actions. Stable disclosure keys preserve expansion when counts or text change.
Long explanations use `UI.more`; decisions, blockers and consequences stay visible.
`UI.button` and section/list selection callbacks escape their handler once; use
`jsArg()` for values passed to those APIs.

Older `.note`, `.sec` and page stat grids still work, styled to match. New and
rewritten work uses the components.

## Checking

Check the section rail at desktop and phone widths, in both themes. Verify
that navigation retains field values, disclosures stay open during polling,
errors reveal hidden fields, the footer is reachable, and completed-history
clearing preserves failures and recovery records. Inspect screenshots as well
as checking overflow; a dialog fitting its box is not enough.

### Rules the tests hold you to

`tests/design-rules.test.js` reads the web app's code and fails, naming the
file and line, when it finds:

- a button written into a table cell instead of an `actionBar()` (unless it
  is marked `data-form-row`);
- a list made by mapping items to `serviceRow()` - a list of like things is a
  `tbl stack` table;
- a table that is not `tbl stack`;
- a hand-written Settings title/subtitle instead of `UI.moduleHeader`;
- rail, section navigation, pane or footer markup authored outside `ui.js`;
- a hand-written dismissal row instead of `UI.actions`;
- page headers, module headers, Settings wrappers, navigation or save bars written
  outside the shared UI module;
- a `UI.ack` sentence over 16 words;
- a container or VM start that does not use `startReview`, an image update
  review that does not use `capacityHostTable`, or the older placement blocks
  used outside `views-workloads.js`;
- more uses than today of older markup - `.note`, `.sec`, `deployCapacityHtml`,
  hand-written acknowledgement checkboxes and "I accept" wording. That count
  only shrinks: lower it in the same change that removes some.

It also checks this file still states those rules. Change a rule in both
places at once; never quiet the test for one screen.

With the demo running (`PORT=4173 WEBROOT=web python server/server.py`):

```bash
node scripts/audit_pages.mjs
# PAGE_OUTPUT selects the capture folder; HOMESTEAD_AUDIT_THEME=light checks light mode.
```

```bash
node scripts/audit_dialogs.mjs
```

Each opens every page or dialog it knows at a desktop and a phone width,
saves it whole under `release-assets/pages/` or `release-assets/dialogs/`,
and fails when something runs off the screen, a page scrolls sideways, text
is smaller than 10px, or a page or dialog cannot be opened. The dialog audit
also holds the verbosity budget: too many visible words, more than one notice,
or a dialog that says the same thing twice.
`node scripts/dialog_sheets.mjs mobile 4 4 pages` lays captures side by side
for review, and reading them is part of the check: the scripts catch what
can be measured, not clutter.

A new page is added to `scripts/audit_pages.mjs` and a new dialog to
`scripts/audit_dialogs.mjs`, with demo data in `web/js/demo.js` if it needs
any, so CI checks it from then on.


### Dialog contact sheets

Render both themes with `scripts/audit_dialogs.mjs`, setting `DIALOG_OUTPUT`
to `release-assets/dialog-review/dark` or `release-assets/dialog-review/light`
and `HOMESTEAD_AUDIT_THEME` to the matching theme. Each audit includes desktop
and phone screenshots, visible copy and layout measurements.

Run `python scripts/build_dialog_contact_sheets.py release-assets/dialog-review`
with Pillow installed. It validates matching theme inventories, then writes
paired PNG cards, paginated contact sheets, a ZIP of those sheets and a searchable
`index.html` gallery. Open the gallery locally; review images use demo data.

The same contact-sheet builder supports page captures:
`python scripts/build_dialog_contact_sheets.py release-assets/page-review --kind pages`.
Capture both themes into that folder with `PAGE_OUTPUT` before building it.


### Customizable dashboard

`web/js/dashboard.js` owns the versioned widget registry, validated layout,
canvas and editor. Feature renderers provide widget content; editing and viewing
use the same renderers. Portal tiles come from `portalTiles`, shared with Portal.
Do not fork charts, links or resource cards for the editor.

- Use a 12-column grid with third, half, two-thirds and full-width cards.
  A widget may constrain its widths to keep its content usable. Node health supports
  all widths. Height is a minimum; compact resource and Portal lists scroll inside
  short, medium or tall cards.
- Desktop Start column and Start a new row preserve intentional gaps. Calculate
  cells in array order without overlaps; narrow canvases discard desktop positions.
- Array order is visual, keyboard and mobile reading order. Do not use dense grid
  backfilling or CSS order. Narrow canvases stack cards and reset minimum heights.
- Keep editing explicit. Give cards a move handle, resize handle and accessible
  settings. Collapse the control panel on phones and retain the editing toolbar.
  Provide arrow-key movement, Earlier/Later controls, Undo/Redo,
  recoverable Reset, Cancel and Save. Phone preview uses the same container rules.
- Put selected-widget settings before the bounded, scrollable library. Settings
  buttons bring that inspector into view and move keyboard focus to it. Use themed
  scrollbars for nested lists. Container groups, resource status, Portal sections
  and Portal compact/tiles display are account options, preserved during refresh.
  An omitted group filter means all groups (including future additions); an empty
  selection means none. Keep Portal icons, safe links and status dots shared.
- Keep the library and size settings outside the live content. Widget actions are
  inert during editing; refresh cannot replace the draft. Cluster alerts remain
  outside the customizable grid. Saving does not modify cluster resources.
- Persist versioned layouts in the account store through
  `/api/auth/preferences/dashboard`, using the authenticated account identity.
  Layouts follow the user across sessions, browsers and devices. Read the latest
  layout before editing; save with its revision and Kubernetes resourceVersion
  checks. Keep drafts on server errors or concurrent-session conflicts. Import a
  legacy browser layout only if the account has no layout; never overwrite a
  server layout during migration. Warn before leaving a draft; clear it on sign-out.
- Add metadata and content to the registry to introduce a widget. Validate old
  layouts, unknown IDs, duplicate IDs and sizes before rendering. An empty saved
  layout is valid. Keep behavior coverage in `tests/integration/dashboard-editor.mjs`.


### Custom dashboard content

Custom text / HTML cards reuse the dashboard shell, sizes, placement and account
preferences. Up to four independent cards are available; each accepts an 80-character
title and 16,384 characters of content. Plain text is the default and is escaped.
Keep the editor in Widget settings, with an explicit Update preview action.

Static HTML is rebuilt from a small formatting-tag allowlist by
`DashboardCustom`. Only selected inline visual properties with non-resource values
survive. Drop executable/resource elements, event handlers, links, forms, SVG,
MathML, custom attributes and user stylesheets. Parsing uses an unconnected template;
never insert the original markup into the live page or allow it into shared UI HTML.
Limit sanitizer depth and node count as well as stored source length.

Render the result in an iframe with an **empty sandbox**, no same-origin or script
permissions, and no-referrer. Its own CSP denies every resource type except inline
styles and prohibits base URLs and forms. Do not relax the app-wide CSP for custom
content. No custom script, network access, navigation, popup, API, cookie or parent-DOM
capability is exposed. This is for static cards; external integrations need their own
reviewed feature. Account APIs store source as data, never serve it as an HTML page.
Maintain hostile-content browser tests under the production parent CSP, including
parent/storage isolation, blocked network requests, scripts, malformed markup and
saved-content rendering in both themes at desktop and phone widths.

### Mobile page layout contract

Use the same shared components and content order at every size. At 900px and
below, page chrome is compact (12px outer padding, 10px header gap, 12px module
gap). Do not stack short controls or facts merely because the screen is narrow.

- `UI.pageHeader` keeps actions and summaries by default. Use
  `mobileSummary:"omit"` only for introductory copy already conveyed by the page
  title/tabs; never omit counts, warnings or operation state. Group desktop-only
  actions with `actionsClass:"hide-sm"` so an empty action row takes no space.
- `UI.workspace` owns mobile section navigation. Supply `backLabel`, `back` and
  `currentLabel`: a neutral return link on the left, current section on the right.
  Settings uses **All settings**, hosts use **All sections**. Save and discard
  remain in the shared save bar and retain the existing unsaved-change check.
  Entering a section focuses its visible back link; returning focuses its list item.
- `settingRow` keeps labels beside short controls, switches, sliders and segmented
  choices when they fit. Long text fields and selects get the full next row.
  At 360px and below, segmented choices may wrap as a unit. Keep touch targets at
  least 40px (44px for back navigation); do not reduce text to fit.
- Facts use two equal columns with wrapping values. Charts and short numeric
  summaries may share a row using container queries. Long prose, large charts and
  editors remain full width. Tables should use the existing summary/disclosure
  collection pattern rather than hide essential values or stack every table cell.
- Dashboard layout editing is available above 900px. Phones show the saved account
  layout without Edit or desktop preview controls. If an editor is resized into
  mobile width, retain its draft and show a concise notice plus Save/Cancel;
  resume the editor at a wider size. Desktop's phone preview remains available.
- Use `UI.more`/`UI.guide` for secondary explanations and `tip()` for brief field
  help. Keep warnings, failed checks, required inputs and current progress visible.

Validate 320px and 390px in light/dark themes and a desktop width, including
keyboard focus, unsaved changes, empty dashboards and switching viewport mid-edit.

Dashboard drag feedback: once the pointer moves past the drag threshold, a
translucent copy follows it at the original grab offset. Keep the source in place
and faded, and outline the destination. The preview must preserve card dimensions
and container-query styling, remain inert and hidden from assistive technology,
and disappear on drop, Escape, pointer cancellation, loss of focus or navigation.
Keyboard reordering continues to use the normal card and live announcements.


### Health advice and update monitoring

- `HealthInsights` owns read-only observation rules and the same advice in dashboard widgets and **Cluster → Health**. Keep Critical, Medium and Low text beside color; unknown observations are actionable gaps, never healthy zeroes. Only reported etcd roles inform quorum advice; worker count does not.
- `UI.insightList` is the compact shared row for observations: status dot, title, one brief explanation, status text and a review action. `UI.statusDot` also marks Jobs list entries and cards. Keep color accompanied by words.
- Dashboard additions are optional account widgets. Do not insert them into existing saved layouts. Fetch only sources required by mounted widgets, share requests and reuse the existing Jobs refresh loop. Platform/host checks complement image checks in the Updates widget.
- Backup freshness shows recorded external-backup ages and missing copies. A schedule, replica or snapshot is not proof of a successful external backup. Do not assume a daily recovery target for a weekly schedule.
- Image selection ends with **Review selected** in `UI.actions`; checking registries is secondary. Review and acknowledgement still precede mutation. Keep policy and exact image/capacity detail in disclosures.
- `rolloutProgress` uses `UI.progress` and `UI.checklist` for download, replacement pods and readiness. Show download percentages only with byte totals; old-pod readiness is not overall update progress. Finished queues count successfully updated apps. Preserve last-known progress under a disconnect notice.
- SMART defaults stay **1 reallocated → warning; 1 pending / uncorrectable → critical**. Explain historical counts in tooltips and preserve user thresholds. The [smartmontools manual](https://github.com/smartmontools/smartmontools/blob/main/src/smartd.conf.5.in) reports nonzero pending and offline uncorrectable counts by default; severity mapping is Homestead's conservative policy, not a prediction of remaining drive life. [K3s embedded-etcd guidance](https://docs.k3s.io/datastore/ha-embedded) recommends an odd number of servers and at least three for high availability.


### Alert acknowledgement

Use the shared `UI.insightList` for active conditions, with severity text, a
resource title, concise detail, and **Review** / **Acknowledge** actions. Put
acknowledged conditions behind `UI.more`, with **Undo** available per condition.
Keep **Close** in `UI.actions`. Account acknowledgement changes notification
attention, never the underlying health verdict. Do not use a green healthy
state for an acknowledged problem. See [notification rules](notifications.md)
for trigger, delivery and wording contracts.


### Node health dashboard widget

Node health supports third, half, two-thirds and full widths. Compact summaries
are the default: repeat labelled metrics per host in a grid that responds to the
widget width, with automatic stacking on phones. Reuse shared meters, status
dots and typography. Show missing readings as unknown; show SMART warnings even
when Kubernetes reports Ready. Keep a detailed comparison as a saved per-account
widget option. Configuration must be available through a visible Settings action,
as well as selecting the card or using its resize handle.

Dashboard cluster warnings and health-widget links open Cluster → Health with
the section focused. That section must include the reasons shown in the warning,
not just platform readiness or general suggestions.

Containers and Virtual machines are optional list widgets sharing one compact
row component: name, state, observed CPU and memory. Keep stopped and missing
readings distinct from zero usage. Container lists exclude platform helpers.
Support third through full width, with a Short (240 px) height for shallow
layouts. Wide lists flow into parallel groups of four rows; narrow lists stack
the groups. Scroll rows within the card with column headings and a count.
Keep these summaries read-only; View all opens the existing management page.

### Mobile notification entry

The top-bar bell opens the shared Notifications dialog on phones. Give it a
visible title, comfortably sized action rows, scrollable content and the shared
Close footer. Running jobs and attention items lead to their existing review
screens. Desktop keeps the anchored menu. Do not use a small detached bottom
popover for an attention surface containing several kinds of action.
