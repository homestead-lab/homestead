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

**Colour means risk.** Amber is a warning; red is an error, or something that
cannot be undone. A plain note or help text carries information. If
everything is amber, nothing is.

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

1. **Header** (`.phead`) - the title, a one-line subtitle saying what the page
   is for, and the page's actions on the right (the main one `pri`). On a
   phone the actions wrap under the title.
2. **Tabs** (`.seg`), if the page has views.
3. **Guide** (`UI.guide`) - collapsed: how this part of Homestead works, for
   whoever wants to know.
4. **One callout**, only when something needs doing now (a clash, a failure).
5. **Stats** (`UI.stats`) - two to four figures that answer "is it well?".
6. **Content** - sections with `.sec` headings or cards (`.card` with
   `.ctitle` and `.csub`), each holding one of the collections below.

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
first column the item's name.

### Phones

Pages are designed for a phone as much as a desktop:

- **Nothing scrolls sideways.** A wide table stacks; a long name wraps.
- **Use the width.** Pair short things - stats, facts, tiles, short table
  values - two to a line. A short value alone on a full-width line, or a
  label at one edge and its value at the other, wastes the screen.
- **The important thing first.** State and figures before explanation;
  explanation collapsed.
- **Actions stay reachable** - in the header, or at the foot of each card.
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

### Density

Homestead is a working tool: show more at once, pad less.

- **Use the shared sizes.** Buttons are 32px tall (`.btn.sm` 28px), inputs
  34px, icon buttons 34px, pills and tags 3px by 10px. They live in the
  density block at the end of `web/style.css`. A page does not pad its own
  buttons or fields beyond them.
- **Phones are tighter still**: page padding 12px, card padding 13px, gaps
  10px, big figures 28px - but fields use 16px text there, or the phone zooms
  in while typing.
- **Three visible actions per card at most.** Primary action, the most used
  one, then `…` (`details.actionmenu`) for the rest. Delete always lives in
  `…`, never as a full button in a row. On a phone, secondary buttons carry
  `sm-more` and a copy in the menu carries `sm-only`, so the row keeps one or
  two buttons plus `…`.
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
| `UI.stats(cards)` | A page's headline figures | Long lists |
| `UI.ack(id, sentence)` | The one checkbox a risky action needs | Settings |
| `UI.fields(...)` / `UI.field(label, control, { help })` | Forms: two columns on a desktop, one on a phone | - |
| `UI.chip(label, tone)` | A short status next to a name | Sentences |
| `UI.button(label, onclick, { kind })` / `UI.cancel()` | Buttons | - |
| `UI.actions(buttons, start)` | A dialog's buttons | Buttons that act on one row |

Older markup - `.note`, `.sec`, `.row` and `.modalactions` in dialogs,
`.grid.statgrid` on pages - still works, styled to match. New and rewritten
work uses the components.

## Checking

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
