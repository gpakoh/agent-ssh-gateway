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
