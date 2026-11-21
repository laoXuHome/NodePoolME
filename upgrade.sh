#!/usr/bin/env bash
# NodePoolME upgrade helper: try the registry first, fall back to the GitHub release bundle.
# Usage:  sudo ./upgrade.sh
# Reads NODEPOOL_IMAGE / NODEPOOL_ROOT from the .env in the same directory.
set -euo pipefail

cd "$(dirname "$0")"

REPO="laoXuHome/NodePoolME"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) BUNDLE_ARCH="amd64" ;;
  aarch64|arm64) BUNDLE_ARCH="arm64" ;;
  *) BUNDLE_ARCH="amd64" ;;
esac

if [ ! -f .env ]; then
  echo "!! .env not found in $(pwd)" >&2
  exit 1
fi

# shellcheck disable=SC1091
IMAGE="$(grep -E '^NODEPOOL_IMAGE=' .env | tail -1 | cut -d= -f2- | tr -d '"' | xargs)"
if [ -z "${IMAGE:-}" ]; then
  echo "!! NODEPOOL_IMAGE missing from .env" >&2
  exit 1
fi

TAG="${IMAGE##*:}"
echo "==> target image: $IMAGE"

# "latest" is fine for docker pull, but release bundles are published per version tag,
# so resolve the newest release when the image tag is latest.
BUNDLE_TAG="$TAG"
if [ "$TAG" = "latest" ]; then
  BUNDLE_TAG="$(curl -fsSL --connect-timeout 10 \
    "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null \
    | grep -m1 '"tag_name"' | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/')" || true
  if [ -z "$BUNDLE_TAG" ]; then
    echo "!! cannot resolve latest release tag; pass the version explicitly or retry later" >&2
    exit 2
  fi
  echo "==> latest resolves to release $BUNDLE_TAG"
fi

pull_ok=0
echo "==> trying: docker compose pull"
if docker compose pull; then
  pull_ok=1
else
  echo "!! registry pull failed, switching to release bundle for tag $BUNDLE_TAG"
fi

if [ "$pull_ok" -eq 0 ]; then
  BUNDLE="nodepoolme-${BUNDLE_TAG}-${BUNDLE_ARCH}.tar.gz"
  URL="https://github.com/${REPO}/releases/download/${BUNDLE_TAG}/${BUNDLE}"
  TMP="/tmp/${BUNDLE}"
  echo "==> downloading $URL"
  if ! curl -fL --retry 3 --connect-timeout 15 -o "$TMP" "$URL"; then
    echo "!! download failed. If GitHub needs the proxy, retry with:" >&2
    echo "   curl -fL -x http://192.168.33.250:7080 -o $TMP $URL" >&2
    exit 2
  fi
  echo "==> importing image from $BUNDLE ($(du -h "$TMP" | cut -f1))"
  gunzip -c "$TMP" | docker load
  rm -f "$TMP"
fi

echo "==> recreating container"
docker compose up -d

echo "==> done, current image:"
docker inspect --format '{{.Config.Image}} ({{index .Config.Labels "org.opencontainers.image.revision"}})' \
  "$(docker compose ps -q 2>/dev/null | head -1)" 2>/dev/null || docker compose images
