#!/usr/bin/env bash
# Provision the EMPTY dedicated LKG volume using a short-lived root helper.
# The actual deploy job is non-root and cannot chown Docker volumes.
# Never migrate or change an existing rollback receipt.
set -euo pipefail

[ -n "$REGISTRY" ] && [ -n "$RELEASE_SHA" ] && [ -n "$DEPLOY_STATE_DIR" ]
if [ "$DEPLOY_STATE_DIR" != /deploy/web-ssh-gateway-state ]; then
  echo "::error::Unexpected deploy-state mount" >&2
  exit 1
fi
if ! [[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::RELEASE_SHA must be a pinned Git commit" >&2
  exit 1
fi

# A repository-qualified immutable artifact from this exact CI run only.
helper_tag="$REGISTRY/gpakoh/mcp-server:$RELEASE_SHA"
helper_ref="$(docker image inspect --format '{{index .RepoDigests 0}}' "$helper_tag")"
case "$helper_ref" in
  "$REGISTRY/gpakoh/mcp-server@sha256:"*) ;;
  *) echo "::error::Untrusted helper image repository" >&2; exit 1 ;;
esac
helper_digest="$(printf '%s' "$helper_ref" | sed 's/^.*@sha256://')"
if ! [[ "$helper_digest" =~ ^[0-9a-f]{64}$ ]]; then
  echo "::error::Unpinned helper image digest" >&2
  exit 1
fi

deploy_uid="$(id -u)"
deploy_gid="$(id -g)"
# Mount exactly one named volume. An existing receipt is left untouched;
# Python deploy_state.py will validate it under the deploy job's actual UID.
docker run --rm --user 0:0 \
  -v "ssh-gateway-deploy-state:/trusted-state" \
  --entrypoint /bin/sh "$helper_ref" -ec '
    set -eu
    uid="$1"
    gid="$2"
    root=/trusted-state
    receipt="$root/web-ssh-gateway-deploy.json"
    [ -d "$root" ] && [ ! -L "$root" ] || exit 1
    if [ -e "$receipt" ] || [ -L "$receipt" ]; then
      exit 0
    fi
    if [ -n "$(ls -A "$root")" ]; then
      echo "Unrecognized contents in LKG volume" >&2
      exit 1
    fi
    owner="$(stat -c %u "$root")"
    if [ "$owner" != 0 ] && [ "$owner" != "$uid" ]; then
      echo "Unexpected LKG volume owner" >&2
      exit 1
    fi
    chown "$uid:$gid" "$root"
    chmod 0700 "$root"
    [ "$(stat -c %u "$root")" = "$uid" ] || exit 1
    [ "$(stat -c %a "$root")" = 700 ] || exit 1
  ' sh "$deploy_uid" "$deploy_gid"
