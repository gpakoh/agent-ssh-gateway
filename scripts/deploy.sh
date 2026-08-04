#!/usr/bin/env bash
# Rebuild + redeploy the gateway stack with BUILD_SHA/BUILD_TIME actually
# populated (T81.3) — plain `docker compose up -d --build` leaves both
# "unknown" in /health forever, since docker-compose.yml's build args only
# get real values if the calling shell exports them first.
#
# Usage: docker/../scripts/deploy.sh [extra docker compose args...]
# Example: scripts/deploy.sh web-ssh-gateway mcp-server
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

export BUILD_SHA="$(git rev-parse HEAD)"
export BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "BUILD_SHA=${BUILD_SHA}"
echo "BUILD_TIME=${BUILD_TIME}"

docker compose -p web-ssh-gateway -f docker/docker-compose.yml up -d --build "$@"
