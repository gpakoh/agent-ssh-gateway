#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 REGISTRY USERNAME" >&2
  exit 64
fi

registry="$1"
username="$2"
IFS= read -r password
if [ -z "$password" ]; then
  echo "::error::registry password is empty" >&2
  exit 64
fi

login_log="$(mktemp)"
cleanup() {
  rm -f "$login_log"
  unset password
}
trap cleanup EXIT

is_transient_failure() {
  grep -Eqi \
    'Client\.Timeout exceeded|context deadline exceeded|i/o timeout|TLS handshake timeout|connection reset|connection refused|Temporary failure in name resolution|Network is unreachable|no such host|unexpected EOF|HTTP[^[:digit:]]*5[0-9]{2}|statusCode=5[0-9]{2}' \
    "$login_log"
}

max_attempts=3
last_status=1
for attempt in $(seq 1 "$max_attempts"); do
  : >"$login_log"
  if printf '%s\n' "$password" | docker login "$registry" -u "$username" --password-stdin >"$login_log" 2>&1; then
    cat "$login_log"
    exit 0
  else
    last_status=$?
  fi

  cat "$login_log" >&2
  if ! is_transient_failure; then
    echo "::error::registry login failed with a non-transient error; not retrying" >&2
    exit "$last_status"
  fi

  if [ "$attempt" -lt "$max_attempts" ]; then
    delay=$((attempt * 5))
    echo "::warning::registry login attempt ${attempt}/${max_attempts} hit a transient network/service error; retrying in ${delay}s..." >&2
    sleep "$delay"
  fi
done

echo "::error::registry login failed after ${max_attempts} transient attempts" >&2
exit "$last_status"
