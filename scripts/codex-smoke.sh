#!/bin/bash
# Smoke tests for agent-ssh-gateway — designed to run from any agent session.
#
# Usage:
#   export API_KEY="afdvw9..."
#   bash scripts/codex-smoke.sh [BASE_URL]
#
# Default BASE_URL: http://127.0.0.1:8085

set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:8085}"
PASS=0
FAIL=0

green() { printf '\033[32m%s\033[0m\n' "$1"; }
red()   { printf '\033[31m%s\033[0m\n' "$1"; }
info()  { printf '\033[36m%s\033[0m\n' "$1"; }

check() {
    local label="$1" expected="$2" actual="$3"
    if [ "$actual" = "$expected" ]; then
        green "  PASS: $label"
        PASS=$((PASS + 1))
    else
        red "  FAIL: $label (expected=$expected got=$actual)"
        FAIL=$((FAIL + 1))
    fi
}

cleanup() {
    if [ -n "${SID:-}" ]; then
        curl -sf -X POST -H "X-API-Key: $API_KEY" \
            -H "Content-Type: application/json" \
            -d "{\"session_id\":\"$SID\"}" \
            "$BASE_URL/api/ssh/disconnect" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
info "1. Health (no auth)"
# ---------------------------------------------------------------------------
HEALTH=$(curl -sf "$BASE_URL/health" 2>/dev/null || echo "FAIL")
HEALTH_STATUS=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','FAIL'))" 2>/dev/null || echo "FAIL")
check "health status=ok" "ok" "$HEALTH_STATUS"

# ---------------------------------------------------------------------------
info "2. Capabilities (no auth)"
# ---------------------------------------------------------------------------
CAPS=$(curl -sf "$BASE_URL/api/capabilities" 2>/dev/null || echo "FAIL")
echo "  capabilities: $(echo "$CAPS" | head -c 200)"
case "$CAPS" in
    *version*|*auth_mode*) green "  PASS: capabilities has expected fields" && PASS=$((PASS+1)) ;;
    *) red "  FAIL: capabilities unexpected" && FAIL=$((FAIL+1)) ;;
esac

# ---------------------------------------------------------------------------
info "3. Auth — 401 without key"
# ---------------------------------------------------------------------------
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/api/ssh/sessions" 2>/dev/null || echo "000")
check "auth rejected (no key)" "401" "$CODE"

# ---------------------------------------------------------------------------
info "4. Auth — 401 with bad key"
# ---------------------------------------------------------------------------
CODE=$(curl -s -o /dev/null -w '%{http_code}' \
    -H "X-API-Key: bad-key-here" \
    "$BASE_URL/api/ssh/sessions" 2>/dev/null || echo "000")
check "auth rejected (bad key)" "401" "$CODE"

# ---------------------------------------------------------------------------
info "5. Auth — 200 with valid key"
# ---------------------------------------------------------------------------
CODE=$(curl -s -o /dev/null -w '%{http_code}' \
    -H "X-API-Key: $API_KEY" \
    "$BASE_URL/api/ssh/sessions" 2>/dev/null || echo "000")
check "auth accepted (valid key)" "200" "$CODE"

# ---------------------------------------------------------------------------
info "6. Sessions — list shape"
# ---------------------------------------------------------------------------
SESSIONS=$(curl -sf -H "X-API-Key: $API_KEY" "$BASE_URL/api/ssh/sessions" 2>/dev/null || echo "FAIL")
SESSION_COUNT=$(echo "$SESSIONS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('count', 'FAIL'))" 2>/dev/null || echo "FAIL")
case "$SESSION_COUNT" in
    ''|*[!0-9]*) red "  FAIL: sessions list unexpected" && FAIL=$((FAIL+1)) ;;
    *) green "  PASS: sessions list OK (count=$SESSION_COUNT)" && PASS=$((PASS+1)) ;;
esac

# ---------------------------------------------------------------------------
info "7. Servers — list (may be empty)"
# ---------------------------------------------------------------------------
SRV=$(curl -sf -H "X-API-Key: $API_KEY" "$BASE_URL/api/servers" 2>/dev/null || echo "FAIL")
case "$SRV" in
    *servers*|*count*) green "  PASS: servers list OK" && PASS=$((PASS+1)) ;;
    *) red "  FAIL: servers list unexpected" && FAIL=$((FAIL+1)) ;;
esac

# ---------------------------------------------------------------------------
# If SSH host is configured, test connect → execute → disconnect
# ---------------------------------------------------------------------------
SSH_HOST="${SSH_HOST:-}"
SSH_USER="${SSH_USER:-root}"
SSH_PORT="${SSH_PORT:-22}"
SSH_PASS="${SSH_PASS:-}"

if [ -n "$SSH_HOST" ]; then
    # -----------------------------------------------------------------------
    info "8. SSH connect"
    # -----------------------------------------------------------------------
    CONN=$(curl -sf -X POST -H "X-API-Key: $API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"host\":\"$SSH_HOST\",\"port\":$SSH_PORT,\"username\":\"$SSH_USER\",\"password\":\"$SSH_PASS\"}" \
        "$BASE_URL/api/ssh/connect" 2>/dev/null || echo "FAIL")
    SID=$(echo "$CONN" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id','FAIL'))" 2>/dev/null || echo "FAIL")
    if [ "$SID" != "FAIL" ] && [ -n "$SID" ]; then
        green "  PASS: connect returned session_id"
        PASS=$((PASS+1))
        green "  session_id=$SID"

        # -------------------------------------------------------------------
        info "9. SSH execute — echo hello"
        # -------------------------------------------------------------------
        EXEC=$(curl -sf -X POST -H "X-API-Key: $API_KEY" \
            -H "Content-Type: application/json" \
            -d "{\"session_id\":\"$SID\",\"command\":\"echo hello\",\"timeout\":10}" \
            "$BASE_URL/api/ssh/execute" 2>/dev/null || echo "FAIL")
        STDOUT=$(echo "$EXEC" | python3 -c "import sys,json; print(json.load(sys.stdin).get('stdout','').strip())" 2>/dev/null || echo "FAIL")
        check "execute stdout" "hello" "$STDOUT"

        # -------------------------------------------------------------------
        info "10. SSH execute — exit code"
        # -------------------------------------------------------------------
        EXEC2=$(curl -sf -X POST -H "X-API-Key: $API_KEY" \
            -H "Content-Type: application/json" \
            -d "{\"session_id\":\"$SID\",\"command\":\"exit 42\",\"timeout\":10}" \
            "$BASE_URL/api/ssh/execute" 2>/dev/null || echo "FAIL")
        EC=$(echo "$EXEC2" | python3 -c "import sys,json; print(json.load(sys.stdin).get('exit_code',''))" 2>/dev/null || echo "FAIL")
        check "execute exit_code=42" "42" "$EC"

        # -------------------------------------------------------------------
        info "11. Heartbeat"
        # -------------------------------------------------------------------
        HB=$(curl -sf -X POST -H "X-API-Key: $API_KEY" \
            -H "Content-Type: application/json" \
            -d "{\"session_id\":\"$SID\"}" \
            "$BASE_URL/api/ssh/heartbeat" 2>/dev/null || echo "FAIL")
        case "$HB" in
            *ok*) green "  PASS: heartbeat OK" && PASS=$((PASS+1)) ;;
            *) red "  FAIL: heartbeat unexpected" && FAIL=$((FAIL+1)) ;;
        esac

        # -------------------------------------------------------------------
        info "12. Disconnect"
        # -------------------------------------------------------------------
        DISC=$(curl -sf -X POST -H "X-API-Key: $API_KEY" \
            -H "Content-Type: application/json" \
            -d "{\"session_id\":\"$SID\"}" \
            "$BASE_URL/api/ssh/disconnect" 2>/dev/null || echo "FAIL")
        case "$DISC" in
            *disconnected*) green "  PASS: disconnect OK" && PASS=$((PASS+1)) ;;
            *) red "  FAIL: disconnect unexpected" && FAIL=$((FAIL+1)) ;;
        esac

        SID=""
    fi
else
    info "Skipping SSH connect/execute/disconnect — set SSH_HOST to enable"
fi

# ---------------------------------------------------------------------------
info "13. Agent token — generate"
# ---------------------------------------------------------------------------
TOKEN_RESP=$(curl -sf -X POST -H "X-API-Key: $API_KEY" \
    -H "Content-Type: application/json" -d '{}' \
    "$BASE_URL/api/agent/token" 2>/dev/null || echo "FAIL")
AGENT_TOKEN=$(echo "$TOKEN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token','FAIL'))" 2>/dev/null || echo "FAIL")
TOKEN_EXP=$(echo "$TOKEN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('expires_at','FAIL'))" 2>/dev/null || echo "FAIL")
if [ "$AGENT_TOKEN" != "FAIL" ] && [ -n "$AGENT_TOKEN" ] && [ "$TOKEN_EXP" != "FAIL" ] && [ -n "$TOKEN_EXP" ]; then
    green "  PASS: agent token generated (expires_at present)"
    PASS=$((PASS+1))
else
    red "  FAIL: agent token generated"
    FAIL=$((FAIL+1))
fi
if [ "$AGENT_TOKEN" != "FAIL" ] && [ -n "$AGENT_TOKEN" ]; then

    # -----------------------------------------------------------------------
    info "14. Agent token — use token as auth"
    # -----------------------------------------------------------------------
    CODE=$(curl -s -o /dev/null -w '%{http_code}' \
        -H "X-API-Key: $AGENT_TOKEN" \
        "$BASE_URL/api/ssh/sessions" 2>/dev/null || echo "000")
    check "agent token auth accepted" "200" "$CODE"
fi

# ---------------------------------------------------------------------------
info "15. Agent token — refresh"
# ---------------------------------------------------------------------------
REFRESH_RESP=$(curl -sf -X POST -H "X-API-Key: $API_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"token\":\"$AGENT_TOKEN\"}" \
    "$BASE_URL/api/agent/token/refresh" 2>/dev/null || echo "FAIL")
REFRESH_TOKEN=$(echo "$REFRESH_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token','FAIL'))" 2>/dev/null || echo "FAIL")
REFRESH_EXP=$(echo "$REFRESH_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('expires_at','FAIL'))" 2>/dev/null || echo "FAIL")
if [ "$REFRESH_TOKEN" != "FAIL" ] && [ -n "$REFRESH_TOKEN" ] && [ "$REFRESH_EXP" != "FAIL" ] && [ -n "$REFRESH_EXP" ]; then
    green "  PASS: agent token refreshed (expires_at present)"
    PASS=$((PASS+1))
else
    red "  FAIL: agent token refreshed"
    FAIL=$((FAIL+1))
fi
if [ "$REFRESH_TOKEN" != "FAIL" ] && [ -n "$REFRESH_TOKEN" ]; then

    # old agent token should now fail
    CODE=$(curl -s -o /dev/null -w '%{http_code}' \
        -H "X-API-Key: $AGENT_TOKEN" \
        "$BASE_URL/api/ssh/sessions" 2>/dev/null || echo "000")
    check "old agent token rejected after refresh" "401" "$CODE"
fi

# ---------------------------------------------------------------------------
info "16. Session config endpoint"
# ---------------------------------------------------------------------------
CFG=$(curl -sf -H "X-API-Key: $API_KEY" "$BASE_URL/api/config/session" 2>/dev/null || echo "FAIL")
case "$CFG" in
    *session_timeout*|*active_sessions*) green "  PASS: config OK" && PASS=$((PASS+1)) ;;
    *) red "  FAIL: config unexpected" && FAIL=$((FAIL+1)) ;;
esac

# ---------------------------------------------------------------------------
echo ""
info "=========================================="
info "  Results: $PASS passed, $FAIL failed"
info "=========================================="
echo ""

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
