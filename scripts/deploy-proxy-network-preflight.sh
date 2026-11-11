#!/usr/bin/env bash
# Check the existing, operator-owned proxy-parser network before any rollout.
# Never create a replacement network: an empty bridge would strand SSH agents
# without their approved proxy provider and could make rollback fail too.
set -euo pipefail

proxy_network="${GATEWAY_PROXY_PARSER_NETWORK:-proxy-parser_default}"
if [[ ! "$proxy_network" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
  echo "Gateway proxy network preflight: NO-GO (invalid network name)" >&2
  exit 1
fi
if ! docker network inspect "$proxy_network" >/dev/null 2>&1; then
  echo "Gateway proxy network preflight: NO-GO (required external network unavailable; no rollout attempted)" >&2
  exit 1
fi
echo "Gateway proxy network preflight: OK"
