# Bug reports and diagnostic logs

Administrators can open **Settings → Troubleshooting** and choose **Record a
bug**. Reproduce the problem in the same tab, then use the recording bar's
**Stop recording** button. Stopping the recorder does not cancel cluster jobs.
Add a summary and comment, choose the diagnostic sources, and prepare the report.
**Keep draft** saves the comment without collecting service logs.

The timeline includes clicks (including controls that do nothing), navigation,
focus, dropdown and checkbox changes, disclosures, scrolling, navigation keys
and shortcuts, loading/error state changes, browser errors, viewport information,
and API request results and timing. Control identities use handler names and
element positions. Request IDs correlate browser and server observations.
This is an interaction timeline, not screen video or a visual replay.

The recorder does not read form values, password input, clipboard contents,
authentication headers, request bodies, terminal contents, or the DOM's text.
Browser error messages and service logs can themselves contain sensitive text.
Only the current tab is recorded. A reload or cluster switch interrupts it;
the recording bar offers the saved evidence for review. Events not yet sent
when a tab closes can be missing. Failed batch uploads are retried with the
same sequence number, so an uncertain response cannot duplicate events.

## Downloads and sharing

Both the readable `.log` and diagnostic `.zip` offer two formats:

- **Anonymised**, the default: removes detected credentials and replaces known
  identifiers, addresses and URLs with consistent aliases. The alias mapping
  is never exported. Review before sharing: arbitrary text can contain secrets
  that automatic checks cannot recognise.
- **Full**: preserves original collected diagnostic text and identifiers. It
  may contain secrets and is intended for private troubleshooting.

**GitHub issue…** builds an independently anonymised draft for
`homestead-lab/homestead`, regardless of the selected download format. Review the
exact title and body and acknowledge public sharing before continuing to
GitHub. Submit the issue there using your own account. No attachment is
uploaded automatically. A long comment is shortened for the draft link; its
full text remains in the downloaded report.

**Download logs** collects a package without starting a recording. Packages
contain the readable report, structured events, platform health, selected
Homestead pod logs, cluster events and related saved job summaries, and a
manifest describing missing or truncated sources. Job summaries never poll
job resolvers or open VM consoles. Kubernetes event timestamps can predate
the selected service-log window. Missing sources do not discard the report.
Full exports remain available if anonymisation cannot complete; public drafts
and anonymised downloads fail closed.

## Retention and access

Reports belong to the administrator who created them. Every diagnostics route
requires an administrator session and checks report ownership. Originals are
stored with private file permissions under the data volume's `diagnostics/`
directory and are excluded from configuration backups. Downloads use
`Cache-Control: no-store` and the service worker never caches API responses.

Recordings stop after 10 minutes or 10,000 events, with a 2 MiB event budget.
The browser buffers at most 500 unsent events and sends at most 100 per batch.
Each collected source is limited to 1 MiB; there are at most 20 retained reports
across the installation. Originals expire after 24 hours, cannot be downloaded
after expiry, and are removed by periodic cleanup (every five minutes) or the
next diagnostics visit. Delete a report to remove its saved originals sooner;
copies already downloaded to a device are unaffected.
