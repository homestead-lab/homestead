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
- **Permissions stay narrow.** A new permission goes in `deploy/deploy.yaml`
  with a comment saying what uses it; limit it by `resourceNames` where Kubernetes
  allows.

## Commits and pull requests

- One topic per pull request, with the tests passing.
- A commit message's first line says what changed for a person using Homestead;
  the body says why, and anything a reviewer should look at closely.
- Screenshots help for anything visual, at desktop and phone width.

## Releases

Maintainers release from `main`:

```bash
python scripts/bump_version.py 2.8.80
git commit -am "..."
git tag v2.8.80 && git push origin main v2.8.80
```

The tag runs the tests and publishes a multi-architecture image to GHCR.

## Security

Please report a vulnerability privately through
[GitHub security advisories](https://github.com/wjcloudy/homestead/security/advisories/new)
rather than in an issue.

## Licence

Homestead is MIT licensed. By contributing you agree that your contribution is
released under the same [licence](LICENSE). Third-party code you add must be
compatible with it and listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
