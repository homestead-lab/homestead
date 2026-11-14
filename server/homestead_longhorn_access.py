"""Homestead's way in to Longhorn's own API (longhorn-backend, TCP 9500).

Trim now and rolling a volume back to a snapshot are done through that API.
Longhorn 1.13's chart turns networkPolicies.restrictInternalTraffic on by
default: NetworkPolicies in longhorn-system then let only Longhorn's own pods
reach longhorn-manager, and Homestead's calls are refused.

Where - and only where - some other policy already fences the manager pods
in (Longhorn's own, or one someone made), Homestead keeps one narrow policy
of its own beside them: its own pods, TCP 9500, nothing else. NetworkPolicies
add up, so the others stay as they are. Where nothing fences the manager in,
Homestead keeps none: a policy of its own, alone, would fence the manager in
itself and cut off Longhorn's CSI plugin, UI and webhooks. The policies that
fence it own it, so when they all go Kubernetes removes it too, within
seconds (until then it alone fences the manager in); the leader's next check
does as well.

The leader keeps it every ten minutes (ensure). require() also runs ensure,
once, from a request on whichever replica serves it, when Longhorn's API
does not answer: so two replicas may write it at once, and the second's
create (409) is taken as done. That adds at most the 5 s reachability check,
two reads and one write to the refused request.
"""
import urllib.error
import urllib.request

import homestead_kube as KUBE
import homestead_names as NAMES

LHNS = "longhorn-system"
PORT = 9500
URL = f"http://longhorn-backend.{LHNS}:{PORT}/v1"
POLICY = f"{NAMES.BRAND}-backend-access"
POLICIES = f"/apis/networking.k8s.io/v1/namespaces/{LHNS}/networkpolicies"
MANAGER = f"/apis/apps/v1/namespaces/{LHNS}/daemonsets/longhorn-manager"
LOG = {"made": "Longhorn's API opened to Homestead beside Longhorn's own network policies",
       "updated": "Homestead's way in to Longhorn's API brought up to date",
       "removed": "Homestead's way in to Longhorn's API removed: Longhorn no longer fences its manager in"}


def _matches(selector, labels):
    """A Kubernetes label selector against one set of labels."""
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for rule in selector.get("matchExpressions") or []:
        key, op, values = rule.get("key"), rule.get("operator"), rule.get("values") or []
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and key in labels and labels[key] in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


def fences(policy, labels):
    """Whether a policy fences in (limits ingress to) pods with these labels."""
    spec = policy.get("spec") or {}
    return "Ingress" in (spec.get("policyTypes") or ["Ingress"]) and _matches(spec.get("podSelector") or {}, labels)


def policy(namespace, owners):
    """Homestead's policy: its pods, and its node probe's, may reach
    longhorn-manager on 9500. The probe's watchdog salvages Homestead's data
    volume through it while Homestead is down (homestead_watchdog.py). Owned
    by each of Longhorn's policies that fences the manager in."""
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": POLICY, "namespace": LHNS, "labels": {NAMES.key("managed"): "true"},
                         "ownerReferences": [{"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                                              "name": owner["metadata"]["name"], "uid": owner["metadata"]["uid"]}
                                             for owner in sorted(owners, key=lambda p: p["metadata"]["name"])]},
            "spec": {"podSelector": {"matchLabels": {"app": "longhorn-manager"}},
                     "policyTypes": ["Ingress"],
                     "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}},
                                            "podSelector": {"matchExpressions": [{"key": "app", "operator": "In",
                                                                                  "values": [NAMES.BRAND, NAMES.NODEPROBE]}]}}],
                                  "ports": [{"protocol": "TCP", "port": PORT}]}]}}


def _same(found, want):
    meta, wanted = found.get("metadata") or {}, want["metadata"]
    refs = lambda refs: sorted((r.get("name"), r.get("uid")) for r in refs or [])
    return (found.get("spec") == want["spec"]
            and all((meta.get("labels") or {}).get(k) == v for k, v in wanted["labels"].items())
            and refs(meta.get("ownerReferences")) == refs(wanted["ownerReferences"]))


def ensure(kget, ksend, namespace):
    """Keep Homestead's policy exactly while Longhorn fences its manager in.
    Returns "made", "updated", "removed", or "" when nothing changed."""
    manager = KUBE.get_optional(MANAGER, read=kget)
    if not manager:
        return ""                                   # no Longhorn
    labels = ((manager.get("spec") or {}).get("template") or {}).get("metadata", {}).get("labels") or {}
    items = kget(POLICIES).get("items") or []
    ours = next((p for p in items if p["metadata"]["name"] == POLICY), None)
    owners = [p for p in items if p["metadata"]["name"] != POLICY and fences(p, labels)]
    if not owners:
        if ours is None:
            return ""
        try:
            ksend("DELETE", f"{POLICIES}/{POLICY}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            return ""
        return "removed"
    want = policy(namespace, owners)
    if ours is None:
        try:
            ksend("POST", POLICIES, want)
        except urllib.error.HTTPError as error:
            if error.code != 409:                   # another replica made it first
                raise
            return ""
        return "made"
    if _same(ours, want):
        return ""
    want["metadata"]["resourceVersion"] = ours["metadata"].get("resourceVersion", "")
    ksend("PUT", f"{POLICIES}/{POLICY}", want)
    return "updated"


def reachable(timeout=5):
    """"" when Longhorn's API answers at all (any HTTP status), else why not."""
    try:
        with urllib.request.urlopen(urllib.request.Request(URL, headers={"Accept": "application/json"}),
                                    timeout=timeout):
            return ""
    except urllib.error.HTTPError as error:
        error.close()
        return ""
    except (urllib.error.URLError, OSError, ValueError) as error:
        return str(getattr(error, "reason", None) or error)


def require(kget, ksend, namespace, what):
    """Refuse `what`, changing nothing, while Longhorn's API cannot be reached.
    Lets Homestead in first, if Longhorn's policies are what keep it out."""
    why = reachable()
    if not why:
        return
    try:
        change = ensure(kget, ksend, namespace)
    except Exception:
        change = ""
    hint = (" Homestead has just let itself through Longhorn's network policies; try again in a minute."
            if change in ("made", "updated") else "")
    raise ValueError(f"{what}: Homestead cannot reach Longhorn's API. Nothing was changed. "
                     f"longhorn-backend, port {PORT}: {why}.{hint}")
