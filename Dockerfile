# syntax=docker/dockerfile:1

# ---------- frontend ----------
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------- runtime ----------
FROM python:3.12-slim AS runtime

ARG MIHOMO_VERSION=v1.19.34
ARG TARGETARCH=amd64

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    NODEPOOL_DATA_DIR=/data \
    NODEPOOL_CONFIG_DIR=/config \
    NODEPOOL_LOG_DIR=/logs \
    NODEPOOL_BACKUP_DIR=/backup \
    NODEPOOL_STATE_DIR=/state \
    NODEPOOL_MIGRATION_ENABLED=0 \
    NODEPOOL_MIHOMO_BIN=/usr/local/bin/mihomo \
    NODEPOOL_MIHOMO_API=http://127.0.0.1:9090

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tzdata \
 && case "${TARGETARCH}" in \
      arm64) MIHOMO_ARCH=linux-arm64 ;; \
      *)     MIHOMO_ARCH=linux-amd64-compatible ;; \
    esac \
 && curl -fsSL "https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/mihomo-${MIHOMO_ARCH}-${MIHOMO_VERSION}.gz" -o /tmp/mihomo.gz \
 && gunzip -c /tmp/mihomo.gz > /usr/local/bin/mihomo \
 && chmod 755 /usr/local/bin/mihomo \
 && rm -f /tmp/mihomo.gz \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY scripts ./scripts
COPY --from=web /backend/static ./backend/static

RUN mkdir -p /data /config /logs /backup /state

LABEL org.opencontainers.image.title="NodePoolME" \
      org.opencontainers.image.description="NodePoolME subscription pool, health testing and publishing" \
      org.opencontainers.image.source="https://github.com/laoXuHome/NodePoolME"

# The official compose deliberately runs as root because bind-mounted /data,
# /config, /logs, /backup and /state are created by the host/docker as root.
USER root

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--log-level", "info"]
