#!/bin/sh
# Turn a bare Linux machine into a k3s (or RKE2) cluster running Homestead -
# or join another machine to one.
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
#   A k3s (or RKE2) server that is already running, to add Longhorn and Homestead to it:
#     curl -sfL .../bootstrap-k3s.sh | sudo sh -s - addons
#
#   RKE2 instead of k3s - full upstream Kubernetes, hardened, what Harvester
#   runs on - add --rke2 to any of these. RKE2's servers are joined on 9345:
#     curl -sfL .../bootstrap-k3s.sh | sudo sh -s - server --rke2
#     curl -sfL .../bootstrap-k3s.sh | sudo sh -s - agent https://<first-machine>:9345 <token> --rke2
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
# Options for every mode:
#   --rke2                   RKE2 instead of k3s
#   --node-ip 192.168.1.50   the address the cluster registers this machine
#                            by, when it has more than one
#
# What "server" does:
#   1. installs what Longhorn needs on the host (open-iscsi, NFS client);
#   2. installs k3s with an embedded etcd, so more servers can join later
#      (or RKE2, which always has one, with its ServiceLB turned on so apps
#      get the nodes' addresses as they do on k3s);
#   3. drops a HelmChart for Longhorn and Homestead's manifest into the
#      manifests folder, which k3s or RKE2 applies itself - nothing else to run;
#   4. asks Homestead, in its manifest, to install kube-vip (VIPs) and Multus
#      (a VM's or container's own LAN address) once it is up - Homestead
#      installs and upgrades them, as it does from Settings > Cluster > Add-ons;
#   5. waits for Homestead and prints its address.
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
NODE_IP=""
RAW=https://raw.githubusercontent.com/wjcloudy/homestead

say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nerror: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || fail "run as root (sudo)"
command -v curl >/dev/null 2>&1 || fail "curl is needed"

host_packages() {
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
}

# KubeVirt and CDI from their newest releases, dropped into the manifests
# folder like the rest; each one's switch goes in once its CRD is there.
install_kubevirt() {
  say "Installing KubeVirt and CDI"
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
  curl -sfL "$KV_RELEASES/$KV/kubevirt-operator.yaml" -o "$MANIFESTS/kubevirt-operator.yaml" \
    || fail "Could not download KubeVirt $KV."
  curl -sfL "$CDI_RELEASES/download/$CDI/cdi-operator.yaml" -o "$MANIFESTS/cdi-operator.yaml" \
    || fail "Could not download CDI $CDI."
  modprobe kvm_intel 2>/dev/null || modprobe kvm_amd 2>/dev/null || true
  EMULATION=""
  if [ ! -e /dev/kvm ]; then
    EMULATION="      useEmulation: true"
    echo "  Hardware virtualisation (/dev/kvm) is not available: KubeVirt will use emulation."
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
    i=$((i+1)); if [ $i -gt 60 ]; then echo "  $1 is not available yet; $DIST will retry."; return 0; fi; sleep 3; done
}

install_k3s() {
  say "Installing k3s${K3S_VERSION:+ $K3S_VERSION} ($1)"
  if [ -n "$K3S_VERSION" ]; then export INSTALL_K3S_VERSION="$K3S_VERSION"; fi
  if [ -n "$NODE_IP" ]; then set -- "$@" --node-ip "$NODE_IP"; fi
  curl -sfL https://get.k3s.io | sh -s - "$@"
}

# RKE2 reads its settings from a file rather than flags: written first, then
# RKE2 installed and its service started (which waits for it to come up).
install_rke2() { # server|agent [url token]
  type="$1"; url="${2:-}"; token="${3:-}"
  say "Installing RKE2${RKE2_VERSION:+ $RKE2_VERSION} ($type)"
  mkdir -p /etc/rancher/rke2
  {
    [ -n "$NODE_IP" ] && echo "node-ip: $NODE_IP"
    [ -n "$url" ] && echo "server: $url"
    [ -n "$token" ] && echo "token: $token"
    # A server publishes LoadBalancer Services on the nodes' own addresses,
    # as k3s does - Homestead's own address among them.
    [ "$type" = server ] && echo "enable-servicelb: true"
    true
  } > /etc/rancher/rke2/config.yaml
  if [ -n "$RKE2_VERSION" ]; then export INSTALL_RKE2_VERSION="$RKE2_VERSION"; fi
  curl -sfL https://get.rke2.io | INSTALL_RKE2_TYPE="$type" sh -
  say "Starting RKE2 (the first start takes several minutes while images are downloaded)"
  systemctl enable --now "rke2-$type.service"
}

parse_common() { # sets DIST, NODE_IP, versions; leaves the rest to the caller
  case "$1" in
    --rke2) DIST=rke2; return 1 ;;
    --node-ip) NODE_IP="$2"; return 2 ;;
    --k3s-version) K3S_VERSION="$2"; return 2 ;;
    --rke2-version) RKE2_VERSION="$2"; return 2 ;;
  esac
  return 0
}

case "$MODE" in
  agent|join)
    [ $# -ge 2 ] || fail "Usage: $MODE https://<server>:6443 <token> (RKE2: https://<server>:9345 <token> --rke2)"
    URL="$1"; TOKEN="$2"; shift 2
    while [ $# -gt 0 ]; do
      set +e; parse_common "$@"; used=$?; set -e
      [ "$used" = 0 ] && fail "Unknown option: $1"
      shift "$used"
    done
    host_packages
    if [ "$DIST" = rke2 ]; then
      if [ "$MODE" = agent ]; then install_rke2 agent "$URL" "$TOKEN"; else install_rke2 server "$URL" "$TOKEN"; fi
    else
      export K3S_URL="$URL" K3S_TOKEN="$TOKEN"
      if [ "$MODE" = agent ]; then install_k3s agent
      else unset K3S_URL; install_k3s server --server "$URL"; fi
    fi
    say "Node joined the cluster. It appears on the Homestead Nodes page within a few minutes."
    exit 0 ;;
  server|addons) ;;
  *) sed -n '2,55p' "$0" 2>/dev/null || true; fail "Specify a mode: server, agent, join or addons." ;;
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
  esac
  set +e; parse_common "$@"; used=$?; set -e
  [ "$used" = 0 ] && fail "Unknown option: $1"
  shift "$used"
done

if [ "$DIST" = rke2 ]; then
  [ "$LONGHORN" = 1 ] || fail "RKE2 has no built-in storage, so --no-longhorn is not supported. Longhorn stores Homestead's data."
  MANIFESTS=/var/lib/rancher/rke2/server/manifests
  KUBECTL="/var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml"
  SERVICE=rke2-server
else
  MANIFESTS=/var/lib/rancher/k3s/server/manifests
  KUBECTL="k3s kubectl"
  SERVICE=k3s
fi

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
mkdir -p "$MANIFESTS"

if [ "$LONGHORN" = 1 ] && [ "$MODE" = addons ] && $KUBECTL get crd volumes.longhorn.io >/dev/null 2>&1 \
   && [ ! -f "$MANIFESTS/longhorn.yaml" ]; then
  # Installed another way already: used as it is.
  say "Longhorn is already installed"
  CLASS=longhorn; MODE_RW=ReadWriteMany
elif [ "$LONGHORN" = 1 ]; then
  say "Installing Longhorn${LONGHORN_VERSION:+ $LONGHORN_VERSION}"
  # One replica until more nodes join; raise it on the Volumes page later.
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
EOF
  CLASS=longhorn; MODE_RW=ReadWriteMany
else
  CLASS=local-path; MODE_RW=ReadWriteOnce
fi

if [ "$KUBEVIRT" = 1 ]; then install_kubevirt; fi

say "Downloading the Homestead manifest"
REF=main
if [ -n "$HOMESTEAD_VERSION" ]; then REF="v${HOMESTEAD_VERSION#v}"; fi
TMP=$(mktemp)
curl -sfL "$RAW/$REF/deploy/deploy.yaml" -o "$TMP" || fail "Could not download $RAW/$REF/deploy/deploy.yaml."
# Harvester's answers become this cluster's: its storage class, and this
# machine's address - the ServiceLB publishes Services on the nodes' own
# addresses, so there is no separate VIP to choose.
sed -e "s/longhorn-r2/$CLASS/g" \
    -e "s/accessModes: \[ReadWriteMany\]/accessModes: [$MODE_RW]/" \
    -e "s/192\.168\.1\.242/$IP/g" \
    -e "/kube-vip.io\/loadbalancerIPs/d" \
    "$TMP" > "$MANIFESTS/homestead.yaml"
rm -f "$TMP"
# Components Homestead installs after it starts: kube-vip and Multus, the
# same HelmCharts as Settings > Cluster > Add-ons, at tested versions unless
# pinned here. Homestead records each installation, so a component removed
# later is not reinstalled.
flag_word() { if [ "$1" = 1 ]; then echo yes; else echo no; fi; }
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
EOF
[ "$KUBE_VIP" = 1 ] && echo "  kube-vip will be installed by Homestead after it starts."
[ "$MULTUS" = 1 ] && echo "  Multus will be installed by Homestead after it starts."
true

say "Waiting for Homestead to start (this takes several minutes when Longhorn is being installed)"
i=0; until $KUBECTL -n lab rollout status deployment/homestead --timeout=10s >/dev/null 2>&1; do
  i=$((i+1)); [ $i -gt 90 ] && fail "Homestead did not start. See: $KUBECTL -n lab get pods"; sleep 10; done

say "Homestead is running at http://$IP:8088. Open this address to create the administrator account."
echo "   To add nodes, see Cluster > Add a host in Homestead."
