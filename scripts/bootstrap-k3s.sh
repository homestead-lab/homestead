#!/bin/sh
# Turn a bare Linux machine into a k3s cluster running Homestead - or join
# another machine to one.
#
#   New cluster, on the first machine:
#     curl -sfL https://raw.githubusercontent.com/wjcloudy/homestead/main/scripts/bootstrap-k3s.sh | sudo sh -s - server
#
#   Another machine, as a worker (the token is in /var/lib/rancher/k3s/server/node-token on the first):
#     curl -sfL .../bootstrap-k3s.sh | sudo sh -s - agent https://<first-machine>:6443 <token>
#
#   Another machine, as a second or third server (control plane and etcd):
#     curl -sfL .../bootstrap-k3s.sh | sudo sh -s - join https://<first-machine>:6443 <token>
#
#   A k3s server that is already running, to add Longhorn and Homestead to it:
#     curl -sfL .../bootstrap-k3s.sh | sudo sh -s - addons
#
# For a guided install that asks these questions and checks the machine
# first, use install.sh instead.
#
# Options for "server", after the word:
#   --no-longhorn        use k3s's local-path storage instead of Longhorn: no
#                        replicas, and Homestead's data lives on this machine
#   --kubevirt           also install KubeVirt and CDI, for virtual machines
#                        (emulated, and slow, if this machine has no /dev/kvm)
#   --k3s-version v1.31.4+k3s1   pin k3s (default: k3s's stable channel)
#   --homestead-version 2.8.95   pin Homestead (default: the newest release)
# Options for every mode:
#   --node-ip 192.0.2.50   the address k3s registers this machine by, when
#                            it has more than one
#
# What "server" does:
#   1. installs what Longhorn needs on the host (open-iscsi, NFS client);
#   2. installs k3s with an embedded etcd, so more servers can join later;
#   3. drops a HelmChart for Longhorn and Homestead's manifest into k3s's
#      manifests folder, which k3s applies itself - nothing else to run;
#   4. waits for Homestead and prints its address.
# It is safe to run again: each step finds what the last run left.
set -eu

MODE="${1:-}"; [ $# -gt 0 ] && shift
LONGHORN=1
KUBEVIRT=0
K3S_VERSION=""
HOMESTEAD_VERSION=""
NODE_IP=""
MANIFESTS=/var/lib/rancher/k3s/server/manifests
RAW=https://raw.githubusercontent.com/wjcloudy/homestead

say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nerror: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || fail "run as root (sudo)"
command -v curl >/dev/null 2>&1 || fail "curl is needed"

host_packages() {
  # Longhorn mounts volumes over iSCSI and serves shared (RWX) volumes over NFS.
  say "Installing what Longhorn needs on this host"
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq open-iscsi nfs-common
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q iscsi-initiator-utils nfs-utils
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install -y open-iscsi nfs-client
  else
    echo "  unknown package manager: install open-iscsi and an NFS client yourself"
  fi
  systemctl enable --now iscsid >/dev/null 2>&1 || true
  modprobe iscsi_tcp 2>/dev/null || true
}

# KubeVirt and CDI from their newest releases, dropped into k3s's manifests
# folder like the rest; each one's switch goes in once its CRD is there.
install_kubevirt() {
  say "Asking k3s to install KubeVirt and CDI"
  KV_RELEASES=https://github.com/kubevirt/kubevirt/releases/download
  CDI_RELEASES=https://github.com/kubevirt/containerized-data-importer/releases
  KV=$(curl -sfL https://storage.googleapis.com/kubevirt-prow/release/kubevirt/kubevirt/stable.txt) \
    || fail "could not read KubeVirt's newest release"
  # GitHub sends .../releases/latest on to the newest release's tag; its API says it too.
  CDI=$(curl -sfLI -o /dev/null -w '%{url_effective}' "$CDI_RELEASES/latest" | sed 's|.*/||')
  case "$CDI" in v*) ;; *) CDI=$(curl -sfL https://api.github.com/repos/kubevirt/containerized-data-importer/releases/latest \
    | sed -n 's/.*"tag_name": *"\(v[^"]*\)".*/\1/p' | head -n 1) ;; esac
  case "$KV" in v*) ;; *) fail "could not tell KubeVirt's newest release ($KV)" ;; esac
  case "$CDI" in v*) ;; *) fail "could not tell CDI's newest release ($CDI)" ;; esac
  echo "  KubeVirt $KV, CDI $CDI"
  curl -sfL "$KV_RELEASES/$KV/kubevirt-operator.yaml" -o "$MANIFESTS/kubevirt-operator.yaml" \
    || fail "could not download KubeVirt $KV"
  curl -sfL "$CDI_RELEASES/download/$CDI/cdi-operator.yaml" -o "$MANIFESTS/cdi-operator.yaml" \
    || fail "could not download CDI $CDI"
  modprobe kvm_intel 2>/dev/null || modprobe kvm_amd 2>/dev/null || true
  EMULATION=""
  if [ ! -e /dev/kvm ]; then
    EMULATION="      useEmulation: true"
    echo "  this machine has no /dev/kvm (hardware virtualisation): KubeVirt will emulate, and VMs run slowly"
  fi
  wait_crd kubevirts.kubevirt.io
  cat > "$MANIFESTS/kubevirt-cr.yaml" <<KUBEVIRT_CR
apiVersion: kubevirt.io/v1
kind: KubeVirt
metadata:
  name: kubevirt
  namespace: kubevirt
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
  wait_crd cdis.cdi.kubevirt.io
  cat > "$MANIFESTS/cdi-cr.yaml" <<'CDI_CR'
apiVersion: cdi.kubevirt.io/v1beta1
kind: CDI
metadata:
  name: cdi
spec:
  imagePullPolicy: IfNotPresent
  config:
    featureGates: [HonorWaitForFirstConsumer]
CDI_CR
}

wait_crd() {
  i=0; until $KUBECTL get crd "$1" >/dev/null 2>&1; do
    i=$((i+1)); if [ $i -gt 60 ]; then echo "  $1 is not there yet; k3s keeps trying"; return 0; fi; sleep 3; done
}

install_k3s() {
  say "Installing k3s ($*)"
  if [ -n "$K3S_VERSION" ]; then export INSTALL_K3S_VERSION="$K3S_VERSION"; fi
  if [ -n "$NODE_IP" ]; then set -- "$@" --node-ip "$NODE_IP"; fi
  curl -sfL https://get.k3s.io | sh -s - "$@"
}

case "$MODE" in
  agent|join)
    [ $# -ge 2 ] || fail "usage: $MODE https://<server>:6443 <token>"
    URL="$1"; TOKEN="$2"; shift 2
    while [ $# -gt 0 ]; do case "$1" in
      --k3s-version) K3S_VERSION="$2"; shift 2 ;;
      --node-ip) NODE_IP="$2"; shift 2 ;;
      *) fail "unknown option $1" ;; esac; done
    host_packages
    export K3S_URL="$URL" K3S_TOKEN="$TOKEN"
    if [ "$MODE" = agent ]; then install_k3s agent
    else unset K3S_URL; install_k3s server --server "$URL"; fi
    say "Joined. The machine appears on Homestead's Nodes page within a minute or two."
    exit 0 ;;
  server|addons) ;;
  *) sed -n '2,40p' "$0" 2>/dev/null || true; fail "say server, agent, join or addons" ;;
esac

while [ $# -gt 0 ]; do case "$1" in
  --no-longhorn) LONGHORN=0; shift ;;
  --kubevirt) KUBEVIRT=1; shift ;;
  --k3s-version) K3S_VERSION="$2"; shift 2 ;;
  --homestead-version) HOMESTEAD_VERSION="$2"; shift 2 ;;
  --node-ip) NODE_IP="$2"; shift 2 ;;
  *) fail "unknown option $1" ;; esac; done

[ "$LONGHORN" = 1 ] && host_packages
if [ "$MODE" = addons ]; then
  # k3s is running already: only what goes on top of it.
  k3s kubectl get nodes >/dev/null 2>&1 || fail "no k3s server is running here; use server to make one"
else
  install_k3s server --cluster-init
fi

KUBECTL="k3s kubectl"
say "Waiting for this node to be Ready"
i=0; until $KUBECTL get nodes 2>/dev/null | grep -q " Ready"; do
  i=$((i+1)); [ $i -gt 90 ] && fail "the node did not become Ready; see: journalctl -u k3s"; sleep 2; done
IP=$($KUBECTL get node "$(hostname)" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)
[ -n "$IP" ] || IP=$($KUBECTL get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
mkdir -p "$MANIFESTS"

if [ "$LONGHORN" = 1 ]; then
  say "Asking k3s to install Longhorn"
  # One replica until more nodes join; raise it on the Volumes page later.
  cat > "$MANIFESTS/longhorn.yaml" <<'EOF'
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
  targetNamespace: longhorn-system
  createNamespace: true
  valuesContent: |
    persistence:
      defaultClassReplicaCount: 1
    defaultSettings:
      defaultReplicaCount: 1
EOF
  CLASS=longhorn; MODE_RW=ReadWriteMany
else
  CLASS=local-path; MODE_RW=ReadWriteOnce
fi

if [ "$KUBEVIRT" = 1 ]; then install_kubevirt; fi

say "Fetching Homestead's manifest"
REF=main
if [ -n "$HOMESTEAD_VERSION" ]; then REF="v${HOMESTEAD_VERSION#v}"; fi
TMP=$(mktemp)
curl -sfL "$RAW/$REF/deploy/deploy.yaml" -o "$TMP" || fail "could not download $RAW/$REF/deploy/deploy.yaml"
# Harvester's answers become this cluster's: its storage class, and this
# machine's address - k3s's ServiceLB publishes Services on the nodes' own
# addresses, so there is no separate VIP to choose.
sed -e "s/longhorn-r2/$CLASS/g" \
    -e "s/accessModes: \[ReadWriteMany\]/accessModes: [$MODE_RW]/" \
    -e "s/192\.0\.2\.242/$IP/g" \
    -e "/kube-vip.io\/loadbalancerIPs/d" \
    "$TMP" > "$MANIFESTS/homestead.yaml"
rm -f "$TMP"

say "Waiting for Homestead to start (Longhorn first, if it is being installed: a few minutes)"
i=0; until $KUBECTL -n lab rollout status deployment/homestead --timeout=10s >/dev/null 2>&1; do
  i=$((i+1)); [ $i -gt 90 ] && fail "Homestead did not start; see: k3s kubectl -n lab get pods"; sleep 10; done

say "Homestead is running: open http://$IP:8088 and create the first account."
echo "   To add machines, Cluster > Add a host shows the commands for this cluster."
