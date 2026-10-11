#!/usr/bin/env bash
# ci-docker-push-retry.sh — push a single image tag, retrying transient
# registry/network failures. Mirrors ci-docker-build-retry.sh and
# ci-docker-login-retry.sh. docker push is idempotent: a failed attempt can
# be retried without side effects (uploaded layers are deduplicated).
set -uo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 IMAGE[:TAG]" >&2
  exit 64
fi

image="$1"

max_attempts="${CI_DOCKER_PUSH_MAX_ATTEMPTS:-3}"
retry_delay_seconds="${CI_DOCKER_PUSH_RETRY_DELAY_SECONDS:-10}"

case "$max_attempts" in
  ''|*[!0-9]*|0) echo "invalid CI_DOCKER_PUSH_MAX_ATTEMPTS: $max_attempts" >&2; exit 2 ;;
esac
case "$retry_delay_seconds" in
  ''|*[!0-9]*) echo "invalid CI_DOCKER_PUSH_RETRY_DELAY_SECONDS: $retry_delay_seconds" >&2; exit 2 ;;
esac

push_log="$(mktemp)"
cleanup() {
  rm -f "$push_log"
}
trap cleanup EXIT

is_transient_failure() {
  grep -Eqi \
    'Client\.Timeout exceeded|context deadline exceeded|i/o timeout|TLS handshake timeout|connection reset|connection refused|Temporary failure in name resolution|Network is unreachable|no such host|unexpected EOF|proxyconnect tcp: dial tcp|HTTP[^[:digit:]]*5[0-9]{2}|statusCode=5[0-9]{2}|502[[:space:]]+Bad Gateway|503[[:space:]]+Service Unavailable|504[[:space:]]+Gateway Timeout' \
    "$push_log"
}

attempt=1
while [ "$attempt" -le "$max_attempts" ]; do
  : > "$push_log"
  docker push "$image" 2>&1 | tee "$push_log"
  rc=${PIPESTATUS[0]}

  if [ "$rc" -eq 0 ]; then
    exit 0
  fi

  if ! is_transient_failure; then
    echo "docker push failed with a non-transient error; not retrying" >&2
    exit "$rc"
  fi

  if [ "$attempt" -ge "$max_attempts" ]; then
    echo "docker push failed after ${max_attempts} transient attempts: $image" >&2
    exit "$rc"
  fi

  echo "::warning::docker push attempt ${attempt}/${max_attempts} hit a transient network/registry error; retrying in ${retry_delay_seconds}s: $image" >&2
  sleep "$retry_delay_seconds"
  attempt=$((attempt + 1))
done

exit 1