# API Enhancements — Port Checker, Session Info, Env Inspect, Config, SSH Keys, Command Templates

**Date:** 2026-05-28
**Status:** Approved

## Overview

Add 6 new API capabilities to the SSH Gateway to make it more self-sufficient for AI agents. All features are independent and can be implemented in parallel.

## 1. Port Checker

`GET /api/ssh/check-port?host=X&port=22`

Async TCP connect with 5s timeout. Does NOT require an SSH session or API auth. Returns:

```json
{"host": "10.0.0.1", "port": 22, "reachable": true, "duration_ms": 42}
```

- **File:** `app/routers/ssh.py`
- **Auth:** None (like /health)
- **Implementation:** `asyncio.open_connection()` with timeout

## 2. Session Info

`GET /api/ssh/sessions` — extended response

Current: returns list of session IDs.
New: returns list of objects with `session_id`, `host`, `port`, `username`, `connected_at`, `last_command_at`, `idle_seconds`.

- **File:** `app/routers/ssh.py`
- **Model:** New `SessionInfoResponse(BaseModel)` with list of `SessionInfo`
- **Data source:** `_state.manager._sessions dict` — each SessionRecord has these fields

## 3. Env Inspect

`GET /api/ssh/session/{session_id}/env?prefix=PATH`

Executes `printenv` through existing SSH session, parses stdout into key-value JSON. Optional `prefix` filter.

- **File:** `app/routers/ssh.py`
- **Implementation:** `_state.manager.execute(session_id, "printenv")` + stdout parsing

## 4. Config

`GET /api/config`

Returns runtime configuration from `settings` object. All secrets masked as `****`.

```json
{
  "session_timeout": 3600,
  "cleanup_interval": 300,
  "ssh_default_timeout": 30,
  "max_sessions_per_ip": 10,
  "persistent_sessions_enabled": false,
  "agent_token_enabled": true,
  "known_hosts_store": "file",
  "api_auth_enabled": true,
  "rate_limit_requests": 100,
  "rate_limit_window": 60
}
```

- **File:** `app/routers/system.py`
- **Secrets masked:** `ENCRYPTION_KEY`, `API_KEY`, `AGENT_TOKEN`, `DATABASE_URL`

## 5. SSH Key Upload

`POST /api/ssh/keys` — multipart form upload

Uploads private key file, stores in `/app/ssh_keys/` (container volume). Returns key name and path.

```json
{"name": "my-key.pem", "path": "/app/ssh_keys/my-key.pem", "size": 1675}
```

- **File:** `app/routers/ssh.py`
- **Validation:** Must start with `-----BEGIN`, must not contain `-----END RSA PRIVATE KEY-----` (reject weak formats), must not exceed 64KB
- **Storage:** Write to `/app/ssh_keys/` (mounted volume, read-only mounted as `:ro` — need to check if write works on the volume source)

## 6. Command Templates

`GET /api/templates` — list predefined commands
`POST /api/templates/run` — execute a template with params

Predefined templates:
| ID | Name | Command | Params |
|---|---|---|---|
| `deploy` | Deploy service | `systemctl restart {service} && systemctl status {service}` | service |
| `healthcheck` | Service health | `systemctl is-active --quiet {service} && echo "active" \|\| echo "inactive"` | service |
| `disk-usage` | Disk usage | `df -h {path}` | path=/ |
| `memory` | Memory status | `free -h` | — |
| `docker-ps` | Docker processes | `docker ps --format '{{.ID}}\t{{.Image}}\t{{.Status}}\t{{.Names}}'` | — |
| `docker-stats` | Docker stats | `docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'` | — |
| `nginx-reload` | Reload nginx | `nginx -t && systemctl reload nginx` | — |

- **File:** New `app/routers/templates.py`
- **Models:** `TemplateInfo`, `TemplateRunRequest`, `TemplateRunResponse`
- **Security:** Commands use parameterized substitution (not shell injection), then pass through `sanitize_command`

## Router Map

| Feature | Method | Path | Router File | Auth |
|---|---|---|---|---|
| Port checker | GET | `/api/ssh/check-port` | ssh.py | None |
| Session info | GET | `/api/ssh/sessions` | ssh.py | API Key |
| Env inspect | GET | `/api/ssh/session/{id}/env` | ssh.py | API Key |
| Config | GET | `/api/config` | system.py | API Key |
| SSH keys | POST | `/api/ssh/keys` | ssh.py | API Key |
| Templates | GET | `/api/templates` | templates.py | API Key |
| Templates | POST | `/api/templates/run` | templates.py | API Key |

## Tags

Add `"templates"` to `TAGS_META` + `_path_tag`. `"port-checker"` optional (can go under `"ssh"` tag).

## Testing

- Port checker: mock socket connect, test unreachable case
- Session info: mock SessionRecord with all fields
- Env inspect: mock `manager.execute` to return fake env output
- Config: assert secrets masked
- SSH keys: mock file write, test validation
- Templates: test list returns all predefined, test run with param substitution
