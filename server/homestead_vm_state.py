"""Read-only persistent TPM/EFI/CBT storage evidence.

KubeVirt backend-storage.go v1.3.1/v1.4.0/v1.9.0: older releases use a
fixed PVC name; newer ones select by label with a legacy-name fallback.
Names/labels discover candidates, never establish permission to adopt data.
The projected fresh claim is only a placement placeholder, never submitted.
"""
import re
import urllib.error
from urllib.parse import quote, unquote

import homestead_pod_resources as RESOURCES
import homestead_vm_state_policy as POLICY

PREFIX = "persistent-state-for"
IDENTITY = ("namespace", "name", "uid", "resourceVersion")


def needed(vm, spec, vmi=None):
    domain = spec.get("domain") or {}
    tpm = (domain.get("devices") or {}).get("tpm") or {}
    efi = ((domain.get("firmware") or {}).get("bootloader") or {}).get("efi") or {}
    return tpm.get("persistent") is True or efi.get("persistent") is True or any(
        (((obj or {}).get("status") or {}).get("changedBlockTracking") or {}).get("state") in ("Initializing", "Enabled")
        for obj in (vm, vmi))


def inspect(vm, spec, config, read, *, vmi=None, version=None, cold=True):
    policy = POLICY.selection(vm, spec, config, read, version=version, cold=cold)
    result = {"volumes": [], "planned_claims": {}, "dependencies": policy["dependencies"],
              "blockers": policy["blockers"], "warnings": policy["warnings"], "initialization": None}
    if not needed(vm, spec, vmi) and not policy["automatic"]:
        return result
    meta = vm["metadata"]
    namespace, name = meta["namespace"], meta["name"]
    base = f"/api/v1/namespaces/{quote(namespace, safe='')}/persistentvolumeclaims"
    legacy = PREFIX + "-" + name

    def identity(value):
        return {key: (value.get("metadata") or {}).get(key) for key in IDENTITY}

    def safe_read(path):
        try:
            return read(path)
        except urllib.error.HTTPError:
            raise
        except Exception:
            raise ValueError("Persistent VM state dependency could not be read") from None

    def object_at(path, *, missing=False, namespaced=False):
        try:
            value = safe_read(path)
        except urllib.error.HTTPError as error:
            if missing and error.code == 404:
                result["dependencies"][path] = None
                return None
            raise ValueError("Persistent VM state dependency could not be read") from None
        info = identity(value)
        if (not all(info[key] for key in ("name", "uid", "resourceVersion")) or
                info["name"] != unquote(path.rsplit("/", 1)[-1]) or
                (namespaced and info["namespace"] != namespace) or
                (value.get("metadata") or {}).get("deletionTimestamp")):
            raise ValueError("Persistent VM state dependency identity is incomplete or deleting")
        result["dependencies"][path] = info
        return value

    def collection(path):
        value = safe_read(path)
        if not isinstance(value.get("items"), list) or (value.get("metadata") or {}).get("continue"):
            raise ValueError("Persistent VM state inventory is incomplete")
        return value["items"]

    def owned(obj):
        refs = [ref for ref in (obj.get("metadata") or {}).get("ownerReferences") or [] if ref.get("controller") is True]
        return bool(meta.get("uid") and len(refs) == 1 and refs[0].get("kind") == "VirtualMachine" and
                    refs[0].get("apiVersion", "").split("/")[0] == "kubevirt.io" and
                    refs[0].get("name") == name and refs[0].get("uid") == meta["uid"])

    try:
        reported = []
        if vmi:
            if not owned(vmi) or not all(identity(vmi).values()) or (vmi.get("metadata") or {}).get("namespace") != namespace:
                raise ValueError("Persistent VM state VMI ownership is unverified")
            result["dependencies"]["state-vmi"] = identity(vmi)
            migration = (vmi.get("status") or {}).get("migrationState") or {}
            if migration and not (migration.get("completed") or migration.get("failed")):
                raise ValueError("Persistent VM state is migrating; wait for handoff/recovery")
            for volume in (vmi.get("status") or {}).get("volumeStatus") or []:
                if str(volume.get("name", "")).startswith(PREFIX):
                    claim = (volume.get("persistentVolumeClaimInfo") or {}).get("claimName")
                    if not claim:
                        raise ValueError("Persistent VM state status has no claim identity")
                    reported.append(claim)
            if len(reported) > 1:
                raise ValueError("Persistent VM state status is ambiguous")
        inventory = collection(base)
        candidates = [obj for obj in inventory if (obj.get("metadata") or {}).get("namespace") == namespace and (
            (obj.get("metadata") or {}).get("name") == legacy or
            ((obj.get("metadata") or {}).get("labels") or {}).get(PREFIX) == name or
            (obj.get("metadata") or {}).get("name") in reported)]
        result["dependencies"]["state-candidates:" + base] = sorted(
            [identity(obj) for obj in candidates], key=lambda item: str(item["name"]))
        if len(candidates) > 1:
            raise ValueError("Multiple persistent VM state PVCs match; resolve migration/legacy state before starting")
        if candidates:
            candidate = candidates[0]
            claim = (candidate.get("metadata") or {}).get("name")
            pvc = object_at(base + "/" + quote(claim, safe=""), namespaced=True)
            if identity(pvc) != identity(candidate):
                raise ValueError("Persistent VM state PVC changed during inspection")
            if not owned(pvc):
                raise ValueError("Persistent VM state PVC belongs to another or unverifiable VM identity; it will not be adopted")
            if reported and reported != [claim]:
                raise ValueError("Persistent VM state PVC disagrees with the running instance")
            state = pvc.get("spec") or {}
            if state.get("volumeMode", "Filesystem") != "Filesystem" or not set(state.get("accessModes") or []) & {"ReadWriteOnce", "ReadWriteMany", "ReadWriteOncePod"}:
                raise ValueError("Persistent VM state needs a filesystem PVC with verified access modes")
            phase = (pvc.get("status") or {}).get("phase")
            if phase == "Bound":
                volume = state.get("volumeName")
                if not volume:
                    raise ValueError("Persistent VM state has no bound PV")
                pv = object_at("/api/v1/persistentvolumes/" + quote(volume, safe=""))
                ref = (pv.get("spec") or {}).get("claimRef") or {}
                if (ref.get("uid") != pvc["metadata"]["uid"] or ref.get("name") != claim or
                        ref.get("namespace") != namespace or (pv.get("status") or {}).get("phase") in ("Released", "Failed")):
                    raise ValueError("Persistent VM state PV binding identity is unavailable or mismatched")
            elif phase == "Pending":
                klass = state.get("storageClassName")
                sc = object_at("/apis/storage.k8s.io/v1/storageclasses/" + quote(klass or "", safe=""))
                if not klass or sc.get("volumeBindingMode") != "WaitForFirstConsumer":
                    raise ValueError("Persistent VM state PVC is still provisioning")
                result["warnings"].append("Persistent VM state waits for first-consumer binding; provisioning capacity is unverified")
            else:
                raise ValueError("Persistent VM state PVC is not Bound or waiting for first-consumer placement")
            result["volumes"].append({"name": "homestead-state-evidence", "persistentVolumeClaim": {"claimName": claim}})
            result["warnings"].append(f"Persistent VM state uses PVC {claim}; preserve it with the VM's disks when moving or recovering this VM")
            return result
        if (reported or (vm.get("status") or {}).get("created") or (vm.get("status") or {}).get("ready") or
                ((vmi or {}).get("status") or {}).get("phase") in ("Running", "Succeeded", "Failed")):
            raise ValueError("Previously created/running VM has no persistent state PVC; recover its state before starting")
        if object_at(base + "/" + quote(legacy, safe=""), missing=True, namespaced=True) is not None:
            raise ValueError("Persistent VM state PVC appeared during inspection; refresh")
        explicit = (config or {}).get("vmStateStorageClass")
        klass = explicit
        if not klass:
            path = "/apis/storage.k8s.io/v1/storageclasses"
            classes = collection(path)
            result["dependencies"][path] = sorted([identity(obj) for obj in classes], key=lambda item: str(item["name"]))
            defaults = lambda key: [obj for obj in classes if ((obj.get("metadata") or {}).get("annotations") or {}).get(key) == "true"]
            normal = defaults("storageclass.kubernetes.io/is-default-class")
            virt = defaults("storageclass.kubevirt.io/is-default-virt-class")
            match = re.fullmatch(r"v?1\.(\d+)\.\d+", version or "")
            minor = int(match[1]) if match else None
            if minor == 3:
                choices = normal
            elif minor is not None and 4 <= minor <= 9:
                choices = virt or normal
            else:
                if virt and {identity(obj)["name"] for obj in virt} != {identity(obj)["name"] for obj in normal}:
                    raise ValueError("Persistent VM state default differs by KubeVirt version; configure an explicit VM-state storage class")
                choices = normal
            if len(choices) != 1:
                raise ValueError("Persistent VM state needs one unambiguous default storage class")
            klass = choices[0]["metadata"]["name"]
        sc = object_at("/apis/storage.k8s.io/v1/storageclasses/" + quote(klass, safe=""))
        if sc["metadata"]["name"] != klass:
            raise ValueError("Persistent VM state storage class identity mismatch")
        profile = object_at("/apis/cdi.kubevirt.io/v1beta1/storageprofiles/" + quote(klass, safe=""), missing=True)
        if profile and profile["metadata"]["name"] != klass:
            raise ValueError("Persistent VM state storage profile identity mismatch")
        properties = ((profile or {}).get("status") or {}).get("claimPropertySets") or []
        modes = {mode for prop in properties if prop.get("volumeMode") == "Filesystem" for mode in prop.get("accessModes") or []}
        mode = "ReadWriteMany" if "ReadWriteMany" in modes else "ReadWriteOnce" if "ReadWriteOnce" in modes else "ReadWriteMany" if explicit else "ReadWriteOnce"
        minimum = (((profile or {}).get("metadata") or {}).get("annotations") or {}).get("cdi.kubevirt.io/minimumSupportedPvcSize", "10Mi")
        size = str(max(10 * 1024**2, RESOURCES.quantity(minimum)))
        result["planned_claims"][legacy] = {"name": legacy, "storage_class": klass, "access_mode": mode, "volume_mode": "Filesystem", "size": size}
        result["volumes"].append({"name": "homestead-state-evidence", "persistentVolumeClaim": {"claimName": legacy}})
        if meta.get("uid"):
            result["initialization"] = {"name": name, "namespace": namespace, "uid": meta["uid"],
                "reason": "No persistent state PVC was found. Initializing fresh TPM/EFI/backup state is not recovery and may make encrypted guest data inaccessible."}
        result["warnings"].append(f"No persisted TPM/EFI/CBT state was found: KubeVirt would create fresh state ({klass}, {mode}). If this VM ran before, recover its original PVC first. Provisioning/size mutation is not guaranteed; the displayed claim name is only a placement placeholder.")
    except ValueError as error:
        result["blockers"].append(str(error))
    except Exception:
        result["blockers"].append("Persistent VM state dependencies could not be verified; check inventory permissions/connectivity")
    return result
