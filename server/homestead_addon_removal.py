"""Removing an add-on Homestead installed (#1336): virtual machines first.

A removal is reviewed, then run as a job. The review lists what goes and what
stays, and refuses while anything still uses the add-on. The job removes only
what Homestead installed, one step at a time:

1. The add-on's settings objects (KubeVirt's KubeVirt, CDI's CDI), so their
   operators remove what they run. Each must carry the Helm release annotation
   of Homestead's own chart, and goes with a uid precondition taken from the
   same read that checked it. The job waits for each to be gone.
2. Homestead's HelmCharts (labelled as Homestead's), the same way. The Helm
   controller then uninstalls each release: its operators and the namespaces
   it created.
3. Last, exactly two resource types (CRDs): kubevirts.kubevirt.io and
   cdis.cdi.kubevirt.io, the settings types, each only when no object of it is
   left. Every other KubeVirt and CDI type is its operator's to remove.

What is not Homestead's is kept and named, whatever its name. Its words follow
docs/design.md (house style). REMOVALS keeps the add-on's facts apart from the
steps, so Multus and kube-vip can use the same steps later.
"""
import time
import urllib.error

import homestead_names as NAMES
import homestead_routes

KIND = "addon-remove"
CONTROLLER_NS = "kube-system"
HELMCHARTS = f"/apis/helm.cattle.io/v1/namespaces/{CONTROLLER_NS}/helmcharts"
CRDS = "/apis/apiextensions.k8s.io/v1/customresourcedefinitions"
RELEASE = "meta.helm.sh/release-name"
RELEASE_NS = "meta.helm.sh/release-namespace"
# How long one step may wait for an operator or the Helm controller, counting
# only the time Homestead was looking (a gap of GAP starts it again).
WAIT_LIMIT = 30 * 60
GAP = 5 * 60
SHOWN = 5   # names listed in a refusal before "and N more"

REMOVALS = {
    "kubevirt": {
        "name": "virtual machines",
        "title": "Remove virtual machines",
        # The settings objects, in order, and the chart whose release created each.
        "resources": [("KubeVirt settings object", "/apis/kubevirt.io/v1/namespaces/kubevirt/kubevirts/kubevirt",
                       "homestead-kubevirt"),
                      ("CDI settings object", "/apis/cdi.kubevirt.io/v1beta1/cdis/cdi", "homestead-cdi")],
        "charts": ["homestead-macvtap", "homestead-kubevirt", "homestead-cdi"],
        # The settings types, removed last and only when no object of theirs is
        # left. Never more: every other KubeVirt and CDI type is its operator's.
        "crds": ["kubevirts.kubevirt.io", "cdis.cdi.kubevirt.io"],
    },
}

kget = ksend = None
platform = lambda: {}   # noqa: E731  (bound: homestead_platform.detect)
snapshot = lambda: []   # noqa: E731  (bound: the job store's running jobs)
_clock = time.time


def bind(_kget, _ksend, _platform, _snapshot):
    """homestead_addons sets these itself, beside its own (its bind)."""
    global kget, ksend, platform, snapshot
    kget, ksend, platform, snapshot = _kget, _ksend, _platform, _snapshot


# ------------------------------------------------------------------ reading
def _named(rows, noun, plural=None):
    names = [r for r in rows]
    shown = ", ".join(names[:SHOWN]) + (f" and {len(names) - SHOWN} more" if len(names) > SHOWN else "")
    return f"{len(names)} {noun if len(names) == 1 else (plural or noun + 's')} ({shown})"


def _items(path):
    return kget(path).get("items") or []


def _where(obj):
    meta = obj.get("metadata") or {}
    return f"{meta['namespace']}/{meta['name']}" if meta.get("namespace") else meta.get("name", "")


def _cdi_volume(claim):
    """A volume CDI created or filled for a VM disk."""
    meta = claim.get("metadata") or {}
    return (any(key.startswith("cdi.kubevirt.io/") for key in (meta.get("annotations") or {}))
            or (meta.get("labels") or {}).get("app") == "containerized-data-importer")


def _is_vm_job(row):
    kind = row.get("kind") or ""
    return kind.startswith("vm-") or kind in ("unraid-vm-import", "k3s-cluster")


# Every type KubeVirt and CDI serve goes when their operators remove their CRDs,
# with every object in it: VM snapshots (which own VolumeSnapshots, disk
# backups), exports, clones, restores, pools, instance types, data sources,
# import crons and the rest. So any object of any of their types refuses the
# removal, read from the CRDs of the groups kubevirt.io and *.kubevirt.io at
# the time (a newer release's new types too). Not counted: the settings objects
# this removal deletes itself (SETTINGS), and what an operator created by
# default (_made_by_operator).
SETTINGS = {("kubevirt.io", "KubeVirt"), ("cdi.kubevirt.io", "CDI")}
# The kinds an operator creates by default, and the only ones that can be
# exempt: labels and ownerReferences can be written by anyone who can create
# a VM, so for any other kind (VirtualMachine, VirtualMachineInstance,
# DataVolume, snapshots and their contents, exports, clones, restores, pools,
# DataSources, DataImportCrons, ...) nothing exempts an object, whatever it carries.
CLUSTER_DEFAULTS = {"VirtualMachineClusterInstancetype", "VirtualMachineClusterPreference"}
# Kinds with words of their own; any other is named by its kind.
WORDS = {"VirtualMachine": ("virtual machine", "Delete {it} first."),
         "VirtualMachineInstance": ("running VM", "Stop and delete {it} first."),
         "DataVolume": ("VM disk", "Delete {it} under Import > Disk images first.")}


def _ours_group(group):
    return group == "kubevirt.io" or group.endswith(".kubevirt.io")


def _settings_uids():
    """The live KubeVirt and CDI settings objects' uids, by kind, read now."""
    uids = {}
    for _, path, _ in REMOVALS["kubevirt"]["resources"]:
        found = _optional(path)
        if found:
            uids[found.get("kind")] = (found.get("metadata") or {}).get("uid")
    return uids


def _owned_by_settings(obj, uids):
    """Owned by the live settings object: the owner's group, kind and uid all match."""
    for owner in (obj.get("metadata") or {}).get("ownerReferences") or []:
        group = str(owner.get("apiVersion") or "").split("/")[0]
        kind = owner.get("kind")
        if ((kind, group) in (("KubeVirt", "kubevirt.io"), ("CDI", "cdi.kubevirt.io"))
                and uids.get(kind) and owner.get("uid") == uids[kind]):
            return True
    return False


def _made_by_operator(obj, uids):
    """What an operator created by default, and removes with its settings. Only
    these kinds can be: virt-operator's cluster-wide instance types and
    preferences (its managed-by label); CDI's CDIConfig (owned by the live CDI
    settings object, by uid); and CDI's StorageProfiles, one per storage class
    with an empty spec (one with a spec set is a person's change)."""
    kind, meta = obj.get("kind"), obj.get("metadata") or {}
    if kind in CLUSTER_DEFAULTS:
        return (meta.get("labels") or {}).get("app.kubernetes.io/managed-by") == "virt-operator"
    if kind == "CDIConfig":
        return _owned_by_settings(obj, uids)
    if kind == "StorageProfile":
        return not obj.get("spec")
    return False


def _types():
    """(group, version, plural, kind) of every type KubeVirt and CDI serve now."""
    out = []
    for row in _items(CRDS):
        spec = row.get("spec") or {}
        group, names = spec.get("group") or "", spec.get("names") or {}
        version = next((v.get("name") for v in spec.get("versions") or [] if v.get("served")), "")
        if _ours_group(group) and version and (group, names.get("kind")) not in SETTINGS:
            out.append((group, version, names.get("plural"), names.get("kind") or names.get("plural")))
    return sorted(out)


def _said(error):
    return f"HTTP {error.code}" if isinstance(error, urllib.error.HTTPError) else (str(error)[:120] or type(error).__name__)


def blockers(what):
    """What stops the removal now, in words; an empty list when nothing does.
    What cannot be read is a reason too: nothing is removed on a guess."""
    if what != "kubevirt":
        raise ValueError("Homestead removes only virtual machines for now")
    found = []
    try:
        types, uids = _types(), _settings_uids()
    except Exception as error:
        return [f"Homestead could not read which VM types the cluster serves ({_said(error)}), so nothing is removed."]
    for group, version, plural, kind in types:
        try:
            rows = [_where(row) for row in _items(f"/apis/{group}/{version}/{plural}") if not _made_by_operator(row, uids)]
        except urllib.error.HTTPError as error:
            if error.code == 404:   # gone since the CRDs were read: none of them
                continue
            found.append(f"Homestead could not check the {kind} objects ({_said(error)}), so nothing is removed.")
            continue
        except Exception as error:
            found.append(f"Homestead could not check the {kind} objects ({_said(error)}), so nothing is removed.")
            continue
        if not rows:
            continue
        one = len(rows) == 1
        noun, way = WORDS.get(kind, (kind, "Delete {it} first, or keep virtual machines."))
        verb = ("still runs" if one else "still run") if kind == "VirtualMachineInstance" else ("still exists" if one else "still exist")
        found.append(f"{_named(rows, noun)} {verb}. {way.format(it='it' if one else 'them')}")
    try:
        volumes = [_where(c) for c in _items("/api/v1/persistentvolumeclaims") if _cdi_volume(c)]
        if volumes:
            found.append(f"{_named(volumes, 'volume')} {'holds a VM disk image' if len(volumes) == 1 else 'hold VM disk images'}. "
                         f"Delete {'it' if len(volumes) == 1 else 'them'} first, or keep virtual machines.")
    except Exception as error:
        found.append(f"Homestead could not check the volumes ({_said(error)}), so nothing is removed.")
    jobs = [row.get("title") or row.get("kind") for row in snapshot()
            if _is_vm_job(row) and row.get("status") not in ("succeeded", "failed", "cancelled")]
    if jobs:
        found.append(f"A VM job is running ({', '.join(jobs[:SHOWN])}). Wait for it to end.")
    return found


def _optional(path):
    """The object at path, or None. homestead_kube imports the job store, which
    imports homestead_addons, which imports this: so it is imported here."""
    import homestead_kube as KUBE
    return KUBE.get_optional(path, read=kget)


def _chart(name):
    return _optional(f"{HELMCHARTS}/{name}")


def _ours(chart):
    return bool(chart) and NAMES.label_of(chart.get("metadata"), "managed") == "true"


def _released_by(obj, release):
    annotations = (obj.get("metadata") or {}).get("annotations") or {}
    return annotations.get(RELEASE) == release and annotations.get(RELEASE_NS) == CONTROLLER_NS


def review(what):
    """What the removal would do, and what refuses it, before anything changes."""
    spec = REMOVALS.get(what)
    if not spec:
        raise ValueError("Homestead removes only virtual machines for now")
    p = platform() or {}
    if p.get("harvester"):
        return {"can": False, "title": spec["title"],
                "blockers": ["Harvester manages KubeVirt, so Homestead does not remove it."], "removes": [], "keeps": []}
    charts = {name: _chart(name) for name in spec["charts"]}
    ours = [name for name, chart in charts.items() if _ours(chart)]
    stop = list(blockers(what))
    if not ours:
        stop.insert(0, "KubeVirt was not installed by Homestead, so Homestead does not remove it.")
    foreign = [name for name, chart in charts.items() if chart and not _ours(chart)]
    keeps = ["Volumes, storage classes and everything else not listed here are not changed.",
             "Anything Homestead did not create is kept, even under the same name."]
    if foreign:
        keeps.append(f"{', '.join(foreign)} {'was' if len(foreign) == 1 else 'were'} not installed by Homestead, "
                     f"so {'it is' if len(foreign) == 1 else 'they are'} kept.")
    keeps.append("Install under Cluster > Add-ons offers virtual machines again.")
    return {"can": not stop, "title": spec["title"], "blockers": stop, "keeps": keeps,
            "removes": ["The KubeVirt and CDI settings. Their operators then remove the VM services they run.",
                        f"The charts {', '.join(ours) or 'Homestead installed'}, with their operators and the "
                        "kubevirt and cdi namespaces.",
                        "Everything in those namespaces, including anything a person put there.",
                        "The settings types kubevirts.kubevirt.io and cdis.cdi.kubevirt.io, once unused."]}


# ------------------------------------------------------------------ the job
def _waited(ref):
    """How long this step has waited, with Homestead looking throughout."""
    now = _clock()
    state = ref.get("wait") if isinstance(ref.get("wait"), dict) else {}
    since, last = state.get("since"), state.get("last")
    if since is None or last is None or last > now or now - last > GAP:
        since = now
    ref["wait"] = {"since": since, "last": now}
    return now - since


def _next(ref, stage=None):
    ref.pop("wait", None)
    ref.pop("sent", None)
    if stage:
        ref.update(stage=stage, index=0)
    else:
        ref["index"] = ref.get("index", 0) + 1


def _done_words(ref):
    removed = ref.get("removed") or []
    return ("Removed: " + ", ".join(removed) + ".") if removed else "Nothing was removed."


def _stopped(ref, why):
    ref["stage"] = "stopped"
    kept = ref.get("kept") or []
    return ("failed", 100, f"{why} {_done_words(ref)}" + (f" Kept: {', '.join(kept)}." if kept else ""))


def _delete(path, found):
    ksend("DELETE", path, {"apiVersion": "v1", "kind": "DeleteOptions",
                           "preconditions": {"uid": (found.get("metadata") or {}).get("uid")}})


def _gone_or_waiting(ref, label, path, found):
    """A step whose object was sent away: done once it is gone; until then it
    waits, within WAIT_LIMIT, naming what holds it."""
    if found is None:
        ref.setdefault("removed", []).append(label)
        _next(ref)
        return None
    held = ", ".join((found.get("metadata") or {}).get("finalizers") or [])
    by = f" It still carries the finalizer {held}." if held else ""
    if _waited(ref) >= WAIT_LIMIT:
        return _stopped(ref, (
            f"The {label} ({_where(found)}) was not gone {WAIT_LIMIT // 60} minutes after it was asked to go, so the "
            f"removal stopped.{by} The operator or controller that set it has not finished. Check its logs. Removing "
            "the finalizer by hand lets the object go, but leaves behind whatever that operator did not clean up. "
            "Remove virtual machines again once it is gone."))
    return "running", 10 + 20 * (ref.get("index", 0)), f"Waiting for the {label} to be removed.{by}"


def _resource_step(spec, ref):
    label, path, release = spec["resources"][ref["index"]]
    found = _optional(path)
    if ref.get("sent") or (found and (found.get("metadata") or {}).get("deletionTimestamp")):
        # Sent by this job, or already going (an earlier removal): wait on it.
        ref["sent"] = True
        return _gone_or_waiting(ref, label, path, found)
    if found is None:
        _next(ref)
        return None
    if not _released_by(found, release):
        ref.setdefault("kept", []).append(label)
        return _stopped(ref, f"The {label} was not created by Homestead's chart, so the removal stopped before it.")
    # Checked again right before the first write: a VM made since the review stops it.
    stop = blockers(ref["addon"])
    if stop:
        return _stopped(ref, " ".join(stop))
    _delete(path, found)
    ref["sent"] = True
    return "running", 10, f"Removing the {label}"


def _chart_step(spec, ref):
    name = spec["charts"][ref["index"]]
    path = f"{HELMCHARTS}/{name}"
    found = _optional(path)
    if ref.get("sent"):
        return _gone_or_waiting(ref, f"chart {name}", path, found)
    if found is None:
        _next(ref)
        return None
    if not _ours(found):
        ref.setdefault("kept", []).append(name)
        _next(ref)
        return None
    _delete(path, found)
    ref["sent"] = True
    return "running", 50, f"Removing the chart {name}"


def _crd_step(spec_, ref):
    name = spec_["crds"][ref["index"]]
    found = _optional(f"{CRDS}/{name}")
    if found is None:
        _next(ref)
        return None
    spec = found.get("spec") or {}
    version = next((v.get("name") for v in spec.get("versions") or [] if v.get("served")), "")
    try:
        left = _items(f"/apis/{spec.get('group')}/{version}/{(spec.get('names') or {}).get('plural')}") if version else [1]
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        left = []
    if left:
        ref.setdefault("kept", []).append(f"{name} (still in use)")
        _next(ref)
        return None
    try:
        _delete(f"{CRDS}/{name}", found)
        ref.setdefault("removed", []).append(name)
    except urllib.error.HTTPError as error:
        if error.code not in (404, 409):
            raise
        if error.code == 409:       # replaced since this read: not the one checked
            ref.setdefault("kept", []).append(name)
    _next(ref)
    return None


def status(item):
    """The job's next step: (status, progress, message)."""
    ref = item["ref"]
    spec = REMOVALS[ref["addon"]]
    for _ in range(32):     # steps that only skip run on in the same round
        stage = ref.get("stage", "resources")
        if stage == "stopped":
            return item.get("status", "failed"), 100, item.get("message", "")
        if stage == "done":
            return "succeeded", 100, item.get("message", "")
        if stage == "resources":
            if ref.get("index", 0) >= len(spec["resources"]):
                _next(ref, "charts")
                continue
            result = _resource_step(spec, ref)
        elif stage == "charts":
            if ref.get("index", 0) >= len(spec["charts"]):
                _next(ref, "crds")
                continue
            result = _chart_step(spec, ref)
        else:
            if ref.get("index", 0) >= len(spec["crds"]):
                ref["stage"] = "done"
                homestead_routes.forget("helm", "platform")
                kept = ref.get("kept") or []
                return ("succeeded", 100, f"Virtual machines were removed. {_done_words(ref)}"
                        + (f" Kept: {', '.join(kept)}." if kept else "")
                        + " Install under Cluster > Add-ons offers them again.")
            result = _crd_step(spec, ref)
        if result:
            return result
    return "running", item.get("progress", 10), item.get("message", "")


def cancel_plan(item):
    removed = item["ref"].get("removed") or []
    return {"mode": "forget", "needs": "admin",
            "keeps": [("What was removed stays removed: " + ", ".join(removed) + ".") if removed
                      else "Nothing has been removed yet.",
                      "The removal stops after the current step. Nothing else is removed."]}


def cancel_run(item, _options):
    homestead_routes.forget("helm", "platform")
    return f"Stopped. {_done_words(item['ref'])}"


# ------------------------------------------------------------------ routes
def _review(request):
    return review("kubevirt")


def _remove(request):
    import homestead_operations as OPS
    if str(request.body.get("confirm") or "").strip() != "kubevirt":
        raise ValueError("Type kubevirt to confirm")
    found = review("kubevirt")
    if not found["can"]:
        raise ValueError(found["blockers"][0])
    operation = OPS.start(KIND, "Remove virtual machines", {"kind": "HelmChart", "name": "homestead-kubevirt",
                                                            "namespace": CONTROLLER_NS},
                          "/settings", {"addon": "kubevirt", "stage": "resources", "index": 0},
                          "Removing virtual machines")
    homestead_routes.forget("helm", "platform")
    return {"ok": True, "operation": operation, "detail": "Virtual machines are being removed. Follow it under Jobs."}


# Served as homestead_addons' own routes (homestead_addons.ROUTES).
REMOVAL_ROUTES = {
    ("POST", "/api/addons/kubevirt/remove/review"): ("admin", _review),
    ("POST", "/api/addons/kubevirt/remove"): ("admin", _remove),
}


def register(ops):
    ops.RESOLVERS[KIND] = status
    ops.CANCELLERS[KIND] = (cancel_plan, cancel_run)
