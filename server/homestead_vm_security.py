"""Read-only confidential-guest admission, not attestation or an HA guarantee.

Rules follow KubeVirt v1.3.1 and v1.7.0-v1.9.0 launch-security validation,
util/amd64.go, node-selector/resource renderers and feature-gate definitions.
Unknown builds require verification rather than silently dropping encryption.
Never return session blobs, guest-owner certificates or attestation contents.
"""
import re


def evidence(spec, config, version=None):
    result = {"selectors": {}, "requests": {}, "blockers": [], "warnings": []}
    domain = spec.get("domain") or {}
    security = domain.get("launchSecurity")
    if security is None:
        return result
    blockers = result["blockers"]
    if not isinstance(security, dict) or set(security) - {"sev", "snp", "tdx"}:
        blockers.append("VM launch-security settings are unsupported or invalid")
        return result
    modes = [key for key, value in security.items() if value is not None]
    if len(modes) != 1 or not isinstance(security[modes[0]], dict):
        blockers.append("VM launch security requires exactly one supported mode: SEV, SNP or TDX; other policies need verified admission support")
        return result
    mode = modes[0]
    options = security[mode]
    allowed = {"policy", "attestation", "session", "dhCert"} if mode == "sev" else set()
    if set(options) - allowed:
        blockers.append("VM launch-security mode contains unsupported settings")
    policy = options.get("policy")
    if policy is not None and (not isinstance(policy, dict) or set(policy) - {"encryptedState"} or
                               (policy.get("encryptedState") is not None and type(policy["encryptedState"]) is not bool)):
        blockers.append("VM SEV encrypted-state policy is invalid")
        policy = None
    attestation = options.get("attestation")
    if attestation is not None and (not isinstance(attestation, dict) or attestation):
        blockers.append("VM SEV attestation settings are unsupported or invalid")
    if any(key in options and not isinstance(options[key], str) for key in ("session", "dhCert")):
        blockers.append("VM SEV session/certificate settings are invalid")

    # Labels and device pools are separate requirements. SNP also needs the
    # base SEV label/device; encrypted-state SEV adds the SEV-ES label.
    result["selectors"]["kubernetes.io/arch"] = "amd64"
    result["selectors"]["kubevirt.io/" + ("tdx" if mode == "tdx" else "sev")] = "true"
    result["requests"]["devices.kubevirt.io/" + ("tdx" if mode == "tdx" else "sev")] = 1
    if mode == "snp":
        result["selectors"]["kubevirt.io/sev-snp"] = "true"
    if mode == "sev" and (policy or {}).get("encryptedState") is True:
        result["selectors"]["kubevirt.io/sev-es"] = "true"
    if spec.get("architecture") != "amd64":
        blockers.append("Encrypted-guest admission requires an explicitly resolved amd64 architecture; resolve the VM profile/default before starting")

    match = re.fullmatch(r"v?1\.(\d+)\.\d+", version or "")
    minor = int(match[1]) if match else None
    known = minor is not None and 3 <= minor <= 9
    if not known:
        blockers.append("Encrypted-guest policy is unverified on this KubeVirt build or during its upgrade; verify supported admission before starting")
    elif mode in ("snp", "tdx") and minor < 7:
        blockers.append("SNP/TDX launch-security settings require KubeVirt 1.7 or newer")
    developer = config.get("developerConfiguration") or {}
    gates, disabled = developer.get("featureGates", []), developer.get("disabledFeatureGates", [])
    gates = [] if gates is None else gates
    disabled = [] if disabled is None else disabled
    if any(not isinstance(values, list) or any(not isinstance(value, str) for value in values) for values in (gates, disabled)):
        blockers.append("KubeVirt encryption feature-gate configuration is invalid")
    else:
        gate = "WorkloadEncryptionTDX" if mode == "tdx" else "WorkloadEncryptionSEV"
        # 1.9 SEV is Beta (default on); explicit enable precedes disable upstream.
        # Earlier versions are opt-in. Do not assume future/vendor defaults.
        enabled = gate in gates or (minor == 9 and mode != "tdx" and gate not in disabled)
        if not enabled:
            blockers.append(f"KubeVirt {gate} is not enabled for this encrypted guest")
    if developer.get("useEmulation") is True:
        blockers.append("Encrypted guests require hardware virtualization, not software emulation")
    if (config.get("hypervisor") or {}).get("name") not in (None, "", "kvm"):
        blockers.append("Encrypted-guest admission is only verified for the KVM hypervisor")

    firmware = domain.get("firmware") or {}
    bootloader = firmware.get("bootloader") or {}
    efi = bootloader.get("efi")
    if not isinstance(efi, dict) or bootloader.get("bios") is not None:
        blockers.append("Encrypted guests require UEFI firmware without a BIOS bootloader")
    else:
        if mode != "tdx" and efi.get("secureBoot") is not False:
            blockers.append("SEV/SNP guests require Secure Boot explicitly disabled")
        if mode in ("snp", "tdx") and efi.get("persistent") is not None and efi["persistent"] is not False:
            blockers.append("SNP/TDX guests cannot use persistent EFI variables")
    smm = (domain.get("features") or {}).get("smm")
    if mode == "tdx" and smm is not None and (not isinstance(smm, dict) or smm.get("enabled") is not False):
        blockers.append("TDX guests cannot enable System Management Mode (SMM)")
    if any(iface.get("bootOrder") is not None for iface in (domain.get("devices") or {}).get("interfaces") or []):
        blockers.append("Encrypted guests cannot boot from a network interface")
    if mode == "sev" and attestation is not None:
        if spec.get("startStrategy") != "Paused":
            blockers.append("SEV attestation requires the guest start strategy to be Paused")
        result["warnings"].append("SEV attestation must be completed separately before unpausing; this capacity review does not verify or inject guest secrets")
    result["warnings"].append("Encryption host labels and device capacity do not prove firmware trust, attestation, guest compatibility or migration/failover support")
    return result
