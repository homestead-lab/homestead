"""A VM's start, edit or creation - or a k3s cluster of VMs - reviewed against
the cluster's room before anything is sent.

Each preview works out what the change would need (homestead_vm_capacity) and
signs that review (homestead_capacity_review); the change itself is admitted
only with a token for the same request, and the cluster is read again just
before each write, so a review never outlives what it looked at.
"""
import copy
import re
import secrets
import urllib.error

import homestead_capacity_review as CAPACITY_REVIEW
import homestead_imports as IMP
import homestead_ipam as IPAM
import homestead_isos as ISOS
import homestead_k3scluster as K3SC
import homestead_logos as LOGOS
import homestead_operations as OPS
import homestead_place as PLACE
import homestead_platform as PLATFORM
import homestead_routes
import homestead_shared as SHARED
import homestead_vm_batch as VM_BATCH
import homestead_vm_capacity as VM_CAPACITY
import homestead_vm_claims as VM_CLAIMS
import homestead_vm_mutation_job as VM_MUTATION_JOB
import homestead_vm_power_job as VM_POWER_JOB
import homestead_vm_profiles as VM_PROFILES
import homestead_vms as VMS
import homestead_vmstore as VMSTORE

# Bound by the server: its cluster client, the namespace a VM is in when none
# is named, its name check, the identity a review is tied to, the app's
# settings, the class a new disk lands on, why an address cannot be a VM's,
# and what the New VM form can offer.
kget = ksend = None
DEFAULT_NS = "lab"
_dns_name = rollout_review_context = get_app_settings = None
vm_default_class = vm_address_problem = create_options = None


def bind(_kget, _ksend, default_ns, dns_name, review_context, app_settings, default_class, address_problem,
         _create_options):
    global kget, ksend, DEFAULT_NS, _dns_name, rollout_review_context, get_app_settings
    global vm_default_class, vm_address_problem, create_options
    kget, ksend, DEFAULT_NS = _kget, _ksend, default_ns
    _dns_name, rollout_review_context, get_app_settings = dns_name, review_context, app_settings
    vm_default_class, vm_address_problem, create_options = default_class, address_problem, _create_options


def vm_power_capacity_plan(body):
    ns = _dns_name(body.get("ns", DEFAULT_NS), "namespace")
    name = _dns_name(body.get("name"), "VM name")
    action = body.get("action")
    if action not in ("start", "restart", "unpause"):
        raise ValueError("Only Start, Restart and Resume need a VM capacity review")
    current = kget(f"{VMS.API}/namespaces/{ns}/virtualmachines/{name}")
    rollout_review_context(current)  # require identity/version, not a name-only approval
    expanded_spec = None
    if action != "unpause":
        expanded_spec = VM_PROFILES.expand(current, kget)
    dependencies = {}
    def observed_read(path):
        capture = any(part in path for part in ("/persistentvolumeclaims/", "/persistentvolumes/", "/storageclasses/",
                                                "/datavolumes/", "/network-attachment-definitions/"))
        try:
            value = kget(path)
        except urllib.error.HTTPError as error:
            if capture and error.code == 404:
                dependencies[path] = None
            raise
        if capture:
            dependencies[path] = VM_CAPACITY.VMRES.identity(value)
        return value
    threshold = get_app_settings()["thresholds"]["memory"]["critical"]
    plan = VM_CAPACITY.plan(current, observed_read, PLACE.get_nodes(), action=action, current=current,
                            warning_percent=threshold, expanded_spec=expanded_spec, power_intents=OPS._read())
    strategy = VMS._strategy(current)
    policy_after = "Always" if action == "start" and strategy == "Halted" else strategy
    plan["vm"]["policy_before"], plan["vm"]["policy_after"] = strategy, policy_after
    if strategy == "Once" and action in ("start", "restart"):
        plan["blockers"].append("This VM uses the Once run strategy. Edit its run strategy and review that change before starting it again.")
        plan["blocked"] = True
    if policy_after != strategy:
        plan["warnings"].append("Starting a Halted VM changes its KubeVirt run strategy to Always: it will be restarted after shutdown or failure until you stop it.")
    plan["warnings"].append("Power requests address the VM by name. Identity is rechecked immediately before sending, but this is not an atomic scheduler reservation or a cross-resource transaction.")
    plan["requires_confirmation"] = True
    context = {"action": "vm-power", "observations": plan["vm"]["context"],
               "policy_before": strategy, "policy_after": policy_after,
               "expanded_spec": expanded_spec, "dependencies": dependencies}
    return plan, context


def preview_vm_power(body):
    plan, context = vm_power_capacity_plan(body)
    return {"capacity": plan, "capacity_token": CAPACITY_REVIEW.issue(body, context)}


def reviewed_vm_power(body):
    if body.get("action") in ("stop", "force-stop", "pause"):
        return _reviewed_vm_power(body)
    # Serialize local dispatches through the durable intent becoming visible.
    # Kubernetes still owns the final allocation across external clients.
    with SHARED.SharedLock("vm-device-power", strict=True, directory=lambda: OPS.DATA_DIR, timeout=60):
        return _reviewed_vm_power(body)


def _reviewed_vm_power(body):
    action = body.get("action", "")
    ns = _dns_name(body.get("ns", DEFAULT_NS), "namespace")
    name = _dns_name(body.get("name"), "VM name")
    if action in ("stop", "force-stop", "pause"):
        # Recovery must remain available even if capacity/config inventory fails.
        return VMS.power(ns, name, action)
    plan, context = vm_power_capacity_plan(body)
    CAPACITY_REVIEW.enforce(body, plan, context)
    for path, expected in context["dependencies"].items():
        observed = VM_CAPACITY._optional(kget, path)
        if (VM_CAPACITY.VMRES.identity(observed) if observed else None) != expected:
            raise CAPACITY_REVIEW.Rejected("A VM storage/network dependency changed during admission; review it again", plan)
    # Re-read both identities after the inventory and token checks. Never retry
    # an API conflict by fetching and writing a new run policy.
    current = kget(f"{VMS.API}/namespaces/{ns}/virtualmachines/{name}")
    observations = context["observations"]
    if VM_CAPACITY.VMRES.identity(current) != observations["vm"]:
        raise CAPACITY_REVIEW.Rejected("The VM changed during admission; review it again", plan)
    vmi = VM_CAPACITY._optional(kget, f"{VMS.API}/namespaces/{ns}/virtualmachineinstances/{name}")
    if (VM_CAPACITY.VMRES.identity(vmi) if vmi else None) != observations.get("vmi"):
        raise CAPACITY_REVIEW.Rejected("The running VM instance changed during admission; review it again", plan)
    def before_send():
        fresh, fresh_context = vm_power_capacity_plan(body)
        CAPACITY_REVIEW.enforce(body, fresh, fresh_context)
    return VM_POWER_JOB.dispatch(body, context, OPS,
        lambda: VMS.power(ns, name, action, raw_errors=True), before_send)


def vm_create_configuration(body, *, preview=False):
    cfg = copy.deepcopy(body)
    cfg["namespace"] = _dns_name(cfg.get("namespace", DEFAULT_NS), "namespace")
    cfg["name"] = _dns_name(cfg.get("name"), "VM name")
    if cfg.get("isolated") is not True and not cfg.get("mac"):
        if not preview:
            raise ValueError("Review VM creation first so its generated MAC is fixed")
        cfg["mac"] = IMP._vm_mac()
    if cfg.get("store_id"):
        source = VMSTORE.source_for(str(cfg["store_id"]))
        cfg["image_id"], cfg["image_url"] = source.get("image_id", ""), source.get("image_url", "")
        # What it runs, as Harvester labels it, so it has its OS's logo before its agent says.
        entry = VMSTORE.BY_ID.get(str(cfg["store_id"])) or {}
        os_key = LOGOS.os_logo(entry.get("distro"), cfg["store_id"])
        if os_key:
            cfg["labels"] = {**(cfg.get("labels") or {}), VMS.OS_LABEL: os_key}
        cfg["disk_gb"] = max(int(cfg.get("disk_gb") or 0), source["min_gb"])
    cfg["storage_class"] = str(cfg.get("storage_class") or vm_default_class())
    return cfg


def vm_creation_capacity(prepared):
    """Read-only create evidence, including controller-created disk intentions."""
    observations = {}
    def read(path):
        try:
            value = kget(path)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                observations[path] = None
            raise
        # Pod reservations are checked fresh, not frozen for ten minutes. Only
        # actual dependencies/configuration bind the user's reviewed intent.
        if path != "/api/v1/pods":
            if isinstance(value.get("items"), list):
                observations[path] = sorted([VM_CAPACITY.VMRES.identity(row) for row in value["items"]],
                                            key=lambda row: (row.get("namespace") or "", row.get("name") or ""))
            else:
                observations[path] = VM_CAPACITY.VMRES.identity(value)
        return value
    claims = VM_CLAIMS.plans(prepared["vm"], read, prepared["claims"], prepared["downloads"])
    VM_CLAIMS.pin(prepared["vm"], claims, prepared["claims"])
    borrowed = {(volume.get("persistentVolumeClaim") or {}).get("claimName") or (volume.get("dataVolume") or {}).get("name")
                for volume in prepared["vm"]["spec"]["template"]["spec"].get("volumes") or []} - {None, ""} - set(claims)
    borrowed_users = []
    if borrowed:
        # A stopped VM still owns its guest disk. Do not rely on active Pods
        # alone or the display-oriented best-effort import inventory.
        inventory = kget("/apis/kubevirt.io/v1/virtualmachines")
        if not isinstance(inventory.get("items"), list) or (inventory.get("metadata") or {}).get("continue"):
            raise ValueError("VM disk ownership inventory is incomplete")
        for owner in inventory["items"]:
            if owner.get("metadata", {}).get("namespace") != prepared["namespace"]:
                continue
            volumes = ((owner.get("spec", {}).get("template") or {}).get("spec") or {}).get("volumes") or []
            if any(((volume.get("persistentVolumeClaim") or {}).get("claimName") or (volume.get("dataVolume") or {}).get("name")) in borrowed for volume in volumes):
                borrowed_users.append(owner["metadata"]["name"])
    threshold = get_app_settings()["thresholds"]["memory"]["critical"]
    plan = VM_CAPACITY.plan(prepared["vm"], read, PLACE.get_nodes(), action="create", warning_percent=threshold,
                           planned_claims=claims, planned_configmaps={e["path"]: e["body"] for e in prepared.get("effects", [])
                                                                      if e["kind"] == "configmap" and e["body"]})
    plan["requires_confirmation"] = True
    if borrowed_users:
        plan["blockers"].append("Selected disk is referenced by existing VM(s), including stopped VMs: " + ", ".join(sorted(borrowed_users)))
        plan["blocked"] = True
    plan["warnings"] += ["Image import/provisioning may start before the guest. Importer and controller overhead is not fully rendered in this estimate.",
                         "If a later step fails, created images, claims or Secrets are retained for inspection; do not blindly repeat creation."]
    if prepared["vm"]["spec"].get("runStrategy") == "Halted":
        # Still show the future start plan, but a stopped VM does not allocate
        # a launcher. Storage provisioning/import can run independently.
        plan["future_start_blocked"] = plan["blocked"]
        plan["blocked"] = bool(plan["blockers"])
        plan["warnings"].append("The VM is created stopped. Displayed guest placement is for a future start and will be checked again then.")
    context = {"action": "vm-create", "prepared": copy.deepcopy(prepared), "dependencies": observations}
    return plan, claims, context


def preview_vm_create(body):
    cfg = vm_create_configuration(body, preview=True)
    prepared = IMP.prepare_vm(cfg, PLATFORM.detect(), cfg["storage_class"])
    IMP._recheck_vm_creation(prepared)
    plan, claims, context = vm_creation_capacity(prepared)
    if cfg.get("static_ip"):
        problem = vm_address_problem(str(cfg["static_ip"].get("address") or "").strip())
        if problem:
            plan["blockers"].append(problem)
            plan["blocked"] = True
    # Echo only the user's config plus normalized/generated values. Prepared
    # Secrets/cloud-init manifests are never included in the preview response.
    return {"config": cfg, "capacity": plan, "volumes": list(claims.values()),
            "capacity_token": CAPACITY_REVIEW.issue(cfg, context)}


def reviewed_vm_create(body):
    cfg = vm_create_configuration(body)
    prepared = IMP.prepare_vm(cfg, PLATFORM.detect(), cfg["storage_class"])
    IMP._recheck_vm_creation(prepared)
    plan, _, context = vm_creation_capacity(prepared)
    CAPACITY_REVIEW.enforce(cfg, plan, context)
    def check_address():
        if cfg.get("static_ip"):
            problem = vm_address_problem(str(cfg["static_ip"].get("address") or "").strip())
            if problem:
                raise ValueError(problem)
    check_address()
    def admit_after_preparation(resolved):
        check_address()
        # Downloads may resolve an image-specific class. Evaluate that exact
        # resolved manifest, retaining the original signed input/consent.
        fresh, _, _ = vm_creation_capacity(resolved)
        for path, expected in context["dependencies"].items():
            value = VM_CAPACITY._optional(kget, path) if not isinstance(expected, list) else kget(path)
            if isinstance(expected, list):
                if not isinstance(value.get("items"), list) or (value.get("metadata") or {}).get("continue"):
                    raise ValueError("VM dependency inventory became incomplete")
                actual = sorted([VM_CAPACITY.VMRES.identity(row) for row in value["items"]],
                                key=lambda row: (row.get("namespace") or "", row.get("name") or ""))
            else:
                actual = VM_CAPACITY.VMRES.identity(value) if value else None
            if actual != expected:
                raise CAPACITY_REVIEW.Rejected("VM creation dependencies changed during image preparation; inspect retained resources and review again", fresh)
        CAPACITY_REVIEW.enforce(cfg, fresh, context)
    result = VM_MUTATION_JOB.dispatch("vm-create", cfg, cfg["namespace"], cfg["name"], None, OPS, IMP.ksend,
        lambda send: IMP.commit_vm(prepared, before_save=admit_after_preparation, send=send))
    if result.get("address"):
        try:
            IPAM.save_record({"ip": result["address"], "name": cfg["name"], "kind": "static",
                              "category": "server", "mac": result.get("mac", ""), "owner": "homestead",
                              "note": f"VM {cfg['namespace']}/{cfg['name']}"})
        except Exception:
            result["warning"] = " ".join(filter(None, [result.get("warning"),
                "VM created, but its IP-address record could not be saved; inspect IP addresses before reusing the address."]))
    return result


def vm_edit_capacity(prepared):
    """Admit proposed edits, not the old VMI's resource requirements.

    Metadata-only and stop/manual-policy edits remain available without a
    functioning capacity inventory. A template change is never assumed inert:
    KubeVirt LiveUpdate may apply it without an explicit restart request.
    """
    current, vm = prepared["current"], prepared["vm"]
    before, after = VMS._strategy(current), VMS._strategy(vm)
    needed = (vm["spec"]["template"] != current["spec"]["template"] or
              (vm.get("metadata", {}).get("labels") or {}) != (current.get("metadata", {}).get("labels") or {}) or
              bool(prepared["resize"] or prepared["effects"] or prepared["to_create"]) or
              (before != after and after not in ("Halted", "Manual")))
    observations, claims, expanded_spec = {}, {}, None
    def read(path):
        try:
            value = kget(path)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                observations[path] = None
            raise
        if path != "/api/v1/pods":
            if isinstance(value.get("items"), list):
                observations[path] = sorted([VM_CAPACITY.VMRES.identity(row) for row in value["items"]],
                                            key=lambda row: (row.get("namespace") or "", row.get("name") or ""))
            else:
                observations[path] = VM_CAPACITY.VMRES.identity(value)
        return value
    if needed:
        # Existing controller templates are not promises to recreate a missing
        # disk. Only new definitions get the planned-claim exception.
        proposed = copy.deepcopy(vm)
        old_claims = {VMS._volume_claim(volume) for volume in current["spec"]["template"]["spec"].get("volumes") or []}
        proposed["spec"]["dataVolumeTemplates"] = [row for row in VMS._dv_templates(vm) if row["metadata"]["name"] not in old_claims]
        VMS._set_claim_templates(proposed, [row for row in VMS._claim_templates(vm) if row["metadata"]["name"] not in old_claims])
        downloads = [effect for effect in prepared["effects"] if effect["kind"] == "image-download"]
        claims = VM_CLAIMS.plans(proposed, read, prepared["to_create"], downloads)
        # Pin only newly planned disks. Existing controller templates must not
        # be rewritten using today's storage defaults.
        VM_CLAIMS.pin(vm, claims, prepared["to_create"])
        expanded_spec = VM_PROFILES.expand(vm, kget, ksend)
        threshold = get_app_settings()["thresholds"]["memory"]["critical"]
        plan = VM_CAPACITY.plan(vm, read, PLACE.get_nodes(), action="edit", current=current,
                               warning_percent=threshold, planned_claims=claims, expanded_spec=expanded_spec,
                               planned_configmaps={e["path"]: e["body"] for e in prepared["effects"] if e["kind"] == "configmap" and e["body"]})
        plan["warnings"].append("Template and restart-policy changes may take effect immediately through KubeVirt. Saving is not a promise that the guest remains stopped or unchanged.")
        if after == "Halted":
            plan["warnings"].append("The requested policy is Halted. Resource placement shown is conservative; a separate reviewed Start is required to run it again.")
    else:
        plan = {"blocked": False, "blockers": [], "warnings": [], "vm": {"action": "edit"}}
    for effect in prepared["effects"]:
        if effect["kind"] == "replace-datavolume" and effect.get("identity"):
            plan["blockers"].append("Existing DataVolume replacement is unsafe in an edit. Add a disk with a new name/source, then detach the old disk; no old disk is deleted.")
            plan["blocked"] = True
    plan["vm"].update(admission_needed=needed, policy_before=before, policy_after=after)
    plan["warnings"].append("VM, Secret and disk changes are not a transaction. If saving fails, inspect retained resources before trying again. No automatic restart is sent by Save.")
    plan["requires_confirmation"] = True
    context = {"action": "vm-edit", "prepared": copy.deepcopy(prepared), "dependencies": observations,
               "expanded_spec": expanded_spec}
    return plan, claims, context


def prepare_vm_edit(body):
    if body.get("restart"):
        raise ValueError("Save the VM edit first, then review Restart separately against its saved resources")
    ns = _dns_name(body.get("ns", DEFAULT_NS), "namespace")
    name = _dns_name(body.get("name"), "VM name")
    prepared = VMS.prepare_edit(ns, name, body)
    VMS._recheck_edit(prepared)
    return prepared


def preview_vm_edit(body):
    prepared = prepare_vm_edit(body)
    plan, claims, context = vm_edit_capacity(prepared)
    return {"capacity": plan, "volumes": list(claims.values()),
            "capacity_token": CAPACITY_REVIEW.issue(body, context)}


def reviewed_vm_edit(body):
    prepared = prepare_vm_edit(body)
    plan, _, context = vm_edit_capacity(prepared)
    CAPACITY_REVIEW.enforce(body, plan, context)
    def before_save(resolved):
        fresh, _, fresh_context = vm_edit_capacity(resolved)
        if fresh_context["expanded_spec"] != context["expanded_spec"]:
            raise CAPACITY_REVIEW.Rejected("VM profile expansion changed during preparation; review the proposed resources again", fresh)
        for path, expected in context["dependencies"].items():
            value = VM_CAPACITY._optional(kget, path) if not isinstance(expected, list) else kget(path)
            if isinstance(expected, list):
                if not isinstance(value.get("items"), list) or (value.get("metadata") or {}).get("continue"):
                    raise ValueError("VM edit dependency inventory became incomplete")
                actual = sorted([VM_CAPACITY.VMRES.identity(row) for row in value["items"]],
                                key=lambda row: (row.get("namespace") or "", row.get("name") or ""))
            else:
                actual = VM_CAPACITY.VMRES.identity(value) if value else None
            if actual != expected:
                raise CAPACITY_REVIEW.Rejected("VM edit dependencies changed; inspect retained resources and review again", fresh)
        CAPACITY_REVIEW.enforce(body, fresh, context)
        VMS._recheck_edit(resolved)
    return VM_MUTATION_JOB.dispatch("vm-edit", body, prepared["namespace"], prepared["name"], prepared["identity"], OPS, VMS.ksend,
        lambda send: VMS.commit_edit(prepared, before_save=before_save, send=send))


def vm_cluster_configuration(body, *, preview=False):
    cfg = copy.deepcopy(body)
    cfg["namespace"] = _dns_name(cfg.get("namespace") or DEFAULT_NS, "namespace")
    built = K3SC.plan(cfg)
    cfg["name"] = built["name"]
    if preview:
        cfg["review_id"] = secrets.token_hex(16)
        cfg["macs"] = {node["name"]: IMP._vm_mac() for node in built["nodes"]}
    if not re.fullmatch(r"[a-f0-9]{32}", str(cfg.get("review_id") or "")):
        raise ValueError("Review the VM cluster first to freeze its generated identifiers")
    if not isinstance(cfg.get("macs"), dict) or set(cfg["macs"]) != {node["name"] for node in built["nodes"]}:
        raise ValueError("VM cluster MAC addresses are incomplete; review again")
    if len(set(cfg["macs"].values())) != len(built["nodes"]):
        raise ValueError("VM cluster MAC addresses must be distinct")
    cfg["storage_class"] = str(cfg.get("storage_class") or vm_default_class())
    return cfg


def vm_cluster_snapshot():
    cache, external = {}, {}
    def read(path):
        capture = bool(re.fullmatch(r"/api/v1/namespaces/[^/]+", path)) or any(part in path for part in ("/storageclasses", "/storageprofiles/", "/network-attachment-definitions/", "/kubevirts"))
        if path not in cache:
            try:
                cache[path] = kget(path)
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
                cache[path] = error
                if capture:
                    external[path] = None
        value = cache[path]
        if isinstance(value, Exception):
            raise value
        if capture:
            external[path] = (sorted([VM_CAPACITY.VMRES.identity(row) for row in value["items"]],
                                    key=lambda row: (row.get("namespace") or "", row.get("name") or ""))
                              if isinstance(value.get("items"), list) else VM_CAPACITY.VMRES.identity(value))
        return copy.deepcopy(value)
    # Allocation observations bracket a live RPC. Identity/policy reads must
    # bypass this batch's otherwise useful immutable inventory cache.
    read.fresh = kget
    return read, external


def prepare_vm_cluster(cfg):
    token = CAPACITY_REVIEW.derive_secret(cfg, "k3s-bootstrap-join")
    batch = K3SC.prepare(cfg, token=token, guest_checks=True)
    read, external = vm_cluster_snapshot()
    platform = PLATFORM.detect()
    prepared = []
    for config in batch["configs"]:
        item = IMP.prepare_vm(config, platform, cfg["storage_class"])
        K3SC.HEALTH.pin(item, batch["guest_health"])
        IMP._recheck_vm_creation(item)
        claims = VM_CLAIMS.plans(item["vm"], read, item["claims"], item["downloads"])
        VM_CLAIMS.pin(item["vm"], claims, item["claims"])
        prepared.append(item)
    plan = VM_BATCH.plan(prepared, read, PLACE.get_nodes(), threshold=get_app_settings()["thresholds"]["memory"]["critical"])
    context = {"action": "vm-cluster-create", "prepared": copy.deepcopy(prepared), "external": external,
               "numa_policy": copy.deepcopy(plan.get("numa_policy") or {})}
    return batch, prepared, plan, context


def preview_vm_cluster(body):
    cfg = vm_cluster_configuration(body, preview=True)
    batch, _, capacity, context = prepare_vm_cluster(cfg)
    return {**batch["plan"], "ok": not capacity["blocked"], "config": cfg, "capacity": capacity,
            "capacity_token": CAPACITY_REVIEW.issue(cfg, context)}


def reviewed_vm_cluster(body):
    cfg = vm_cluster_configuration(body)
    batch, prepared, plan, context = prepare_vm_cluster(cfg)
    CAPACITY_REVIEW.enforce(cfg, plan, context)
    receipts = {}
    def admit():
        # All remaining controllers still count, including already-created VMs
        # whose launchers have not appeared in Kubernetes yet.
        for item, config in zip(prepared, batch["configs"]):
            if item["name"] not in receipts:
                IMP._recheck_vm_creation(item)
                problem = vm_address_problem(config["static_ip"]["address"])
                if problem:
                    raise ValueError(problem)
        read, _ = vm_cluster_snapshot()
        fresh = VM_BATCH.plan(prepared, read, PLACE.get_nodes(), created=receipts,
                              threshold=get_app_settings()["thresholds"]["memory"]["critical"])
        if (fresh.get("numa_policy") or {}) != context["numa_policy"]:
            raise CAPACITY_REVIEW.Rejected("Host allocation policy changed; retain partial resources and review again", fresh)
        for path, expected in context["external"].items():
            value = VM_CAPACITY._optional(read, path) if expected is None else read(path)
            actual = (sorted([VM_CAPACITY.VMRES.identity(row) for row in VM_CAPACITY._items(read, path)],
                             key=lambda row: (row.get("namespace") or "", row.get("name") or ""))
                      if isinstance(expected, list) else VM_CAPACITY.VMRES.identity(value) if value else None)
            if actual != expected:
                raise CAPACITY_REVIEW.Rejected("VM batch dependencies changed; retain partial resources and review again", fresh)
        CAPACITY_REVIEW.enforce(cfg, fresh, context)
    def before_node(config, made):
        receipts.update({row["name"]: row["identity"] for row in made})
        admit()
    def create_one(config, send=None):
        index = next(i for i, item in enumerate(prepared) if item["name"] == config["name"])
        def after_images(resolved):
            # Image downloads can resolve a new storage class. Pin and re-admit
            # that exact manifest before any PVC/Secret/VM mutation.
            read, _ = vm_cluster_snapshot()
            claims = VM_CLAIMS.plans(resolved["vm"], read, resolved["claims"], resolved["downloads"])
            VM_CLAIMS.pin(resolved["vm"], claims, resolved["claims"])
            prepared[index] = resolved
            admit()
        result = IMP.commit_vm(prepared[index], before_save=after_images, send=send)
        if result.get("address"):
            try:
                IPAM.save_record({"ip": result["address"], "name": config["name"], "kind": "static",
                                  "category": "server", "mac": result.get("mac", ""), "owner": "homestead",
                                  "note": f"VM {config['namespace']}/{config['name']}"})
            except Exception:
                # A failed address record is not authority to repeat creation.
                # Halt the batch with its pre-dispatch recovery intent retained.
                raise ValueError("VM created but its IP-address record could not be saved; inspect it before continuing") from None
        return result
    return K3SC.commit(batch, OPS, create_one=create_one, before_node=before_node, review=cfg, send=IMP.ksend)

def _power_preview_route(request):
    b = request.body
    if b.get("action") in ("start", "restart"):
        ISOS.unlock(b.get("ns") or DEFAULT_NS)      # before it is reviewed, so the review sees it
    return preview_vm_power(b)


def _changed_vms(call):
    """The VM list dropped from server.py's cache, then call()."""
    homestead_routes.forget("vms")
    return call()


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/vm/create-options"): ("viewer", lambda request: create_options()),
    ("POST", "/api/vm/power/preview"): ("operator", _power_preview_route),
    ("POST", "/api/vm/power"): ("operator", lambda request: _changed_vms(lambda: reviewed_vm_power(request.body))),
    ("POST", "/api/vm/edit/preview"): ("operator", lambda request: preview_vm_edit(request.body)),
    ("POST", "/api/vm/edit"): ("operator", lambda request: _changed_vms(lambda: reviewed_vm_edit(request.body))),
    ("POST", "/api/vm/create/preview"): ("operator", lambda request: preview_vm_create(request.body)),
    ("POST", "/api/vm/create"): ("operator", lambda request: _changed_vms(lambda: reviewed_vm_create(request.body))),
    ("POST", "/api/vm/k3s-cluster/plan"): ("operator", lambda request: preview_vm_cluster(request.body)),
    ("POST", "/api/vm/k3s-cluster"): ("operator", lambda request: {"ok": True, "operation": reviewed_vm_cluster(request.body)}),
}
