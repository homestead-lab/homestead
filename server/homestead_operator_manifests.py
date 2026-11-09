"""Upstream operator installs made by the host installer, without a Helm release.

Older installs have no ownership mark. An administrator explicitly enables
management after we check their objects; a missing HelmChart is not evidence
that Homestead, or anybody else, installed them. Updates use the upstream
operator manifests and leave the existing KubeVirt/CDI configuration alone.
"""
import copy
import json
import re

import homestead_hostrun as HOSTRUN
import homestead_kube as KUBE
import homestead_names as NAMES
import homestead_updates as UPDATES

MARK = NAMES.key("operator-install")
PARTS = {
    "kubevirt": ("KubeVirt", "/apis/kubevirt.io/v1/kubevirts", "kubevirt", "virt-operator"),
    "cdi": ("CDI", "/apis/cdi.kubevirt.io/v1beta1/cdis", "cdi", "cdi-operator"),
}
CONTROL = ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")
READ_COMPLETE = "\nhomestead-operator-read-complete\n"


def external(obj, allowed_owner=""):
    meta = obj.get("metadata") or {}
    labels, annotations = meta.get("labels") or {}, meta.get("annotations") or {}
    managed = UPDATES.managed_by(obj)
    if managed:
        return managed["by"]
    if annotations.get("meta.helm.sh/release-name") or labels.get("app.kubernetes.io/managed-by") == "Helm":
        return "Helm"
    if any(key.startswith(("olm.", "operators.coreos.com/")) for key in labels):
        return "Operator Lifecycle Manager"
    if any(owner.get("uid") != allowed_owner for owner in meta.get("ownerReferences") or []):
        return "another operator"
    return ""


def inspect(component, read, platform):
    label, path, namespace, deployment = PARTS[component]
    if platform.get("harvester") or platform.get("distribution") not in ("k3s", "rke2"):
        raise ValueError("Manifest upgrades are available on k3s and RKE2 clusters")
    rows = KUBE.items(path, read=read)
    if len(rows) != 1:
        raise ValueError(f"Expected one set of {label} settings. Check the installation before turning on upgrades.")
    cr = rows[0]
    meta = cr.get("metadata") or {}
    if (meta.get("name") != component or (component == "kubevirt" and meta.get("namespace") != namespace)
            or not meta.get("uid") or not meta.get("resourceVersion") or meta.get("deletionTimestamp")):
        raise ValueError(f"{label} does not have the standard installation identity")
    dep = read(f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}")
    for obj in (cr, dep):
        owner = external(obj)
        if owner:
            raise ValueError(f"{label} is managed by {owner}. Upgrade it through that manager.")
    containers = dep.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
    images = [c.get("image", "") for c in containers if c.get("name") == deployment]
    if len(images) != 1 or not re.fullmatch(rf"quay\.io/kubevirt/{deployment}:v\d+\.\d+\.\d+", images[0]):
        raise ValueError(f"{label} uses a custom operator image. Keep using its existing upgrade method.")
    spec = cr.get("spec") or {}
    if spec.get("imageTag") or spec.get("imageRegistry"):
        raise ValueError(f"{label} has a custom image version or registry. Keep using its existing upgrade method.")
    return cr


def state(component, read, platform):
    try:
        cr = inspect(component, read, platform)
        managed = (cr.get("metadata", {}).get("annotations") or {}).get(MARK) == "manifests"
        return {"how": "manifests" if managed else "manual", "can_manage": not managed,
                "note": "Managed by Homestead using upstream manifests." if managed else
                        "Installed from manifests. Enable Homestead upgrades to manage it here."}
    except Exception as error:
        return {"how": "manual", "can_manage": False, "note": str(error)[:240]}


def _servers(read):
    rows = KUBE.items("/api/v1/nodes", read=read)
    servers = [row for row in rows if any(key in (row.get("metadata", {}).get("labels") or {}) for key in CONTROL)]
    if not servers or any(not any(c.get("type") == "Ready" and c.get("status") == "True"
                                   for c in row.get("status", {}).get("conditions") or []) for row in servers):
        raise ValueError("Every control-plane host must be ready so old installer files can be checked")
    return sorted(row["metadata"]["name"] for row in servers)


def _kubectl(distribution):
    return ("k3s kubectl" if distribution == "k3s" else
            "env KUBECONFIG=/etc/rancher/rke2/rke2.yaml /var/lib/rancher/rke2/bin/kubectl")


def _check_files(component, servers):
    # Do not silently disable somebody else's auto-deploy files. The existing
    # installer-file migration creates .skip files; an active one blocks us.
    script = ('set -eu\nfor d in /var/lib/rancher/k3s/server/manifests /var/lib/rancher/rke2/server/manifests; do\n'
              f'  for f in {component}-operator.yaml {component}-cr.yaml; do\n'
              '    if [ -e "$d/$f" ] && [ ! -f "$d/$f.skip" ]; then echo "Active auto-deploy file: $d/$f" >&2; exit 1; fi\n'
              '  done\ndone\nprintf "homestead-files-checked\\n"\n')
    for node in servers:
        out, err = HOSTRUN.run(node, script, timeout=60)
        if out.strip() != "homestead-files-checked":
            raise ValueError(f"Cannot enable operator upgrades on {node}: {(err or out)[:240]}")


def _read_input(node, command, arguments, payload):
    # Exec v4 sends stdin without closing it. kubectl -f - waits for EOF,
    # so give it a complete file after reading exactly the supplied bytes.
    script = ('set -eu\numask 077\nf=$(mktemp /tmp/homestead-operator-read.XXXXXX)\n'
              'trap \'rm -f "$f"\' EXIT\n'
              f'head -c {len(payload)} > "$f"\n'
              f'[ "$(wc -c < "$f")" -eq {len(payload)} ] || {{ echo "Incomplete operator input" >&2; exit 1; }}\n'
              f'{command} {arguments} -f "$f" -o json\n'
              'printf "\\nhomestead-operator-read-complete\\n"\n')
    out, err = HOSTRUN.run(node, script, timeout=90, stdin=payload)
    # A failed command can still print complete objects before it stops. Exec
    # with stdin does not expose a reliable exit status, so require our receipt.
    if not out.endswith(READ_COMPLETE):
        return "", err.strip() or "kubectl inspection did not finish; no change was made"
    return out[:-len(READ_COMPLETE)], err


def _documents(component, version, addons, node, command):
    if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        raise ValueError("The installed operator version must be a numbered upstream release")
    base = addons.KUBEVIRT if component == "kubevirt" else addons.CDI
    text = addons.fetch(f"{base}/download/{version}/{component}-operator.yaml")[0]
    # Use Kubernetes' own YAML parser: release CRD schemas include multiline
    # quoted scalars outside Homestead's deliberately small Compose parser.
    out, err = _read_input(node, command, "create --dry-run=client --validate=false", text.encode())
    try:
        # kubectl create visits and prints each resource separately. Its JSON
        # output is a stream of pretty-printed objects, not one List. Consume
        # every character so a damaged tail cannot become a partial manifest.
        decoder, whitespace, rows, offset = json.JSONDecoder(), re.compile(r"[ \t\r\n]*"), [], 0
        if not out.strip():
            raise ValueError("kubectl returned no manifest objects")
        while offset < len(out):
            offset = whitespace.match(out, offset).end()
            if offset == len(out):
                break
            answer, offset = decoder.raw_decode(out, offset)
            if not isinstance(answer, dict):
                raise ValueError("expected a Kubernetes object")
            items = answer["items"] if answer.get("kind") == "List" else [answer]
            if not isinstance(items, list) or any(not isinstance(row, dict) for row in items):
                raise ValueError("expected a Kubernetes resource list")
            if answer.get("kind") == "List" and (answer.get("metadata") or {}).get("continue"):
                raise ValueError("incomplete Kubernetes resource list")
            rows.extend(items)
    except (ValueError, TypeError, KeyError) as error:
        detail = err.strip() or str(error) or "kubectl returned an unreadable manifest"
        raise ValueError(f"Homestead could not read the upstream operator manifest: {detail[:200]}") from None
    allowed = {"Namespace": "v1", "ServiceAccount": "v1", "ConfigMap": "v1", "Deployment": "apps/v1",
               "ClusterRole": "rbac.authorization.k8s.io/v1", "ClusterRoleBinding": "rbac.authorization.k8s.io/v1",
               "Role": "rbac.authorization.k8s.io/v1", "RoleBinding": "rbac.authorization.k8s.io/v1",
               "CustomResourceDefinition": "apiextensions.k8s.io/v1", "PriorityClass": "scheduling.k8s.io/v1"}
    namespaced = {"ServiceAccount", "ConfigMap", "Deployment", "Role", "RoleBinding"}
    seen = set()
    for row in rows:
        meta = row.get("metadata") or {}
        key = (row.get("apiVersion"), row.get("kind"), meta.get("namespace", ""), meta.get("name"))
        if (allowed.get(key[1]) != key[0] or not key[3] or key in seen
                or key[2] != (component if key[1] in namespaced else "")
                or (key[1] == "Namespace" and key[3] != component)
                or (key[1] == "Deployment" and key[3] != PARTS[component][3])
                or (key[1] == "CustomResourceDefinition" and key[3] !=
                    ("kubevirts.kubevirt.io" if component == "kubevirt" else "cdis.cdi.kubevirt.io"))
                or (key[1] not in ("Namespace", "Deployment", "CustomResourceDefinition")
                    and not re.match(rf"^{component}[.:-]", key[3]))):
            raise ValueError("The upstream operator manifest contains an unexpected resource")
        seen.add(key)
    if not rows or not any(row.get("kind") == "Deployment" for row in rows):
        raise ValueError("The upstream operator manifest has no operator deployment")
    return rows


def _key(row):
    meta = row.get("metadata") or {}
    return row.get("apiVersion"), row.get("kind"), meta.get("namespace", ""), meta.get("name")


def prepare(component, version, read, platform, addons):
    cr = inspect(component, read, platform)
    nodes = _servers(read)
    _check_files(component, nodes)
    command = _kubectl(platform["distribution"])
    rows = _documents(component, version, addons, nodes[0], command)
    out, err = _read_input(nodes[0], command, "get --ignore-not-found=true",
                           json.dumps({"apiVersion": "v1", "kind": "List", "items": rows}).encode())
    try:
        answer = json.loads(out)
        if not isinstance(answer, dict):
            raise ValueError("expected a Kubernetes object")
        found = answer["items"] if answer.get("kind") == "List" else [answer]
        if not isinstance(found, list) or any(not isinstance(row, dict) for row in found):
            raise ValueError("expected a Kubernetes resource list")
        current = {_key(row): row for row in found}
        if len(current) != len(found) or (answer.get("metadata") or {}).get("continue"):
            raise ValueError("incomplete resource list")
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError(f"Homestead could not check the operator resources: {(err or str(error))[:200]}") from None
    allowed = {_key(row) for row in rows}
    if set(current) - allowed:
        raise ValueError("The operator resource inspection returned unexpected objects")
    operator = ("apps/v1", "Deployment", component, PARTS[component][3])
    if operator not in current:
        raise ValueError("The operator deployment was removed during the check. Refresh before trying again.")
    expected = read(f"/apis/apps/v1/namespaces/{component}/deployments/{PARTS[component][3]}")
    if (current[operator].get("metadata") or {}).get("uid") != (expected.get("metadata") or {}).get("uid"):
        raise ValueError("The host's Kubernetes connection does not match this operator, so nothing was changed")
    for row in rows:
        live = current.get(_key(row))
        if live is not None:
            meta = live.get("metadata") or {}
            owner = external(live, cr["metadata"]["uid"])
            if owner or meta.get("deletionTimestamp") or not meta.get("uid") or not meta.get("resourceVersion"):
                raise ValueError(f"Cannot manage {row['kind']} {row['metadata']['name']}: {owner or 'identity is unavailable or changing'}")
            # SSA keeps fields outside the upstream manifest. Resource version
            # and UID pin every existing object to the ownership we inspected.
            row["metadata"].update(uid=meta["uid"], resourceVersion=meta["resourceVersion"])
    return cr, nodes[0], command, rows


def enable(component, version, read, send, platform, addons):
    cr, _, _, _ = prepare(component, version, read, platform, addons)
    path = (f"/apis/kubevirt.io/v1/namespaces/kubevirt/kubevirts/kubevirt" if component == "kubevirt"
            else "/apis/cdi.kubevirt.io/v1beta1/cdis/cdi")
    annotations = dict(cr["metadata"].get("annotations") or {})
    annotations[MARK] = "manifests"
    send("PATCH", path, [{"op": "test", "path": "/metadata/uid", "value": cr["metadata"]["uid"]},
                         {"op": "test", "path": "/metadata/resourceVersion", "value": cr["metadata"]["resourceVersion"]},
                         {"op": "add", "path": "/metadata/annotations", "value": annotations}],
         ctype="application/json-patch+json")


def upgrade(component, target, read, platform, addons):
    cr, node, command, rows = prepare(component, target, read, platform, addons)
    if (cr["metadata"].get("annotations") or {}).get(MARK) != "manifests":
        raise ValueError("Enable Homestead upgrades before updating this operator")
    # Run the operator deployment last. No force, replace, deletion, or CR
    # recreation. Every object is dry-run first; admission/ownership conflicts
    # stop the update. New objects use create, so a racing owner gets a 409.
    rows.sort(key=lambda row: row["kind"] == "Deployment")
    # Pass release data through stdin, not the exec URL/argument list: an
    # operator's CRD schema can exceed the OS argument and proxy URL limits.
    commands = ["set -eu", "d=$(mktemp -d /tmp/homestead-operator.XXXXXX)",
                "trap 'rm -f \"$d\"/*.json; rmdir \"$d\"' EXIT HUP INT TERM"]
    for index in range(len(rows)):
        commands.append(f'IFS= read -r payload; printf "%s" "$payload" > "$d/{index}.json"')
    for dry in (True, False):
        for index, row in enumerate(rows):
            verb = "apply --server-side --field-manager=kubectl" if row["metadata"].get("uid") else "create"
            commands.append(f'{command} {verb}{" --dry-run=server" if dry else ""} -f "$d/{index}.json" >/dev/null')
    script = "\n".join(commands) + "\nprintf 'homestead-operator-applied\\n'\n"
    payload = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows).encode()
    out, err = HOSTRUN.run(node, script, timeout=180, change=f"Upgrade {PARTS[component][0]} to {target}", stdin=payload)
    if out.strip() != "homestead-operator-applied":
        raise ValueError(f"Operator update stopped: {(err or out)[:500]}. Some resources may have been applied. Check before trying again.")
    return f"{PARTS[component][0]} is moving to {target}. Its existing settings are kept."


def configuration(component, read):
    """Carry the whole live CR into Helm upgrades, including custom settings."""
    label, path, _, _ = PARTS[component]
    rows = KUBE.items(path, read=read)
    if len(rows) != 1 or not isinstance(rows[0].get("spec"), dict):
        raise ValueError(f"The complete {label} settings cannot be read, so no upgrade was started")
    row = rows[0]
    meta = row.get("metadata") or {}
    if meta.get("name") != component or (component == "kubevirt" and meta.get("namespace") != "kubevirt"):
        raise ValueError(f"Cannot verify the {label} configuration identity")
    if row["spec"].get("imageTag") or row["spec"].get("imageRegistry"):
        raise ValueError(f"{label} has a custom image version or registry. Keep using its existing upgrade method.")
    return json.dumps({"apiVersion": row.get("apiVersion") or ("kubevirt.io/v1" if component == "kubevirt" else "cdi.kubevirt.io/v1beta1"),
                       "kind": label, "metadata": {key: copy.deepcopy(meta[key]) for key in ("name", "namespace", "labels", "annotations") if key in meta},
                       "spec": copy.deepcopy(row["spec"])}) + "\n"
