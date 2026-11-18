#!/usr/bin/env python3
"""Validate a kernel or platform version the way people install Homestead
(issue #1348, section 5.4).

On a CI runner with /dev/kvm: boot a fresh Ubuntu minimal cloud image, optionally
with a candidate kernel installed and booted, install Homestead with the
published installer and its defaults (k3s, Longhorn, KubeVirt), and run the
smoke checks:

- k3s and Homestead Ready;
- a Longhorn ReadWriteOnce and a ReadWriteMany volume written and read back;
- a KubeVirt VM started and still running a minute later, with no crash
  event: what a kernel that breaks QEMU (#1348's AppArmor stacking) fails;
- an unclean restart (sysrq, no drain), then Homestead and both volumes back
  without help.

The KubeVirt VM is a third level of virtualisation on a CI runner, so it runs
in KubeVirt's emulation mode: QEMU, its threads and the host's AppArmor
profile are the same, which is what the check is for.

Writes results.json: each check, passed or failed, with what it saw, and the
features a failure affects. Exits 1 when a check failed.

    python3 tests/integration/validation_vm.py [--kernel PACKAGE=VERSION] [--release resolute]
"""
import argparse
import json
import os
import pathlib
import shlex
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request

# Ubuntu minimal, as Homestead recommends (Installing on k3s).
IMAGE = "https://cloud-images.ubuntu.com/minimal/daily/{release}/current/{release}-minimal-cloudimg-amd64.img"
INSTALLER = "https://raw.githubusercontent.com/homestead-lab/homestead/main/scripts/install.sh"
SSH_PORT = 2222
AFFECTS = {"homestead": ["homestead"], "volumes": ["longhorn"], "vm": ["kubevirt"], "unclean-restart": ["homestead", "longhorn"]}


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class Guest:
    def __init__(self, work, key):
        self.work, self.key = work, key

    def ssh(self, command, timeout=600, check=True):
        done = subprocess.run(["ssh", "-i", str(self.key), "-p", str(SSH_PORT), "-o", "StrictHostKeyChecking=no",
                               "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR",
                               "ubuntu@127.0.0.1", command], capture_output=True, text=True, timeout=timeout)
        if check and done.returncode != 0:
            raise RuntimeError(f"{command[:80]}: exit {done.returncode}: {(done.stderr or done.stdout)[-600:]}")
        return done.stdout

    def kubectl(self, args, **options):
        return self.ssh("sudo k3s kubectl " + args, **options)

    def wait_ssh(self, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                if "up" in self.ssh("echo up", timeout=20, check=False):
                    return
            except subprocess.TimeoutExpired:
                pass
            time.sleep(5)
        raise RuntimeError("the guest did not answer on SSH")

    def wait(self, what, check, seconds, every=10):
        deadline, last = time.time() + seconds, ""
        while time.time() < deadline:
            try:
                last = check()
                if last is True:
                    return
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                last = str(error)[-300:]
            time.sleep(every)
        raise RuntimeError(f"{what} did not happen within {seconds // 60} minutes; last seen: {last}")


def boot(work, release, kernel):
    """The cloud image, a cloud-init seed, and QEMU started with KVM."""
    base = work / f"{release}.img"
    if not base.exists():
        log(f"downloading the {release} cloud image")
        urllib.request.urlretrieve(IMAGE.format(release=release), base)
    disk = work / "disk.qcow2"
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(base), str(disk), "60G"], check=True)
    key = work / "key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    runcmd = ["echo 1 > /proc/sys/kernel/sysrq"]
    if kernel:
        package, version = kernel.split("=", 1)
        abi = package[len("linux-image-"):]
        runcmd += [f"apt-get update -qq",
                   f"DEBIAN_FRONTEND=noninteractive apt-get install -y -qq {package}={version} linux-modules-{abi}={version}",
                   "touch /var/lib/cloud/candidate-installed", "reboot"]
    user_data = "#cloud-config\n" + json.dumps({
        "users": [{"name": "ubuntu", "sudo": "ALL=(ALL) NOPASSWD:ALL", "shell": "/bin/bash",
                   "ssh_authorized_keys": [key.with_suffix(".pub").read_text().strip()]}],
        "growpart": {"mode": "auto", "devices": ["/"]}, "runcmd": runcmd})
    (work / "user-data").write_text(user_data)
    (work / "meta-data").write_text("instance-id: homestead-validation\nlocal-hostname: node-1\n")
    seed = work / "seed.img"
    subprocess.run(["cloud-localds", str(seed), str(work / "user-data"), str(work / "meta-data")], check=True)
    cpus = max(2, os.cpu_count() or 2)
    memory = int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 2 * 0.8)
    log(f"booting with {cpus} CPUs and {memory} MiB")
    qemu = subprocess.Popen(["qemu-system-x86_64", "-enable-kvm", "-cpu", "host", "-smp", str(cpus), "-m", str(memory),
                             "-drive", f"file={disk},if=virtio", "-drive", f"file={seed},if=virtio,format=raw",
                             "-netdev", f"user,id=n0,hostfwd=tcp:127.0.0.1:{SSH_PORT}-:22", "-device", "virtio-net-pci,netdev=n0",
                             "-display", "none", "-serial", f"file:{work / 'console.log'}"])
    return qemu, Guest(work, key)


def install(guest, kernel):
    if kernel:
        package = kernel.split("=", 1)[0]
        guest.wait("the candidate kernel's boot",
                   lambda: package[len("linux-image-"):] in guest.ssh("test -f /var/lib/cloud/candidate-installed && uname -r", check=False) or "not yet",
                   1500, every=15)
    log("running the installer, as a person would")
    env = "HS_ROLE=new HS_KUBEVIRT=yes HS_YES=1"
    guest.ssh(f"curl -sfL {INSTALLER} -o /tmp/install.sh && sudo {env} sh /tmp/install.sh > /tmp/install.log 2>&1 "
              "|| (tail -n 60 /tmp/install.log; exit 1)", timeout=3600)


def homestead_ready(guest):
    out = guest.kubectl("-n lab get deploy homestead -o jsonpath='{.status.readyReplicas}'", check=False).strip()
    return True if out == "1" else f"homestead ready replicas: {out or 'none'}"


VOLUMES = """
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: smoke-rwo, namespace: default}
spec: {accessModes: [ReadWriteOnce], storageClassName: longhorn, resources: {requests: {storage: 1Gi}}}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: smoke-rwx, namespace: default}
spec: {accessModes: [ReadWriteMany], storageClassName: longhorn, resources: {requests: {storage: 1Gi}}}
---
apiVersion: v1
kind: Pod
metadata: {name: smoke-writer, namespace: default}
spec:
  restartPolicy: Never
  containers:
  - name: w
    image: busybox:1.36
    command: ["sh", "-c", "cat /rwo/mark /rwx/mark 2>/dev/null || (echo written > /rwo/mark && echo written > /rwx/mark && sync && cat /rwo/mark /rwx/mark)"]
    volumeMounts: [{name: rwo, mountPath: /rwo}, {name: rwx, mountPath: /rwx}]
  volumes:
  - {name: rwo, persistentVolumeClaim: {claimName: smoke-rwo}}
  - {name: rwx, persistentVolumeClaim: {claimName: smoke-rwx}}
"""

VM = """
apiVersion: kubevirt.io/v1
kind: VirtualMachineInstance
metadata: {name: smoke-vm, namespace: default}
spec:
  domain:
    devices: {disks: [{name: root, disk: {bus: virtio}}]}
    resources: {requests: {memory: 128Mi}}
  volumes:
  - name: root
    containerDisk: {image: quay.io/kubevirt/cirros-container-disk-demo:latest}
"""


def apply(guest, manifest):
    guest.ssh("sudo k3s kubectl apply -f - <<'EOF'\n" + textwrap.dedent(manifest) + "\nEOF")


def volumes_written(guest):
    phase = guest.kubectl("-n default get pod smoke-writer -o jsonpath='{.status.phase}'", check=False).strip()
    if phase != "Succeeded":
        return f"writer pod: {phase or 'not there'}"
    out = guest.kubectl("-n default logs smoke-writer")
    return True if out.split() == ["written", "written"] else f"read back: {out.strip()!r}"


def vm_running(guest, since):
    phase = guest.kubectl("-n default get vmi smoke-vm -o jsonpath='{.status.phase}'", check=False).strip()
    crashed = guest.kubectl("-n default get events --field-selector involvedObject.name=smoke-vm "
                            "-o jsonpath='{range .items[*]}{.reason}: {.message}{\"\\n\"}{end}'", check=False)
    if "crashed" in crashed.lower() or phase == "Failed":
        raise RuntimeError(f"the VM crashed: {crashed[-600:]}")
    if phase != "Running":
        return f"VM phase: {phase or 'not there'}"
    return True if time.time() - since >= 60 else "running, watching for a minute"


def check(results, name, fn):
    started = time.time()
    try:
        fn()
        results.append({"check": name, "passed": True, "seconds": int(time.time() - started)})
        log(f"PASS {name}")
    except Exception as error:
        results.append({"check": name, "passed": False, "seconds": int(time.time() - started),
                        "detail": str(error)[-1500:], "affects": AFFECTS.get(name, [])})
        log(f"FAIL {name}: {error}")


def run(args):
    work = pathlib.Path(args.work or tempfile.mkdtemp(prefix="homestead-validation-"))
    work.mkdir(parents=True, exist_ok=True)
    results, qemu = [], None
    try:
        qemu, guest = boot(work, args.release, args.kernel)
        guest.wait_ssh(900)
        check(results, "install", lambda: install(guest, args.kernel))
        if not results[-1]["passed"]:
            return results, work
        check(results, "homestead", lambda: guest.wait("Homestead Ready", lambda: homestead_ready(guest), 1800))
        check(results, "volumes", lambda: (apply(guest, VOLUMES),
                                           guest.wait("both volumes written", lambda: volumes_written(guest), 900)))
        since = time.time()
        check(results, "vm", lambda: (apply(guest, VM), guest.wait("the VM running a minute", lambda: vm_running(guest, since), 1200)))

        def unclean():
            guest.ssh("sudo sh -c 'echo 1 > /proc/sys/kernel/sysrq; echo b > /proc/sysrq-trigger'", timeout=20, check=False)
            time.sleep(20)
            guest.wait_ssh(900)
            guest.wait("Homestead Ready after the restart", lambda: homestead_ready(guest), 1800)
            guest.kubectl("-n default delete pod smoke-writer --wait=true", timeout=300, check=False)
            apply(guest, VOLUMES)
            guest.wait("both volumes read back after the restart", lambda: volumes_written(guest), 1200)
        check(results, "unclean-restart", unclean)
        try:
            (work / "cluster.txt").write_text(guest.kubectl("get nodes,pods -A -o wide", check=False)
                                              + guest.ssh("uname -r; sudo k3s --version", check=False))
        except Exception:
            pass
        return results, work
    except Exception as error:
        results.append({"check": "boot", "passed": False, "detail": str(error)[-1500:], "affects": ["homestead"]})
        return results, work
    finally:
        if qemu:
            qemu.terminate()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kernel", default="", help="a candidate kernel, as linux-image-<abi>=<version>")
    parser.add_argument("--release", default="resolute", help="the Ubuntu cloud image release (26.04: resolute)")
    parser.add_argument("--work", default="")
    args = parser.parse_args()
    if args.kernel and not args.kernel.startswith("linux-image-"):
        sys.exit("--kernel is linux-image-<abi>=<version>")
    results, work = run(args)
    report = {"release": args.release, "kernel": args.kernel, "passed": all(r["passed"] for r in results),
              "checks": results, "run": os.environ.get("GITHUB_SERVER_URL", "") + "/" + os.environ.get("GITHUB_REPOSITORY", "")
              + "/actions/runs/" + os.environ.get("GITHUB_RUN_ID", "")}
    (work / "results.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
