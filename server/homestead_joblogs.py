"""What a job has to show for itself, beyond its steps: the output of what
does its work.

Most jobs run a pod - an import's copy, a storage move, a backup run, Helm -
and that pod's log says what it is doing and, when it fails, why. A deploy
or an update is a rollout: its newest pod's events and log. A k3s cluster
is VMs installing themselves: what each prints on its serial console, which
KubeVirt keeps as the log of the guest-console-log container in the VM's
launcher pod (KubeVirt 1.1 and later). Where there is nothing to read, the
job's own steps are all there is, and it says so.
"""
import urllib.parse

import homestead_k3scluster as K3SC

kget = ktext = None
TAIL = 200


def bind(_kget, _ktext):
    global kget, ktext
    kget, ktext = _kget, _ktext


def _q(value):
    return urllib.parse.quote(str(value), safe="")


def _pods(ns, selector):
    items = kget(f"/api/v1/namespaces/{_q(ns)}/pods?labelSelector={_q(selector)}").get("items", [])
    return sorted(items, key=lambda p: (p.get("metadata") or {}).get("creationTimestamp", ""), reverse=True)


def _tail(ns, pod, container="", lines=TAIL):
    query = f"?tailLines={lines}" + (f"&container={_q(container)}" if container else "")
    return str(ktext(f"/api/v1/namespaces/{_q(ns)}/pods/{_q(pod)}/log{query}") or "")


def _pod_source(ns, pod, title, container=""):
    name = (pod.get("metadata") or {}).get("name", "")
    phase = (pod.get("status") or {}).get("phase", "")
    try:
        text = _tail(ns, name, container)
        note = "" if text.strip() else f"{name} has printed nothing yet ({phase or 'starting'})"
    except Exception as error:
        text, note = "", f"{name}'s log could not be read ({phase or 'not started'}): {error}"[:300]
    return {"title": title, "pod": name, "text": text[-60000:], "note": note}


def job_output(ns, job, title="Output"):
    """The newest pod of a Kubernetes Job."""
    if not job:
        return [{"title": title, "text": "", "note": "it has not started a pod yet"}]
    pods = _pods(ns, f"job-name={job}")
    if not pods:
        return [{"title": title, "text": "", "note": f"the job {job} has no pod yet, or it has been cleaned up"}]
    return [_pod_source(ns, pods[0], title)]


def _ref_job(key="name", ns_key="namespace", title="Output"):
    return lambda item: job_output(item["ref"].get(ns_key, ""), item["ref"].get(key, ""), title)


def _events(ns, name):
    selector = _q(f"involvedObject.name={name}")
    rows = kget(f"/api/v1/namespaces/{_q(ns)}/events?fieldSelector={selector}").get("items", [])
    rows.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
    return "\n".join(f"{(e.get('lastTimestamp') or e.get('eventTime') or '')[:19].replace('T', ' ')}  "
                     f"{e.get('type', '')[:7]:7}  {e.get('reason', '')}: {' '.join(str(e.get('message') or '').split())}"
                     for e in rows[-40:])


def rollout(item):
    """A deploy or an update: its newest pod's events, then its log."""
    ref = item["ref"]
    dep = kget(f"/apis/apps/v1/namespaces/{_q(ref['namespace'])}/deployments/{_q(ref['name'])}")
    labels = ((dep.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    pods = _pods(ref["namespace"], ",".join(f"{k}={v}" for k, v in sorted(labels.items()))) if labels else []
    if not pods:
        return [{"title": "Pods", "text": "", "note": f"{ref['name']} has no pod yet"}]
    pod = pods[0]
    name = pod["metadata"]["name"]
    out = [{"title": f"Events · {name}", "pod": name, "text": _events(ref["namespace"], name),
            "note": ""}]
    source = _pod_source(ref["namespace"], pod, f"Log · {name}")
    out.append(source)
    return out


def disk_import(item):
    """CDI's importer pod for the DataVolume."""
    ref = item["ref"]
    pods = [p for p in kget(f"/api/v1/namespaces/{_q(ref['namespace'])}/pods").get("items", [])
            if str((p.get("metadata") or {}).get("name", "")).startswith(("importer-", "importer-prime-"))
            and ref["name"] in p["metadata"]["name"]]
    if not pods:
        return [{"title": "Importer", "text": "", "note": "CDI has not started its importer yet, or it has finished"}]
    return [_pod_source(ref["namespace"], pods[0], "Importer")]


def image_cleanup(item):
    ref = item["ref"]
    out = []
    for name in ref.get("pods") or []:
        try:
            pod = kget(f"/api/v1/namespaces/{_q(ref['namespace'])}/pods/{_q(name)}")
        except Exception:
            out.append({"title": name, "text": "", "note": "this pod has gone"})
            continue
        node = (pod.get("spec") or {}).get("nodeName", "")
        out.append(_pod_source(ref["namespace"], pod, f"On {node or name}"))
    return out


def k3s_cluster(item):
    """Each node's serial console: cloud-init, then the install as it runs."""
    ref = item["ref"]
    out = []
    for node in ref.get("nodes") or []:
        title = f"{node['name']} · {node['role']} · {node['address']}"
        pods = [p for p in _pods(ref["namespace"], f"vm.kubevirt.io/name={node['name']}")
                if (p.get("status") or {}).get("phase") in ("Running", "Succeeded", "Failed")]
        if not pods:
            out.append({"title": title, "text": "", "note": "the VM is not running yet"})
            continue
        containers = [c.get("name") for c in (pods[0].get("spec") or {}).get("containers") or []]
        if "guest-console-log" not in containers:
            out.append({"title": title, "text": "", "note": (
                f"this KubeVirt does not keep what the VM prints: open {node['name']}'s console, "
                f"or read {K3SC.LOG} inside it")})
            continue
        out.append(_pod_source(ref["namespace"], pods[0], title, "guest-console-log"))
    return out


def register(ops):
    for kind, reader in (
            ("import", _ref_job(title="Copy")),
            ("protect-run", _ref_job(title="Run")),
            ("helm", _ref_job(title="Helm")),
            ("restructure", _ref_job(key="job", title="Copy")),
            ("reclass", _ref_job(key="job", title="Copy and check")),
            ("self-data-move", _ref_job(key="job", title="Copy")),
            ("vm-disk-import", disk_import),
            ("image-cleanup", image_cleanup),
            ("deployment", rollout), ("image-update", rollout), ("image-rollback", rollout),
            ("k3s-cluster", k3s_cluster)):
        ops.LOGGERS[kind] = reader
