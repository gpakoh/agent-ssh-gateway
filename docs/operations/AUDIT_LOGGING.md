# Audit Logging

This document describes what the gateway logs, what it intentionally does **not** log, where logs live, retention limits, and how to query and rotate them.

---

## Two audit systems

The gateway has **two independent audit subsystems**. They serve different purposes, use different formats, and are not connected.

### 1. Security Audit Logger (`app/security.py:291`)

**Format:** Python `logging` module — plain-text lines in `logs/audit.log`.

**Initialized at:** `app/main.py:132` — `state.audit_logger = AuditLogger()`.

**Log path:** `logs/audit.log` (relative to working directory). Created automatically on first write.

**Line format:**
```
2026-07-17 14:32:01,123 | INFO | COMMAND | session=sess_abc123 | ip=10.10.10.5 | cmd=ls -la /app
```

**Event types:**

| Event | Level | Triggered by | Example detail |
|-------|-------|-------------|----------------|
| `COMMAND` | INFO | Successful command execution (`ssh.py:318`, `ssh.py:401`) | `session=sess_abc123 \| ip=10.10.10.5 \| cmd=ls -la` |
| `FILE` | INFO | File read (`files.py:102`) | `session=sess_abc123 \| ip=10.10.10.5 \| op=READ \| path=src/main.py` |
| `AUTH` | INFO | Authentication attempt (if wired) | `user=admin \| ip=10.10.10.5 \| success=True` |
| `SECURITY` | WARNING | Security events — blocked host, blocked command, policy decision, async job blocked | `type=BLOCKED_COMMAND \| ip=10.10.10.5 \| ...` |

**SECURITY sub-types** (all at WARNING level):

| Sub-type | Where | What |
|----------|-------|------|
| `BLOCKED_TARGET_HOST` | `ssh.py:228` | SSRF protection — target host rejected by CIDR rules |
| `BLOCKED_COMMAND` | `ssh.py:281`, `ssh.py:574` | `sanitize_command()` rejected the command |
| `COMMAND_POLICY_DECISION` | `ssh.py:297`, `ssh.py:595` | Command policy evaluated — includes `allowed`, `reason`, `profile` |
| `ASYNC_JOB_BLOCKED` | `jobs.py:56`, `jobs.py:143` | Async job command rejected by policy |

### 2. Workspace Audit Logger (`app/workspace/snapshot.py:480`)

**Format:** JSONL (one JSON object per line). Append-only.

**Initialized at:** by workspace write operations (not by `main.py`).

**Log path:** configurable — must NOT be inside any project root. Examples: `/var/log/web-ssh-gateway/audit.jsonl` or a temp dir. When `log_path=None`, logging is in-memory only.

**In-memory buffer:** capped at 500 entries (`_DEFAULT_MAX_AUDIT_ENTRIES`). Oldest entries are dropped when the cap is hit. This is a safety net, not a persistent store.

**Line format (JSONL):**
```json
{"receipt_id":"rcpt_a1b2c3","project_id":"my-project","relative_path":"src/main.py","operation":"write","before_hash":"sha256:e3b0c44...","after_hash":"sha256:9f86d08...","size":38,"timestamp":1721213521.123,"identity":"agent-abc","success":true,"error":""}
```

**Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `receipt_id` | str | Snapshot/receipt identifier |
| `project_id` | str | Registered project identifier |
| `relative_path` | str | Project-relative path (never absolute) |
| `operation` | str | `write`, `edit`, `patch`, `rollback` |
| `before_hash` | str | SHA-256 of content before operation |
| `after_hash` | str | SHA-256 of content after operation |
| `size` | int | Content size in bytes |
| `timestamp` | float | Unix timestamp |
| `identity` | str | Caller fingerprint (opaque string, no secrets) |
| `success` | bool | Whether the operation succeeded |
| `error` | str | Error message if failed (empty string on success) |

---

## What is intentionally NOT logged

- **File content** — never written to audit logs. Only hashes.
- **Patch text, `old_string`, `new_string`, `diff`** — excluded from all audit records.
- **Command stdout/stderr** — commands are logged, but output is not.
- **Secrets, API keys, passwords, tokens** — `redact_secrets()` (`security.py:252`) strips these before logging.
- **Absolute host paths** — workspace audit uses project-relative paths only.
- **Rollback content** — rollback is a separate lifecycle, not logged by the workspace audit (see api_help.py `rollback_note`).

---

## Where logs live

| System | Default path | Format | Persistent |
|--------|-------------|--------|-----------|
| Security Audit Logger | `logs/audit.log` | Plain text | Yes (file on disk) |
| Workspace Audit Logger | Configurable (`log_path`) | JSONL | Only if `log_path` is set; in-memory otherwise |

In Docker, `logs/` is inside the container. Mount a volume to persist across restarts:
```yaml
volumes:
  - ./logs:/app/logs
```

For the workspace audit logger, set `log_path` to a path outside any project root (e.g., `/var/log/web-ssh-gateway/audit.jsonl`).

---

## Retention knobs

Three independent audit systems, each with its own configuration.

| System | Env var | Default | Controls |
|--------|---------|---------|----------|
| Structured audit (JSONL + ring buffer) | `AUDIT_LOG_PATH` | `./data/audit/events.jsonl` | JSONL file path on disk |
| Structured audit (ring buffer) | `AUDIT_RECENT_LIMIT` | `500` | In-memory ring buffer size (`/api/admin/audit/recent` reads this) |
| Security audit (plain text) | *(hardcoded)* | `logs/audit.log` | No env var — path set in `app/security.py:294` |
| Workspace audit (JSONL) | *(per-call)* | `None` (in-memory only) | `log_path` passed to `WorkspaceAuditLogger()` constructor |

**Not implemented yet** (proposed for future):
- `AUDIT_MAX_BYTES` — max size of `events.jsonl` before auto-rotation
- `AUDIT_BACKUP_COUNT` — number of rotated JSONL backups to keep

These would replace manual logrotate. Until implemented, use external rotation (see below).

---

## Retention and limits

| System | Limit | Behavior when exceeded |
|--------|-------|----------------------|
| Structured audit JSONL | No built-in cap | Grows unbounded — rotate externally |
| Structured audit ring buffer | `AUDIT_RECENT_LIMIT` (default 500) | Oldest events dropped (FIFO) |
| Security audit (`logs/audit.log`) | No built-in cap | Grows unbounded — use logrotate |
| Workspace audit JSONL | No built-in cap | Grows unbounded — rotate externally |
| Workspace audit in-memory | 500 entries | Oldest entries dropped (FIFO) |

---

## Example event

### Security audit (plain text)
```
2026-07-17 14:32:01,123 | WARNING | SECURITY | type=COMMAND_POLICY_DECISION | ip=10.10.10.5 | session_id=sess_abc123; command=rm -rf /tmp/test; allowed=False; reason=blocked_by_profile; profile=readonly; identity_type=api_key; identity_name=agent-1
```

### Workspace audit (JSONL)
```json
{"receipt_id":"rcpt_f6e5d4c3b2a1","project_id":"web-ssh-gateway","relative_path":"src/main.py","operation":"edit","before_hash":"sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","after_hash":"sha256:9f86d08...","size":36,"timestamp":1721213521.123,"identity":"agent-abc","success":true,"error":""}
```

---

## Querying audit logs

### Security audit (plain text)

```bash
# Recent commands
grep "COMMAND" logs/audit.log | tail -20

# Blocked commands only
grep "BLOCKED_COMMAND" logs/audit.log

# Policy decisions
grep "COMMAND_POLICY_DECISION" logs/audit.log

# Events from a specific IP
grep "ip=10.10.10.5" logs/audit.log

# Events in the last hour
awk -F'|' '/2026-07-17 14:/' logs/audit.log
```

### Workspace audit (JSONL)

```bash
# Pretty-print last 10 entries
tail -10 audit.jsonl | jq .

# Entries for a specific project
jq 'select(.project_id == "web-ssh-gateway")' audit.jsonl

# Failed operations only
jq 'select(.success == false)' audit.jsonl

# Operations on a specific file
jq 'select(.relative_path == "src/main.py")' audit.jsonl

# Entries in the last hour (using timestamp)
jq 'select(.timestamp > (now - 3600))' audit.jsonl
```

### ✅ IMPLEMENTED: `/api/admin/audit/recent` endpoint

**Endpoint:** `GET /api/admin/audit/recent`
**Auth:** master API key (`X-API-Key` header)
**Query params:**
- `limit` (int, default 100, max 1000) — number of entries to return
- `event_type` (str, optional) — filter by event type (e.g. `command.deny`, `workspace.readonly_block`)
- `decision` (str, optional) — filter by decision (`allowed`, `denied`, `error`)
- `sort` (str, default `newest`) — sort order: `newest` or `oldest`

**Response:**
```json
{
  "events": [
    {
      "event_id": "a1b2c3d4e5f6",
      "timestamp": "2026-07-17T15:00:00.000+00:00",
      "event_type": "command.deny",
      "decision": "denied",
      "reason": "Root command 'systemctl' not in readonly allowlist",
      "profile": "readonly",
      "source_ip": "10.0.0.1",
      "route": "POST /api/ssh/execute",
      "target_type": "session",
      "target_id": "sid",
      "metadata": {"command_root": "systemctl"}
    }
  ],
  "total": 1,
  "buffer_size": 42
}
```

**Notes:**
- Returns events from the in-memory ring buffer only (max 500).
- Does NOT read from the JSONL file on disk.
- The in-memory buffer is reset on gateway restart.
- For persistent audit queries, query the JSONL file directly.

**Query examples:**
```bash
# Last 10 denied commands
curl -s -H "X-API-Key: $API_KEY" \
  "http://localhost:8085/api/admin/audit/recent?limit=10&decision=denied" | jq .

# All workspace readonly blocks
curl -s -H "X-API-Key: $API_KEY" \
  "http://localhost:8085/api/admin/audit/recent?event_type=workspace.readonly_block" | jq .

# Oldest 50 events (chronological)
curl -s -H "X-API-Key: $API_KEY" \
  "http://localhost:8085/api/admin/audit/recent?limit=50&sort=oldest" | jq .

# Count denied events in buffer
curl -s -H "X-API-Key: $API_KEY" \
  "http://localhost:8085/api/admin/audit/recent?decision=denied&limit=1000" | jq '.total'
```

**Note:** The endpoint returns at most 1000 events per call. The ring buffer holds 500 events by default (`AUDIT_RECENT_LIMIT`). For full history, query the JSONL file:
```bash
# All denied commands from persistent JSONL
jq 'select(.decision == "denied")' data/audit/events.jsonl

# Events in the last hour
jq 'select(.timestamp > (now - 3600 | todate))' data/audit/events.jsonl
```

---

## Rotating / deleting audit logs safely

### What is safe to delete

| File | Safe to delete? | Condition |
|------|----------------|-----------|
| `logs/audit.log` | ✅ Yes | After rotation/backup. Gateway recreates on next event. |
| `data/audit/events.jsonl` | ✅ Yes | After rotation/backup. Gateway recreates on next event. |
| `data/audit/events.jsonl.*` (rotated) | ✅ Yes | After confirming backup is valid. |
| In-memory ring buffer | ✅ Yes | Automatic on restart. No manual action needed. |

### What must be preserved for incident response

- **Last 7 days** of `events.jsonl` — contains command policy decisions, denied commands, workspace readonly blocks. Critical for investigating "who ran what when."
- **Last 30 days** of `logs/audit.log` — contains COMMAND, BLOCKED_COMMAND, COMMAND_POLICY_DECISION, AUTH events. Required for security audit trails.
- **Any event with `decision=denied`** — these are the security-relevant events. Preserve at least 90 days if possible.
- **Rotated files during active incidents** — do not delete rotated logs while an incident is open.

### Structured audit (`data/audit/events.jsonl`)

```bash
# logrotate config — create /etc/logrotate.d/audit-events:
/data/audit/events.jsonl {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    dateext
    dateformat -%Y%m%d
}

# Manual rotation (safe — copytruncate pattern)
cp data/audit/events.jsonl data/audit/events.jsonl.$(date +%Y%m%d)
> data/audit/events.jsonl   # truncate in place — no handle restart needed
gzip data/audit/events.jsonl.$(date +%Y%m%d)

# jq-based compaction (merge old rotated files into summary)
jq -s 'group_by(.event_type) | map({type: .[0].event_type, count: length, denied: map(select(.decision == "denied")) | length})' \
  data/audit/events.jsonl.20260717.gz | gunzip | jq . > audit_summary.json
```

**Important:** Use `copytruncate` — the `AuditEventLogger` holds an open file handle. Do NOT `mv` without truncating, or the logger keeps writing to the old file.

### Security audit (`logs/audit.log`)

```bash
# logrotate config — create /etc/logrotate.d/web-ssh-gateway:
/logs/web-ssh-gateway/logs/audit.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}

# Manual rotation
cp logs/audit.log logs/audit.log.$(date +%Y%m%d)
> logs/audit.log
gzip logs/audit.log.$(date +%Y%m%d)
```

Same `copytruncate` requirement — `AuditLogger` holds a `FileHandler` open.

### Workspace audit (configurable JSONL)

```bash
# Only exists if WorkspaceAuditLogger was initialized with a log_path.
# Same pattern as structured audit:
cp audit.jsonl audit.jsonl.$(date +%Y%m%d)
> audit.jsonl
```

### In-memory buffers

Both ring buffers (structured audit `AUDIT_RECENT_LIMIT` and workspace audit 500 entries) are reset on gateway restart. No manual cleanup needed. No API to flush without restart — this is by design.

---

## MCP-Local Audit

The MCP server has its **own separate audit logger** — not shared with the gateway. It uses `McpAuditLogger` in `examples/mcp_server/mcp_audit.py`.

**Log path:** `MCP_AUDIT_LOG_PATH` (default: `logs/mcp_audit.jsonl`)
**Buffer size:** `MCP_AUDIT_RECENT_LIMIT` (default: `500`)
**Format:** JSONL (one JSON object per line), append-only

**Key properties:**
- Metadata-only (command root, decision, reason)
- Redacted — no secrets, no command output, no full prompts
- NOT visible through `/api/admin/audit/recent` (gateway endpoint)
- Query the MCP audit file directly or via MCP server

**Event types:**

| Event Type | Description |
|-----------|-------------|
| `mcp.tool_blocked` | Tool invocation blocked by policy |
| `mcp.command_denied` | Command denied by policy (readonly, etc.) |
| `mcp.tool_denied` | Tool denied by model-specific rules |

**Query examples:**
```bash
# Recent MCP blocks
tail -20 logs/mcp_audit.jsonl | jq .

# All command denials
jq 'select(.event_type == "mcp.command_denied")' logs/mcp_audit.jsonl

# Blocks in the last hour
jq 'select(.timestamp > (now - 3600 | todate))' logs/mcp_audit.jsonl
```

---

## ⚠️ PLANNED: Built-in rotation

Built-in JSONL rotation (`AUDIT_MAX_BYTES`, `AUDIT_BACKUP_COUNT`) is **not implemented yet**. Until then:

- Use `logrotate` with `copytruncate` for all JSONL files.
- Use `logrotate` with `copytruncate` for `logs/audit.log`.
- Do NOT rely on `mv` + signal — the loggers hold open file handles.
- Monitor disk usage: `du -sh data/audit/ logs/`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `logs/audit.log` not created | No audit events yet, or permission denied | Check `logs/` directory exists and is writable |
| `events.jsonl` not created | `AUDIT_LOG_PATH` directory doesn't exist | Gateway creates parent dirs automatically — check permissions |
| `/api/admin/audit/recent` returns 503 | `event_audit_logger` not initialized | Check `AUDIT_LOG_PATH` is set and writable |
| Ring buffer empty after restart | In-memory buffer is volatile | By design. Query JSONL file for persistent history. |
| `audit.jsonl` missing | `WorkspaceAuditLogger` not initialized or `log_path=None` | Set `log_path` in the logger constructor |
| In-memory buffer shows stale entries | Gateway not restarted | Buffer is capped; oldest entries are dropped automatically |
| Duplicate log lines | Multiple `AuditLogger` instances | The constructor deduplicates `FileHandler` instances — check if multiple modules create `AuditLogger()` |
| Secrets visible in logs | `redact_secrets()` not matching the pattern | Report as a security issue — secrets should always be redacted |
| Log file growing unbounded | No rotation configured | Set up logrotate with `copytruncate` (see Rotating section) |
| Disk full from audit logs | Rotation not running or `rotate` count too high | Check `logrotate -d /etc/logrotate.d/audit-events` for debug |
