"""Moving an app to another host: its image there first, the move, then
where it actually landed.

A move asks Kubernetes to prefer the chosen host (unless it pins the app),
and a preference is one score among several. A host that already has the
app's image scores for that: Frigate's 2.2 GB image on k3s-1 outweighed the
preference for k3s-3, so the app restarted where it was, and the move said
it had succeeded. Now the app's images are pulled onto the chosen host
before the move, so the old host's copy cannot win, and the job ends by
naming the host the app runs on - failing, and saying why, when it is not
the one chosen.
"""
import time
import urllib.error

import homestead_operations as OPS
import homestead_update_registry as REGISTRY

KIND = "host-move"
PULL_WAIT = 1800      # a pull that has not finished by then: move anyway, and say so


def _api():
    # The workloads module's Kubernetes access, bound by server.py.
    import homestead_workloads as W
    return W.kget, W.ksend


def _images(dep):
    spec = dep["spec"]["template"]["spec"]
    return sorted({c.get("image", "") for c in (spec.get("initContainers") or []) + spec.get("containers", [])
                   if c.get("image")})


def _on_node(image, node_images):
    """Whether a node already holds an image: by digest when the reference has
    one, else by repository and tag, Docker Hub's short names normalised."""
    try:
        want = REGISTRY.parse_image(image)
    except ValueError:
        return False
    for name in node_images:
        try:
            have = REGISTRY.parse_image(name)
        except ValueError:
            continue
        if have["base"] != want["base"]:
            continue
        if want["digest"] and have["digest"] == want["digest"]:
            return True
        if not want["digest"] and not have["digest"] and have["tag"] == want["tag"]:
            return True
    return False


def missing(dep, node):
    """The app's images the host does not have yet, from what its kubelet reports."""
    kget, _ = _api()
    try:
        status = kget(f"/api/v1/nodes/{node}").get("status") or {}
    except Exception:
        return []           # unknown: move at once, as a move always did
    held = [name for image in status.get("images") or [] for name in image.get("names") or []]
    return [image for image in _images(dep) if not _on_node(image, held)]


def start(body, proposed, capacity, apply, settled=None):
    """Start the move: when the host lacks any of the app's images, pull them
    there first and apply the reviewed placement once they are in; else apply
    it now. apply() writes it (the caller's reviewed write). settled: the
    review's digest of the app as read (CAPACITY_REVIEW.review_identity), so
    a status write during the pull does not end the move."""
    import homestead_image_cache as IMAGES
    meta = proposed["metadata"]
    node, mode = capacity["move"]["node"] or "", capacity["move"]["mode"]
    ref = {"namespace": meta["namespace"], "name": meta["name"], "node": node, "mode": mode, "undo": "rollout"}
    need = missing(proposed, node) if node else []
    pulls = []
    for image in need:
        try:
            pulls.append(IMAGES.prepull(image, [node])["daemonset"])
        except Exception as error:      # a pull that cannot start: move now, as before
            ref["pull_note"] = f"its image could not be pulled onto {node} first: {str(error)[:160]}"
            pulls = []
            break
    title = f"Move {meta['name']}" + (f" to {node}" if node else "")
    if pulls:
        ref.update(stage="pulling", pulls=pulls, images=need, since=time.time(), proposed=proposed)
        if settled:
            ref["settled"] = settled
        return OPS.start(KIND, title, {"kind": "Deployment", "name": meta["name"], "namespace": meta["namespace"]},
                         "/containers", ref, f"Pulling its image onto {node} first, so it lands there")
    apply()
    ref["stage"] = "rolling"
    return OPS.start(KIND, title, {"kind": "Deployment", "name": meta["name"], "namespace": meta["namespace"]},
                     "/containers", ref)


def _pulled(names):
    kget, _ = _api()
    for name in names:
        try:
            ds = kget(f"/apis/apps/v1/namespaces/{_pull_namespace()}/daemonsets/{name}")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                continue            # swept away by the image cache once complete
            raise
        status = ds.get("status") or {}
        desired, ready = int(status.get("desiredNumberScheduled") or 0), int(status.get("numberReady") or 0)
        if not desired or ready < desired:
            return False
    return True


def _pull_namespace():
    import homestead_image_cache as IMAGES
    return IMAGES.I.NS


def _clear(names):
    import homestead_image_cache as IMAGES
    for name in names:
        try:
            IMAGES.stop_prepull(name)
        except Exception:
            pass


def _put_reviewed(proposed, settled, retries=3):
    """PUT the reviewed manifest, resourceVersion and all: an edit made since
    the review conflicts rather than being overwritten. A 409 from a status
    write alone (same uid, and the app as read apart from its status and the
    server's bookkeeping, #826) sends it again on the new version, up to
    retries times; anything else that changed raises the 409."""
    import homestead_storage_journal as JOURNAL
    kget, ksend = _api()
    meta = proposed["metadata"]
    path = f"/apis/apps/v1/namespaces/{meta['namespace']}/deployments/{meta['name']}"
    for attempt in range(retries + 1):
        try:
            return ksend("PUT", path, proposed)
        except urllib.error.HTTPError as error:
            if error.code != 409 or not settled or attempt == retries:
                raise
            fresh = kget(path)
            now = fresh.get("metadata") or {}
            if (now.get("uid") != meta.get("uid") or now.get("deletionTimestamp")
                    or now.get("resourceVersion") == meta.get("resourceVersion") or JOURNAL.settled(fresh) != settled):
                raise
            meta["resourceVersion"] = now["resourceVersion"]
            # One copied from the older read would roll back what controllers recorded since.
            meta.pop("managedFields", None)


def status(item):
    ref = item["ref"]
    ns, name, node = ref["namespace"], ref["name"], ref.get("node", "")
    if ref.get("stage") == "pulling":
        waited = time.time() - ref.get("since", time.time())
        if not _pulled(ref.get("pulls") or []) and waited < PULL_WAIT:
            return "running", 5, f"Pulling {', '.join(ref.get('images') or [])} onto {node} first, so it lands there"
        if waited >= PULL_WAIT:
            ref["pull_note"] = f"its image was still being pulled onto {node} after {PULL_WAIT // 60} minutes"
        _clear(ref.get("pulls") or [])
        proposed = ref.pop("proposed")
        try:
            _put_reviewed(proposed, ref.pop("settled", None))
        except urllib.error.HTTPError as error:
            if error.code == 409:
                return "failed", 100, f"{name} changed while its image was being pulled; nothing was moved. Review the move again"
            raise
        ref["stage"] = "rolling"
        return "running", 10, f"Image on {node}; moving {name}"
    state, progress, message = OPS._deployment(item)
    if state != "succeeded" or not node:
        return state, progress, message
    hosts = sorted({pod.get("node") for pod in (OPS.deployment_progress(ns, name).get("pods") or [])
                    if pod.get("node") and pod.get("phase") == "Running"})
    if hosts and node not in hosts:
        why = ref.get("pull_note") or ("it is pinned elsewhere" if ref.get("mode") == "pinned"
                                       else "Kubernetes preferred another host; pin the app to make the move certain")
        return "failed", 100, f"{name} is running on {', '.join(hosts)}, not {node}: {why}"
    return "succeeded", 100, f"{name} is running on {node}" + (f" ({ref['pull_note']})" if ref.get("pull_note") else "")


def register(ops):
    ops.RESOLVERS[KIND] = status
