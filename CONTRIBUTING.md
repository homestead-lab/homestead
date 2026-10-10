# Contributing to Homestead

Thanks for helping. Homestead manages a whole cluster from inside it, so the bar
is careful rather than clever: a change should say what it does, check before it
acts, and leave a person able to see what happened.

## How the project is built

- **Server:** Python 3.12, standard library only. No `pip install`, no
  frameworks - `server/server.py` routes requests and each feature lives in its
  own `server/homestead_*.py` module. A new dependency needs a very good reason;
  the image is small and has nothing to patch.
- **Browser:** plain JavaScript and one stylesheet, no build step. Files in
  `web/js/` are loaded in order by `web/index.html` and share globals such as
  `api()`, `paint()`, `modal()` and `toast()`. The one vendored library is the
  Monaco editor in `web/vendor/monaco/`.
- **Manifests:** `deploy/deploy.yaml` is the source of truth for what Homestead
  installs and the permissions it holds. Homestead carries it in its image and
  updates its own ClusterRole to match on start, so a new permission reaches
  existing installs by an ordinary upgrade.

## Running it locally

You need Python 3.12 and, for the browser tests only, Node.js 20 or later.

```bash
PORT=8124 WEBROOT=web DATA_DIR=/tmp/homestead python server/server.py
```

Open <http://localhost:8124/?demo=1>. The demo answers every API call from
deterministic sample data in `web/js/demo.js`, so the whole UI works with no
cluster. Without `?demo=1`, Homestead needs a cluster to talk to: run it in
one, or give it a service-account token the way the Deployment does.

## Tests

Run all three before sending a change; CI runs the same.

```bash
python -m unittest discover -s tests
for file in web/js/*.js web/sw.js; do node --check "$file"; done
node --test tests/*.test.js
```

Tests talk to small fakes of the Kubernetes API rather than a cluster. A change
in behaviour comes with a test that says, in its name, what should happen -
`test_a_ready_node_is_not_removed_from_here` rather than `test_remove_2`.

With the local demo running and Playwright installed, the installed-phone
checks run with `node scripts/check_mobile_pwa.mjs`. Set `HOMESTEAD_URL` to
the demo address and `HOMESTEAD_TEST_BROWSER=webkit` to check the iPhone
layout, Safari's installed-app signal, refresh guards and gesture fixtures.
Chromium additionally exercises trusted touch input. Windows WebKit omits
native overscroll CSS; that assertion remains enabled in Chromium, and every
engine checks the viewport bounds. Emulation does not replace an iPhone test.

## Generated files

Some files are produced by scripts; edit the source and re-run the script.
Tests fail when the two disagree.

| Generated | From | Run |
|---|---|---|
| `deploy/nodeprobe.yaml` | `server/homestead_probe.py`, `server/probe/` | `python scripts/render_nodeprobe.py` |
| `deploy/rbac.yaml` | the RBAC objects in `deploy/deploy.yaml` | `python scripts/render_rbac.py` |
| `web/icons/*.png` | the mark's geometry in the script | `python scripts/render_icons.py` |

## Style

- **Match the code around you** - its naming, its comment density, its idioms.
- **Comments explain why**, in full sentences: the constraint, the trade-off,
  what went wrong before. What the code does should be readable from the code.
- **UI text is plain and specific.** Say what will happen and what it costs -
  "Stops frigate on shed, backs up its volumes, restores them here" - not
  "Proceed with operation?". Anything that deletes or restarts shows its impact
  first, and deleting data asks for the name to be typed.
- **Pages work at phone width.** Check a changed page at 375px as well as on a
  desktop, and in the light theme as well as the dark one.
- **Pages and dialogs follow [docs/design.md](docs/design.md).** Build them
  from the shared components - `web/js/ui.js`, stacked tables, stat cards, the
  guide - rather than one-off markup: one callout at most, numbers as meters
  and tables, explanations collapsed, buttons last. Add a new dialog to
  `scripts/audit_dialogs.mjs` and a new page to `scripts/audit_pages.mjs`;
  CI runs both at desktop and phone widths.
- **Permissions stay narrow.** A new permission goes in `deploy/deploy.yaml`
  with a comment saying what uses it; limit it by `resourceNames` where Kubernetes
  allows.

## Commits and pull requests

- One topic per pull request, with the tests passing.
- A commit message's first line says what changed for a person using Homestead;
  the body says why, and anything a reviewer should look at closely.
- Screenshots help for anything visual, at desktop and phone width.

## Releases

`main` is the prod channel. `dev` is the preview channel. Both branches run
the same CI, and publication waits for that commit's successful CI. Existing
installs default to prod; admins change a cluster's channel in **Settings →
Updates**. Changing channel does not install anything. The normal review and
update policy still apply, including when returning from dev to an older prod
version. Helpers and app images keep their own update policies.

Maintainers release stable versions from `main`:

```bash
python scripts/bump_version.py 2.8.80
git commit -am "..."
git tag v2.8.80 && git push origin main v2.8.80
```

The tag runs the tests and publishes a multi-architecture image to GHCR.

For a dev release, merge the desired changes into `dev`, then bump and commit
the preview version before tagging:

```bash
git switch dev
python scripts/bump_version.py 2.8.81-dev.1
git commit -am "Release Homestead 2.8.81-dev.1"
git tag v2.8.81-dev.1
git push origin dev v2.8.81-dev.1
```

Increase the numeric dev suffix for each preview of a planned stable version:
`-dev.1`, `-dev.2`, and so on. Tags are immutable. To promote the code, merge
`dev` into `main`, bump to the stable version without the suffix, commit and
tag the stable release. Merge `main` back into `dev` before starting the next
preview line. A release tag must match the version in source and its commit
must belong to the corresponding branch.

Every release publishes its exact image version and a versioned Helm chart.
Prod alone updates `latest`, `prod`, and the major/minor image aliases. Dev
alone updates the `dev` image alias; its GitHub release is a prerelease and
never GitHub Latest. Updates discover numbered tags and install by digest,
so mutable aliases cannot change an approved rollout or its rollback image.
The release cleanup keeps the newest five dev release pages independently of
prod retention, while retaining all Git tags and registry images.

## Security

Please report a vulnerability privately through
[GitHub security advisories](https://github.com/homestead-lab/homestead/security/advisories/new)
rather than in an issue.

## Licence

Homestead's original code is source-available under [Apache License 2.0 with
Commons Clause 1.0](LICENSE), and that is how everyone receives it, your
contributions included.

By intentionally submitting a contribution for inclusion, you license it to
the maintainer, and the maintainer's successors and assigns, under the plain
[Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0), without the
Commons Clause. You also agree that the maintainer may distribute your
contribution as part of Homestead under the project's licence, or under other
terms, including a different licence or a separate commercial licence. The
wider grant lets the project change its licence later without tracing every
contributor; it does not take anything away from you. You retain your
copyright, and you may use your own contribution however you like.

Contributions are voluntary and unpaid. Any sponsorship, donation or licence
fee the project receives goes to the maintainer and creates no obligation or
payment to contributors.

Do not submit work you lack the right to license this way, including work
subject to incompatible employer terms.

### Sign-off

Each commit carries a `Signed-off-by:` line to confirm the
[Developer Certificate of Origin](https://developercertificate.org/) and the
terms above. `git commit -s` adds it from your Git name and email:

```text
Signed-off-by: Your Name <you@example.com>
```

A pull request whose commits are not signed off is not merged. To sign off
commits you have already made, run `git rebase --signoff origin/dev` and force
push the branch.

Third-party code retains its own licence. Keep its notices and source-access
requirements intact and list it in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
Do not apply the Commons Clause to third-party files. In particular, changes
to MPL-covered noVNC files remain MPL-covered, and GPL code must not be copied
or linked into Homestead's restricted code. Independently executed tools and
services require a separate compatibility and distribution review.

See [docs/licensing.md](docs/licensing.md) for the scope and compatibility audit.
