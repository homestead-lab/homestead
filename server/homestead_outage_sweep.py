"""Outage webhook Secrets whose app or VM is gone are deleted (#931 follow-up).

#942 keeps an outage webhook's address in a Secret of Homestead's own per app
or VM (homestead_outage_secret), with no ownerReference: garbage collection
would delete it when a move or a rename deletes and recreates the app or VM.
Deleting the app or VM in Homestead deletes its Secret too; one deleted
outside Homestead (kubectl, Flux, Helm) leaves it behind, token and all. So
the leader sweeps them, after outage_tick's other work, and only when it is
sure:

- Only a Secret Homestead keeps for an outage webhook: labelled for an app or
  a VM (its LABEL) and managed by Homestead, whose owner annotation names an
  app or VM of that kind, and whose name is the one Homestead gives that
  owner's Secret. Any other Secret is never touched.
- Only while its owner is missing from lists really read: every Deployment
  and every VM (homestead_kube.items, which raises on a failed or cut-short
  list; a cluster without KubeVirt has no VMs), and the jobs. A round whose
  lists cannot be read sweeps nothing and starts every clock again (#620).
- Only once the owner has been missing in two rounds at least GRACE apart,
  with every round in between read too (none more than 2 x EVERY apart),
  and the same Secret (uid) each time. A move or a recreate shows the owner
  again within GRACE, and that clock starts over the next time it is missing.
- Never while an app or a VM of that namespace and name has a job not over
  (queued, running, cancelling, or failed and waiting to be recovered), nor
  while a present app or VM names the Secret on its annotation (a rename,
  until the leader moves the address under the new name).
- The delete names the Secret's uid and resourceVersion as read: one saved
  again meanwhile is not deleted.
- At most MAX_DELETES a round, one round every EVERY seconds. Each delete is
  logged and written to the audit log (homestead_audit), with no address.

What it remembers is this copy's own. A new leader, or a copy that leads
again after a gap, starts every clock again: a sweep comes later, never sooner.
"""
import datetime
import os
import time
import urllib.parse

import homestead_audit as AUDIT
import homestead_kube as KUBE
import homestead_names as NAMES
import homestead_outage as OUTAGE
import homestead_outage_secret as SECRET

EVERY = 300
GRACE = 3600
MAX_DELETES = 20
KINDS = ("app", "vm")
DEPLOYMENTS = "/apis/apps/v1/deployments"
VMS = "/apis/kubevirt.io/v1/virtualmachines"
SECRETS = "/api/v1/secrets?labelSelector=" + urllib.parse.quote(f"{SECRET.LABEL} in (app,vm)", safe="")
MANAGED = ("app.kubernetes.io/managed-by", "homestead")
# (ns, Secret name) -> {"uid", "first"}: when this copy first saw its owner missing.
_seen = {}
_next = {}


def _owner(secret):
    """(kind, owner name) for a Secret Homestead keeps for an outage webhook
    of that kind, written for that owner; None for any other Secret."""
    meta = (secret or {}).get("metadata") or {}
    labels = meta.get("labels") or {}
    kind = labels.get(SECRET.LABEL)
    if kind not in KINDS or labels.get(MANAGED[0]) != MANAGED[1]:
        return None
    owner_kind, _, name = SECRET.owner_of(secret).partition("/")
    if owner_kind != kind or not name or meta.get("name") != SECRET.secret_name(kind, name):
        return None
    return kind, name


def _present():
    """{(kind, ns, name)} of every app and VM, and {(ns, Secret name)} their
    annotations name; raises when a list cannot be read whole."""
    kget = OUTAGE._bound["kget"]
    found = [("app", d) for d in KUBE.items(DEPLOYMENTS, read=kget)] + \
            [("vm", v) for v in KUBE.items(VMS, "kubevirt.io", read=kget)]
    there, named = set(), set()
    for kind, obj in found:
        meta = obj.get("metadata") or {}
        there.add((kind, meta.get("namespace"), meta.get("name")))
        ref = (OUTAGE.stored(meta.get("annotations") or {}, NAMES) or {}).get("secret")
        if ref:
            named.add((meta.get("namespace"), ref))
    return there, named


def _audit(ns, name, owner, missing_for):
    at = datetime.datetime.fromtimestamp(time.time(), datetime.timezone.utc)
    AUDIT.emit({"v": 1, "at": at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                # No "target": the Timeline links one to the app of that name, and a
                # Secret is no app. Its user reads "homestead DELETE ..." there.
                "replica": os.environ.get("HOSTNAME", ""), "via": "leader", "user": "homestead",
                "event": "outage-secret-swept", "method": "DELETE", "path": f"/api/v1/namespaces/{ns}/secrets/{name}",
                "for": owner,
                "detail": f"its {owner.replace('/', ' ', 1)} was missing for {round(missing_for / 60)} minutes"})


def sweep(busy, now=None):
    """One round, on the leader (outage_tick). busy(ns, name) says whether an
    app or VM of that name has a job not over; it may raise when the
    jobs cannot be read. None when it is not time; else {"swept": ["ns/name"]}.
    Never raises."""
    now = time.time() if now is None else now
    if now < _next.get("at", 0):
        return None
    _next["at"] = now + EVERY
    if now - _next.get("last", now) > 2 * EVERY:
        # No round ran between (this copy lost the lead, or its round stopped):
        # the owner may have come back meanwhile, so every clock starts again.
        _seen.clear()
    swept = []
    try:
        listed = KUBE.items(SECRETS, read=OUTAGE._bound["kget"])
        there, named = _present()
        candidates = []
        for secret in listed:
            meta = secret.get("metadata") or {}
            owner = _owner(secret)
            ns, name = meta.get("namespace"), meta.get("name")
            if owner is None or not ns:
                continue
            kind, owner_name = owner
            if (kind, ns, owner_name) in there or (ns, name) in named or busy(ns, owner_name):
                continue
            candidates.append((ns, name, kind, owner_name, meta))
    except Exception as error:
        _seen.clear()           # absence counts only over rounds read in a row (#620)
        _next.pop("last", None)
        if _next.get("said") != type(error).__name__:   # once, and again when it changes
            _next["said"] = type(error).__name__
            print(f"outage actions: webhook Secrets not checked for a missing app or VM "
                  f"({type(error).__name__}); none deleted until they can be", flush=True)
        return {"swept": []}
    _next.update(last=now, said=None)
    missing = {}
    for ns, name, kind, owner_name, meta in candidates:
        key = (ns, name)
        before = _seen.get(key)
        if not before or before["uid"] != meta.get("uid"):
            before = {"uid": meta.get("uid"), "first": now}
        missing[key] = before
        if now - before["first"] < GRACE or len(swept) >= MAX_DELETES:
            continue
        owner = f"{kind}/{owner_name}"
        try:
            done = KUBE.delete_if_present(f"/api/v1/namespaces/{ns}/secrets/{name}",
                                   {"kind": "DeleteOptions", "apiVersion": "v1",
                                    "preconditions": {"uid": meta.get("uid"),
                                                      "resourceVersion": meta.get("resourceVersion")}},
                                   send=OUTAGE._bound["ksend"])
        except Exception as error:
            print(f"outage actions: Secret {ns}/{name} of missing {owner} not deleted "
                  f"({type(error).__name__}); tried again next round", flush=True)
            continue
        missing.pop(key)
        if done is None:
            continue            # already gone (deleted with its app meanwhile): nothing this did
        swept.append(f"{ns}/{name}")
        print(f"outage actions: deleted Secret {ns}/{name}: its {owner} was missing for "
              f"{round((now - before['first']) / 60)} minutes", flush=True)
        try:
            _audit(ns, name, owner, now - before["first"])
        except Exception:
            pass                # the delete is in the pod log above
    _seen.clear()
    _seen.update(missing)
    return {"swept": swept}
