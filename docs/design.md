# Dialog design

Every dialog in Homestead is built from the same components, in the same
order, and works on a phone. This page is the rulebook; `web/js/ui.js` holds
the components and `scripts/audit_dialogs.mjs` checks every dialog against
the rules that can be measured, in CI.

## The shape of a dialog

Top to bottom, leaving out what a dialog does not need:

1. **Title** - what the dialog is about: `Delete volume · media`,
   `Start frigate?`, `New share`. Sentence case, the object after a `·`.
2. **Lead** (`UI.lead`) - one or two sentences: what happens if you go ahead.
3. **One callout** (`UI.callout`) - the thing that needs attention, if there
   is one. Never more than one warning or error callout per dialog: gather
   everything that needs attention into one, as a list.
4. **Content** - sections (`UI.section`) of facts, tables, checklists, steps
   or form fields.
5. **Details** (`UI.more`) - how something is calculated, background, the
   objects behind it: anything most people will not need, behind a
   disclosure.
6. **Confirmation** (`UI.ack`, or a typed name) - only for actions that are
   risky or cannot be undone.
7. **Actions** (`UI.actions`) - always last.

## Components

| Component | Use it for | Not for |
|---|---|---|
| `UI.lead(html)` | The opening sentences | Long explanations - those go in `UI.more` |
| `UI.callout(tone, title, html)` | The one thing that needs attention. `warn` and `bad` are for real risk; `ok` confirms a check passed; `info` for a notice that must be seen | Information that is merely useful: use help text or `UI.more` |
| `UI.section(title, html)` | A titled group | A single line |
| `UI.facts([[label, value]])` | A handful of numbers or values | Long text |
| `UI.table(columns, rows)` | Lists of items with the same fields: hosts, volumes, workloads. On a phone each row becomes a card, with each value labelled | Two or three values - use `UI.facts` |
| `UI.checklist(items)` | Checks and their results (`ok`, `warn`, `bad`, `run`, `todo`, `skip`) | Steps the person follows - use `UI.steps` |
| `UI.steps(list, current)` | A numbered guide (`current = -1`), or where a long job has got to | Unordered facts |
| `UI.progress(value, options)` | Work under way; `null` while its size is unknown | How full something is - use `UI.meter` |
| `UI.meter({ now, after, warnAt })` | How full a host, disk or volume is, now and after a change | Progress over time |
| `UI.more(summary, html)` | Detail most people do not need | Anything needed to decide |
| `UI.ack(id, sentence)` | The one checkbox a risky action needs. One short sentence: what the person accepts | Settings - those are form fields |
| `UI.fields(...)` / `UI.field(label, control, { help })` | Forms: two columns on a desktop, one on a phone, help under the field | - |
| `UI.chip(label, tone)` | A short status next to a name | Sentences |
| `UI.button(label, onclick, { kind })` / `UI.cancel()` | Buttons | - |
| `UI.actions(buttons, start)` | The dialog's buttons | Buttons that act on one row - keep those in the row |

The existing `.note`, `.sec`, `.row` and `.modalactions` markup still works,
styled to match, and a row of buttons ending a dialog is moved into the
actions bar automatically. New and rewritten dialogs use the components.

## Rules

**Colour means risk.** Amber is a warning and red is an error or something
that cannot be undone. A plain note or help text carries information. If
everything is amber, nothing is.

**Say it once.** A warning appears once - in the callout - not again in a
note, a table and the confirmation.

**Numbers are shown, not described.** Capacity is a meter, a set of values is
`UI.facts`, a list of hosts is a table. A sentence that reads "12.6 GiB of
15.6 GiB, 81%" belongs in a meter.

**Keep sentences short.** The lead is at most two sentences. A paragraph that
runs past three lines belongs in `UI.more`.

**Buttons.** The actions bar is the last thing in the dialog, pinned to its
bottom edge while the content scrolls. Cancel (or Close) comes first and the
main action last, on the right; on a phone they stack full width, main action
on top. A button names what it does: `Delete volume`, `Start anyway`,
`Create share` - not `OK` or `Submit`. A destructive main action is
`kind: "danger"`.

**Risky actions ask once, clearly.** An action that can lose data or take
something down asks for one acknowledgement (`UI.ack`) or, when it cannot be
undone, for the object's name to be typed. Never both a checkbox and a typed
name for the same risk.

**Phones.** Dialogs open as a sheet from the bottom of the screen, full
width. Tables turn into cards and field grids into one column on their own;
nothing needs a fixed width. Check every dialog at 390 pixels wide.

**Type.** Nothing smaller than 10px. Body text is 12.5-14px; labels 11.5px.
Section titles are sentence case, not upper case.

## Checking a dialog

With the demo running (`PORT=4173 WEBROOT=web python server/server.py`):

```bash
node scripts/audit_dialogs.mjs
```

It opens every dialog it knows at a desktop and a phone width, saves each
whole in `release-assets/dialogs/`, and fails when a dialog's content is wider
than the dialog or its text is smaller than 10px, or when it cannot be opened.
`node scripts/dialog_sheets.mjs mobile` lays the captures out side by side for
review.

A new dialog is added to the list in `scripts/audit_dialogs.mjs`, with demo
data in `web/js/demo.js` if it needs any, so CI checks it from then on.
