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

## Retention and limits

| System | Limit | Behavior when exceeded |
|--------|-------|----------------------|
| Security Audit Logger | No built-in cap | Grows unbounded — use logrotate or external retention |
| Workspace Audit Logger (file) | No built-in cap | Grows unbounded — rotate externally |
| Workspace Audit Logger (in-memory, `log_path=None`) | 500 entries | Oldest entries are dropped (FIFO) |

**Recommended retention:**
- `logs/audit.log`: use `logrotate` or equivalent. Daily rotation, 30-day retention is typical.
- `audit.jsonl`: rotate with `logrotate` or `jq`-based compaction. The file is append-only and line-oriented, so standard log rotation tools work.

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

### ⚠️ PLANNED — NOT IMPLEMENTED YET: `/api/admin/audit/recent` endpoint

> **This endpoint does not exist in the running gateway.** Do not call it. Do not document it as available. This section is a design spec for future implementation (target: C4 after audit endpoint wiring). Current workaround: query the JSONL file on disk directly.

**Endpoint:** `GET /api/admin/audit/recent`
**Auth:** master API key (`X-API-Key` header)
**Query params:**
- `limit` (int, default 50, max 200) — number of entries to return
- `project_id` (str, optional) — filter by project
- `operation` (str, optional) — filter by operation type

**Response:**
```json
{
  "entries": [
    {
      "receipt_id": "rcpt_a1b2c3",
      "project_id": "my-project",
      "relative_path": "src/main.py",
      "operation": "write",
      "before_hash": "sha256:e3b0c44...",
      "after_hash": "sha256:9f86d08...",
      "size": 38,
      "timestamp": 1721213521.123,
      "identity": "agent-abc",
      "success": true,
      "error": ""
    }
  ],
  "count": 1,
  "buffer_size": 42
}
```

**Notes:**
- Returns entries from the in-memory buffer only (max 500).
- Does NOT read from the JSONL file on disk.
- The in-memory buffer is reset on gateway restart.
- For persistent audit queries, query the JSONL file directly.

---

## Rotating / deleting audit logs safely

### Security audit (`logs/audit.log`)

```bash
# Standard logrotate (recommended)
# Create /etc/logrotate.d/web-ssh-gateway:
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
mv logs/audit.log logs/audit.log.$(date +%Y%m%d)
kill -USR1 $(pgrep -f "uvicorn app.main")  # Reopen log file (if using Python logging FileHandler)
```

**Important:** The `AuditLogger` holds a `FileHandler` open. After renaming the file, the handler continues writing to the old (renamed) file. Use `copytruncate` with logrotate, or restart the gateway after rotation.

### Workspace audit (`audit.jsonl`)

```bash
# Rotate safely — the file is append-only and line-oriented
mv audit.jsonl audit.jsonl.$(date +%Y%m%d)

# Or compact with jq (merge old entries into a summary)
jq -s 'group_by(.project_id) | map({project: .[0].project_id, count: length})' \
  audit.jsonl.20260717 > audit_summary.json
rm audit.jsonl.20260717
```

**Do NOT** delete the JSONL file while the gateway is running — the `WorkspaceAuditLogger` holds an open file handle. Rotate by renaming, then the logger will create a new file on the next write.

### In-memory buffer

The in-memory buffer (500 entries max) is reset on gateway restart. No manual cleanup needed. To force a flush without restarting, there is currently no API — this is by design (the buffer is a safety net, not a primary store).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `logs/audit.log` not created | No audit events yet, or permission denied | Check `logs/` directory exists and is writable |
| `audit.jsonl` missing | `WorkspaceAuditLogger` not initialized or `log_path=None` | Set `log_path` in the logger constructor |
| In-memory buffer shows stale entries | Gateway not restarted | Buffer is capped at 500; oldest entries are dropped automatically |
| Duplicate log lines | Multiple `AuditLogger` instances | The constructor deduplicates `FileHandler` instances — check if multiple modules create `AuditLogger()` |
| Secrets visible in logs | `redact_secrets()` not matching the pattern | Report as a security issue — secrets should always be redacted |
