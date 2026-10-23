# Security

How Homestead protects the cluster it runs, how changes are checked before
they ship, what the audits (2.8.215, and the installer in 2.8.304) found and
fixed, and what remains by design. Read this before changing sign-in, roles,
anything that builds pods, the installer, or how pages put values into HTML.

## The model

Homestead's service account can do almost anything in the cluster - start
privileged pods, read Secrets, drain nodes - because its features need it.
So **a Homestead admin is a cluster admin**, and the lines that matter are:

| Who | May |
|---|---|
| Nobody signed in | The sign-in page, the app's own files, cached icons |
| viewer | Read everything that is not a secret |
| operator | Deploy, edit, start, stop and move ordinary workloads and VMs; management, system and privileged workloads require admin |
| admin | Everything else: users, hosts, storage, shares, imports, Helm, raw resources, linked clusters, host access |

Roles are enforced on the server for every route (`needed_role`), re-read from
the user store on every request (a demotion is immediate), and carried to a
linked cluster, which applies its own rules to them.

## What protects what

- **Passwords**: PBKDF2-HMAC-SHA256, 600,000 iterations, per-user salt; at
  least 10 characters. Sign-in is limited per address and per account, and
  every attempt is in the sign-in history (Events › Sign-ins).
- **First administrator**: until an account exists, setup asks for a one-time
  setup code (#737), so whoever reaches the address first on install day
  cannot take the cluster. The code is in the Secret `homestead-setup` in
  Homestead's own namespace, readable only with cluster access; the installer
  prints it and the Helm notes give the command. Homestead keeps only a
  salted hash of it on the data volume (PBKDF2-HMAC-SHA256, a fresh random
  salt, the password iterations) and never logs the code, only where it is.
  Wrong codes count against the sign-in limits (per address, and 20 per five
  minutes for all setup attempts together), and the comparison takes constant
  time. The format is the pairing code's from #602: 10 characters of
  Crockford base32 (50 bits). A code set ahead (`HS_SETUP_CODE`, Helm
  `setupCode`) is the same Secret made in advance. Once the first account
  exists the Secret is deleted and the hash cleared; setup is refused from then
  on whatever code is sent, and a code that was used once never opens setup
  again. Setup is still refused through a Cloudflare tunnel. The shared limit
  is a trade-off: anyone on the LAN can hold setup off for five minutes at a
  time by sending wrong codes; it never lets them in.
- **Sessions**: HMAC-signed tokens in an `HttpOnly`, `SameSite=Strict` cookie
  (`Secure` behind TLS), with an idle and an absolute limit; a password
  change or "sign out everywhere" revokes every session at once.
- **Cross-site requests**: every write needs the `X-Homestead-Auth` header,
  which a form on another site cannot send; no GET changes anything.
  Consoles check `Origin` against `Host`.
- **Headers**: a Content-Security-Policy allowing scripts only from Homestead
  itself, no framing, `nosniff`, a same-origin referrer, HSTS behind TLS.
- **Cloudflare Access**, when set up: every request through the tunnel must
  carry an Access token signed by the team (RS256, issuer, audience and expiry
  checked). Cloudflare's client-address header is believed only then.
- **Kubernetes API paths**: a name from a request cannot contain `.`/`..`
  segments, encoded slashes or whitespace (`api_path` in `server.py`), so it
  cannot reach an object other than the one it names.
- **Host access**: privileged mode, added capabilities, the host's network,
  processes or IPC, and host folders are for admins to give
  (`homestead_host_access.py`). Editing or opening a console in an existing
  privileged workload also requires an admin.
- **Bulk jobs**: setting reservations or priority tiers for many apps checks
  each app as the person who started it, as Edit does - an operator cannot
  reach a management, service-account or host-privileged workload through
  them.
- **Scheduling**: Homestead may create PriorityClasses (Kubernetes cannot
  limit creating them by name) and reads only its own two. A critical app may
  evict any pod with no tier, VMs included, when room is short; operators can
  set it. Homestead joins the critical tier itself once any app is in it, so
  no app can evict it.
- **Files**: the volume browser refuses `..` and quotes every path it passes
  to its helper pod; nothing runs through a shell with request data.
- **The pod**: runs as a non-root user with a read-only root filesystem, no
  privilege escalation and every capability dropped.
- **Secrets on screen**: the MQTT password, SMB passwords and the UniFi key
  never leave the server; a Secret's values are shown to admins only.
  Helm values/notes and VM cloud-init are also admin-only. Operators editing
  other VM hardware preserve cloud-init without receiving its contents.
- **Certificate expiry**: to date TLS Secrets, the leader lists them (the API
  cannot leave out `tls.key`), decodes only `tls.crt` to its two dates and
  drops each page at once. It keeps and shows kind, namespace, name, expiry and
  state only; never a key, and nothing from inside the certificate. API keys
  and their owners are listed there for admins only.
- **Linked clusters**: every request between them is HMAC-signed with a shared
  key over its sender, time, a single-use nonce, method, path, person, role
  and body. See `docs/multi-cluster.md`.
- **Configuration backups** are sealed with a passphrase Homestead does not keep.

## Session and request boundaries

Sessions use the version-2 token format and bind to an account identity that is
never inherited by a recreated username. Existing cookies from older releases
require a new sign-in after upgrading. Password changes and revoke-all invalidate
the current account generation. A login must still refer to the identity whose
password was verified if the account changes concurrently.

Auth mutations require `X-Homestead-Auth: 1` and `Content-Type: application/json`,
including login and first-admin setup, which also needs the setup code.
Browser origins are checked. Every API
route has an explicit declaration in `homestead_route_policy.py`; undeclared
routes are denied. Public static assets remain accessible for installation.

Password attempts are reserved atomically before hashing, with per-address and
per-account limits persisted in the account Secret. Replicas and restarts share
those limits. Password hashing has a separate concurrency bound. During read-only
data-move recovery, sign-in cannot write the Secret: its atomic rate counters are
process-local, while the hash concurrency bound still applies.

Ordinary mutations and open consoles require a fresh credential-store read.
Read-only requests may use cached authorization for at most 30 seconds from the
last successful store read; an outage beyond that fails closed. Core HTTP handlers
bound concurrent connections and enforce header, body and idle deadlines.

Container, host and VM consoles close on local account revocation, recheck
credentials every five seconds, and have a one-hour maximum lifetime. A linked
console is monitored where the user signed in; the target also checks continued
fleet membership and key validity. Closing a console does not guarantee that a
command already started inside the guest or container has terminated.

Operators cannot exec or replace code in management/system workloads, helper
pods, nondefault service accounts or workloads with host privileges. Checks apply
to the original identity and the proposed manifest, including sidecars. A
background job an operator starts - rebalancing containers - is checked for that
operator when it is reviewed, and each change it makes is written as them, since
the job itself runs with no request behind it. New ordinary deployments do not
mount service-account tokens. Custom installations must also avoid granting
powerful Kubernetes rights to the default service account.
API control keys are limited to the same ordinary workload targets; their scopes
never select the internal authorization bypass.

**AI agents (MCP)** are off until an administrator turns them on, and `/mcp`
answers `404` while off. Only a key made with "Allow AI agents (MCP)" works
there, never a browser session, and a browser Origin must be Homestead's own. A
key acts as an operator at most, even when an administrator made it, and sees
only the tools its scopes allow. Deletes, snapshot reverts, restores, backup
deletes, storage class changes and changes that need an administrator do not
run: they become agent requests that a person approves or denies on the
`/agents` page. On approval Homestead plans again as the approver and runs only
if the plan is unchanged; requests expire after 30 minutes, with at most 10
pending per key. Calls are limited per key per minute (120 reads, 20 changes, 5
destructive), requests to 256 KiB with no batches. Logs and other app-written
text return marked as untrusted data, control codes stripped. Users, keys,
settings, shells and exec, Secrets, the file manager, host power and updates,
Homestead itself and linked clusters are never offered. Every key call is
audited for 30 days with secrets and environment values hidden.

Remote icons connect only to resolved public addresses, with hostname-verified
TLS and the same policy on redirects. Downloads have size and time bounds.
Compose/YAML inputs have depth and expanded-alias budgets. Volume saves require
the revision that was read, use a unique staged file and per-target lock, verify
the content, preserve mode/ownership, and commit by rename with a backup. A crash
can leave a `.homestead-lock` directory; inspect the file and backup before an
administrator clears that lock.

The installer stages downloads in a private temporary directory and defaults
to an immutable release ref; `--ref` remains an explicit override. A cluster
join token is never written to a log or shown on screen: command lines are
logged and printed with it replaced by `[token hidden]`, the installer and
node doctor logs (`/var/log/homestead-install.log`,
`/var/log/homestead-doctor.log`) are readable by root only, and so is RKE2's
`/etc/rancher/rke2/config.yaml`, which holds the token. The token is still an
argument to the bootstrap script while it runs, so it is visible to other
users of the machine through the process list during installation. Node probes
use a separate `homestead-smart-key` Secret, derived for that helper only, rather
than mounting user password hashes or the session-signing key. Homestead migrates
an existing probe on reconciliation and provisions the key for manual/chart
installations. It must be running for the SMART sidecar's credential to appear.

Release publication is gated on fresh candidate-image and built-in base/helper
image scans for amd64 and arm64;
weekly scans and dependency update checks cover newly published advisories.
`scripts/security_scan.sh` writes the reports without pushing images. Maintainers
must address scan failures, rebuild affected releases, review scanner/version
updates and keep vendored browser libraries current. A passing scan covers known
advisories in its inventory, not every possible vulnerability.
The browser inventory and versions are in `THIRD_PARTY_NOTICES.md`. Maintainers
review it when updating vendored assets. Operators must separately inventory and
scan configured image overrides, optional third-party add-ons and user workloads;
the release gate cannot determine what a particular cluster has installed.

Registry credentials are sent only over HTTPS to the registry's own auth realm,
Docker's official auth realm, or an explicitly configured `REGISTRY_AUTH_HOSTS`
authority. Credential-bearing auth redirects are rejected. Backups accept only
the supported bounded scrypt parameters and cap decompressed contents; existing
version-1 encrypted backups remain readable.

## Writing pages safely

Pages are HTML strings. Three rules keep what they show from running:

1. **Text and attributes**: `esc(value)`.
2. **A value in an inline handler**: `jsq(value)` - `onclick="fn(${jsq(name)})"`.
   Never `'${esc(name)}'`: the browser turns `&#39;` back into `'` before the
   handler runs, so an escaped quote still ends the string. Handler text given
   to `UI.button()` uses `jsArg(value)`, since `UI.button` escapes it once.
   `tests/handler-escaping.test.js` refuses the old form.
3. **A link from data** (an app's page, release notes, a portal tile):
   `href="${safeHref(url)}"` - only http(s) and same-site paths; a
   `javascript:` URL becomes `#`.

## How changes are checked

Every pull request and every push to `dev` and `main` runs CI
(`.github/workflows/ci.yml`). A first job reads what the change touches, and
the slow checks run only when their part of the code changed; a change to the
docs alone runs none of them, and a release commit that only moves version
numbers counts as no change.

| Check | Runs when | What it holds |
|---|---|---|
| Backend and frontend tests | Any code changes | `python -m unittest` over `tests/`, `node --test`, a syntax check of every script, and the image built and run read-only with every capability dropped for the data-copy and file-permission tests |
| Page and dialog checks | `web/` or the checks change | Every page and dialog opened against the demo data at desktop and phone widths, in light and dark, and held to `docs/design.md`: nothing wider than the screen, no text under 10px, nothing that stops opening. They run in eight groups of about two minutes in parallel (`scripts/ci_ui_checks.sh`) |
| Installer screens | The installer or host console changes | The installer's and node doctor's menus driven in tmux as a person would |
| Data moves on k3s | The self-data code changes | Two real data moves on a throwaway k3s cluster, with retained volumes and helper cleanup |
| Source copy | Any code changes | SSH trust and interrupted copies rehearsed in an isolated container with no network |

A release (`.github/workflows/release.yml`) waits for that CI run on its
commit, or runs the tests itself when there was none, and publishes nothing
until the candidate image and its base and helper images pass the security
scan for amd64 and arm64 (`scripts/security_scan.sh`). Releases go to the dev
channel first (`2.8.N-dev.1`) and reach `main` as the same code.

## The 2.8.215 audit

Found and fixed:

| Finding | Risk | Fix |
|---|---|---|
| Values in about 280 inline handlers were HTML-escaped inside a JavaScript string, which does not keep them in the string | Stored script injection by anyone who could name something shown to an admin: a VIP label, a share, a linked cluster's name, a UniFi device name from the network | `jsq()`/`jsArg()` at every handler; a test guards it |
| Links from outside data could be `javascript:` URLs | Script on click from an App Store feed or release data | `safeHref()` |
| Names from requests went into Kubernetes API paths unchecked | A `../` name could address another object as Homestead, e.g. read a Secret through a viewer route | `api_path()` in `kget`/`ksend` |
| Cloudflare's client-address header was believed from anyone | Dodging the per-address sign-in limit; false addresses in the sign-in history | Believed only with Access set up |
| Operators could deploy privileged pods, add capabilities, use the host network or mount host folders | An operator became root on a node, and so cluster admin | Admin-only (`homestead_host_access.py`) |
| A console on a linked cluster was relayed with the browser's `Origin` | Consoles through a link were refused (a fault, not a hole) | The entry checks `Origin`, the member sees its own |

Remaining, by design or for later:

- **Admin is cluster admin.** Keep admin accounts few, with strong passwords.
- **Hardware passthrough runs privileged.** Deploying or editing those
  workloads requires an administrator, including features an admin defined.
- **Plain HTTP on a LAN** carries the session cookie and linked-cluster
  traffic (signed, not encrypted; linking sends the shared key once). Use
  HTTPS - a Cloudflare Tunnel or an ingress - where the LAN is not trusted.
- **`unsafe-inline` scripts** are allowed because pages use inline handlers,
  so the CSP does not stop injected script: the escaping rules above do.
  Moving to delegated listeners would let the CSP drop it.
- **Setup on a fresh install** is open to anyone on the LAN until the first
  admin is created (never through the tunnel).
- **Linked-cluster nonces** are claimed in Kubernetes with resource-version
  conflict checks, so replicas share the replay boundary. A replay-store outage
  denies signed requests rather than weakening that protection.

## The 2.8.304 installer review

A full review of `scripts/install.sh`, the installer and node doctor. Each
finding was reproduced before it was fixed, and each fix has a test in
`tests/test_install_script.py`.

| Finding | Risk | Fix |
|---|---|---|
| Joining with text prompts or `--dry-run` wrote the cluster token into `/var/log/homestead-doctor.log`, readable by every local user; RKE2's `config.yaml` with the token had default permissions | Anyone with a login on the node could read the token and add a machine of their own to the cluster | The token is replaced by `[token hidden]` in every logged or shown command line; the logs and RKE2's config are root-only |
| Cancel or Back on a question ended only the subshell that asked it | The installation went on with an empty answer: Back on *Node IP Address* led to a summary with no node address | Every question stops the installation when cancelled; its button says Cancel |
| Ctrl+C during a progress bar closed the installer but not the step it was running | An installation kept going unseen after it seemed to be cancelled | The interrupt stops the step and everything it started, and says so |
| With the API down, a restore could not count the server nodes and dropped its warning | A restore on one server of several, without stopping the others first, splits the cluster | An unknown count gives the warning anyway |
| The clock fix enabled `chronyd`, which on Ubuntu is an alias systemctl refuses | The fix failed on Ubuntu | It uses the unit's real name |

Also fixed: a typo in a text menu, the VIP or the Longhorn size asks again
rather than ending the installation; *Apply all safe fixes* runs each fix once
(it could restart k3s several times); CoreDNS is checked on RKE2 under its own
name.

Remaining: the token is an argument to the bootstrap script while it runs, so
another user on the node can see it in the process list during installation.
