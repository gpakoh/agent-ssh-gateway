#!/usr/bin/env bash
# Trusted master-push CI only. Stream one fixed root-owned host secret via a
# short-lived, read-only Docker helper. Never relax the host file's 0600 mode,
# put the token in argv/environment, write it to an artifact, or echo it to logs.
# Always pipe stdout directly into docker-login --password-stdin or a
# root-owned single-file secret volume provisioner.
set -euo pipefail
set +x

if [ "$#" -ne 0 ]; then
  echo "::error::registry secret streamer takes no arguments" >&2
  exit 64
fi
if [ "${GITHUB_EVENT_NAME:-}" != "push" ] || [ "${GITHUB_REF_NAME:-}" != "master" ]; then
  echo "::error::registry secret streamer is restricted to master push jobs" >&2
  exit 64
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "::error::Docker daemon is required to stream the root-only registry token" >&2
  exit 1
fi

# This is the same preinstalled, operator-controlled image used for the deploy
# job; resolve its immutable local image ID before invoking it. No network or
# pulling is permitted. The host bind path is an exact nonconfigurable value.
helper_image="sha256:dfd0f8d9c05f49a0b99f6812ae76809eaa79fb1c36963ca79078d2ce827caab8"
helper_id="$(docker image inspect --format '{{.Id}}' "$helper_image")"
if [ "$helper_id" != "$helper_image" ]; then
  echo "::error::registry secret helper image has no exact local image ID" >&2
  exit 1
fi

docker run --rm --pull=never \
  --user 0:0 --read-only --network none --cap-drop ALL \
  --security-opt no-new-privileges \
  --mount 'type=bind,source=/media/1TB/Python/quart-platform/infra-quart/secrets/registry.token,target=/run/registry.token,readonly' \
  --entrypoint /bin/cat "$helper_id" /run/registry.token
