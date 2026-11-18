"""The validated-versions list built into this image (homestead_validated):
what Homestead uses offline, and until it downloads a newer signed list.

The scheduled workflow publishes newer lists apart from Homestead's releases;
this copy is refreshed from the published one when a release is made.
"""
# Distributions whose host versions Homestead validates (decided 2026-11-18,
# issue #1348): Ubuntu 26.04 first. Elsewhere host versions read as untested
# and host updates work as they always have; platform versions stay validated.
FIRST_TIER = ("ubuntu-26.04-amd64", "ubuntu-26.04-arm64")

LIST = {
    "generated": "2026-11-18T00:00:00Z",
    "host": {
        "ubuntu-26.04-amd64": [
            {"package": "linux-image-7.0.0-43-generic", "version": "7.0.0-43.43", "status": "bad",
             "affects": ["kubevirt"],
             "reason": "AppArmor stacks unprivileged profile changes, so QEMU in KubeVirt cannot signal its own threads "
                       "and every VM exits at start",
             "workaround": {"sysctl": {"kernel.apparmor_restrict_unprivileged_unconfined": "0"}},
             "evidence": "https://github.com/homestead-lab/homestead/issues/1348"},
        ],
    },
    "platform": {"k3s": [], "rke2": [], "longhorn": [], "kubevirt": [], "cdi": []},
    "images": {},
}
