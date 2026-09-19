FROM python:3.12-alpine

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="HarvUI" \
      org.opencontainers.image.description="An Unraid-style UI for Harvester, Longhorn and KubeVirt" \
      org.opencontainers.image.source="https://github.com/wjcloudy/harvui" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.revision="$REVISION" \
      org.opencontainers.image.licenses="MIT"

ENV PORT=8080 \
    WEBROOT=/web \
    DATA_DIR=/data \
    HARVUI_VERSION=$VERSION \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN addgroup -S -g 10001 harvui && adduser -S -D -H -u 10001 -G harvui harvui \
    && mkdir -p /srv /web /data \
    && chown -R harvui:harvui /data

COPY --chown=harvui:harvui server/*.py /srv/
COPY --chown=harvui:harvui web/index.html web/style.css /web/
COPY --chown=harvui:harvui web/js/*.js /web/

USER 10001:10001
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
CMD ["python3", "/srv/server.py"]
