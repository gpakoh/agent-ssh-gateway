#!/usr/bin/env bash
set -uo pipefail

max_attempts="${CI_DOCKER_BUILD_MAX_ATTEMPTS:-3}"
retry_delay_seconds="${CI_DOCKER_BUILD_RETRY_DELAY_SECONDS:-10}"

case "$max_attempts" in
  ''|*[!0-9]*|0) echo "invalid CI_DOCKER_BUILD_MAX_ATTEMPTS: $max_attempts" >&2; exit 2 ;;
esac
case "$retry_delay_seconds" in
  ''|*[!0-9]*) echo "invalid CI_DOCKER_BUILD_RETRY_DELAY_SECONDS: $retry_delay_seconds" >&2; exit 2 ;;
esac

log_file=$(mktemp)
cleanup() {
  rm -f "$log_file"
}
trap cleanup EXIT

is_transient_network_failure() {
  grep -Eqi \
    'TLS handshake timeout|i/o timeout|connection reset by peer|Temporary failure in name resolution|no such host|net/http: request canceled|context deadline exceeded|client error \(Connect\)|operation timed out' \
    "$log_file"
}

attempt=1
while [ "$attempt" -le "$max_attempts" ]; do
  : > "$log_file"
  docker build "$@" 2>&1 | tee "$log_file"
  rc=${PIPESTATUS[0]}

  if [ "$rc" -eq 0 ]; then
    exit 0
  fi

  if ! is_transient_network_failure; then
    echo "docker build failed with a non-retryable error; not retrying" >&2
    exit "$rc"
  fi

  if [ "$attempt" -ge "$max_attempts" ]; then
    echo "docker build failed after ${attempt}/${max_attempts} transient-network attempts" >&2
    exit "$rc"
  fi

  echo "::warning::docker build attempt ${attempt}/${max_attempts} hit a transient network/registry error; retrying" >&2
  if [ "$retry_delay_seconds" -gt 0 ]; then
    sleep "$retry_delay_seconds"
  fi
  attempt=$((attempt + 1))
done

exit 1
