# Router structure

`agent-ssh-gateway` uses small domain routers instead of one large API router.

## System / meta

- `app/routers/system.py`

Contains only lightweight system and meta endpoints:

- `GET /health`
- `GET /api/capabilities`
- `GET /api/config`
- `GET /api/help`
- `GET /metrics`
- `GET /api/sdk/download`
- `GET /api/circuit-breaker/stats`
- `GET /`

This router should not contain feature-specific POST/DELETE workflows.

`GET /health` retains the legacy `status`, `ready` and `components`
fields. The optional `command_plane` object adds a bounded, non-auth status:

- `state=ready` means the manager is initialized and required SSH/Redis/PG
  probes succeeded; it is not a guarantee that arbitrary remote targets work.
- `state=starting`, `failure_class=not_initialized`, `retry_after_s=3`
  means the SSH manager has not initialized (for example during boot).
- `state=overloaded`, `failure_class=timeout`, `retry_after_s=5`
  means a required bounded health probe timed out. This does **not** prove CPU
  saturation; it tells callers to back off instead of re-authenticating.
- `state=unavailable` with `connect_error|unavailable` and a bounded
  `retry_after_s=5` means a required dependency is unavailable.
- `state=unknown` means a legacy Gateway response or insufficient evidence;
  do not infer command readiness, lost credentials or expired sessions.

The object also includes `build_sha` and `started_at`. A failed MCP-to-Gateway
health transport returns a sanitized `command_plane` with
`failure_class=transport_timeout|transport_unavailable`; an auth failure
returns `state=unknown`, `failure_class=auth_failure`, **without** a
retry-after hint. The MCP health tool preserves a Gateway-reported state
without overriding it. Existing aggregate readiness and per-component
failure semantics remain unchanged.

A session's existence is not inferable from aggregate health. Check a
particular session using authenticated `POST /api/session/check`. The
`SESSION_NOT_FOUND` response must be handled for that session only; do not
blindly repeat a mutating command or reauthenticate after a transport timeout.
During deployments, tools may disappear briefly while the Gateway restarts;
read `command_plane`, compare `build_sha`, and retry only idempotent reads
after the advertised bounded delay. If the health endpoint itself is
unreachable, no server-side state can be established.


## Feature routers

- `app/routers/auth.py` — auth diagnostic routes (whoami, auth check)
- `app/routers/diagnostics.py` — latency breakdown, system info, session check
- `app/routers/servers.py` — server inventory and connection routes
- `app/routers/snapshots.py` — snapshot create/list/restore/delete routes
- `app/routers/webhooks.py` — webhook and deployment routes
- `app/routers/known_hosts.py` — SSH known-hosts management
- `app/routers/batch.py` — batch execution routes
- `app/routers/search_replace.py` — global search/replace routes
- `app/routers/code.py` — code search/generation/completion routes
- `app/routers/project_inspection.py` — project analytics and tree routes

## Rule of thumb

New feature-specific endpoints should go into a dedicated feature router.

`system.py` should stay limited to system health, metadata, help, metrics, SDK download, and UI root endpoints.
