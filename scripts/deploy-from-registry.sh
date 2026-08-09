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
# CI's deploy job passes DEPLOY_SHA=${{ github.sha }}, the exact commit
# that passed every other job and triggered this deploy -- build-and-push
# already tags images with both :latest and :$DEPLOY_SHA. Deploying the
# SHA tag instead of resolving :latest's digest at pull time is
# defense-in-depth alongside ci.yml's own top-level `concurrency:` block
# (which now serializes master runs of the whole workflow): without
# DEPLOY_TAG, `docker compose pull` + a fresh `docker inspect ...:latest`
# moments later could still observe a different image than the one that
# triggered this specific run (e.g. a manual re-run/workflow_dispatch
# replay) -- silently deploying the wrong commit without either deploy
# run failing or even noticing. :latest is kept as the fallback for
# manual/local invocation, where no DEPLOY_SHA exists.
DEPLOY_TAG="${DEPLOY_SHA:-latest}"
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

validate_image_ref() {
  # STATE_FILE is intentionally world-writable (chmod 777/666 above) so
  # both the manual-root path and the CI job's non-root UID can write it
  # -- but that also means any other local account on this host can edit
  # it. Without this check, a tampered gateway_image/mcp_server_image
  # field would be fed straight into `docker compose up` as the ROLLBACK
  # target with no validation at all. Only accept the exact digest-pinned
  # shape repo_digest() itself produces for the expected repo -- anything
  # else (a different image, a local path, garbage) fails the deploy
  # instead of silently deploying it.
  local ref="$1" expected_repo="$2"
  if [[ "$ref" =~ ^${expected_repo}@sha256:[0-9a-f]{64}$ ]]; then
    return 0
  fi
  return 1
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

run_migrations() {
  # M15: app/main.py's startup already runs Base.metadata.create_all()
  # (create_tables() on audit_log_store/event_hook_store/delivery_service)
  # as its own startup-resilience mechanism for a DB that somehow never
  # got migrated -- every existing Alembic migration (001/002/003) checks
  # before acting specifically so it stays a real no-op once create_all()
  # already produced that shape. This step's job is only to keep
  # alembic_version stamped/current going forward, so a *future* migration
  # gets a real signal instead of depending on every author remembering
  # the same defensive-guard convention. Runs inside the already-deployed
  # gateway container -- it already has alembic + the app code + the real
  # DATABASE_URL, on the same network as mcp-postgres.
  docker exec web-ssh-gateway alembic upgrade head
}

alembic_revision() {
  # Last non-INFO line of `alembic current`'s output is the revision id
  # (e.g. "003_webhook_delivery_headers (head)"), or empty on a container
  # that doesn't exist / can't reach the DB -- callers treat empty as
  # "unknown", not as "unchanged".
  docker exec "$1" alembic current 2>&1 | tail -1 || true
}

log "=== deploy-from-registry: checking for a new image (tag: $DEPLOY_TAG) ==="

# Pull the exact tag we're about to check/deploy directly -- not via
# `$COMPOSE pull`, which follows docker-compose.yml's own (env-substituted,
# usually :latest-defaulting) image references and would silently pull the
# wrong tag whenever DEPLOY_TAG is a SHA.
docker pull "$GATEWAY_REPO:$DEPLOY_TAG"
docker pull "$MCP_REPO:$DEPLOY_TAG"

RUNNING_GATEWAY_ID=$(image_id web-ssh-gateway)
RUNNING_MCP_ID=$(image_id mcp-server)
PULLED_GATEWAY_ID=$(docker images --no-trunc --format '{{.ID}}' "$GATEWAY_REPO:$DEPLOY_TAG" | head -1)
PULLED_MCP_ID=$(docker images --no-trunc --format '{{.ID}}' "$MCP_REPO:$DEPLOY_TAG" | head -1)

if [ "$RUNNING_GATEWAY_ID" = "$PULLED_GATEWAY_ID" ] && [ "$RUNNING_MCP_ID" = "$PULLED_MCP_ID" ]; then
  log "Up to date — nothing to deploy."
  exit 0
fi

PREVIOUS_GATEWAY_IMAGE=$(read_state_field gateway_image)
PREVIOUS_MCP_IMAGE=$(read_state_field mcp_server_image)

# Pin by digest for the actual deploy + state recording — never a floating
# tag (:latest or otherwise).
NEW_GATEWAY_IMAGE=$(repo_digest "$GATEWAY_REPO:$DEPLOY_TAG")
NEW_MCP_IMAGE=$(repo_digest "$MCP_REPO:$DEPLOY_TAG")

# MAJOR audit finding: rollback below reverts the application images but
# has never reverted the DB schema -- Alembic has no downgrade step here,
# and a real auto-downgrade is its own hazard (data loss on a migration
# that dropped/renamed a column). The correct discipline is that
# migrations themselves stay backward-compatible with the previous app
# version for at least one release (expand/contract), which no script
# can enforce after the fact. What this script CAN do honestly: notice
# whether a migration actually advanced the schema before a rollback, and
# say so plainly instead of printing an unqualified "Rollback OK" that
# implies full reversion when only the application was reverted.
PRE_DEPLOY_REVISION=$(alembic_revision web-ssh-gateway)

log "New image detected — deploying $NEW_GATEWAY_IMAGE / $NEW_MCP_IMAGE"
deploy_services "$NEW_GATEWAY_IMAGE" "$NEW_MCP_IMAGE"

log "Running database migrations (alembic upgrade head)..."
if ! run_migrations; then
  log "Database migration FAILED."
elif smoke; then
  write_state "$NEW_GATEWAY_IMAGE" "$NEW_MCP_IMAGE"
  log "Deploy OK — recorded as last known good."
  exit 0
else
  log "Smoke test FAILED."
fi

POST_MIGRATION_REVISION=$(alembic_revision web-ssh-gateway)

if [ -z "$PREVIOUS_GATEWAY_IMAGE" ]; then
  log "No previous known-good image recorded (first deploy) — cannot roll back. Leaving the failed deploy in place for manual investigation."
  exit 1
fi

if ! validate_image_ref "$PREVIOUS_GATEWAY_IMAGE" "$GATEWAY_REPO" || ! validate_image_ref "$PREVIOUS_MCP_IMAGE" "$MCP_REPO"; then
  log "Rollback state in $STATE_FILE does not look like a valid digest-pinned image reference — refusing to roll back to it. Leaving the failed deploy in place for manual investigation."
  exit 1
fi

log "Rolling back to $PREVIOUS_GATEWAY_IMAGE / $PREVIOUS_MCP_IMAGE"
deploy_services "$PREVIOUS_GATEWAY_IMAGE" "$PREVIOUS_MCP_IMAGE"

SCHEMA_ADVANCED=false
if [ -n "$PRE_DEPLOY_REVISION" ] && [ -n "$POST_MIGRATION_REVISION" ] && [ "$PRE_DEPLOY_REVISION" != "$POST_MIGRATION_REVISION" ]; then
  SCHEMA_ADVANCED=true
fi

if smoke; then
  if [ "$SCHEMA_ADVANCED" = "true" ]; then
    log "Rollback PARTIAL — application reverted to $PREVIOUS_GATEWAY_IMAGE and passed smoke, but the DB schema was NOT reverted: still at '$POST_MIGRATION_REVISION' (was '$PRE_DEPLOY_REVISION' before this deploy). The rolled-back application is now running against a newer schema than it shipped with -- verify compatibility manually; downgrading the schema automatically is not attempted here (real data-loss risk on some migrations)."
  else
    log "Rollback OK — $NEW_GATEWAY_IMAGE failed smoke, reverted to $PREVIOUS_GATEWAY_IMAGE. No schema change to reconcile (migrations were a no-op or never ran)."
  fi
else
  log "Rollback ALSO failed smoke — manual investigation required."
fi

exit 1
