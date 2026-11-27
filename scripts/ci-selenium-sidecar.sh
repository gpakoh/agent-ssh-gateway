#!/usr/bin/env bash
# Disposable E2E sidecar lifecycle, fenced to one job and Docker Engine.
# Keep a private job-local receipt across failed steps. Never source it as shell.
set -euo pipefail
umask 077

fail() { echo "::error::Selenium sidecar: $*" >&2; exit 1; }
valid_id() { [[ "$1" =~ ^[0-9a-f]{64}$ ]]; }
valid_engine() { [[ "$1" =~ ^[A-Za-z0-9:_.-]{1,128}$ ]]; }
engine() {
    local observed
    observed=$(timeout -k 5 20 docker info --format '{{.ID}}' 9<&-) || return 1
    valid_engine "$observed" || return 1
    printf '%s\n' "$observed"
}
check_engine() {
    local observed
    observed=$(engine) || fail 'cannot observe Docker Engine identity'
    [[ "$observed" == "$engine_id" ]] || fail 'Docker Engine identity changed'
}
write_state() {
    local temporary
    temporary=$(mktemp "$receipt_dir/.state.XXXXXX")
    printf '%s|%s|%s|%s\n' "$phase" "$engine_id" "$id" "$name" > "$temporary"
    mv -f -- "$temporary" "$receipt_dir/state"
}
load_state() {
    local extra rows
    [[ -f "$receipt_dir/state" && ! -L "$receipt_dir/state" ]] || fail 'missing receipt'
    mapfile -t rows < "$receipt_dir/state"
    [[ ${#rows[@]} == 1 ]] || fail 'invalid receipt length'
    IFS='|' read -r phase engine_id id name extra <<< "${rows[0]}"
    [[ "${rows[0]}" == "$phase|$engine_id|$id|$name" ]] || fail 'invalid receipt fields'
    valid_engine "$engine_id" || fail 'invalid receipt Engine'
    [[ "$name" == "$prefix-initial" || "$name" == "$prefix-replacement" ]] || fail 'invalid receipt name'
    case "$phase" in
        preparing|starting) [[ "$id" == - ]] || fail 'invalid pre-container receipt' ;;
        running|removing|removed) valid_id "$id" || fail 'invalid receipt container ID' ;;
        *) fail 'invalid receipt phase' ;;
    esac
}
prove_absent() {
    local rows
    check_engine
    rows=$(timeout -k 5 20 docker container ls -a --no-trunc --filter "id=$id" --format '{{.ID}}' 9<&-) || fail "cleanup observation unavailable for $id"
    check_engine
    [[ -z "$rows" ]] || fail "cleanup not proven for $id"
}
retire() {
    check_engine
    if [[ "$phase" == running ]]; then
        # Atomic receipt publication before the only removal dispatch. A lost
        # response or interrupted step permits observation, not another rm.
        phase=removing
        write_state
        if ! timeout -k 5 20 docker rm -f "$id" 9<&- >/dev/null 2>&1; then
            echo "::warning::Selenium removal response uncertain for $id; checking inventory" >&2
        fi
    fi
    prove_absent
    phase=removed
    write_state
}
start() {
    local image="$1" suffix="$2" observed
    [[ "$image" =~ @sha256:[0-9a-f]{64}$ ]] || fail 'digest-pinned image required'
    check_engine
    name="$prefix-$suffix"
    id=-
    # Image acquisition cannot create a sidecar. Record that fact before the
    # potentially slow registry operation, and never let run pull implicitly.
    phase=preparing
    write_state
    if ! timeout -k 5 20 docker image inspect "$image" 9<&- >/dev/null 2>&1; then
        timeout -k 5 600 docker pull "$image" 9<&- >&2 || fail 'pinned image acquisition failed before container dispatch'
    fi
    check_engine
    timeout -k 5 20 docker image inspect "$image" 9<&- >/dev/null || fail 'pinned image unavailable before container dispatch'
    timeout -k 5 20 docker inspect "$job_container" 9<&- >/dev/null || fail 'job container unavailable'
    check_engine
    phase=starting
    write_state
    # Bound the CLI wait, not the daemon outcome: a timeout leaves starting.
    observed=$(timeout -k 5 180 docker run -d --pull=never --name "$name" \
        --network "container:${job_container}" \
        --shm-size=2g --cpus=2 --memory=3g \
        -e SE_SESSION_REQUEST_TIMEOUT=600 \
        -e SE_OPTS='--session-timeout 840' "$image" 9<&-) || fail "creation outcome uncertain for $name"
    valid_id "$observed" || fail "creation returned invalid ID for $name"
    check_engine
    id="$observed"
    phase=running
    write_state
    printf '%s\n' "$id"
}

mode="${1:-}"
job_container=$(hostname)
[[ "$job_container" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$ ]] || fail 'invalid job container hostname'
run_attempt="${GITHUB_RUN_ATTEMPT:-1}"
[[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ && "$run_attempt" =~ ^[0-9]+$ ]] || fail 'missing run identity'
prefix="ci-selenium-${GITHUB_RUN_ID}-${run_attempt}-${job_container}"
receipt_dir="${TMPDIR:-/tmp}/$prefix"
if [[ "$mode" == start ]]; then
    mkdir -m 700 -- "$receipt_dir" || fail 'receipt directory already exists or is unavailable'
elif [[ ! -e "$receipt_dir" && ! -L "$receipt_dir" ]]; then
    # Failed steps may lose GITHUB_ENV updates. An absent receipt and an
    # absent environment ID do not prove that no create was dispatched.
    fail 'missing job receipt; cleanup outcome unknown'
fi
[[ -d "$receipt_dir" && ! -L "$receipt_dir" && $(stat -c '%a' "$receipt_dir") == 700 ]] || fail 'untrusted receipt directory'
# BusyBox timeout forks a watchdog which can outlive the wrapped CLI.
# Keep the lock in this shell, never in timeout or its child processes.
exec 9< "$receipt_dir"
flock -n -x 9 || fail 'another sidecar operation is active'
case "$mode" in
    start)
        engine_id=$(engine) || fail 'cannot observe Docker Engine identity'
        start "${2:?pinned image required}" initial
        ;;
    replace)
        load_state
        [[ "$phase" == running && "$name" == "$prefix-initial" && "${E2E_SELENIUM_CONTAINER:-}" == "$id" ]] || fail 'replacement requires the exact initial running receipt'
        retire
        start "${2:?pinned image required}" replacement
        ;;
    finalize)
        load_state
        case "$phase" in
            preparing) check_engine ;; # No container creation was dispatched.
            starting) fail "unresolved creation: $name" ;;
            running|removing) retire ;;
            removed) prove_absent ;;
        esac
        ;;
    *) fail 'expected start, replace, or finalize' ;;
esac
