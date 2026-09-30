#!/usr/bin/env bash
# Deploy a published Homestead image to a Harvester/RKE2 host.
set -euo pipefail

NS="${NS:-lab}"
HOST="${HOST:?set HOST to your Harvester host, e.g. HOST=rancher@192.0.2.210}"
IMAGE="${IMAGE:-ghcr.io/wjcloudy/homestead}"
TAG="${TAG:-2.8.30}"
INSTALL_NODE_PROBE="${INSTALL_NODE_PROBE:-true}"
K='sudo -n /var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml'
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE="/tmp/homestead-release"

echo "==> uploading Kubernetes manifests"
ssh "$HOST" "mkdir -p $REMOTE"
scp "$ROOT/deploy/deploy.yaml" "$ROOT/deploy/nodeprobe.yaml" "$HOST:$REMOTE/"

echo "==> applying Homestead resources"
ssh "$HOST" "$K apply -f $REMOTE/deploy.yaml"
ssh "$HOST" "$K -n $NS set image deployment/homestead homestead=$IMAGE:$TAG"

if [[ "$INSTALL_NODE_PROBE" == "true" ]]; then
  echo "==> applying optional node telemetry probe"
  ssh "$HOST" "$K apply -f $REMOTE/nodeprobe.yaml"
fi

echo "==> waiting for $IMAGE:$TAG"
ssh "$HOST" "$K -n $NS rollout status deployment/homestead --timeout=300s"
ssh "$HOST" "$K -n $NS get deployment/homestead"
ssh "$HOST" "$K -n $NS get pvc,service -l app=homestead"
