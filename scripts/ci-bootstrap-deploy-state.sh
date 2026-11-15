#!/usr/bin/env bash
# One-time LKG seeding pinned to successful Gitea CI release #19659.
# NEVER import the legacy host-checkout .state or trust a mutable :latest.
set -euo pipefail

: "$DEPLOY_STATE_DIR"
: "$REGISTRY"
test -n "$DEPLOY_STATE_DIR" && test -n "$REGISTRY"
state_file="$DEPLOY_STATE_DIR/web-ssh-gateway-deploy.json"
if [ ! -d "$DEPLOY_STATE_DIR" ] || [ -L "$DEPLOY_STATE_DIR" ]; then
  echo "::error::Deploy-state volume is absent or symlinked" >&2
  exit 1
fi
# Never rehabilitate an existing untrusted receipt by changing its mode.
if [ -e "$state_file" ] || [ -L "$state_file" ]; then
  python3 scripts/deploy_state.py preflight "$DEPLOY_STATE_DIR"
  exit 0
fi
if [ -n "$(ls -A "$DEPLOY_STATE_DIR")" ]; then
  echo "::error::Unrecognized contents in new deploy-state volume" >&2
  exit 1
fi

# Recovery from an actually empty dedicated volume is permitted ONLY for
# independently attested exact image sets, never arbitrary healthy containers.
# Original #19659 qualified the historic seed; #25510 (2026-11-14) passed
# build, deploy AND host-smoke for 891f3482 and was independently inspected
# against the five immutable container refs below (see Issue #909).
# This does NOT repair an invalid existing receipt or infer a baseline from
# mutable :latest, a legacy checkout, or an unauthenticated health response.
historic_gateway="$REGISTRY/gpakoh/agent-ssh-gateway@sha256:138039b5da85ce0cb30c5b593b413b654d0dadd9d1712bb3bd39aa405f5ed0de"
accepted_gateway="$REGISTRY/gpakoh/agent-ssh-gateway@sha256:8723a10a93546e746ae122d66ab6c97f56f8ba4e68ce05a5ec75b6f8f69981ac"
live_gateway="$(docker inspect --format '{{.Config.Image}}' web-ssh-gateway)"
case "$live_gateway" in
  "$historic_gateway")
    baseline_sha=b4c6c9c6d268f8db638f22d4c9256832dddcd875
    gateway_ref="$historic_gateway"
    mcp_ref="$REGISTRY/gpakoh/mcp-server@sha256:a9a13014490aac48fb3f6a401061d09cc039c7ae7b355f65c551aff5aebde0ad"
    sshd_ref="$REGISTRY/gpakoh/ssh-gateway-sshd@sha256:0c96682a4b8903b31bc0245d3bcbafec5750ed38be4eed52dd4966c644bc4ed8"
    ;;
  "$accepted_gateway")
    baseline_sha=891f3482b339711616af3d92c42903001cc69dee
    gateway_ref="$accepted_gateway"
    mcp_ref="$REGISTRY/gpakoh/mcp-server@sha256:157b3873d91d6c3ae2eeab3bf99c0fcfa78b9a6c036e16b32ee28f682729f8e0"
    sshd_ref="$REGISTRY/gpakoh/ssh-gateway-sshd@sha256:f616f78acc163eaa6745bae249dc5beba17e97237a7c67761db417b84a3386a4"
    ;;
  *)
    echo "::error::No independently attested release matches the running Gateway image" >&2
    exit 1
    ;;
esac

verify_live() {
  local name="$1" expected="$2" image health
  image="$(docker inspect --format '{{.Config.Image}}' "$name")"
  health="$(docker inspect --format '{{.State.Health.Status}}' "$name")"
  if [ "$image" != "$expected" ] || [ "$health" != healthy ]; then
    echo "::error::$name is not the healthy attested image" >&2
    exit 1
  fi
}
verify_live web-ssh-gateway "$gateway_ref"
verify_live mcp-server "$mcp_ref"
verify_live mcp-oauth "$mcp_ref"
verify_live ssh-gateway-sshd "$sshd_ref"
verify_live ssh-gateway-agent-sshd "$sshd_ref"
for name in web-ssh-gateway mcp-server mcp-oauth; do
  actual="$(docker exec "$name" printenv BUILD_SHA)"
  if [ "$actual" != "$baseline_sha" ]; then
    echo "::error::$name BUILD_SHA is not independently attested" >&2
    exit 1
  fi
done

# Permission changes are permitted ONLY on a proven empty dedicated volume.
deploy_uid="$(id -u)"
if [ "$(stat -c %u "$DEPLOY_STATE_DIR")" != "$deploy_uid" ]; then
  chown "$deploy_uid:$(id -g)" "$DEPLOY_STATE_DIR"
fi
chmod 0700 "$DEPLOY_STATE_DIR"
python3 scripts/deploy_state.py preflight "$DEPLOY_STATE_DIR"
python3 scripts/deploy_state.py write "$DEPLOY_STATE_DIR" \
  "$gateway_ref" "$mcp_ref" "$sshd_ref" "$baseline_sha"
python3 scripts/deploy_state.py preflight "$DEPLOY_STATE_DIR"
for field in gateway_image mcp_server_image sshd_image deploy_sha; do
  case "$field" in
    gateway_image) expected="$gateway_ref" ;;
    mcp_server_image) expected="$mcp_ref" ;;
    sshd_image) expected="$sshd_ref" ;;
    deploy_sha) expected="$baseline_sha" ;;
  esac
  actual="$(python3 scripts/deploy_state.py read "$DEPLOY_STATE_DIR" "$field")"
  if [ "$actual" != "$expected" ]; then
    echo "::error::Post-write LKG receipt verification failed: $field" >&2
    exit 1
  fi
done
echo "Seeded trusted baseline LKG for attested release $baseline_sha"
