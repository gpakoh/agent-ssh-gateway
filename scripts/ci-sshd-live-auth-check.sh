#!/usr/bin/env bash
# Post-deploy SSHD acceptance: validate both running executors, never mutate them.
set -euo pipefail

if [ "$#" -ne 1 ] || [[ ! "$1" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::expected exact deployed Git SHA is required" >&2
  exit 2
fi
expected_sha="$1"

verify_live_executor() {
  local name="$1" running_sha effective setting
  running_sha="$(docker exec "$name" printenv BUILD_SHA)"
  if [ "$running_sha" != "$expected_sha" ]; then
    echo "::error::$name runs unexpected build SHA" >&2
    return 1
  fi
  effective="$(docker exec "$name" sshd -T -C user=mcpuser,host=localhost,addr=127.0.0.1)"
  for setting in \
    "passwordauthentication no" \
    "kbdinteractiveauthentication no" \
    "permitemptypasswords no" \
    "pubkeyauthentication yes" \
    "authenticationmethods publickey"; do
    if ! printf '%s\n' "$effective" | grep -Fqx "$setting"; then
      echo "::error::$name missing effective setting: $setting" >&2
      return 1
    fi
  done
  if ! docker exec "$name" /bin/sh -c 'grep -Eq "^mcpuser:NP:" /etc/shadow'; then
    echo "::error::$name is missing the non-crypt mcpuser password sentinel" >&2
    return 1
  fi
  echo "$name: pinned image, effective key-only auth, no usable password: PASS"
}

verify_live_executor ssh-gateway-sshd
verify_live_executor ssh-gateway-agent-sshd
