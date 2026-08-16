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
# CI publishes the executor next to the gateway artifact.
SSHD_REPO="${SSH_GATEWAY_SSHD_REPO:-${GATEWAY_REPO%/*}/ssh-gateway-sshd}"
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
      if [ "$container" = "ssh-gateway-sshd" ] || [ "$container" = "ssh-gateway-agent-sshd" ]; then
        local memory_bytes
        memory_bytes=$(docker inspect --format '{{.HostConfig.Memory}}' "$container" 2>/dev/null || echo "0")
        if ! [[ "$memory_bytes" =~ ^[0-9]+$ ]] || (( memory_bytes < 17179869184 )); then
          echo "FAIL (memory ceiling=${memory_bytes:-unknown}, required>=17179869184)"
          return 1
        fi
      fi
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
  wait_docker_health "ssh-gateway-sshd" ssh-gateway-sshd 120 || ok=false
  wait_docker_health "ssh-gateway-agent-sshd" ssh-gateway-agent-sshd 120 || ok=false
  # No curl against localhost:8085 here — this script may run from a CI
  # job container whose "localhost" is its own network namespace, not the
  # host's (same class of bug quart-core's deploy script documents).
  # web-ssh-gateway's and mcp-server's own Dockerfile HEALTHCHECKs already
  # run inside each container; wait_docker_health reads that via `docker
  # inspect`, which is the same signal without the cross-namespace problem.
  # 120s: the gateway HEALTHCHECK is interval 30s / retries 3, so a cold
  # first boot can legitimately take ~60-90s before reporting healthy.
  wait_docker_health "web-ssh-gateway" web-ssh-gateway 120 || ok=false
  # The dedicated executor has its own hostname. Strict KnownHostsPolicy
  # intentionally rejects any hostname that has not been explicitly trusted,
  # so merely starting a healthy agent-sshd is not enough to make it usable.
  # Bootstrap trust through the gateway's authenticated known-hosts API from
  # inside the gateway container. API_KEY stays in the container environment;
  # only the non-secret executor host/port are supplied to docker exec.
  if $ok; then
    echo -n "  agent-sshd (known host): "
    if docker exec -e AGENT_SSH_HOST="${MCP_AGENT_EXECUTOR_SSH_HOST:-agent-sshd}" -e AGENT_SSH_PORT="${MCP_AGENT_EXECUTOR_SSH_PORT:-2222}" web-ssh-gateway python3 scripts/ensure-agent-known-host.py; then
      echo "OK"
    else
      echo "FAIL"; ok=false
  wait_docker_health "mcp-server"      mcp-server      120 || ok=false
  # mcp-oauth (the public ChatGPT-facing OAuth endpoint, port 8788) reuses
  # the same mcp-server image but was, until this check existed, never
  # actually redeployed by this script at all -- deploy_services() below
  # only ever recreated web-ssh-gateway/mcp-server, so every CI-built image
  # landed in the registry with real provenance but mcp-oauth kept running
  # whatever had last been deployed to it manually (P0 audit finding:
  # "MCP runtime build_sha: unknown" -- root cause was this exact gap, not
  # a code bug in how BUILD_SHA is baked in or reported). Its own Docker
  # HEALTHCHECK (scripts/mcp_oauth_healthcheck.py) already does a real
  # authenticated MCP initialize+tools/list through the OAuth provider --
  # unlike mcp-server's HEALTHCHECK, which deliberately hits an
  # auth-exempt /healthz -- so wait_docker_health here is already a
  # meaningful smoke check, no separate docker-exec step needed.
  wait_docker_health "mcp-oauth"       mcp-oauth       120 || ok=false

  # P1 BLOCKER audit finding: wait_docker_health above only proves each
  # container's own HEALTHCHECK passes (process readiness) -- mcp-server's
  # HEALTHCHECK in particular hits /healthz, deliberately exempt from
  # bearer auth, so neither check had ever exercised the real API surface
  # or the auth boundary. `docker exec` runs inside each container's own
  # namespace regardless of where this script itself is running, so the
  # same cross-namespace problem the comment above avoids doesn't apply
  # here either. Only run these once the containers are already healthy
  # -- no point authenticating against a service still booting.
  if $ok; then
    # No -e API_KEY/-e MCP_STREAMABLE_HTTP_BEARER_TOKEN here -- `docker
    # exec` already inherits the TARGET container's own environment by
    # default, which is what's actually authoritative for this running
    # instance (set by docker-compose.yml at container start). Reading
    # this script's own sourced docker/.env instead would test against
    # what's on disk right now, not what the deployed container is
    # actually configured with -- a subtle drift if those two ever
    # disagree.
    echo -n "  web-ssh-gateway (authenticated): "
    if docker exec web-ssh-gateway python3 scripts/gateway_black_box_smoke.py; then
      echo "OK"
    else
      echo "FAIL"
      ok=false
    fi
    echo -n "  mcp-server (authenticated MCP):  "
    if docker exec mcp-server python3 scripts/mcp_black_box_smoke.py; then
      echo "OK"
    else
      echo "FAIL"
      ok=false
    fi
    verify_provenance || ok=false
  fi
  $ok
}

verify_provenance() {
  # P0 BLOCKER audit finding: nothing ever confirmed the container actually
  # running after a deploy is the commit that triggered it -- a stuck
  # `docker compose up -d` (wrong image resolved, a stale local tag
  # shadowing the pulled one, ...) could leave the OLD code running while
  # every check above still reports healthy. Only meaningful when
  # DEPLOY_TAG is a real commit SHA (CI always sets DEPLOY_SHA); a manual
  # invocation with no DEPLOY_SHA falls back to the floating :latest tag,
  # which has no single commit to compare against.
  if [ "$DEPLOY_TAG" = "latest" ]; then
    return 0
  fi
  local ok=true
  local name sha
  for name in web-ssh-gateway mcp-server mcp-oauth; do
    sha=$(docker exec "$name" printenv BUILD_SHA 2>/dev/null || echo "")
    if [ "$sha" != "$DEPLOY_TAG" ]; then
      echo "  $name: provenance MISMATCH (running BUILD_SHA='$sha', expected '$DEPLOY_TAG')"
      ok=false
    else
      echo "  $name: provenance OK ($sha)"
    fi
  done
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

validate_sshd_image_ref() {
  local ref="$1" expected_running_id="$2"
  if validate_image_ref "$ref" "$SSHD_REPO"; then
    return 0
  fi
  [ -n "$expected_running_id" ] && [ "$ref" = "$expected_running_id" ] && [[ "$ref" =~ ^sha256:[0-9a-f]{64}$ ]]
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
        'sshd_image': '''$3''',
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
  # recreating multiple containers in a single call. mcp-oauth reuses the
  # same MCP_SERVER_IMAGE as mcp-server (see docker-compose.yml) -- both
  # must be redeployed together or mcp-oauth silently drifts from what
  # was just pushed/rolled back (see verify_provenance()).
  local gateway_image="$1" mcp_image="$2" sshd_image="$3"
  SSH_GATEWAY_SSHD_IMAGE="$sshd_image" WEB_SSH_GATEWAY_IMAGE="$gateway_image" $COMPOSE up -d --no-deps --no-build sshd web-ssh-gateway
  SSH_GATEWAY_SSHD_IMAGE="$sshd_image" $COMPOSE up -d --no-deps --no-build agent-sshd
  MCP_SERVER_IMAGE="$mcp_image" $COMPOSE up -d --no-deps --no-build mcp-server
  MCP_SERVER_IMAGE="$mcp_image" $COMPOSE up -d --no-deps --no-build mcp-oauth
}

publish_agent_source_bundle() {
  # Agents must never clone from the mutable host checkout. CI checked out the
  # exact DEPLOY_SHA that passed the workflow, so publish that Git object graph
  # as an immutable bundle into a dedicated volume. sshd sees this volume
  # read-only; mcp-oauth is the only runtime writer.
  if [ "$DEPLOY_TAG" = "latest" ]; then
    log "Agent source bundle: skipped for unpinned manual :latest deploy."
    return 0
  fi
  if ! [[ "$DEPLOY_TAG" =~ ^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$ ]]; then
    log "Agent source bundle: invalid deploy SHA '$DEPLOY_TAG'."
    return 1
  fi

  local checkout_sha project_key bundle_tmp container_dir container_path container_tmp bundle_head
  checkout_sha=$(git rev-parse HEAD 2>/dev/null || true)
  if [ "$checkout_sha" != "$DEPLOY_TAG" ]; then
    log "Agent source bundle: checkout SHA mismatch ($checkout_sha != $DEPLOY_TAG)."
    return 1
  fi
  project_key=$(python3 -c 'from examples.mcp_server.agent_paths import project_state_key; print(project_state_key("web-ssh-gateway"))')
  bundle_tmp=$(mktemp /tmp/mcp-agent-source.XXXXXX.bundle)
  if ! git bundle create "$bundle_tmp" HEAD; then
    rm -f "$bundle_tmp"
    return 1
  fi
  bundle_head=$(git bundle list-heads "$bundle_tmp" HEAD 2>/dev/null | awk 'NR==1 {print $1}')
  if [ "$bundle_head" != "$DEPLOY_TAG" ]; then
    log "Agent source bundle: generated HEAD mismatch ($bundle_head != $DEPLOY_TAG)."
    rm -f "$bundle_tmp"
    return 1
  fi

  container_dir="/var/lib/mcp-agent/sources/$project_key"
  container_path="$container_dir/${DEPLOY_TAG,,}.bundle"
  container_tmp="$container_path.tmp.$$"
  if ! docker exec mcp-oauth mkdir -p "$container_dir"; then
    rm -f "$bundle_tmp"
    return 1
  fi
  if ! docker exec -i mcp-oauth python3 -c 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())' "$container_tmp" < "$bundle_tmp"; then
    rm -f "$bundle_tmp"
    return 1
  fi
  rm -f "$bundle_tmp"

  bundle_head=$(docker exec mcp-oauth git bundle list-heads "$container_tmp" HEAD 2>/dev/null | awk 'NR==1 {print $1}')
  if [ "$bundle_head" != "$DEPLOY_TAG" ]; then
    log "Agent source bundle: container verification mismatch ($bundle_head != $DEPLOY_TAG)."
    docker exec mcp-oauth python3 -c 'import pathlib,sys; pathlib.Path(sys.argv[1]).unlink(missing_ok=True)' "$container_tmp" || true
    return 1
  fi
  if ! docker exec mcp-oauth python3 -c 'import os,sys; os.replace(sys.argv[1], sys.argv[2])' "$container_tmp" "$container_path"; then
    log "Agent source bundle: atomic publication failed."
    docker exec mcp-oauth python3 -c 'import pathlib,sys; pathlib.Path(sys.argv[1]).unlink(missing_ok=True)' "$container_tmp" || true
    return 1
  fi
  bundle_head=$(docker exec mcp-oauth git bundle list-heads "$container_path" HEAD 2>/dev/null | awk 'NR==1 {print $1}')
  if [ "$bundle_head" != "$DEPLOY_TAG" ]; then
    log "Agent source bundle: final verification mismatch ($bundle_head != $DEPLOY_TAG)."
    docker exec mcp-oauth python3 -c 'import pathlib,sys; pathlib.Path(sys.argv[1]).unlink(missing_ok=True)' "$container_path" || true
    return 1
  fi
  log "Agent source bundle published: $container_path"
  return 0
}


run_migrations() {
  # Persistent-session schema ownership belongs to Alembic. SessionStore
  # no longer creates ssh_sessions during startup, so a deploy must migrate
  # successfully before the new image is considered
  # healthy/usable. Runs inside the already-deployed gateway container -- it
  # carries Alembic, the exact application models and the authoritative
  # DATABASE_URL on the same network as PostgreSQL.
  docker exec web-ssh-gateway alembic upgrade head
}

restart_gateway_after_migrations() {
  # The first container start happens before migrations so Alembic can run
  # from the new image. Restart once after the schema is current so startup
  # session restoration observes the migrated ownership columns.
  docker restart web-ssh-gateway >/dev/null
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
docker pull "$SSHD_REPO:$DEPLOY_TAG"

RUNNING_SSHD_ID=$(image_id ssh-gateway-sshd)

PREVIOUS_GATEWAY_IMAGE=$(read_state_field gateway_image)
PREVIOUS_MCP_IMAGE=$(read_state_field mcp_server_image)
PREVIOUS_SSHD_IMAGE=$(read_state_field sshd_image)
PREVIOUS_SSHD_IMAGE="${PREVIOUS_SSHD_IMAGE:-$RUNNING_SSHD_ID}"

# Pin by digest for the actual deploy + state recording — never a floating
# tag (:latest or otherwise).
NEW_GATEWAY_IMAGE=$(repo_digest "$GATEWAY_REPO:$DEPLOY_TAG")
NEW_MCP_IMAGE=$(repo_digest "$MCP_REPO:$DEPLOY_TAG")
NEW_EXECUTOR_IMAGE=$(repo_digest "$SSHD_REPO:$DEPLOY_TAG")

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

log "Deploying $NEW_GATEWAY_IMAGE / $NEW_MCP_IMAGE / $NEW_EXECUTOR_IMAGE"
deploy_services "$NEW_GATEWAY_IMAGE" "$NEW_MCP_IMAGE" "$NEW_EXECUTOR_IMAGE"

log "Running database migrations (alembic upgrade head)..."
if ! run_migrations; then
  log "Database migration FAILED."
elif ! restart_gateway_after_migrations; then
  log "Gateway restart after database migration FAILED."
elif smoke; then
  if publish_agent_source_bundle; then
    write_state "$NEW_GATEWAY_IMAGE" "$NEW_MCP_IMAGE" "$NEW_EXECUTOR_IMAGE"
    log "Deploy OK — recorded as last known good."
    exit 0
  fi
  log "Agent source publication FAILED."
else
  log "Smoke test FAILED."
fi

POST_MIGRATION_REVISION=$(alembic_revision web-ssh-gateway)

if [ -z "$PREVIOUS_GATEWAY_IMAGE" ]; then
  log "No previous known-good image recorded (first deploy) — cannot roll back. Leaving the failed deploy in place for manual investigation."
  exit 1
fi

if ! validate_image_ref "$PREVIOUS_GATEWAY_IMAGE" "$GATEWAY_REPO" || ! validate_image_ref "$PREVIOUS_MCP_IMAGE" "$MCP_REPO" || ! validate_sshd_image_ref "$PREVIOUS_SSHD_IMAGE" "$RUNNING_SSHD_ID"; then
  log "Rollback state in $STATE_FILE does not look like a valid digest-pinned image reference — refusing to roll back to it. Leaving the failed deploy in place for manual investigation."
  exit 1
fi

log "Rolling back to $PREVIOUS_GATEWAY_IMAGE / $PREVIOUS_MCP_IMAGE / $PREVIOUS_SSHD_IMAGE"
deploy_services "$PREVIOUS_GATEWAY_IMAGE" "$PREVIOUS_MCP_IMAGE" "$PREVIOUS_SSHD_IMAGE"

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
