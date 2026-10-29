# Log alerts

A log alert is a saved [log search](Containers#searching-logs) that raises an
alert when it matches too many lines: "more than 10 lines matching `Unable to
read frames` in a minute from an app". Open the list from **Containers → ⋯ →
Log alerts**. Anyone who can sign in can read the list; making, editing,
pausing, resuming and deleting alerts need the **operator** role.

An alert carries the alert's name, the app and a count, never the lines and
never what you searched for. A log line can hold a password or a token, and so
can a search for one, and alerts reach the bell, alert exports in configuration
backups and the lock screens of devices that get notifications. For the same
reason a name that repeats the search text (three characters or more) is
refused.

## Creating an alert

1. **⋯ → Search logs**, choose the app and type what to look for. **Match case**
   and **Regular expression** are options, as in a search.
2. Choose **Alert on this search**. A form opens with the same text and options.
3. Give it a **name** (up to 60 characters) and set **More than** *N* lines in
   **1, 5 or 15 minutes**. *N* is from 1 to 100000.
4. Choose how to **raise it**: Information, Warning or Critical.
5. **Try on the last hour** shows the matching lines per minute for the past
   hour as bars, the minutes that would have been over the threshold, and a
   warning when it would be over the threshold in more than 20 of the 60 minutes. It shows counts only and
   never saves anything.
6. **Save alert.**

Each alert watches one app in the form (the server accepts up to five). There
can be 20 alerts. Editing the app, the search text or its options starts its
counts again; other edits keep them.

## How it counts

The leader counts once a minute, a couple of seconds after the minute ends, and
reads only what each app wrote since the last count: up to 256 KB from each
container (twice that for one that restarted), and at most 40 containers a minute across all alerts. If an alert
would go over that, the apps left out are named on its row and the rotation
starts at a different alert each minute. Only the leader counts; the list warns
when nothing has been counted for three minutes.

Plain text is the default. A regular expression is matched in a separate
process and gets 2 seconds of matching per alert a minute (the process's start-up
is not counted).

**Not read is never zero.** If an app is not running, could not be read, was
beyond the minute's reads, or its expression ran out of time, the row says
**Could not count** and the minute stays blank in the bars. It neither raises the
alert nor ends one that is raised.

### Crash loops

When a container has restarted since the last count, Homestead reads the log of
the container before it as well, so the lines it wrote before it died count.
Lines already counted are skipped, so none is counted twice. If that earlier log cannot be read (a node
rebooted, say), that minute shows as **Could not count**, and counting carries
on the next minute. Restarts that happened before Homestead first looked, or
before a different host became the leader, are not treated as news.

## Pausing and resuming

**Pause** on an alert's row stops counting it until you choose **Resume**. A
paused alert shows a **Paused** chip, is not read, and cannot be raised. If it
was raised, that alert ends at once as "*name*: paused", not as "back under" a
minute later. Editing a paused alert keeps it paused. After **Resume** the
minutes it was paused stay blank in the bars, so a 5- or 15-minute window never
counts them as zero.

### Auto-pause

An alert whose expression runs out of time 10 counts in a row (10 minutes) is
paused by Homestead. Its row says why ("Homestead paused it: its expression ran
out of time 10 minutes in a row. Make the expression simpler, or use plain text,
then resume it."). If the alert had been raised, it ends as "*name*: paused by
Homestead"; if not, the bell gets that notice once, so you are told without it
repeating "ran out of time" every minute. Any minute that is not out of time starts
the streak again, so only 10 in a row pause it. Auto-pause does not apply to
plain text.

## Deleting

**Delete** removes the alert and its counts. If it was raised, its alert ends as
"*name*: removed". The confirmation suggests pausing instead when you only want
it quiet for a while.

## For AI agents

An agent with the `logs:read` scope can call `list_log_alerts` (read-only): each
alert's apps, what it counts, threshold, whether it is paused, its counts for
the last hour and whether it could count. It needs `logs:read`, not just `read`,
because the search text can hold the secret it looks for. Making, pausing and
deleting alerts stay with people. See [AI agents](AI-agents).
