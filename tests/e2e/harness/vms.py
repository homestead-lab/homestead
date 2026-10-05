"""Real hosts for release tests: Ubuntu VMs under QEMU/KVM on one machine.

Containers cannot stand in for hosts here. A containerised node never gets a
new boot ID, has no systemd to power off, and cannot be pulled from the wall
- and those are exactly what reboots, shutdowns and rolling restarts are
about. A GitHub-hosted Linux runner exposes KVM, enough for three small VMs.

The VMs share a bridge on the runner (10.10.0.0/24, NAT for downloads), so
the runner reaches each one at its own address, as a person's LAN would.
The runner is the hand on the power button: it can press it (ACPI), pull the
plug, and power a host on again after a shutdown.
"""
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from . import log

NET = "10.10.0"
GATEWAY = f"{NET}.1"
BRIDGE = "hsbr0"
VIP = f"{NET}.100"
IMAGE_URL = os.environ.get("E2E_IMAGE_URL", "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img")
CACHE = Path(os.environ.get("E2E_CACHE", Path.home() / ".cache" / "homestead-e2e"))
PREPARED = CACHE / "noble-prepared.qcow2"
USER = "e2e"


def sh(*args, check=True, timeout=600, quiet=False):
    """Run a command on the runner."""
    if not quiet:
        log.debug("$ " + " ".join(str(a) for a in args))
    result = subprocess.run([str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(str(a) for a in args[:4])} failed ({result.returncode}): {result.stdout[-2000:]}")
    return result.stdout


class Node:
    def __init__(self, lab, index):
        self.lab, self.index = lab, index
        self.name = f"node-{index + 1}"
        self.ip = f"{NET}.{11 + index}"
        self.role = "new" if index == 0 else "server"     # as the installer joined it
        self.mac = f"52:54:00:10:00:{11 + index:02x}"
        self.tap = f"hstap{index}"
        self.dir = lab.dir / self.name
        self.process = None

    # ------------------------------------------------------------ power
    def power_on(self):
        """Start the VM, as pressing a powered-off host's button does."""
        if self.running:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        args = ["qemu-system-x86_64", "-enable-kvm", "-cpu", "host", "-machine", "q35",
                "-smp", str(self.lab.cpus), "-m", str(self.lab.memory), "-name", self.name,
                # The system disk boots first, whatever other disks there are.
                "-drive", f"file={self.dir / 'disk.qcow2'},if=none,id=root,cache=unsafe,discard=unmap",
                "-device", "virtio-blk-pci,drive=root,bootindex=0",
                "-drive", f"file={self.dir / 'seed.iso'},if=virtio,format=raw,readonly=on",
                *(["-drive", f"file={self.dir / 'data.qcow2'},if=none,id=data,cache=unsafe,discard=unmap",
                   "-device", "virtio-blk-pci,drive=data,serial=e2e-data"] if self.lab.data_disk else []),
                "-netdev", f"tap,id=lan,ifname={self.tap},script=no,downscript=no",
                "-device", f"virtio-net-pci,netdev=lan,mac={self.mac}",
                "-qmp", f"unix:{self.dir / 'qmp.sock'},server=on,wait=off",
                "-serial", f"file:{self.dir / f'console-{int(time.time())}.log'}",
                "-display", "none"]
        self.process = subprocess.Popen(args, stdout=open(self.dir / "qemu.log", "a"), stderr=subprocess.STDOUT)
        log.info(f"{self.name}: powered on")

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def qmp(self, command):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(str(self.dir / "qmp.sock"))
            s.recv(65536)
            for message in ({"execute": "qmp_capabilities"}, {"execute": command}):
                s.sendall(json.dumps(message).encode() + b"\n")
                s.recv(65536)

    def power_button(self):
        """An ACPI press: the OS shuts down in order, as a short press does."""
        self.qmp("system_powerdown")
        log.info(f"{self.name}: power button pressed")

    def pull_plug(self):
        """Power lost: no shutdown at all."""
        if self.running:
            self.qmp("quit")
            self.process.wait(30)
        log.info(f"{self.name}: power pulled")

    def wait_off(self, timeout=900):
        """Until the host has powered itself off - QEMU exits with it."""
        deadline = time.time() + timeout
        while self.running:
            if time.time() > deadline:
                raise TimeoutError(f"{self.name} did not power off within {timeout}s")
            time.sleep(2)
        log.info(f"{self.name}: powered off")

    # ------------------------------------------------------------ access
    def ssh(self, command, check=True, timeout=600, quiet=False):
        args = ["ssh", "-i", self.lab.key, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15",
                f"{USER}@{self.ip}", command]
        if not quiet:
            log.debug(f"{self.name}$ {command[:300]}")
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(f"{self.name}: `{command}` failed ({result.returncode}): {result.stdout[-3000:]}")
        return result.stdout

    def wait_ssh(self, timeout=600):
        deadline = time.time() + timeout
        while True:
            try:
                self.ssh("true", timeout=15, quiet=True)
                return
            except (RuntimeError, subprocess.TimeoutExpired):
                if time.time() > deadline:
                    raise TimeoutError(f"{self.name} did not answer SSH within {timeout}s")
                time.sleep(3)

    def boot_id(self):
        return self.ssh("cat /proc/sys/kernel/random/boot_id", quiet=True).strip()

    def copy_to(self, local, remote):
        sh("scp", "-i", self.lab.key, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           "-o", "LogLevel=ERROR", local, f"{USER}@{self.ip}:{remote}")


class Lab:
    """The VMs, their network, and the runner's hand on their power."""

    def __init__(self, directory, count, memory=4096, cpus=2, disk="40G", data_disk="", hugepages=0):
        """data_disk: a second, blank disk on each host (serial e2e-data), as
        a disk to give Longhorn. hugepages: 2 MiB pages reserved at every
        boot, before Kubernetes starts - as a host prepared for Longhorn V2."""
        self.dir = Path(directory)
        self.memory, self.cpus, self.disk = memory, cpus, disk
        self.data_disk, self.hugepages = data_disk, hugepages
        self.nodes = [Node(self, i) for i in range(count)]
        self.key = str(self.dir / "id_ed25519")

    def up(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        if not os.path.exists(self.key):
            sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", self.key)
        self._network()
        base = self._base_image()
        for node in self.nodes:
            node.dir.mkdir(parents=True, exist_ok=True)
            sh("qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", base, node.dir / "disk.qcow2", self.disk)
            if self.data_disk:
                sh("qemu-img", "create", "-q", "-f", "qcow2", node.dir / "data.qcow2", self.data_disk)
            self._seed(node)
            node.power_on()
        for node in self.nodes:
            node.wait_ssh()
            node.ssh("cloud-init status --wait >/dev/null 2>&1 || true", timeout=900)
            log.info(f"{node.name}: up at {node.ip}")

    def down(self):
        for node in self.nodes:
            if node.running:
                node.process.kill()

    def _network(self):
        user = os.environ.get("USER") or sh("id", "-un", quiet=True).strip()
        if BRIDGE not in sh("ip", "-o", "link", "show", quiet=True):
            sh("sudo", "ip", "link", "add", BRIDGE, "type", "bridge")
            sh("sudo", "ip", "addr", "add", f"{GATEWAY}/24", "dev", BRIDGE)
            sh("sudo", "ip", "link", "set", BRIDGE, "up")
            sh("sudo", "sysctl", "-qw", "net.ipv4.ip_forward=1")
            sh("sudo", "iptables", "-t", "nat", "-A", "POSTROUTING", "-s", f"{NET}.0/24", "!", "-d", f"{NET}.0/24", "-j", "MASQUERADE")
            # Docker on the runner drops forwarded traffic by default.
            sh("sudo", "iptables", "-I", "FORWARD", "-i", BRIDGE, "-j", "ACCEPT")
            sh("sudo", "iptables", "-I", "FORWARD", "-o", BRIDGE, "-j", "ACCEPT")
        for node in self.nodes:
            if node.tap not in sh("ip", "-o", "link", "show", quiet=True):
                sh("sudo", "ip", "tuntap", "add", node.tap, "mode", "tap", "user", user)
                sh("sudo", "ip", "link", "set", node.tap, "master", BRIDGE)
                sh("sudo", "ip", "link", "set", node.tap, "up")

    def _base_image(self):
        """The prepared image when the cache has it (prepare_base), else the
        cloud image as published - slower: each host updates itself."""
        if PREPARED.exists():
            return str(PREPARED)
        return self._cloud_image()

    def prepare_base(self):
        """Ubuntu updated, with the packages Homestead's installer adds, made
        once a week in CI and cached: a host then starts in seconds, and a
        rolling update has little to download. Cloud-init is reset, so each
        host made from it sets itself up as a new machine."""
        self.dir.mkdir(parents=True, exist_ok=True)
        if not os.path.exists(self.key):
            sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", self.key)
        self._network()
        node = self.nodes[0]
        node.dir.mkdir(parents=True, exist_ok=True)
        sh("qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", self._cloud_image(), node.dir / "disk.qcow2", "8G")
        self._seed(node, prepare=True)
        started = time.time()
        node.power_on()
        node.wait_off(timeout=1500)
        part = PREPARED.with_suffix(".part")
        sh("qemu-img", "convert", "-O", "qcow2", "-c", node.dir / "disk.qcow2", part, timeout=1200)
        part.rename(PREPARED)
        (CACHE / "noble-server-cloudimg-amd64.img").unlink(missing_ok=True)     # not cached twice
        log.info(f"Base image prepared in {int(time.time() - started)}s: {PREPARED}")

    def _cloud_image(self):
        CACHE.mkdir(parents=True, exist_ok=True)
        image = CACHE / "noble-server-cloudimg-amd64.img"
        if not image.exists():
            log.info(f"Downloading {IMAGE_URL}")
            part = image.with_suffix(".part")
            with urllib.request.urlopen(IMAGE_URL, timeout=600) as response, open(part, "wb") as out:
                shutil.copyfileobj(response, out)
            part.rename(image)
        return str(image)

    def _seed(self, node, prepare=False):
        public = Path(self.key + ".pub").read_text().strip()
        (node.dir / "user-data").write_text(f"""#cloud-config
hostname: {node.name}
fqdn: {node.name}
manage_etc_hosts: true
users:
  - name: {USER}
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys: ["{public}"]
growpart: {{mode: auto, devices: ["/"]}}
bootcmd:
  # Kernel messages - out of memory, hung tasks, network - reach the serial
  # console the diagnostics keep, even when the host stops answering.
  - sysctl -w kernel.printk="7 4 1 7"
""" + (f"""  - sysctl -w vm.nr_hugepages={self.hugepages}
""" if self.hugepages and not prepare else "") + """runcmd:
  - systemctl disable --now unattended-upgrades apt-daily.timer apt-daily-upgrade.timer || true
  - systemctl enable --now iscsid || true
  # The host's addresses and routes on the console every 30 seconds: when it
  # stops answering, the diagnostics still show what its network became.
  # Through the kernel log: the login prompt on ttyS0 drops other writers.
  - systemd-run --unit=e2e-netlog sh -c 'while sleep 30; do { echo "== net"; ip -br addr; ip route; getent hosts github.com || echo "dns: github.com no answer"; } | while read -r l; do echo "netlog: $l" > /dev/kmsg; done; done' 
""" + ("""  - DEBIAN_FRONTEND=noninteractive apt-get -q update
  - DEBIAN_FRONTEND=noninteractive apt-get -yq -o Dpkg::Options::=--force-confold dist-upgrade
  - DEBIAN_FRONTEND=noninteractive apt-get -yq install open-iscsi nfs-common
  - apt-get -q clean
  - cloud-init clean --logs --machine-id
  - fstrim -av || true
  - systemctl poweroff
""" if prepare else ""))
        (node.dir / "meta-data").write_text(f"instance-id: {node.name}-{int(time.time())}\nlocal-hostname: {node.name}\n")
        (node.dir / "network-config").write_text(f"""version: 2
ethernets:
  lan:
    match: {{macaddress: "{node.mac}"}}
    set-name: eth0
    addresses: [{node.ip}/24]
    routes: [{{to: default, via: {GATEWAY}}}]
    nameservers: {{addresses: [1.1.1.1, 8.8.8.8]}}
""")
        sh("cloud-localds", "-N", node.dir / "network-config", node.dir / "seed.iso", node.dir / "user-data", node.dir / "meta-data")
