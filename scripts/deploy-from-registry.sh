#!/usr/bin/env bash
# Pull the latest CI-built web-ssh-gateway/mcp-server images, redeploy,
# smoke-test, and roll back to the last known-good digest if the smoke
# check fails. Mirrors quart-core's infra-quart/scripts/deploy-quart-core.sh.
#
# Deploys and records state by *digest* (repo@sha256:...), never by the
# floating :latest tag — if :latest gets overwritten by a bad push before
# a rollback is needed, rolling back to ":latest" would just redeploy the
# same bad image again. A digest reference is immutable regardless of
# what :latest currently points to.
#
# This is the CI/registry deploy path (docker compose pull && up). For
# local dev iteration, use scripts/deploy.sh instead (builds from source,
# no rollback tracking).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# WEB_SSH_GATEWAY_REPO / MCP_SERVER_REPO name the real registry host —
# never hardcoded here (this script is tracked in a public repo). Real
# values live in the gitignored docker/.env, same place
# docker-compose.yml's own real secrets (API_KEY, JWT_SECRET, ...) live;
# `set -a` exports everything .env defines so both this script's own
# variables and the `docker compose` calls below see them.
if [ -f docker/.env ]; then
  set -a
  # shellcheck disable=SC1091
  source docker/.env
  set +a
fi

COMPOSE="${COMPOSE:-docker compose -p web-ssh-gateway -f docker/docker-compose.yml}"
GATEWAY_REPO="${WEB_SSH_GATEWAY_REPO:?set WEB_SSH_GATEWAY_REPO in docker/.env}"
MCP_REPO="${MCP_SERVER_REPO:?set MCP_SERVER_REPO in docker/.env}"
STATE_DIR="$ROOT/.state"
STATE_FILE="$STATE_DIR/web-ssh-gateway-deploy.json"
mkdir -p "$STATE_DIR"
# world-writable: this script may run as root (manual, on the host) or as
# a non-root UID (the CI deploy job, via the bind-mounted host checkout)
# — same rationale as deploy-quart-core.sh's STATE_DIR handling.
chmod 777 "$STATE_DIR" 2>/dev/null || true

log() { echo "[$(date -Is)] $*"; }

image_id() {
  docker inspect --format '{{.Image}}' "$1" 2>/dev/null || echo ""
}

repo_digest() {
  docker inspect --format '{{index .RepoDigests 0}}' "$1" 2>/dev/null || echo ""
}

wait_docker_health() {
  local name="$1" container="$2" timeout="$3"
  echo -n "  $name: "
  local start
  start=$(date +%s)
  while true; do
    local status
    status=$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || echo "missing")
    if [ "$status" = "healthy" ]; then
      echo "OK"
      return 0
    fi
    if (( $(date +%s) - start >= timeout )); then
      echo "FAIL (status=$status after ${timeout}s)"
      return 1
    fi
    sleep 2
  done
}

smoke() {
  local ok=true
  # No curl against localhost:8085 here — this script may run from a CI
  # job container whose "localhost" is its own network namespace, not the
  # host's (same class of bug quart-core's deploy script documents).
  # web-ssh-gateway's and mcp-server's own Dockerfile HEALTHCHECKs already
  # run inside each container; wait_docker_health reads that via `docker
  # inspect`, which is the same signal without the cross-namespace problem.
  # 120s: the gateway HEALTHCHECK is interval 30s / retries 3, so a cold
  # first boot can legitimately take ~60-90s before reporting healthy.
  wait_docker_health "web-ssh-gateway" web-ssh-gateway 120 || ok=false
  wait_docker_health "mcp-server"      mcp-server      120 || ok=false
  $ok
}

read_state_field() {
  python3 -c "
import json
try:
    d = json.load(open('$STATE_FILE'))
    print(d.get('$1', ''))
except Exception:
    print('')
"
}

write_state() {
  python3 -c "
import json
json.dump(
    {
        'gateway_image': '''$1''',
        'mcp_server_image': '''$2''',
        'deployed_at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
    },
    open('$STATE_FILE', 'w'),
    indent=2,
)
"
  chmod 666 "$STATE_FILE" 2>/dev/null || true
}

deploy_services() {
  # One container per `up -d` call, not both together -- mirrors
  # deploy-quart-core.sh's fix for a Compose rename-swap race when
  # recreating multiple containers in a single call.
  local gateway_image="$1" mcp_image="$2"
  WEB_SSH_GATEWAY_IMAGE="$gateway_image" $COMPOSE up -d --no-deps --no-build web-ssh-gateway
  MCP_SERVER_IMAGE="$mcp_image" $COMPOSE up -d --no-deps --no-build mcp-server
}

log "=== deploy-from-registry: checking for a new image ==="

$COMPOSE pull web-ssh-gateway mcp-server

RUNNING_GATEWAY_ID=$(image_id web-ssh-gateway)
RUNNING_MCP_ID=$(image_id mcp-server)
PULLED_GATEWAY_ID=$(docker images --no-trunc --format '{{.ID}}' "$GATEWAY_REPO:latest" | head -1)
PULLED_MCP_ID=$(docker images --no-trunc --format '{{.ID}}' "$MCP_REPO:latest" | head -1)

if [ "$RUNNING_GATEWAY_ID" = "$PULLED_GATEWAY_ID" ] && [ "$RUNNING_MCP_ID" = "$PULLED_MCP_ID" ]; then
  log "Up to date — nothing to deploy."
  exit 0
fi

PREVIOUS_GATEWAY_IMAGE=$(read_state_field gateway_image)
PREVIOUS_MCP_IMAGE=$(read_state_field mcp_server_image)

# Pin by digest for the actual deploy + state recording — never :latest.
NEW_GATEWAY_IMAGE=$(repo_digest "$GATEWAY_REPO:latest")
NEW_MCP_IMAGE=$(repo_digest "$MCP_REPO:latest")

log "New image detected — deploying $NEW_GATEWAY_IMAGE / $NEW_MCP_IMAGE"
deploy_services "$NEW_GATEWAY_IMAGE" "$NEW_MCP_IMAGE"

log "Smoke-testing..."
if smoke; then
  write_state "$NEW_GATEWAY_IMAGE" "$NEW_MCP_IMAGE"
  log "Deploy OK — recorded as last known good."
  exit 0
fi

log "Smoke test FAILED."

if [ -z "$PREVIOUS_GATEWAY_IMAGE" ]; then
  log "No previous known-good image recorded (first deploy) — cannot roll back. Leaving the failed deploy in place for manual investigation."
  exit 1
fi

log "Rolling back to $PREVIOUS_GATEWAY_IMAGE / $PREVIOUS_MCP_IMAGE"
deploy_services "$PREVIOUS_GATEWAY_IMAGE" "$PREVIOUS_MCP_IMAGE"

if smoke; then
  log "Rollback OK — $NEW_GATEWAY_IMAGE failed smoke, reverted to $PREVIOUS_GATEWAY_IMAGE."
else
  log "Rollback ALSO failed smoke — manual investigation required."
fi

exit 1
