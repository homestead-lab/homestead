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
    && addgroup -S -g 10001 harvui && adduser -S -D -H -u 10001 -G harvui harvui \
    && mkdir -p /srv /web /data \
    && chown -R harvui:harvui /data

COPY --chown=harvui:harvui server/*.py /srv/
COPY --chown=harvui:harvui web/index.html web/style.css /web/
COPY --chown=harvui:harvui web/js/*.js /web/js/
COPY --chown=harvui:harvui web/assets/*.svg /web/assets/

USER 10001:10001
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
CMD ["python3", "/srv/server.py"]
