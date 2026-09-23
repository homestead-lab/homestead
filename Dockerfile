FROM python:3.12-alpine

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="Homestead" \
      org.opencontainers.image.description="A friendly homelab control plane for Harvester, Rancher, Longhorn, Fleet and Kubernetes" \
      org.opencontainers.image.source="https://github.com/wjcloudy/homestead" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.revision="$REVISION" \
      org.opencontainers.image.licenses="MIT"

ENV PORT=8080 \
    WEBROOT=/web \
    DATA_DIR=/data \
    HOMESTEAD_VERSION=$VERSION \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apk add --no-cache smartmontools \
    && addgroup -S -g 10001 homestead && adduser -S -D -H -u 10001 -G homestead homestead \
    && mkdir -p /srv /web /data \
    && chown -R homestead:homestead /data

COPY --chown=homestead:homestead server/*.py /srv/
# The licence and the notices for what the image carries travel with it.
COPY --chown=homestead:homestead LICENSE THIRD_PARTY_NOTICES.md /srv/
# The node probe's scripts travel with the release that reads them, so an
# upgrade can bring the probe with it instead of asking for a kubectl command.
COPY --chown=homestead:homestead server/probe/*.py /srv/probe/
# So do Homestead's own permissions: it brings its ClusterRole up to the one
# this release's manifest describes.
COPY --chown=homestead:homestead deploy/deploy.yaml /srv/deploy.yaml
COPY --chown=homestead:homestead web/index.html web/style.css /web/
COPY --chown=homestead:homestead web/js/*.js /web/js/
COPY --chown=homestead:homestead web/assets/*.svg /web/assets/
# The installable app: its worker, manifest and icons.
COPY --chown=homestead:homestead web/sw.js web/manifest.webmanifest /web/
COPY --chown=homestead:homestead web/icons/*.png /web/icons/
# Vendored libraries keep their own directory layout: Monaco loads its pieces
# by relative path at runtime.
COPY --chown=homestead:homestead web/vendor /web/vendor

USER 10001:10001
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
CMD ["python3", "/srv/server.py"]
