#!/usr/bin/env bash
# Push HarvUI into the cluster. Code ships as ConfigMaps, so there is no image build.
set -euo pipefail
NS="${NS:-lab}"
HOST="${HOST:-rancher@192.0.2.210}"
K='sudo -n /var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml'
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> copying sources to $HOST"
ssh "$HOST" 'mkdir -p /tmp/harvui/js'
scp "$ROOT"/server/server.py        "$HOST":/tmp/harvui/
scp "$ROOT"/web/index.html          "$HOST":/tmp/harvui/
scp "$ROOT"/web/style.css           "$HOST":/tmp/harvui/
scp "$ROOT"/web/js/*.js             "$HOST":/tmp/harvui/js/

echo "==> updating ConfigMaps"
ssh "$HOST" "$K -n $NS create configmap harvui-server \
    --from-file=server.py=/tmp/harvui/server.py \
    --dry-run=client -o yaml | $K apply -f -"
ssh "$HOST" "$K -n $NS create configmap harvui-web \
    --from-file=/tmp/harvui/index.html --from-file=/tmp/harvui/style.css \
    --from-file=/tmp/harvui/js --dry-run=client -o yaml | $K apply -f -"

echo "==> rolling out"
ssh "$HOST" "$K -n $NS rollout restart deploy/harvui"
ssh "$HOST" "$K -n $NS rollout status deploy/harvui --timeout=180s"
