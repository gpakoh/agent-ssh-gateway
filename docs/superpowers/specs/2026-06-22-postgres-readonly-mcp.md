# Postgres Read-Only MCP Adapter — Spec

## Goal

Expose `rag_vectordb` (PostgreSQL 15 + pgvector) as a read-only MCP adapter for ChatGPT AI agents behind `ssh.xloud.ru/mcp/postgres`.

Only `SELECT` queries on the `public` schema, with mandatory row limits, timeouts, and strict SQL guardrails. No write, no DDL, no system tables.

## Non-Goals

- Connecting to `kojo_db`, `gitea`, `n8n`, `immich`, `quart`, `astro`, or any other Postgres instance — only `rag_vectordb`.
- Write access (`INSERT`/`UPDATE`/`DELETE`/`COPY`).
- DDL (`CREATE`/`ALTER`/`DROP`/`TRUNCATE`).
- Admin operations (`VACUUM`/`ANALYZE`/`REFRESH MATERIALIZED VIEW`/`SET`/`GRANT`/`REVOKE`/`CALL`/`DO`).
- Exposing system schemas (`pg_catalog`, `information_schema` excluded from user queries).
- Schema evolution (`CREATE EXTENSION`, migrations — done manually by admin).

## Target Database

| Property | Value |
|----------|-------|
| Container | `rag-db` |
| Image | `pgvector/pgvector:pg15-bookworm` |
| DB name | `rag_vectordb` |
| Network | `10.10.10.127` (`proxmox_macvlan` + `internal_net`) |
| Port | 5433 (host mapping) |
| Current state | Empty (no user tables, pgvector extension not enabled) |
| Current user | `raguser` (SUPERUSER — DO NOT USE for MCP) |

## Read-Only User Policy

A dedicated `mcp_readonly` user must be created by the admin (not by the adapter):

```sql
CREATE USER mcp_readonly WITH PASSWORD '<generated-password>';
GRANT CONNECT ON DATABASE rag_vectordb TO mcp_readonly;
GRANT USAGE ON SCHEMA public TO mcp_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mcp_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO mcp_readonly;
ALTER USER mcp_readonly SET default_transaction_read_only = on;
ALTER USER mcp_readonly SET statement_timeout = '30s';
```

### Why separate user?

- `raguser` is SUPERUSER — one SQL injection or tool misuse can drop tables.
- `mcp_readonly` has only `SELECT` on `public`, no DDL, no system tables.
- `default_transaction_read_only = on` kills any write attempt at the protocol level.
- `statement_timeout = 30s` prevents runaway queries.

## SQL Guardrails

### Enforced by adapter (server-side)

| Rule | Implementation |
|------|---------------|
| Multi-statement ban | `sql.strip().count(';') > 1` → reject |
| Only SELECT/WITH | Reject if not starts with `SELECT` or `WITH` |
| Row limit ≤ 1000 | Wrap query in `SELECT * FROM (...) AS _sub LIMIT 1000` |
| Statement timeout | Connection-level `statement_timeout = 30s` |
| System table block | Reject queries referencing `pg_catalog`, `information_schema`, `pg_toast` |
| DDL/DML keyword block | `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`, `COPY`, `CALL`, `DO`, `VACUUM`, `ANALYZE`, `GRANT`, `REVOKE`, `LISTEN`, `NOTIFY` |
| Dangerous functions | `pg_sleep`, `pg_terminate_backend`, `pg_cancel_backend`, `lo_import`, `lo_export`, `copy` |
| SET search_path | Reject if query contains `search_path` or `SET ` |
| Role switching | `SET ROLE`, `SET SESSION AUTHORIZATION` blocked |

### NOT enforced by adapter (deferred)

These are too complex to parse reliably without a full SQL parser. Instead, handled by the read-only user and connection parameters:

| Risk | Mitigation |
|------|-----------|
| `SELECT * FROM pg_shadow` | Read-only user has no access to system tables; banned by guardrail anyway |
| `SELECT 1; DROP TABLE users;` | Multi-statement ban + read-only user kills the DROP |
| `SELECT lo_import('/etc/passwd')` | Lo functions banned by keyword block |
| `COPY ... TO PROGRAM` | `COPY` keyword banned |

## Tool List (MVP — 6 tools)

### `postgres_health`
```
Check connection: SELECT 1, current_database(), current_user, version()
Returns: status string
Fast, no row limit needed
```

### `postgres_list_schemas`
```
SELECT schema_name FROM information_schema.schemata
  WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
Returns: list of non-system schemas
```

### `postgres_list_tables`
```
SELECT table_name, table_type, row_estimate
  FROM information_schema.tables
  WHERE table_schema = 'public'
Returns: table list with type and estimated row count
Row limit: 100
```

### `postgres_describe_table`
```
SELECT column_name, data_type, is_nullable, column_default
  FROM information_schema.columns
  WHERE table_schema = 'public' AND table_name = $1
Returns: column info
Arguments: table_name (required, validated against public schema only)
```

### `postgres_select`
```
Executes a raw SELECT query with enforced LIMIT 1000
Arguments: sql (string, must start with SELECT or WITH)
Returns: JSON array of rows
Guardrails: multi-statement, DDL/DML, system tables, pg_sleep, length limit
```

### `postgres_vector_status`
```
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector'
Returns: { installed: bool, version: string | null }
```

## Deployment Layout

```
Host: LXC 100 (10.10.10.3)
Service: agent-mcp-postgres.service
Internal port: 8784 (unauthenticated, loopback only)
Public port: 8794 (auth proxy, bound to 0.0.0.0)
Env file: /etc/agent-mcp-postgres.env (chmod 600)
Nginx path: /mcp/postgres → http://10.10.10.3:8794/mcp?mcp_token=TOKEN
Tech: psycopg2 (sync, via concurrent.futures) + FastMCP streamable-http
```

### Systemd unit name
```
agent-mcp-postgres.service
```

### Env file contents
```
MCP_PUBLIC_TOKEN=<generated>
MCP_HOST=0.0.0.0
MCP_PORT=8794
MCP_INTERNAL_PORT=8784
PGHOST=10.10.10.127
PGPORT=5432
PGDATABASE=rag_vectordb
PGUSER=mcp_readonly
PGPASSWORD=<readonly-password>
PGSSLMODE=disable
PGAPPNAME=mcp_readonly
PGTARGET_SESSION_ATTRS=read-only
```

## Testing Plan

### Unit tests (pytest)
- `test_guardrails_multi_statement` — `"SELECT 1; DROP TABLE users"` → reject
- `test_guardrails_ddl` — `"DROP TABLE users"` → reject
- `test_guardrails_dml` — `"DELETE FROM users"` → reject
- `test_guardrails_system_table` — `"SELECT * FROM pg_shadow"` → reject
- `test_guardrails_not_select` — `"INSERT INTO users VALUES (1)"` → reject
- `test_guardrails_pg_sleep` — `"SELECT pg_sleep(10)"` → reject
- `test_guardrails_search_path` — `"SET search_path TO public"` → reject
- `test_guardrails_limit_enforced` — query with no explicit LIMIT gets wrapped with LIMIT 1000
- `test_guardrails_limit_max` — `"SELECT * FROM t LIMIT 2000"` → capped to 1000 or rejected
- `test_health` — returns status string
- `test_list_schemas` — returns list
- `test_list_tables` — returns list with limits
- `test_describe_table_present` — returns columns
- `test_describe_table_missing` — returns error
- `test_select_valid` — returns JSON array
- `test_select_invalid` — returns error
- `test_vector_status` — returns installed/version

### Integration tests
- Requires running `rag_vectordb` with `mcp_readonly` user
- `pytest -m integration tests/test_postgres_adapter.py`

### Smoke test
```bash
# Init + tools/list + each tool via /mcp/postgres
curl -s -D /tmp/pg_headers.txt -X POST \
  "https://ssh.xloud.ru/mcp/postgres?mcp_token=TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"initialize","params":{}}'
```

## Rollback

1. `systemctl stop agent-mcp-postgres.service`
2. Remove nginx location block from `/etc/nginx/sites-available/ssh.xloud.ru`
3. `systemctl reload nginx`
4. `iptables -D INPUT -p tcp -s 10.10.10.0/24 --dport 8794 -j ACCEPT`
5. Delete `/etc/agent-mcp-postgres.env`
6. Delete `fleet/postgres_client.py` and `fleet/postgres_server.py`

No data is ever modified, so rollback is clean.

## Open Questions

1. **pgvector extension** — admin to manually enable? (Yes, before adapter go-live)
2. **Row limit** — 1000 or configurable per tool? (1000 MVP, can increase)
3. **JSON output format** — array of objects or array of arrays? (objects, keys = column names)
4. **NULL rendering** — `null` in JSON or skip? (`null`)
5. **Timeout** — per-query 30s or per-tool 60s? (per-query 30s via `statement_timeout`)
