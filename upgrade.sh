#!/usr/bin/env bash
# NodePoolME upgrade helper: pull the image first, fall back to the GitHub release bundle.
#
# Usage:
#   sudo npup                   upgrade to the image tag already in .env
#   sudo npup v2.003            pin that version in .env, then upgrade
#   sudo npup --offline v2.003  skip registry pull and use the release bundle
#   sudo npup /path/to/dir      upgrade a project living elsewhere
#
# The release bundle contains:
#   nodepoolme-<tag>-amd64.tar       docker save archive
#   docker-compose.yml, .env.example, README.md, Dockerfile, INSTALL.md
set -Eeuo pipefail

REPO="laoXuHome/NodePoolME"
DEFAULT_DIR="/vol1/1000/docker/compose/nodepoolme"
TARGET_TAG=""
OFFLINE_ONLY=0

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --offline|-o) OFFLINE_ONLY=1; shift ;;
    *) break ;;
  esac
done

if [ $# -gt 0 ] && printf '%s' "$1" | grep -Eq '^v?[0-9]+\.[0-9]+(\.[0-9]+)?$'; then
  case "$1" in
    v*) TARGET_TAG="$1" ;;
    *)  TARGET_TAG="v$1" ;;
  esac
  shift
fi

PROJECT_DIR="${1:-${NODEPOOL_DIR:-}}"
if [ -z "$PROJECT_DIR" ]; then
  SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
  if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/docker-compose.yml" ] && [ -f "$SELF_DIR/.env" ]; then
    PROJECT_DIR="$SELF_DIR"
  else
    PROJECT_DIR="$DEFAULT_DIR"
  fi
fi

cd "$PROJECT_DIR" || { echo "!! project dir not found: $PROJECT_DIR" >&2; exit 1; }
echo "==> project dir: $PROJECT_DIR"

if [ ! -f .env ]; then
  echo "!! .env not found in $(pwd)" >&2
  exit 1
fi

if [ -n "$TARGET_TAG" ]; then
  echo "==> pinning .env to $TARGET_TAG"
  if grep -q '^NODEPOOL_IMAGE=' .env; then
    sed -i -E "s#^(NODEPOOL_IMAGE=.*):[^:]+#\1:${TARGET_TAG}#" .env
  else
    printf '\nNODEPOOL_IMAGE=ghcr.io/laoxuhome/nodepoolme:%s\n' "$TARGET_TAG" >> .env
  fi
fi

IMAGE_LINE="$(grep -E '^NODEPOOL_IMAGE=' .env | tail -1 || true)"
IMAGE="$(printf '%s' "$IMAGE_LINE" | cut -d= -f2- | tr -d '"' | xargs || true)"
if [ -z "${IMAGE:-}" ]; then
  echo "!! NODEPOOL_IMAGE missing from .env" >&2
  exit 1
fi
case "$IMAGE" in
  *@sha256:*) echo "!! NODEPOOL_IMAGE uses a digest; pin a version tag instead" >&2; exit 1 ;;
esac

TAG="${IMAGE##*:}"
echo "==> target image: $IMAGE"

# The state directory is new in v1.10/P-1.8. Create it before compose starts so
# the bind mount and the maintenance/instance-lock files have a stable home.
NODEPOOL_ROOT="$(grep -E '^NODEPOOL_ROOT=' .env | tail -1 | cut -d= -f2- | tr -d '"' | xargs || true)"
mkdir -p "${NODEPOOL_ROOT:-/opt/nodepool}/state"

if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  echo "!! docker compose not found" >&2
  exit 1
fi

latest_release_tag() {
  local tag=""
  tag="$(curl -fsSL --connect-timeout 10 \
    "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null \
    | grep -m1 '"tag_name"' | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/' || true)"
  if [ -z "$tag" ]; then
    # API rate-limited or blocked: follow the releases/latest redirect.
    tag="$(curl -fsSLI -o /dev/null -w '%{url_effective}' --connect-timeout 10 \
      "https://github.com/${REPO}/releases/latest" 2>/dev/null \
      | sed -n 's#.*/tag/##p' || true)"
  fi
  printf '%s' "$tag"
}

# "latest" is fine for docker pull, but release bundles are published per version tag.
BUNDLE_TAG="$TAG"
if [ "$TAG" = "latest" ]; then
  BUNDLE_TAG="$(latest_release_tag)"
  if ! printf '%s' "$BUNDLE_TAG" | grep -Eq '^v[0-9]+\.[0-9]+(\.[0-9]+)?$'; then
    echo "!! cannot resolve latest release tag; pass the version explicitly or retry later" >&2
    exit 2
  fi
  echo "==> latest resolves to release $BUNDLE_TAG"
fi
case "$BUNDLE_TAG" in
  v*) ;;
  *) BUNDLE_TAG="v$BUNDLE_TAG" ;;
esac

pull_ok=0
if [ "$OFFLINE_ONLY" -eq 1 ]; then
  echo "==> skipping registry pull (--offline)"
else
  echo "==> trying: ${DC[*]} pull"
  if "${DC[@]}" pull; then
    pull_ok=1
  else
    echo "!! registry pull failed, switching to release bundle for tag $BUNDLE_TAG"
  fi
fi

if [ "$pull_ok" -eq 1 ] && ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "!! pull reported success but $IMAGE is missing; switching to release bundle"
  pull_ok=0
fi

if [ "$pull_ok" -eq 0 ]; then
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64|amd64) BUNDLE_ARCH="amd64" ;;
    *)
      echo "!! no offline bundle for $ARCH; releases only publish linux/amd64" >&2
      echo "   retry with registry access on an amd64 host" >&2
      exit 2
      ;;
  esac

  BUNDLE="nodepoolme-${BUNDLE_TAG}-offline-${BUNDLE_ARCH}.tar.gz"
  URL="https://github.com/${REPO}/releases/download/${BUNDLE_TAG}/${BUNDLE}"
  SUMS="nodepoolme-${BUNDLE_TAG}-sha256sums.txt"
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT

  echo "==> downloading $URL"
  if ! curl -fL --retry 3 --connect-timeout 15 -o "$TMP/$BUNDLE" "$URL"; then
    echo "!! download failed. If GitHub needs the proxy, retry with:" >&2
    echo "   curl -fL -x http://192.168.33.250:7080 -o /tmp/$BUNDLE $URL" >&2
    exit 2
  fi

  if curl -fsSL --connect-timeout 15 -o "$TMP/$SUMS" \
      "https://github.com/${REPO}/releases/download/${BUNDLE_TAG}/${SUMS}"; then
    echo "==> verifying SHA-256"
    ( cd "$TMP" && sha256sum -c "$SUMS" )
  else
    echo "!! checksum file unavailable; continuing without verification" >&2
  fi

  echo "==> importing image from $BUNDLE ($(du -h "$TMP/$BUNDLE" | cut -f1))"
  if tar -tzf "$TMP/$BUNDLE" 2>/dev/null | grep -qx 'manifest.json'; then
    # v1.x bundle: the gzip itself is a docker-save archive.
    docker load -i "$TMP/$BUNDLE"
  else
    # v2.x bundle: gzip contains an image tar plus compose/docs.
    mkdir -p "$TMP/unpack"
    tar -xzf "$TMP/$BUNDLE" -C "$TMP/unpack"
    IMAGE_TAR="$(find "$TMP/unpack" -type f -name '*.tar' -print -quit)"
    if [ -z "$IMAGE_TAR" ]; then
      echo "!! no image tar found inside $BUNDLE" >&2
      exit 3
    fi
    docker load -i "$IMAGE_TAR"
  fi

  # The release tar keeps the version tag; make the tag in .env resolve to it too
  # when .env still says latest.
  SOURCE_IMAGE="ghcr.io/laoxuhome/nodepoolme:${BUNDLE_TAG}"
  if ! docker image inspect "$SOURCE_IMAGE" >/dev/null 2>&1; then
    echo "!! loaded image tag not found: $SOURCE_IMAGE" >&2
    exit 3
  fi
  if [ "$IMAGE" != "$SOURCE_IMAGE" ]; then
    docker tag "$SOURCE_IMAGE" "$IMAGE"
  fi
fi

echo "==> recreating container"
"${DC[@]}" up -d

echo "==> done, current image:"
CID="$("${DC[@]}" ps -q 2>/dev/null | head -1 || true)"
if [ -n "$CID" ]; then
  docker inspect --format '{{.Config.Image}} ({{index .Config.Labels "org.opencontainers.image.revision"}})' "$CID" \
    || "${DC[@]}" images
else
  "${DC[@]}" images
fi
