# Security

How Homestead protects the cluster it runs, what the last audit (2.8.215)
found and fixed, and what remains by design. Read this before changing
sign-in, roles, anything that builds pods, or how pages put values into HTML.

## The model

Homestead's service account can do almost anything in the cluster - start
privileged pods, read Secrets, drain nodes - because its features need it.
So **a Homestead admin is a cluster admin**, and the lines that matter are:

| Who | May |
|---|---|
| Nobody signed in | The sign-in page, the app's own files, cached icons |
| viewer | Read everything that is not a secret |
| operator | Deploy, edit, start, stop and move workloads and VMs - without giving any of them the host (below) |
| admin | Everything else: users, hosts, storage, shares, imports, Helm, raw resources, linked clusters, host access |

Roles are enforced on the server for every route (`needed_role`), re-read from
the user store on every request (a demotion is immediate), and carried to a
linked cluster, which applies its own rules to them.

## What protects what

- **Passwords**: PBKDF2-HMAC-SHA256, 600,000 iterations, per-user salt; at
  least 10 characters. Sign-in is limited per address and per account, and
  every attempt is in the sign-in history (Events › Sign-ins).
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
  (`homestead_host_access.py`). An operator may use the hardware features an
  admin defined.
- **Files**: the volume browser refuses `..` and quotes every path it passes
  to its helper pod; nothing runs through a shell with request data.
- **The pod**: runs as a non-root user with a read-only root filesystem, no
  privilege escalation and every capability dropped.
- **Secrets on screen**: the MQTT password, SMB passwords and the UniFi key
  never leave the server; a Secret's values are shown to admins only.
- **Linked clusters**: every request between them is HMAC-signed with a shared
  key over its sender, time, a single-use nonce, method, path, person, role
  and body. See `docs/multi-cluster.md`.
- **Configuration backups** are sealed with a passphrase Homestead does not keep.

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
- **Hardware passthrough runs privileged.** An operator who uses a hardware
  feature an admin defined gets a privileged container of their chosen image.
  Define hardware features only where operators are trusted that far.
- **Plain HTTP on a LAN** carries the session cookie and linked-cluster
  traffic (signed, not encrypted; linking sends the shared key once). Use
  HTTPS - a Cloudflare Tunnel or an ingress - where the LAN is not trusted.
- **`unsafe-inline` scripts** are allowed because pages use inline handlers,
  so the CSP does not stop injected script: the escaping rules above do.
  Moving to delegated listeners would let the CSP drop it.
- **Setup on a fresh install** is open to anyone on the LAN until the first
  admin is created (never through the tunnel).
- **Linked-cluster nonces** are remembered per Homestead process: with
  several replicas, a signed request copied off the wire could be replayed to
  another replica within five minutes.
