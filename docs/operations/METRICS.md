# Metrics

Prometheus metrics exposed by the web-ssh-gateway process.

**Endpoint:** `GET /metrics` (master key required)

## Request Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `ssh_gateway_requests_total` | Counter | `method`, `endpoint`, `status` | Total HTTP requests |
| `ssh_gateway_request_duration_seconds` | Histogram | `method`, `endpoint` | Request latency |

Labels:
- `endpoint` uses route templates (e.g., `/api/ssh/{command}`), not raw paths
- `status` is HTTP status code as string

## SSH Command Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `ssh_gateway_ssh_commands_total` | Counter | `status`, `profile`, `command_root` | SSH commands executed |

Labels:
- `status`: `allowed`, `denied`, `error`
- `profile`: command policy profile (bounded set from config)
- `command_root`: normalized root command from bounded allowlist, else `other`

**No full command, args, session_id, user, IP, or path in labels.**

## SSH Connection Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `ssh_gateway_ssh_connections_active` | Gauge | — | Active SSH connections |
| `ssh_gateway_ssh_connection_duration_seconds` | Histogram | — | SSH connection duration |

## Job Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `ssh_gateway_jobs_enqueued_total` | Counter | `priority` | Jobs enqueued |
| `ssh_gateway_jobs_completed_total` | Counter | `status` | Jobs completed |
| `ssh_gateway_jobs_duration_seconds` | Histogram | — | Job execution duration |
| `ssh_gateway_queue_depth` | Gauge | `queue` | Queue depth (`pending`, `processing`, `dead`) |

## Circuit Breaker Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `ssh_gateway_circuit_breaker_state` | Gauge | `target` | State: 0=closed, 1=half-open, 2=open |

Labels:
- `target`: bounded to `ssh`, `redis`, `postgres` (no raw host/IP)

## Lock Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `ssh_gateway_locks_active` | Gauge | — | Active distributed locks |

## File Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `ssh_gateway_file_operations_total` | Counter | `operation`, `status` | File operations |

## Event Hook Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `ssh_gateway_event_hook_deliveries_total` | Counter | `status`, `event` | Event hook deliveries |
| `ssh_gateway_event_hook_delivery_attempts_total` | Counter | — | Delivery attempts |
| `ssh_gateway_event_hook_delivery_latency_seconds` | Histogram | — | Delivery latency |
| `ssh_gateway_event_hook_dead_letter_count` | Gauge | — | Dead letter count |

## System Info

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `ssh_gateway_info` | Info | `version` | Gateway version |

## Deferred / Not Yet Implemented

- Per-handler latency breakdown (currently request-level only)
- WebSocket-specific metrics (connection count, message rate)
- Redis command latency
- PostgreSQL query latency

## Notes

- Prometheus retention and scraping configuration is external to this service
- All label values are bounded — no raw user input, paths, tokens, or IPs
- Metrics are thread-safe (prometheus_client handles locking)
