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
    'TLS handshake timeout|i/o timeout|connection reset by peer|Temporary failure in name resolution|no such host|net/http: request canceled|context deadline exceeded|client error \(Connect\)|operation timed out|502 Bad Gateway|503 Service Unavailable|504 Gateway Timeout|500 Internal Server Error' \
    "$log_file"
}

sanitize() {
  local value="${1:-}"
  if [ -z "$value" ]; then
    printf 'unknown'
    return 0
  fi
  printf '%s' "$value" | tr -c 'A-Za-z0-9._-' '_'
}

phase="unknown"
expect_dockerfile=0
for arg in "$@"; do
  if [ "$expect_dockerfile" -eq 1 ]; then
    phase=$(sanitize "$arg")
    expect_dockerfile=0
    continue
  fi
  case "$arg" in
    -f|--file)
      expect_dockerfile=1
      ;;
    -f=*|--file=*)
      phase=$(sanitize "${arg#*=}")
      ;;
    *)
      ;;
  esac
done

runner=$(sanitize "${RUNNER_NAME:-}")
run_id=$(sanitize "${GITHUB_RUN_ID:-}")
job=$(sanitize "${GITHUB_JOB:-}")

notice() {
  printf '::notice::docker-build-%s %s\n' "$1" "$2" >&2
}

attempt=1
while [ "$attempt" -le "$max_attempts" ]; do
  : > "$log_file"
  start_epoch=$(date +%s)
  notice start "phase=${phase} runner=${runner} run_id=${run_id} job=${job} attempt=${attempt}/${max_attempts}"
  docker build "$@" 2>&1 | tee "$log_file"
  rc=${PIPESTATUS[0]}
  duration_s=$(( $(date +%s) - start_epoch ))

  if [ "$rc" -eq 0 ]; then
    notice end "phase=${phase} runner=${runner} run_id=${run_id} job=${job} attempt=${attempt}/${max_attempts} status=success duration_s=${duration_s}"
    exit 0
  fi

  if ! is_transient_network_failure; then
    notice end "phase=${phase} runner=${runner} run_id=${run_id} job=${job} attempt=${attempt}/${max_attempts} status=nonretryable_failure duration_s=${duration_s}"
    echo "docker build failed with a non-retryable error; not retrying" >&2
    exit "$rc"
  fi

  notice end "phase=${phase} runner=${runner} run_id=${run_id} job=${job} attempt=${attempt}/${max_attempts} status=transient_failure duration_s=${duration_s}"

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

