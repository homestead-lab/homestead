#!/bin/sh
# Scan the candidate built from this source before publication. No registry push.
set -eu
SCANNER='aquasec/trivy@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa' # 0.75.0
PLATFORMS=${SECURITY_PLATFORMS:-'amd64 arm64'}
VERSION=${VERSION:-security-check}
REVISION=${REVISION:-$(git rev-parse HEAD)}
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' 0
trap 'exit 130' INT
trap 'exit 143' HUP TERM
mkdir -p release-assets/security
reports=$(cd release-assets/security && pwd)
status=0
for arch in $PLATFORMS; do
  case "$arch" in amd64|arm64) ;; *) echo 'Unsupported architecture' >&2; exit 2 ;; esac
  docker buildx build --pull --platform "linux/$arch" --build-arg "VERSION=$VERSION" \
    --build-arg "REVISION=$REVISION" --output "type=docker,dest=$stage/$arch.tar" .
  docker run --rm --mount "type=bind,source=$stage,target=/input,readonly" \
    --mount "type=bind,source=$reports,target=/reports" \
    --mount 'type=volume,source=homestead-trivy-cache,target=/root/.cache/trivy' \
    "$SCANNER" image --input "/input/$arch.tar" --scanners vuln --severity HIGH,CRITICAL \
    --exit-code 1 --format json --output "/reports/$arch.json" || status=1
done
# These default helpers also run code as Homestead or on the host. Scan their
# fresh registry manifests rather than relying on the main image's inventory.
for arch in $PLATFORMS; do
  for image in alpine:3.24 python:3.12-alpine; do
    name=$(printf '%s' "$image" | tr ':/' '__')
    docker run --rm --mount "type=bind,source=$reports,target=/reports" \
      --mount 'type=volume,source=homestead-trivy-cache,target=/root/.cache/trivy' \
      "$SCANNER" image --platform "linux/$arch" --scanners vuln --severity HIGH,CRITICAL \
      --exit-code 1 --format json --output "/reports/$arch-$name.json" "$image" || status=1
  done
done
exit "$status"
