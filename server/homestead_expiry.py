"""Certificates and keys that expire: what, and when, warned some days ahead.

Read only. Where it looks, and what it keeps:

* cert-manager Certificates, when cert-manager is installed: their own
  status (notAfter, renewalTime, Ready) comes first. Their Secrets are not
  opened.
* Other TLS Secrets (type kubernetes.io/tls). The API cannot return only
  tls.crt, so each page of the listing passes through memory here; only
  tls.crt is decoded, to its dates. tls.key is never indexed, kept, returned
  or logged, and the page is dropped as soon as it is read.
* Admission webhooks' CA bundles (public certificates).
* The Kubernetes API server's own certificate, from a TLS handshake.

A row keeps the kind, namespace, name, expiry, state and a note: nothing from
inside the certificate (no names, no key). Operators that rotate their own CA
every few days (KubeVirt, CDI) are recognised and stay quiet until something
has actually expired.

The scan runs on the leader only (the alert loop starts it), at most once per
SCAN_EVERY, bounded (MAX_SECRETS, HANDSHAKE_TIMEOUT), in a thread of its own;
the result is shared on the data volume for every replica's page. A source
that cannot be read is named in errors: the page says "could not read", never
"nothing expiring", and its alerts are kept rather than cleared.

Python's standard library has no certificate parser, so validity() walks the
DER itself, just far enough for the two validity dates. It reads cluster
input, so every length is checked against what is there and anything
malformed is a ValueError.
"""
import base64
import calendar
import datetime
import os
import socket
import ssl
import threading
import time
import urllib.parse

import homestead_api_keys as KEYS
import homestead_fleet as FLEET
import homestead_heartbeats as HEARTBEATS
import homestead_kube as KUBE
import homestead_leader as LEADER
import homestead_shared as SHARED

DAY = 86400
# 21 days, not 30: ACME clients (certbot, acme.sh, cert-manager's default for
# a 90-day certificate) renew at 30 days left, so 30 would alert on every
# normal renewal of a certificate copied in by hand.
WARN_DAYS = 21
CRITICAL_DAYS = 7
SHORT_LIVED = 30 * DAY        # nobody issues a certificate this short by hand: an operator rotates it
RENEWAL_GRACE = DAY           # cert-manager's renewalTime this far past is overdue
SCAN_EVERY = 3600
RETRY_EVERY = 300             # after a scan where a source could not be read
STALE_AFTER = 3 * SCAN_EVERY   # a scan older than this is not judged again for alerts
PAGE = 250
MAX_SECRETS = 2000
HANDSHAKE_TIMEOUT = 5
FILE = "expiry.json"
ROW_FIELDS = ("kind", "namespace", "name", "not_after", "days_left", "state", "note", "renews", "overdue")
STATES = ("expired", "critical", "warn", "unknown", "ok", "renews")
# The kinds each source gives, for keeping their alerts when it cannot be read.
SOURCE_KINDS = {"certificates": "certificate", "tls-secrets": "tls-secret",
                "webhook-cas": "webhook-ca", "api-server": "api-server"}
# Operators that rotate their own certificates: label or annotation keys in
# these API groups, or these owners. KubeVirt's CA lives 7 days, CDI's 2.
ROTATOR_GROUPS = ("kubevirt.io",)
ROTATOR_KEYS = ("auth.openshift.io/certificate-not-after",)   # library-go's rotation, which CDI uses
ROTATOR_MANAGERS = {"virt-operator", "cdi-operator"}
ROTATOR_OWNERS = {"KubeVirt", "CDI"}

PEM_BEGIN, PEM_END = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"


def _tlv(buf, i, end):
    """One DER element at i, inside buf[:end]: (tag, start of value, end of value)."""
    if i + 2 > end:
        raise ValueError("certificate ends early")
    tag, length = buf[i], buf[i + 1]
    i += 2
    if length & 0x80:
        n = length & 0x7F
        if not 1 <= n <= 4 or i + n > end:
            raise ValueError("certificate has a bad length")
        length = int.from_bytes(buf[i:i + n], "big")
        i += n
    if i + length > end:
        raise ValueError("certificate ends early")
    return tag, i, i + length


def _children(buf, start, end):
    out = []
    while start < end:
        tag, vs, ve = _tlv(buf, start, end)
        out.append((tag, vs, ve))
        start = ve
    return out


def _time(tag, raw):
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("certificate has a bad date") from None
    if tag == 0x17 and len(text) == 13:          # UTCTime YYMMDDHHMMSSZ
        if not text[:2].isdigit():
            raise ValueError("certificate has a bad date")
        year = int(text[:2])
        text = f"{1900 + year if year >= 50 else 2000 + year}{text[2:]}"
    elif not (tag == 0x18 and len(text) == 15):  # GeneralizedTime YYYYMMDDHHMMSSZ
        raise ValueError("certificate has a bad date")
    if not (text.endswith("Z") and text[:14].isdigit()):
        raise ValueError("certificate has a bad date")
    try:
        parsed = time.strptime(text[:14], "%Y%m%d%H%M%S")
    except ValueError:
        raise ValueError("certificate has a bad date") from None
    return float(calendar.timegm(parsed))


def validity(der):
    """{not_before, not_after} of one DER certificate, as epoch seconds."""
    _, cs, ce = _tlv(der, 0, len(der))
    parts = _children(der, cs, ce)
    if not parts or parts[0][0] != 0x30:
        raise ValueError("not a certificate")
    fields = _children(der, parts[0][1], parts[0][2])
    if fields and fields[0][0] == 0xA0:          # explicit version
        fields = fields[1:]
    # serial, signature, issuer, validity, subject, ...
    if len(fields) < 5 or fields[3][0] != 0x30:
        raise ValueError("not a certificate")
    dates = _children(der, fields[3][1], fields[3][2])
    if len(dates) != 2:
        raise ValueError("not a certificate")
    (tb, bs, be), (ta, as_, ae) = dates
    return {"not_before": _time(tb, der[bs:be]), "not_after": _time(ta, der[as_:ae])}


def pem_certs(text):
    """The DER of every certificate in a PEM bundle, leaf first."""
    out = []
    for chunk in text.split(PEM_BEGIN)[1:]:
        if PEM_END not in chunk:
            break
        body = "".join(chunk.split(PEM_END, 1)[0].split())
        try:
            out.append(base64.b64decode(body, validate=True))
        except ValueError:
            raise ValueError("certificate is not valid base64") from None
    if not out:
        raise ValueError("no certificate")
    return out


# --------------------------------------------------------------- states
def state_for(not_after, now, renews=False, overdue=False):
    left = not_after - now
    if left <= 0:
        return "expired"
    if renews:
        return "renews"
    if left <= CRITICAL_DAYS * DAY:
        return "critical"
    if left <= WARN_DAYS * DAY or overdue:
        return "warn"
    return "ok"


def _row(kind, namespace, name, not_after, now, renews=False, overdue=False, note=""):
    row = {"kind": kind, "namespace": namespace or "", "name": name or "", "not_after": None, "days_left": None,
           "state": "unknown", "note": note, "renews": bool(renews), "overdue": bool(overdue)}
    if not_after is not None:
        row.update(not_after=int(not_after), days_left=int((not_after - now) / DAY),
                   state=state_for(not_after, now, renews, overdue))
    return row


def self_rotating(meta, lifetime):
    """Whether an operator rotates this certificate itself: it lives under
    SHORT_LIVED, or its labels, annotations or owner say so."""
    if lifetime < SHORT_LIVED:
        return True
    labels, annotations = meta.get("labels") or {}, meta.get("annotations") or {}
    for key in (*labels, *annotations):
        group = key.split("/", 1)[0] if "/" in key else key
        if key in ROTATOR_KEYS or any(group == g or group.endswith("." + g) for g in ROTATOR_GROUPS):
            return True
    if labels.get("app.kubernetes.io/managed-by") in ROTATOR_MANAGERS:
        return True
    return any(o.get("kind") in ROTATOR_OWNERS for o in meta.get("ownerReferences") or [] if isinstance(o, dict))


def _rotated_note(lifetime):
    days = max(1, int(lifetime // DAY))
    return f"Its operator rotates it (it lives {days} day{'s' if days != 1 else ''})"


# -------------------------------------------------------------- sources
def _when(text):
    return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()


def certificates(now):
    """cert-manager Certificates, by their own status, and the Secrets they
    write ((namespace, name), left out of the TLS Secrets)."""
    rows, owned = [], set()
    for c in KUBE.items("/apis/cert-manager.io/v1/certificates", optional_group="cert-manager.io"):
        meta, spec, status = c.get("metadata") or {}, c.get("spec") or {}, c.get("status") or {}
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        if spec.get("secretName"):
            owned.add((ns, spec["secretName"]))
        ready = next((x for x in status.get("conditions") or [] if isinstance(x, dict) and x.get("type") == "Ready"), {})
        if not status.get("notAfter"):
            rows.append(_row("certificate", ns, name, None, now, note="Not issued yet"))
            continue
        try:
            renewal = _when(status["renewalTime"]) if status.get("renewalTime") else None
            not_after = _when(status["notAfter"])
        except (TypeError, ValueError):
            rows.append(_row("certificate", ns, name, None, now, note="Homestead could not read its dates"))
            continue
        overdue = renewal is not None and renewal < now - RENEWAL_GRACE
        is_ready = ready.get("status") == "True"
        note = ("Renewal is overdue: cert-manager has not renewed it" if overdue
                else "" if is_ready else f"Not ready: {str(ready.get('reason') or 'no reason given')[:80]}")
        rows.append(_row("certificate", ns, name, not_after, now,
                         renews=is_ready and renewal is not None and not overdue, overdue=overdue,
                         note=note or "cert-manager renews it"))
    return rows, owned


def _secret_pages():
    """TLS Secrets a page at a time, at most MAX_SECRETS in all: (page, whether
    Kubernetes has more than was read)."""
    base = "/api/v1/secrets?fieldSelector=" + urllib.parse.quote("type=kubernetes.io/tls", safe="") + f"&limit={PAGE}"
    token, read = "", 0
    while True:
        page = KUBE.get(base + (f"&continue={urllib.parse.quote(token, safe='')}" if token else ""))
        rows = page.get("items") if isinstance(page, dict) else None
        if not isinstance(rows, list):
            raise ValueError("Kubernetes returned no list of TLS Secrets")
        token = str((page.get("metadata") or {}).get("continue") or "")
        read += len(rows)
        yield rows[:max(0, MAX_SECRETS - (read - len(rows)))], bool(token)
        rows.clear()        # the Secrets' values go no further
        page.clear()
        if not token or read >= MAX_SECRETS:
            return


def tls_secrets(now, owned):
    """Every TLS Secret cert-manager did not write: (rows, partial). Only
    tls.crt is decoded; nothing else of a Secret leaves this function."""
    rows, partial = [], False
    for page, more in _secret_pages():
        for s in page:
            s = s if isinstance(s, dict) else {}
            meta = s.get("metadata") or {}
            ns, name = meta.get("namespace", ""), meta.get("name", "")
            if (ns, name) in owned or "cert-manager.io/certificate-name" in (meta.get("annotations") or {}):
                continue
            crt = (s.get("data") or {}).get("tls.crt") or ""
            try:
                v = validity(pem_certs(base64.b64decode(crt).decode("ascii"))[0])
            except (TypeError, ValueError):     # binascii.Error and UnicodeDecodeError are ValueErrors
                rows.append(_row("tls-secret", ns, name, None, now,
                                 note="Homestead could not read the certificate in tls.crt"))
                continue
            lifetime = v["not_after"] - v["not_before"]
            renews = self_rotating(meta, lifetime)
            rows.append(_row("tls-secret", ns, name, v["not_after"], now, renews=renews,
                             note=_rotated_note(lifetime) if renews else ""))
        partial = more
    return rows, partial


def webhook_cas(now):
    """Each admission webhook configuration's CA: the soonest of its bundles,
    each bundle counted by its latest certificate (an operator rotating its
    CA keeps the old one beside the new for a while)."""
    found = {}
    for kind in ("validatingwebhookconfigurations", "mutatingwebhookconfigurations"):
        for cfg in KUBE.items(f"/apis/admissionregistration.k8s.io/v1/{kind}"):
            meta = cfg.get("metadata") or {}
            for hook in cfg.get("webhooks") or []:
                bundle = (hook.get("clientConfig") or {}).get("caBundle") if isinstance(hook, dict) else None
                if not bundle:
                    continue
                name = meta.get("name", "")
                try:
                    latest = max((validity(der) for der in pem_certs(base64.b64decode(bundle).decode("ascii"))),
                                 key=lambda v: v["not_after"])
                except (TypeError, ValueError):
                    found.setdefault(name, (meta, None))
                    continue
                if name not in found or found[name][1] is None or latest["not_after"] < found[name][1]["not_after"]:
                    found[name] = (meta, latest)
    rows = []
    for name, (meta, v) in sorted(found.items()):
        if v is None:
            rows.append(_row("webhook-ca", "", name, None, now, note="Homestead could not read its CA bundle"))
            continue
        lifetime = v["not_after"] - v["not_before"]
        renews = self_rotating(meta, lifetime)
        rows.append(_row("webhook-ca", "", name, v["not_after"], now, renews=renews,
                         note=_rotated_note(lifetime) if renews else ""))
    return rows


def _peer_cert(host, port):
    """The DER certificate a TLS server presents. Only its dates are read,
    so it is not verified here."""
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=HANDSHAKE_TIMEOUT) as sock, \
            ctx.wrap_socket(sock, server_hostname=host) as tls:
        return tls.getpeercert(binary_form=True)


def api_server(now):
    host = os.environ.get("KUBERNETES_SERVICE_HOST") or "kubernetes.default.svc"
    port = int(os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS") or os.environ.get("KUBERNETES_SERVICE_PORT") or 443)
    v = validity(_peer_cert(host, port))
    return [_row("api-server", "", "Kubernetes API server", v["not_after"], now,
                 note="k3s and RKE2 renew it when the server restarts close to its expiry")]


# ----------------------------------------------------------------- scan
def _path():
    return os.path.join(SHARED.DIR, FILE)


def _say(error):
    return (str(error) or type(error).__name__)[:200]


def _summarise(rows, now):
    order = {s: i for i, s in enumerate(STATES)}
    rows.sort(key=lambda r: (order.get(r["state"], 9), r["not_after"] if r["not_after"] is not None else 1 << 40,
                             r["namespace"], r["name"]))
    return {s: sum(r["state"] == s for r in rows) for s in STATES}


def scan(now=None):
    """Every source, read once, saved for every replica's page."""
    now = time.time() if now is None else now
    rows, errors, owned, partial = [], {}, set(), False
    try:
        certs, owned = certificates(now)
        rows += certs
    except Exception as error:
        errors["certificates"] = _say(error)
    try:
        secrets, partial = tls_secrets(now, owned)
        rows += secrets
    except Exception as error:
        errors["tls-secrets"] = _say(error)
    for source, read in (("webhook-cas", webhook_cas), ("api-server", api_server)):
        try:
            rows += read(now)
        except Exception as error:
            errors[source] = _say(error)
    try:
        before = (load() or {}).get("failed_scans") or 0
    except SHARED.Unreadable:
        before = 0
    report = {"checked_at": int(now), "items": rows, "counts": _summarise(rows, now), "errors": errors,
              "failed_scans": before + 1 if errors else 0,
              "partial": partial, "max_secrets": MAX_SECRETS, "warn_days": WARN_DAYS, "critical_days": CRITICAL_DAYS}
    SHARED.write_json(_path(), report)
    return report


def load():
    """The last scan, or None before the first."""
    rep = SHARED.read_json(_path(), {})
    return rep if rep.get("checked_at") else None


def refresh(rep, now=None):
    """The saved scan, its days and states counted again for now."""
    now = time.time() if now is None else now
    rows = []
    for r in rep.get("items") or []:
        row = {k: r.get(k) for k in ROW_FIELDS}
        if row["not_after"] is not None:
            row.update(days_left=int((row["not_after"] - now) / DAY),
                       state=state_for(row["not_after"], now, row["renews"], row["overdue"]))
        rows.append(row)
    return {**rep, "items": rows, "counts": _summarise(rows, now), "warn_days": WARN_DAYS, "critical_days": CRITICAL_DAYS}


_scan = {"running": False, "tried": 0}
_scan_lock = threading.Lock()


SOURCE_WORDS = {"certificates": "cert-manager Certificates", "tls-secrets": "TLS Secrets",
                "webhook-cas": "webhook CAs", "api-server": "the API server's certificate"}


def _beat(rep):
    """Settings' line for the scan: failing while a source could not be read."""
    failed = sorted((rep or {}).get("errors") or {})
    if failed:
        HEARTBEATS.beat("expiry", SCAN_EVERY, "Could not read " + ", ".join(SOURCE_WORDS.get(s, s) for s in failed),
                        leader_only=True)
    else:
        HEARTBEATS.beat("expiry", SCAN_EVERY, leader_only=True)


def _every(rep):
    """How long a scan holds: SCAN_EVERY, or after one where a source could
    not be read RETRY_EVERY, doubling with each such scan in a row up to
    SCAN_EVERY. A partial Secret list is not retried sooner: over the cap
    every scan is partial."""
    failed = rep.get("failed_scans") or (1 if rep.get("errors") else 0)
    return min(SCAN_EVERY, RETRY_EVERY * 2 ** (failed - 1)) if failed else SCAN_EVERY


def start_scan_if_due(now=None, wait=False):
    """Start a scan in its own thread when the last has held its time
    (_every), unless one is running or one was tried RETRY_EVERY ago. The
    alert loop calls it; only the leader scans. True when one started."""
    if not LEADER.is_leader():
        return False
    now = time.time() if now is None else now
    try:
        last = load()
    except SHARED.Unreadable:
        last = None
    with _scan_lock:
        if (_scan["running"] or now - _scan["tried"] < RETRY_EVERY
                or (last and now - last["checked_at"] < _every(last))):
            fresh = not _scan["running"] and last is not None and last["checked_at"] >= int(_scan["tried"])
        else:
            fresh = None
            _scan.update(running=True, tried=now)
    if fresh is not None:
        if fresh:
            _beat(last)         # the leader's last scan still holds: say how it went
        return False

    def work():
        try:
            _beat(scan())
        except Exception as error:
            HEARTBEATS.beat("expiry", SCAN_EVERY, _say(error), leader_only=True)
        finally:
            with _scan_lock:
                _scan["running"] = False

    thread = threading.Thread(target=work, name="expiry-scan", daemon=True)
    thread.start()
    if wait:
        thread.join()
    return True


# --------------------------------------------------------------- alerts
KIND_WORDS = {"certificate": "Certificate", "tls-secret": "TLS Secret", "webhook-ca": "Webhook CA",
              "api-server": "Kubernetes API server certificate"}


def nearness(days):
    """Grows as it gets worse, so an acknowledged alert comes back when it
    does: 0 inside the warning window, 1 inside a week, 2 inside a day, 3 expired."""
    return 3 if days < 0 else 2 if days < 1 else 1 if days <= CRITICAL_DAYS else 0


def _what(row):
    if row["kind"] == "api-server":
        return "The Kubernetes API server's certificate"
    where = f"{row['namespace']}/{row['name']}" if row["namespace"] else row["name"]
    return f"{KIND_WORDS.get(row['kind'], row['kind'])} {where}"


def _in_days(days):
    days = max(0, int(days))
    return "today" if days == 0 else f"in {days} day{'s' if days != 1 else ''}"


def _key_facts(now):
    """API keys made to last over SHORT_LIVED that expire within WARN_DAYS: one
    alert, with no key's name or owner, since every account sees alerts and
    the key list is for admins."""
    soon = [k for k in KEYS.list_keys()
            if now < k["expires"] <= now + WARN_DAYS * DAY and k["expires"] - k["created"] > SHORT_LIVED]
    if not soon:
        return []
    days = (min(k["expires"] for k in soon) - now) / DAY
    count = len(soon)
    words = "API key expires" if count == 1 else "API keys expire"
    critical = days <= CRITICAL_DAYS
    return [{"key": "expiry:api-keys", "category": "outage" if critical else "degraded",
             "severity": "critical" if critical else "degraded",
             "title": f"{count} Homestead {words} within {WARN_DAYS} days",
             "body": f"The first stops working {_in_days(days)}. An admin sees which under Settings, Access.",
             "resolved": f"No Homestead API key expires within {WARN_DAYS} days",
             "href": "/settings?tab=access", "signals": {"keys": count, "nearness": nearness(days)}}]


def alert_facts(now=None):
    """Alerts for what expires within WARN_DAYS (critical within
    CRITICAL_DAYS), and for what renews itself only once it has expired.
    A source that could not be read keeps its alerts (unknown_prefix), and
    so does everything when there is no scan yet or the last is stale: old
    dates are not judged again (a 2-day CA would cross its old notAfter).
    A partial Secret list holds nothing: over the cap every scan is partial,
    and holding would keep a renewed Secret's alert for ever."""
    now = time.time() if now is None else now
    start_scan_if_due(now)
    rep = load()
    if rep is None or now - rep["checked_at"] > STALE_AFTER:
        return [{"unknown_prefix": f"expiry:{kind}:"} for kind in SOURCE_KINDS.values()] + _key_facts(now)
    rep = refresh(rep, now)
    facts = [{"unknown_prefix": f"expiry:{SOURCE_KINDS[s]}:"} for s in sorted(rep.get("errors") or {}) if s in SOURCE_KINDS]
    for r in rep["items"]:
        if r["state"] not in ("expired", "critical", "warn"):
            continue
        days = (r["not_after"] - now) / DAY
        critical = r["state"] != "warn"
        what = _what(r)
        if r["state"] == "expired":
            title = f"{what} has expired"
        elif r["overdue"] and days > WARN_DAYS:
            title = f"{what} has not been renewed"
        else:
            title = f"{what} expires {_in_days(days)}"
        facts.append({"key": f"expiry:{r['kind']}:{r['namespace']}/{r['name']}",
                      "category": "outage" if critical else "degraded",
                      "severity": "critical" if critical else "degraded", "title": title,
                      "body": r["note"] or "Nothing renews it: replace it before it expires.",
                      "resolved": f"{what} is renewed, or no longer expires soon",
                      "href": "/networking?tab=expiry", "signals": {"nearness": nearness(days)}})
    return facts + _key_facts(now)


# --------------------------------------------------------------- routes
def report(now=None):
    """The last scan for the page, its days counted for now; checked_at is
    None before the first."""
    rep = load()
    if rep is None:
        return {"checked_at": None, "items": [], "counts": {s: 0 for s in STATES}, "errors": {}, "partial": False, "stale": False,
                "warn_days": WARN_DAYS, "critical_days": CRITICAL_DAYS}
    now = time.time() if now is None else now
    return {**refresh(rep, now), "stale": now - rep["checked_at"] > STALE_AFTER}


def keys(now=None):
    """Homestead's API keys (and the linked-cluster key): admin only, as the
    key list is."""
    now = time.time() if now is None else now
    rows = []
    for k in KEYS.list_keys():
        if k["expires"] < now - SHORT_LIVED:
            continue            # long gone: the key list still has it
        rows.append({**_row("api-key", "", k["name"], k["expires"], now), "owner": k["owner"]})
    if FLEET.linked():
        rows.append({**_row("fleet-key", "", "Linked-cluster key", None, now,
                            note="It has no expiry; it changes when a cluster is unlinked"), "state": "no-expiry"})
    return {"items": rows, "warn_days": WARN_DAYS, "critical_days": CRITICAL_DAYS}


ROUTES = {
    ("GET", "/api/expiry"): ("viewer", lambda request: report()),
    ("GET", "/api/expiry/keys"): ("admin", lambda request: keys()),
}
