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

import homestead_heartbeats as HEARTBEATS
import homestead_kube as KUBE
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
        renewal = _when(status["renewalTime"]) if status.get("renewalTime") else None
        overdue = renewal is not None and renewal < now - RENEWAL_GRACE
        is_ready = ready.get("status") == "True"
        note = ("Renewal is overdue: cert-manager has not renewed it" if overdue
                else "" if is_ready else f"Not ready: {str(ready.get('reason') or 'no reason given')[:80]}")
        rows.append(_row("certificate", ns, name, _when(status["notAfter"]), now,
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
            except (ValueError, UnicodeDecodeError):
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
                try:
                    latest = max((validity(der) for der in pem_certs(base64.b64decode(bundle).decode("ascii"))),
                                 key=lambda v: v["not_after"])
                except (ValueError, UnicodeDecodeError):
                    continue
                name = meta.get("name", "")
                if name not in found or latest["not_after"] < found[name][1]["not_after"]:
                    found[name] = (meta, latest)
    rows = []
    for name, (meta, v) in sorted(found.items()):
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
    report = {"checked_at": int(now), "items": rows, "counts": _summarise(rows, now), "errors": errors,
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
