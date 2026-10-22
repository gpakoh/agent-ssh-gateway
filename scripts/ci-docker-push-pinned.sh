#!/usr/bin/env bash
# Publish only the immutable image ID captured after the build/smoke gate.
# Never use :latest as a substitute for a missing release SHA tag.
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: $0 IMAGE_ID IMAGE_REPOSITORY RELEASE_SHA" >&2
  exit 64
fi
image_id="$1"
repository="$2"
release_sha="$3"
if [[ ! "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] ||
   [[ ! "$release_sha" =~ ^[0-9a-f]{40}$ ]] ||
   [[ ! "$repository" =~ ^[A-Za-z0-9._:-]+/[A-Za-z0-9._/-]+$ ]]; then
  echo "::error::invalid immutable image publication arguments" >&2
  exit 64
fi

observed="$(docker image inspect --format '{{.Id}}' "$image_id")" || {
  echo "::error::captured image ID no longer exists locally" >&2
  exit 1
}
if [ "$observed" != "$image_id" ]; then
  echo "::error::captured image identity mismatch" >&2
  exit 1
fi

sha_tag="${repository}:${release_sha}"
latest_tag="${repository}:latest"
# Exact SHA tags are immutable: an unexpected existing tag must be refused.
if observed="$(docker image inspect --format '{{.Id}}' "$sha_tag" 2>/dev/null)"; then
  if [ "$observed" != "$image_id" ]; then
    echo "::error::release SHA tag refers to a different image" >&2
    exit 1
  fi
else
  docker tag "$image_id" "$sha_tag"
fi
if [ "$(docker image inspect --format '{{.Id}}' "$sha_tag")" != "$image_id" ]; then
  echo "::error::release SHA tag changed before publication" >&2
  exit 1
fi
# Publish the release SHA first. The image ID was independently smoke-tested.
bash scripts/ci-docker-push-retry.sh "$sha_tag"

# :latest is mutable, but it must point at the very same tested image here.
docker tag "$image_id" "$latest_tag"
if [ "$(docker image inspect --format '{{.Id}}' "$latest_tag")" != "$image_id" ]; then
  echo "::error::latest tag changed before publication" >&2
  exit 1
fi
bash scripts/ci-docker-push-retry.sh "$latest_tag"
