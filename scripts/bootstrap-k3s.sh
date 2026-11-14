#!/bin/sh
# Turn a bare Linux machine into a k3s (or RKE2) cluster running Homestead -
# or join another machine to one.
#
#   New cluster, on the first machine:
#     curl -sfL https://raw.githubusercontent.com/homestead-lab/homestead/main/scripts/bootstrap-k3s.sh | sudo sh -s - server
#
#   Another machine, as a worker (the token is in /var/lib/rancher/k3s/server/node-token on the first):
#     curl -sfL .../bootstrap-k3s.sh | sudo sh -s - agent https://<first-machine>:6443 <token> --longhorn-data-path /mnt/longhorn-os
#
#   Another machine, as a second or third server (control plane and etcd):
#     curl -sfL .../bootstrap-k3s.sh | sudo sh -s - join https://<first-machine>:6443 <token> --longhorn-data-path /mnt/longhorn-os
#
#   A k3s (or RKE2) server that is already running, to add Longhorn and Homestead to it:
#     curl -sfL .../bootstrap-k3s.sh | sudo sh -s - addons
#
#   RKE2 instead of k3s - full upstream Kubernetes, hardened, what Harvester
#   runs on - add --rke2 to any of these. RKE2's servers are joined on 9345:
#     curl -sfL .../bootstrap-k3s.sh | sudo sh -s - server --rke2
#     curl -sfL .../bootstrap-k3s.sh | sudo sh -s - agent https://<first-machine>:9345 <token> --rke2 --longhorn-data-path /mnt/longhorn-os
#   (the token is in /var/lib/rancher/rke2/server/node-token on the first).
#
# For a guided install that asks these questions and checks the machine
# first, use install.sh instead.
#
# Options for "server", after the word:
#   --no-longhorn        use k3s's local-path storage instead of Longhorn: no
#                        replicas, and Homestead's data lives on this machine
#                        (k3s only - RKE2 has no storage of its own)
#   --kubevirt           also install KubeVirt and CDI, for virtual machines
#                        (emulated, and slow, if this machine has no /dev/kvm)
#   --k3s-version v1.31.4+k3s1   pin k3s (default: k3s's stable channel)
#   --rke2-version v1.33.4+rke2r1  pin RKE2 (default: RKE2's stable channel)
#   --longhorn-version v1.9.1    pin Longhorn (default: the newest chart)
#   --kubevirt-version v1.6.0    pin KubeVirt (default: KubeVirt's stable release)
#   --cdi-version v1.62.0        pin CDI (default: the newest release)
#   --homestead-version 2.8.95   pin Homestead (default: the newest release)
#   --no-kube-vip        leave out kube-vip: apps stay on the nodes' own
#                        addresses, with no VIP that moves between nodes
#   --no-multus          leave out Multus: no LAN address of their own for
#                        VMs and containers
#   --kube-vip-version 0.11.1    pin kube-vip's chart (default: the one
#                                Homestead has tested)
#   --multus-version v4.3.102    pin RKE2's Multus chart (default: likewise)
#   --vip 192.0.2.200  an unused LAN address for Homestead and apps: once
#                        kube-vip is up, Homestead reserves it, makes it the
#                        apps' default, and puts itself, its backup storage
#                        and shares on it beside the nodes' own addresses
#   --no-node-probe      leave out the node probe: no temperatures, SMART,
#                        or per-node network facts until it is added under
#                        Settings > Cluster > Add-ons
#   --node-probe         install it. Saying neither keeps what this cluster
#                        chose before (homestead-install outlives a
#                        reinstall that keeps Homestead's data), else installs it
# Options for every mode:
#   --rke2                   RKE2 instead of k3s
#   --longhorn-volume auto|none|200   where the system is on LVM, give
#                            replica data a volume at /mnt/longhorn-os,
#                            of that many GB (auto: the volume group's free
#                            space, less a tenth kept for the system), so it
#                            can never fill the system's filesystem
#   --longhorn-data-path /mnt/longhorn-os|/var/lib/longhorn
#                            required when joining: match the existing cluster's
#                            default-data-path setting; new clusters use the
#                            first path, leaving runtime files on the OS
#   --node-ip 192.0.2.50   the address the cluster registers this machine
#                            by, when it has more than one
#
# What "server" does:
#   1. raises the host's inotify limits and caps its journal, installs
#      what Longhorn needs on the host (open-iscsi, NFS client), keeps
#      multipathd off the devices Longhorn makes, and - where the system is
#      on LVM with room - gives Longhorn a volume of its own;
#   2. installs k3s with an embedded etcd, so more servers can join later
#      (or RKE2, which always has one, with its ServiceLB turned on so apps
#      get the nodes' addresses as they do on k3s);
#   3. applies a HelmChart for Longhorn and Homestead's manifest, once - kept
#      in /var/lib/homestead/install, not k3s's auto-deploy folder, which k3s
#      re-applies at every start and so would undo later upgrades;
#   4. asks Homestead, in its manifest, to install kube-vip (VIPs), Multus
#      (a VM's or container's own LAN address) and the node probe once it is
#      up - Homestead installs and upgrades them, as it does from Settings >
#      Cluster > Add-ons;
#   5. waits for Homestead, takes the first etcd snapshot - k3s takes one
#      only every 12 hours, and a cluster with none cannot be restored - and
#      prints its address.
# It is safe to run again: each step finds what the last run left.
set -eu

MODE="${1:-}"; [ $# -gt 0 ] && shift
LONGHORN=1
KUBEVIRT=0
DIST=k3s
K3S_VERSION=""
RKE2_VERSION=""
LONGHORN_VERSION=""
KUBEVIRT_VERSION=""
CDI_VERSION=""
HOMESTEAD_VERSION=""
KUBE_VIP=1
MULTUS=1
KUBE_VIP_VERSION=""
MULTUS_VERSION=""
NODE_PROBE=""
VIP=""
LONGHORN_VOLUME=auto
LONGHORN_DATA_PATH=""
NODE_IP=""
JOIN_TAINT=""
JOIN_LABEL=""
TIEBREAKER=0
RAW=https://raw.githubusercontent.com/homestead-lab/homestead

say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nerror: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || fail "run as root (sudo)"
command -v curl >/dev/null 2>&1 || fail "curl is needed"

host_packages() {
  longhorn_layout
  # Longhorn mounts volumes over iSCSI and serves shared (RWX) volumes over NFS.
  say "Installing host packages for Longhorn (open-iscsi, NFS client)"
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq open-iscsi nfs-common
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q iscsi-initiator-utils nfs-utils
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install -y open-iscsi nfs-client
  else
    echo "  Unknown package manager. Install open-iscsi and an NFS client manually."
  fi
  systemctl enable --now iscsid >/dev/null 2>&1 || true
  modprobe iscsi_tcp 2>/dev/null || true
  host_multipath
  host_longhorn_volume
}

# Existing hosts keep their layout. A joining host must use the cluster's
# configured default path; a join token cannot read Longhorn's settings.
longhorn_layout() {
  [ "$TIEBREAKER" = 1 ] && { LONGHORN_VOLUME=none; return 0; }
  [ -r /etc/fstab ] || fail "Cannot inspect fstab; existing storage must be checked before installation."
  for configured in /var/lib/longhorn /mnt/longhorn-os; do
    if awk -v p="$configured" '$1 !~ /^#/ && $2 == p {found=1} END {exit !found}' /etc/fstab \
       && ! mountpoint -q "$configured" 2>/dev/null; then
      fail "$configured is configured in fstab but is not mounted. Restore that mount before continuing."
    fi
  done
  for point in /var/lib/longhorn /mnt/longhorn-os; do
    if mountpoint -q "$point" 2>/dev/null || [ -n "$(ls -A "$point" 2>/dev/null)" ] \
       || awk -v p="$point" '$1 !~ /^#/ && $2 == p {found=1} END {exit !found}' /etc/fstab; then
      # Runtime on the OS is expected when the separate data mount exists.
      if [ "$point" = /var/lib/longhorn ] && { mountpoint -q /mnt/longhorn-os 2>/dev/null \
         || [ -n "$(ls -A /mnt/longhorn-os 2>/dev/null)" ]; }; then
        mountpoint -q /var/lib/longhorn 2>/dev/null \
          && fail "Both Longhorn locations are in use. Inspect the existing layout before running the installer."
        continue
      fi
      [ -z "$LONGHORN_DATA_PATH" ] || [ "$LONGHORN_DATA_PATH" = "$point" ] \
        || fail "Existing Longhorn storage at $point is preserved. Use host storage maintenance to change its layout."
      LONGHORN_DATA_PATH="$point"
      return 0
    fi
  done
  if [ -z "$LONGHORN_DATA_PATH" ]; then
    case "$MODE" in
      agent|join) fail "Joining a cluster requires --longhorn-data-path: use its Longhorn default-data-path setting (/mnt/longhorn-os for new Homestead clusters, /var/lib/longhorn for older installs). No storage has been changed." ;;
      *) LONGHORN_DATA_PATH=/mnt/longhorn-os ;;
    esac
  fi
}

# Longhorn's replica data in a filesystem of its own, so a volume filling up can
# never fill the system's: where the root filesystem is on LVM and the group
# has room, a logical volume mounted at /mnt/longhorn-os before Longhorn
# first starts. Runtime files remain at /var/lib/longhorn on the OS. A tenth of the group (at
# least 10 GB) stays free for the system to grow into; asked for less, the
# rest stays free for the V2 engine too. As Homestead mounts disks: fstab by
# UUID with nofail, and the empty folder locked, so a machine whose volume
# will not mount still starts and nothing lands on the system disk instead.
host_longhorn_volume() {
  [ "$TIEBREAKER" = 1 ] && return 0
  [ -n "$LONGHORN_DATA_PATH" ] || fail "Longhorn's data path has not been selected"
  point=$LONGHORN_DATA_PATH
  mountpoint -q "$point" 2>/dev/null && return 0
  if awk -v p="$point" '$1 !~ /^#/ && $2 == p {found=1} END {exit !found}' /etc/fstab; then
    fail "$point is configured in fstab but is not mounted. Restore that mount before continuing."
  fi
  if [ -n "$(ls -A "$point" 2>/dev/null)" ] || [ -L "$point" ]; then
    echo "  $point holds data already; Longhorn stays where it is."
    return 0
  fi
  [ "$LONGHORN_VOLUME" = none ] && return 0
  command -v lvcreate >/dev/null 2>&1 || return 0
  root=$(findmnt -n -o SOURCE / 2>/dev/null || true)
  vg=$(lvs --noheadings -o vg_name,lv_path,lv_dm_path 2>/dev/null </dev/null | awk -v r="$root" '$2 == r || $3 == r {print $1; exit}')
  [ -n "$vg" ] || return 0
  if lvs "$vg/longhorn" >/dev/null 2>&1 </dev/null; then
    fail "$vg/longhorn exists but is not mounted at $point. It is left intact; inspect it before continuing."
  fi
  size=$(vgs --noheadings --units g --nosuffix -o vg_size "$vg" </dev/null | awk '{print int($1)}')
  free=$(vgs --noheadings --units g --nosuffix -o vg_free "$vg" </dev/null | awk '{print int($1)}')
  reserve=$((size / 10)); [ "$reserve" -lt 10 ] && reserve=10
  usable=$((free - reserve))
  want=$LONGHORN_VOLUME; [ "$want" = auto ] && want=$usable
  [ "$want" -gt "$usable" ] && want=$usable
  if [ "$want" -lt 20 ]; then
    echo "  $vg has $free GB free, $reserve GB of it kept for the system: too little for a Longhorn volume of its own."
    return 0
  fi
  echo "  Longhorn replica data: $vg/longhorn, $want GB, at $point ($((free - want)) GB left free in $vg)"
  # Once allocation starts, failure requires inspection. Never silently use
  # the OS filesystem instead of the dedicated volume someone selected.
  if ! lvcreate -y -L "${want}G" -n longhorn "$vg" >/dev/null </dev/null; then
    fail "Could not make $vg/longhorn. Inspect LVM before retrying."
  fi
  udevadm settle 2>/dev/null || true
  if ! mkfs.ext4 -q -F -L hs-longhorn "/dev/$vg/longhorn"; then
    fail "Could not format the new $vg/longhorn. It is retained for inspection."
  fi
  uuid=$(blkid -s UUID -o value "/dev/$vg/longhorn")
  mkdir -p "$point"
  chattr +i "$point" 2>/dev/null || fail "Cannot protect the empty mount point $point; inspect the new LV before retrying."
  cp /etc/fstab /etc/fstab.homestead-backup
  echo "UUID=$uuid $point ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2" >> /etc/fstab
  if ! mount "$point"; then
    cp /etc/fstab.homestead-backup /etc/fstab
    chattr -i "$point" 2>/dev/null || true
    fail "$vg/longhorn would not mount. The new LV is retained and fstab restored; inspect it before retrying."
  fi
}

# Longhorn's volumes reach a host as plain /dev/sd* disks, and multipathd -
# on by default on Ubuntu Server - claims every one it sees, after which the
# volume's mount fails as "already mounted or mount point busy". Longhorn
# asks for sd devices to be kept from it; a host that boots from a multipath
# device is left as it is. Homestead does the same on nodes that joined
# before this.
host_multipath() {
  systemctl is-active --quiet multipathd 2>/dev/null || return 0
  grep -qs 'devnode "\^sd\[a-z0-9\]+"' /etc/multipath.conf && return 0
  if for m in $(findmnt -rn -o SOURCE 2>/dev/null | grep '^/dev/'); do lsblk -s -n -o TYPE "$m" 2>/dev/null; done | grep -q mpath; then
    echo "  A filesystem here is mounted from a multipath device; /etc/multipath.conf is left as it is."
    return 0
  fi
  echo "  Keeping multipathd off Longhorn's devices (/etc/multipath.conf)"
  [ -f /etc/multipath.conf ] && cp /etc/multipath.conf /etc/multipath.conf.homestead-backup
  grep -qs '^blacklist *{' /etc/multipath.conf || printf 'blacklist {\n}\n' >> /etc/multipath.conf
  sed -i '/^blacklist *{/a\    devnode "^sd[a-z0-9]+"' /etc/multipath.conf
  systemctl restart multipathd 2>/dev/null || true
  # Maps it made already, on disks nothing has mounted, are let go.
  multipath -F >/dev/null 2>&1 || true
}

# The first etcd snapshot, now: k3s and RKE2 take one only every 12 hours,
# and until then there is nothing to restore the cluster from.
first_snapshot() {
  [ -d "/var/lib/rancher/$DIST/server/db/etcd" ] || return 0
  ls "/var/lib/rancher/$DIST/server/db/snapshots" 2>/dev/null | grep -q . && return 0
  say "Taking the first etcd snapshot"
  "$DIST" etcd-snapshot save --name homestead-install >/dev/null 2>&1 \
    || echo "  The snapshot did not complete; $DIST takes one within 12 hours, or run: $DIST etcd-snapshot save"
}

# Every file watcher is an inotify instance, and a host allows each user 128:
# on a Kubernetes node nearly everything runs as root, so a busy node runs
# out and the next thing that watches files fails to start (macvtap did).
# Raised - never lowered - now and for every boot. Homestead does the same
# on nodes that joined before this.
host_limits() {
  i=$(cat /proc/sys/fs/inotify/max_user_instances); w=$(cat /proc/sys/fs/inotify/max_user_watches)
  [ "$i" -lt 8192 ] && i=8192
  [ "$w" -lt 524288 ] && w=524288
  printf 'fs.inotify.max_user_instances = %s\nfs.inotify.max_user_watches = %s\n' "$i" "$w" > /etc/sysctl.d/90-homestead.conf
  sysctl -q -w fs.inotify.max_user_instances="$i" fs.inotify.max_user_watches="$w" || true
  host_journal
}

# The systemd journal, capped at 1 GB unless someone set a cap already:
# journald's own default is a tenth of the filesystem, up to 4 GB, and a
# chatty node's logs are not what should fill the system disk.
host_journal() {
  grep -qs '^SystemMaxUse=' /etc/systemd/journald.conf /etc/systemd/journald.conf.d/*.conf && return 0
  [ -d /etc/systemd ] || return 0
  mkdir -p /etc/systemd/journald.conf.d
  printf '[Journal]\nSystemMaxUse=1G\n' > /etc/systemd/journald.conf.d/90-homestead.conf
  systemctl restart systemd-journald 2>/dev/null || true
}

# KubeVirt and CDI from their newest releases, dropped into the manifests
# folder like the rest; each one's switch goes in once its CRD is there.
install_kubevirt() {
  say "Installing KubeVirt and CDI"
  # Re-running the installer must not take over an existing Helm/operator
  # installation or reset its configuration. Older manifest installs can be
  # reviewed and enabled under Settings > Updates instead.
  KV_PRESENT=$($KUBECTL get crd kubevirts.kubevirt.io --ignore-not-found -o name) || fail "Could not inspect KubeVirt."
  CDI_PRESENT=$($KUBECTL get crd cdis.cdi.kubevirt.io --ignore-not-found -o name) || fail "Could not inspect CDI."
  KV_RELEASES=https://github.com/kubevirt/kubevirt/releases/download
  CDI_RELEASES=https://github.com/kubevirt/containerized-data-importer/releases
  KV="$KUBEVIRT_VERSION"
  [ -n "$KV" ] || KV=$(curl -sfL https://storage.googleapis.com/kubevirt-prow/release/kubevirt/kubevirt/stable.txt) \
    || fail "Could not retrieve the current KubeVirt release."
  CDI="$CDI_VERSION"
  # GitHub redirects .../releases/latest to the newest release's tag; its API reports it too.
  [ -n "$CDI" ] || CDI=$(curl -sfLI -o /dev/null -w '%{url_effective}' "$CDI_RELEASES/latest" | sed 's|.*/||')
  case "$CDI" in v*) ;; *) CDI=$(curl -sfL https://api.github.com/repos/kubevirt/containerized-data-importer/releases/latest \
    | sed -n 's/.*"tag_name": *"\(v[^"]*\)".*/\1/p' | head -n 1) ;; esac
  case "$KV" in v*) ;; *) KV="v$KV" ;; esac
  case "$CDI" in v*) ;; *) fail "Could not determine the CDI release ($CDI)." ;; esac
  echo "  KubeVirt $KV, CDI $CDI"
  if [ -z "$KV_PRESENT" ]; then
    curl -sfL "$KV_RELEASES/$KV/kubevirt-operator.yaml" -o "$MANIFESTS/kubevirt-operator.yaml" \
      || fail "Could not download KubeVirt $KV."
    apply "$MANIFESTS/kubevirt-operator.yaml"
  fi
  if [ -z "$CDI_PRESENT" ]; then
    curl -sfL "$CDI_RELEASES/download/$CDI/cdi-operator.yaml" -o "$MANIFESTS/cdi-operator.yaml" \
      || fail "Could not download CDI $CDI."
    apply "$MANIFESTS/cdi-operator.yaml"
  fi
  modprobe kvm_intel 2>/dev/null || modprobe kvm_amd 2>/dev/null || true
  EMULATION=""
  if [ ! -e /dev/kvm ]; then
    EMULATION="      useEmulation: true"
    echo "  Hardware virtualisation (/dev/kvm) is not available: KubeVirt will use emulation."
  fi
  if [ -z "$KV_PRESENT" ]; then
  wait_crd kubevirts.kubevirt.io
  cat > "$MANIFESTS/kubevirt-cr.yaml" <<KUBEVIRT_CR
apiVersion: kubevirt.io/v1
kind: KubeVirt
metadata:
  name: kubevirt
  namespace: kubevirt
  annotations:
    homestead.io/operator-install: manifests
spec:
  certificateRotateStrategy: {}
  customizeComponents: {}
  imagePullPolicy: IfNotPresent
  workloadUpdateStrategy: {}
  configuration:
    developerConfiguration:
      featureGates: []
$EMULATION
KUBEVIRT_CR
  apply "$MANIFESTS/kubevirt-cr.yaml"
  else
    say "KubeVirt is already installed; keeping its current manager and settings"
  fi
  if [ -z "$CDI_PRESENT" ]; then
  wait_crd cdis.cdi.kubevirt.io
  cat > "$MANIFESTS/cdi-cr.yaml" <<'CDI_CR'
apiVersion: cdi.kubevirt.io/v1beta1
kind: CDI
metadata:
  name: cdi
  annotations:
    homestead.io/operator-install: manifests
spec:
  imagePullPolicy: IfNotPresent
  config:
    featureGates: [HonorWaitForFirstConsumer]
CDI_CR
  apply "$MANIFESTS/cdi-cr.yaml"
  else
    say "CDI is already installed; keeping its current manager and settings"
  fi
}

wait_crd() {
  i=0; until $KUBECTL get crd "$1" >/dev/null 2>&1; do
    i=$((i+1)); if [ $i -gt 60 ]; then echo "  $1 is not available yet; $DIST will retry."; return 0; fi; sleep 3; done
}

# The release a channel points at, asked of k3s's or RKE2's update server
# with retries. Their own install scripts ask once, and when that answer does
# not come they carry on with the channel's name as the version - and fail
# downloading "releases/download/stable/...". When the update server does
# not answer at all, the project's newest GitHub release stands in. Empty
# when neither can be found.
channel_release() { # k3s|rke2 channel
  repo=k3s-io/k3s; [ "$1" = rke2 ] && repo=rancher/rke2
  for url in "https://update.$1.io/v1-release/channels/$2" "https://github.com/$repo/releases/latest"; do
    # The channel server is given seconds, not minutes - it has taken no
    # connections since October 2026 - and GitHub its five tries.
    case "$url" in https://update.*) tries=1 ;; *) tries=5 ;; esac
    where=$(curl -sfL --retry "$tries" --retry-all-errors --retry-delay 3 --connect-timeout 5 --max-time 60 -o /dev/null \
      -w '%{url_effective}' "$url" 2>/dev/null) || where=""
    case "${where##*/}" in v[0-9]*) echo "${where##*/}"; return 0 ;; esac
  done
}

# A download that fails for a moment is tried again before the install stops.
attempts() { # what command...
  what="$1"; shift; n=1
  until "$@"; do
    [ "$n" -ge 4 ] && return 1
    echo "  $what did not finish; trying again in $((n * 10)) seconds"
    sleep $((n * 10)); n=$((n + 1))
  done
}

install_k3s() {
  [ -n "$K3S_VERSION" ] || K3S_VERSION=$(channel_release k3s stable)
  [ -n "$K3S_VERSION" ] || fail "k3s's release could not be found (update.k3s.io and GitHub did not answer); try again, or give a version with --k3s-version"
  say "Installing k3s${K3S_VERSION:+ $K3S_VERSION} ($1)"
  if [ -n "$K3S_VERSION" ]; then export INSTALL_K3S_VERSION="$K3S_VERSION"; fi
  if [ -n "$NODE_IP" ]; then set -- "$@" --node-ip "$NODE_IP"; fi
  # CDI runs without root and must own the block devices assigned to its pods.
  # Enable on workers too: an importer can be scheduled on any joined node.
  set -- "$@" --nonroot-devices
  attempts "Installing k3s" get_k3s "$@" || fail "k3s could not be installed; see the output above"
}
get_k3s() {
  curl -sfL --retry 5 --retry-all-errors https://get.k3s.io | sh -s - "$@"
}

# RKE2 reads its settings from a file rather than flags: written first, then
# RKE2 installed and its service started (which waits for it to come up).
install_rke2() { # server|agent [url token]
  type="$1"; url="${2:-}"; token="${3:-}"
  [ -n "$RKE2_VERSION" ] || RKE2_VERSION=$(channel_release rke2 stable)
  [ -n "$RKE2_VERSION" ] || fail "RKE2's release could not be found (update.rke2.io and GitHub did not answer); try again, or give a version with --rke2-version"
  say "Installing RKE2${RKE2_VERSION:+ $RKE2_VERSION} ($type)"
  mkdir -p /etc/rancher/rke2
  # The file holds the cluster token, so only root may read it.
  ( umask 077; : >> /etc/rancher/rke2/config.yaml )
  chmod 600 /etc/rancher/rke2/config.yaml
  {
    echo "nonroot-devices: true"
    [ -n "$NODE_IP" ] && echo "node-ip: $NODE_IP"
    [ -n "$url" ] && echo "server: $url"
    [ -n "$token" ] && echo "token: $token"
    [ -n "$JOIN_TAINT" ] && printf 'node-taint:
  - "%s"
' "$JOIN_TAINT"
    [ -n "$JOIN_LABEL" ] && printf 'node-label:\n  - "%s"\n' "$JOIN_LABEL"
    # A server publishes LoadBalancer Services on the nodes' own addresses,
    # as k3s does - Homestead's own address among them.
    [ "$type" = server ] && echo "enable-servicelb: true"
    true
  } > /etc/rancher/rke2/config.yaml
  if [ -n "$RKE2_VERSION" ]; then export INSTALL_RKE2_VERSION="$RKE2_VERSION"; fi
  attempts "Installing RKE2" get_rke2 "$type" || fail "RKE2 could not be installed; see the output above"
  say "Starting RKE2 (the first start takes several minutes while images are downloaded)"
  systemctl enable --now "rke2-$type.service"
}

get_rke2() {
  curl -sfL --retry 5 --retry-all-errors https://get.rke2.io | INSTALL_RKE2_TYPE="$1" sh -
}

parse_common() { # sets DIST, NODE_IP, versions; leaves the rest to the caller
  case "$1" in
    --rke2) DIST=rke2; return 1 ;;
    --node-ip) NODE_IP="$2"; return 2 ;;
    --k3s-version) K3S_VERSION="$2"; return 2 ;;
    --rke2-version) RKE2_VERSION="$2"; return 2 ;;
    --longhorn-volume)
      case "$2" in auto|none) LONGHORN_VOLUME="$2" ;; 0) LONGHORN_VOLUME=none ;;
        *[!0-9]*|"") fail "--longhorn-volume is auto, none or a size in GB" ;; *) LONGHORN_VOLUME="$2" ;; esac
      return 2 ;;
    --longhorn-data-path)
      case "${2:-}" in /mnt/longhorn-os|/var/lib/longhorn) LONGHORN_DATA_PATH="$2" ;;
        *) fail "--longhorn-data-path must be /mnt/longhorn-os or /var/lib/longhorn; custom layouts need manual storage setup" ;; esac
      return 2 ;;
  esac
  return 0
}

case "$MODE" in
  agent|join)
    [ $# -ge 2 ] || fail "Usage: $MODE https://<server>:6443 <token> (RKE2: https://<server>:9345 <token> --rke2)"
    URL="$1"; TOKEN="$2"; shift 2
    while [ $# -gt 0 ]; do
      case "$1" in --tiebreaker) TIEBREAKER=1; shift; continue ;; esac
      set +e; parse_common "$@"; used=$?; set -e
      [ "$used" = 0 ] && fail "Unknown option: $1"
      shift "$used"
    done
    host_limits
    host_packages
    # Ready minutes before Longhorn is running on it: until Homestead sees
    # Longhorn's driver here and lifts this, pods prefer other nodes, so one
    # with a volume does not land here and wait. Only a preference - Longhorn
    # itself still starts here.
    JOIN_TAINT="homestead.io/storage-pending=longhorn:PreferNoSchedule"
    # A tiebreaker holds an etcd vote and nothing else (#364): it never gets
    # Longhorn, so it does not wait for it, and it takes no apps.
    [ "$TIEBREAKER" = 1 ] && [ "$MODE" = agent ] && fail "A tiebreaker joins as a server: use join, not agent"
    if [ "$TIEBREAKER" = 1 ]; then JOIN_TAINT="node-role/tiebreaker=true:NoSchedule"; JOIN_LABEL="node-role/tiebreaker=true"; fi
    if [ "$DIST" = rke2 ]; then
      if [ "$MODE" = agent ]; then install_rke2 agent "$URL" "$TOKEN"; else install_rke2 server "$URL" "$TOKEN"; fi
    else
      export K3S_URL="$URL" K3S_TOKEN="$TOKEN"
      if [ "$MODE" = agent ]; then install_k3s agent --node-taint "$JOIN_TAINT"
      else unset K3S_URL; install_k3s server --server "$URL" --node-taint "$JOIN_TAINT" ${JOIN_LABEL:+--node-label "$JOIN_LABEL"}; fi
    fi
    say "Node joined the cluster. It appears on the Homestead Nodes page within a few minutes."
    exit 0 ;;
  server|addons) ;;
  *) sed -n '2,70p' "$0" 2>/dev/null || true; fail "Specify a mode: server, agent, join or addons." ;;
esac

while [ $# -gt 0 ]; do
  case "$1" in
    --no-longhorn) LONGHORN=0; shift; continue ;;
    --kubevirt) KUBEVIRT=1; shift; continue ;;
    --homestead-version) HOMESTEAD_VERSION="$2"; shift 2; continue ;;
    --longhorn-version) LONGHORN_VERSION="$2"; shift 2; continue ;;
    --kubevirt-version) KUBEVIRT_VERSION="$2"; shift 2; continue ;;
    --cdi-version) CDI_VERSION="$2"; shift 2; continue ;;
    --no-kube-vip) KUBE_VIP=0; shift; continue ;;
    --no-multus) MULTUS=0; shift; continue ;;
    --kube-vip-version) KUBE_VIP_VERSION="$2"; shift 2; continue ;;
    --multus-version) MULTUS_VERSION="$2"; shift 2; continue ;;
    --no-node-probe) NODE_PROBE=0; shift; continue ;;
    --node-probe) NODE_PROBE=1; shift; continue ;;
    --vip)
      printf '%s' "${2:-}" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || fail "--vip needs an IPv4 address"
      VIP="$2"; shift 2; continue ;;
  esac
  set +e; parse_common "$@"; used=$?; set -e
  [ "$used" = 0 ] && fail "Unknown option: $1"
  shift "$used"
done

if [ "$DIST" = rke2 ]; then
  [ "$LONGHORN" = 1 ] || fail "RKE2 has no built-in storage, so --no-longhorn is not supported. Longhorn stores Homestead's data."
  AUTODEPLOY=/var/lib/rancher/rke2/server/manifests
  KUBECTL="/var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml"
  SERVICE=rke2-server
else
  AUTODEPLOY=/var/lib/rancher/k3s/server/manifests
  KUBECTL="k3s kubectl"
  SERVICE=k3s
fi

host_limits
[ "$LONGHORN" = 1 ] && host_packages
if [ "$MODE" = addons ]; then
  # The cluster is running already: only what goes on top of it.
  $KUBECTL get nodes >/dev/null 2>&1 || fail "No $DIST server is running on this machine. Use server mode to create a cluster."
elif [ "$DIST" = rke2 ]; then
  install_rke2 server
else
  install_k3s server --cluster-init
fi

say "Waiting for the node to become ready"
i=0; until $KUBECTL get nodes 2>/dev/null | grep -q " Ready"; do
  i=$((i+1)); [ $i -gt 180 ] && fail "The node did not become ready. See: journalctl -u $SERVICE"; sleep 2; done
IP=$($KUBECTL get node "$(hostname)" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)
[ -n "$IP" ] || IP=$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
# What this script installs is applied once from here. k3s's auto-deploy
# folder ($AUTODEPLOY) is applied again at every start, which would take
# Homestead, Longhorn and KubeVirt back to these versions after each reboot.
MANIFESTS=/var/lib/homestead/install
mkdir -p "$MANIFESTS"
apply() { # file: server-side, so large CRDs fit; retried while the API settles
  i=0; until $KUBECTL apply --server-side --force-conflicts -f "$1" >/dev/null 2>&1; do
    i=$((i+1)); [ $i -gt 60 ] && fail "Could not apply $1. See: $KUBECTL apply --server-side -f $1"; sleep 5; done
}
# An earlier run of this script left files in the auto-deploy folder: k3s
# leaves a file with a .skip beside it alone, and removes nothing it applied.
for f in homestead.yaml longhorn.yaml kubevirt-operator.yaml kubevirt-cr.yaml cdi-operator.yaml cdi-cr.yaml; do
  [ -f "$AUTODEPLOY/$f" ] && [ ! -e "$AUTODEPLOY/$f.skip" ] && touch "$AUTODEPLOY/$f.skip"
done

LONGHORN_FOUND=""
if [ "$LONGHORN" = 1 ]; then
  LONGHORN_FOUND=$($KUBECTL get crd volumes.longhorn.io --ignore-not-found -o name) \
    || fail "Cannot check the existing Longhorn installation. Restore Kubernetes access and retry."
  if [ -z "$LONGHORN_FOUND" ]; then
    LONGHORN_FOUND=$($KUBECTL -n kube-system get helmchart longhorn --ignore-not-found -o name) \
      || fail "Cannot check Longhorn's chart. Restore Kubernetes access and retry."
  fi
fi
if [ "$LONGHORN" = 1 ] && [ -n "$LONGHORN_FOUND" ]; then
  # Reruns preserve all existing chart values, including the storage path.
  say "Longhorn is already installed"
  CLASS=longhorn
elif [ "$LONGHORN" = 1 ]; then
  say "Installing Longhorn${LONGHORN_VERSION:+ $LONGHORN_VERSION}"
  # One replica until more nodes join; raise it on the Volumes page later.
  # system-node-critical: the Job that installs any HelmChart runs at
  # system-cluster-critical, which outranks Longhorn's own default and can
  # preempt the instance manager holding a node's attached volumes. The same
  # values as homestead_addons.longhorn_values.
  CHART_VERSION=""
  [ -n "$LONGHORN_VERSION" ] && CHART_VERSION="  version: ${LONGHORN_VERSION#v}"
  cat > "$MANIFESTS/longhorn.yaml" <<EOF
apiVersion: helm.cattle.io/v1
kind: HelmChart
metadata:
  name: longhorn
  namespace: kube-system
  labels:
    homestead.io/managed: "true"
spec:
  repo: https://charts.longhorn.io
  chart: longhorn
$CHART_VERSION
  targetNamespace: longhorn-system
  createNamespace: true
  valuesContent: |
    persistence:
      defaultClassReplicaCount: 1
    defaultSettings:
      defaultReplicaCount: 1
      priorityClass: system-node-critical
      defaultDataPath: $LONGHORN_DATA_PATH
EOF
  apply "$MANIFESTS/longhorn.yaml"
  CLASS=longhorn
else
  CLASS=local-path
fi

# Homestead's data: a volume one host mounts at a time. A shared one needs a
# share-manager, which an undrained restart can leave down with Homestead
# waiting on it (2026-11-10); only several copies of Homestead need sharing,
# and Settings > About > Redundancy moves the data when that is chosen. A
# rerun keeps the claim's own mode: Kubernetes refuses a change after creation.
MODE_RW=ReadWriteOnce
EXISTING_MODE=$($KUBECTL -n lab get pvc homestead-data --ignore-not-found -o jsonpath='{.spec.accessModes[0]}')   || fail "Cannot check Homestead's data volume. Restore Kubernetes access and retry."
[ -n "$EXISTING_MODE" ] && MODE_RW=$EXISTING_MODE

if [ "$KUBEVIRT" = 1 ]; then install_kubevirt; fi

say "Downloading the Homestead manifest"
REF=main
if [ -n "$HOMESTEAD_VERSION" ]; then REF="v${HOMESTEAD_VERSION#v}"; fi
TMP=$(mktemp)
curl -sfL "$RAW/$REF/deploy/deploy.yaml" -o "$TMP" || fail "Could not download $RAW/$REF/deploy/deploy.yaml."
# Harvester's answers become this cluster's: its storage class, and this
# machine's address - the ServiceLB publishes Services on the nodes' own
# addresses, so there is no separate VIP to choose. The class is rewritten on
# its two lines only (a bare "longhorn" would hit longhorn.io and
# longhorn-system), for this manifest's longhorn and an older release's
# longhorn-r2 (#731).
sed -e "s/storageClassName: longhorn\(-r2\)\{0,1\}$/storageClassName: $CLASS/" \
    -e "s/value: \"longhorn\(-r2\)\{0,1\}\"$/value: \"$CLASS\"/" \
    -e "s/accessModes: \[ReadWriteMany\]/accessModes: [$MODE_RW]/" \
    -e "s/192\.0\.2\.242/$IP/g" \
    -e "/kube-vip.io\/loadbalancerIPs/d" \
    "$TMP" > "$MANIFESTS/homestead.yaml"
rm -f "$TMP"
# Components Homestead installs after it starts: kube-vip and Multus, the
# same HelmCharts as Settings > Cluster > Add-ons, at tested versions unless
# pinned here. Homestead records each installation, so a component removed
# later is not reinstalled.
flag_word() { if [ "$1" = 1 ]; then echo yes; else echo no; fi; }
# The node probe: what this run says, else what homestead-install says from an
# earlier install (#1279: a reinstall that kept the data must not turn a "no"
# back into yes), else yes. A cluster that cannot be asked is never guessed at.
node_probe_word() {
  case "$NODE_PROBE" in 1) echo yes; return ;; 0) echo no; return ;; esac
  kept=$($KUBECTL -n lab get configmap homestead-install --ignore-not-found -o jsonpath='{.data.node-probe}') || return 1
  case "$(printf '%s' "$kept" | tr '[:upper:]' '[:lower:]')" in
    no|false|0|skip) echo no ;;
    *) echo yes ;;
  esac
}
PROBE_WORD=$(node_probe_word) || fail "Could not read homestead-install to keep this cluster's node probe choice. Run again, or say --node-probe or --no-node-probe."
cat >> "$MANIFESTS/homestead.yaml" <<EOF
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: homestead-install
  namespace: lab
  labels:
    homestead.io/managed: "true"
data:
  kube-vip: "$(flag_word "$KUBE_VIP")"
  kube-vip-version: "$KUBE_VIP_VERSION"
  multus: "$(flag_word "$MULTUS")"
  multus-version: "$MULTUS_VERSION"
  node-probe: "$PROBE_WORD"
  vip: "$VIP"
EOF
[ "$KUBE_VIP" = 1 ] && echo "  kube-vip will be installed by Homestead after it starts."
[ "$MULTUS" = 1 ] && echo "  Multus will be installed by Homestead after it starts."
if [ "$PROBE_WORD" = yes ]; then echo "  The node probe will be installed by Homestead after it starts."
elif [ -z "$NODE_PROBE" ]; then echo "  The node probe stays left out, as this cluster chose before (--node-probe installs it)."; fi
apply "$MANIFESTS/homestead.yaml"

say "Waiting for Homestead to start (this takes several minutes when Longhorn is being installed)"
i=0; until $KUBECTL -n lab rollout status deployment/homestead --timeout=10s >/dev/null 2>&1; do
  i=$((i+1)); [ $i -gt 90 ] && fail "Homestead did not start. See: $KUBECTL -n lab get pods"; sleep 10; done

first_snapshot

say "Homestead is running at http://$IP:8088. Open this address to create the administrator account."
# The first admin's one-time setup code (#737). Only the command that shows it:
# under install.sh's menus this output is kept in its log, and install.sh shows
# the code itself at the end, after any HS_SETUP_CODE has replaced it.
echo "   Setup code: $KUBECTL -n lab get secret homestead-setup -o jsonpath='{.data.code}' | base64 -d"
echo "   To add nodes, see Cluster > Add a host in Homestead."
