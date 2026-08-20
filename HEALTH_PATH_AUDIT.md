# Health Path Audit: [Errno -2] Aggregate Health Case

**Audit date:** 2026-08-20
**Commit audited:** 5400dc2b (master)
**Candidate fix:** 07c6633a on `fix/health-bounded-degradation-20260820`
**Scope:** READ-ONLY call-chain analysis of all httpx error propagation paths

---

## 1. Complete Call Chain for `gateway_health()`

### Master (without candidate fix)

```
MCP client
  → register_tool("health")(instrumented("health")(gateway_health))
    → gateway_health()                              [adapters/gateway.py:193]
      → _server_client()                            [line 86, resolves to server.client]
        → server_attr("client")                     [_server_ref.py:28, dynamic import lookup]
      → .health()                                   [gateway_client.py:320]
        → self._get("/health")                      [gateway_client.py:321]
          → httpx.get(                              [gateway_client.py:249]
              f"{self.base_url}/health",
              headers=self._headers(),
              timeout=timeout,
          )
            ↓
            httpx.ConnectError("[Errno -2] Name or service not known")
            (or httpx.TimeoutException, httpx.DNSError, etc.)
            ↓
          ← NO try/except in _get()                 [master: line 249-273]
          ← NO try/except in gateway_health()       [master: line 197]
          ← NO try/except in _run_gateway or run_tool (gateway_health doesn't use either)
            ↓
          RAW httpx.TransportError propagates to MCP framework
            ↓
          FastMCP sees unhandled exception → MCP tool call fails with transport error
            ↓
          AGGREGATE HEALTH ENDPOINT COMPLETELY DOWN
          (while control plane, list_sessions, session_health etc. may still work)
```

### Candidate fix (07c6633a)

```
Same chain until httpx.get()
            ↓
          httpx.ConnectError / httpx.TimeoutException
            ↓
          _get() wraps in _transport_error()        [gateway_client.py:_transport_error()]
            → GatewayClientError("Gateway transport unavailable", body={code: "REMOTE_UNAVAILABLE", retryable: True})
            ↓
          gateway_health() catches GatewayClientError  [adapters/gateway.py:try/except]
            → _bounded_gateway_health_error(exc)
            → Returns {"status": "unreachable", "ready": False, "error": {code, message, retryable}}
            ↓
          MCP health tool returns degraded health, NOT a transport error
          MCP server remains fully available
```

---

## 2. Error Boundary Map: All `_server_client()` Call Sites in `adapters/gateway.py`

### Functions using `_run_gateway()` wrapper (catches GatewayClientError)

| Function | Line | Client method | httpx path | Transport error caught? |
|---|---|---|---|---|
| `gateway_working_directory` | 789 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_info` | 797 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_list_files` | 965 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_tree` | 973 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_list_tree` | 986 | via mcp_client_tools | `_post` | YES (candidate fix) |

`_run_gateway()` catches `GatewayClientError` (gateway.py:121). With candidate fix, httpx errors become `GatewayClientError` → caught here.

### Functions using `run_tool()` wrapper (catches GatewayClientError)

| Function | Line | Client method | httpx path | Transport error caught? |
|---|---|---|---|---|
| `gateway_list_sessions` | 459 | `.list_sessions()` | `_get` | YES (candidate fix) |
| `gateway_session_health` | 474 | `.session_health()` | `_get` | YES (candidate fix) |
| `gateway_execute_restricted` | 488 | `.execute_restricted()` | `_post` | YES (candidate fix) |
| `gateway_job_status` | 597 | `.job_status()` | `_get` | YES (candidate fix) |
| `gateway_job_result` | 612 | `.job_result()` | `_get` | YES (candidate fix) |
| `gateway_wait_job` | 627 | `.wait_job()` | `_get` | YES (candidate fix) |
| `gateway_repo_status` | 762 | `.repo_status()` → multiple | `_post`/`_get` | YES (candidate fix) |
| `gateway_git_status` | 807 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_recent_commits` | 817 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_git_diff_stat` | 827 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_show_changes` | 837 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_git_add` | 847 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_git_commit` | 857 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_git_create_branch` | 867 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_git_push` | 877 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_run_tests` | 891 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_run_lint` | 906 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_run_compileall` | 916 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_read_file` | 933 | via `_server_read_file()` | `_post` | YES (candidate fix) |
| `gateway_search_text` | 943 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_git_diff` | 998 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_git_diff_cached` | 1008 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_show_file_diff` | 1018 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_run_pytest` | 1028 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_run_ruff` | 1042 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_run_mypy` | 1052 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_remotes` | 1062 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_current_branch` | 1072 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_commit_head` | 1082 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_read_handoff` | 1092 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_write_handoff_plan` | 1102 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_show_handoff_status` | 1121 | via mcp_client_tools | `_post` | YES (candidate fix) |
| `gateway_job_wait` | 641 | `.wait_job()` | `_get` | YES (candidate fix) |
| `gateway_project_list` | 255 | (workspace registry, no httpx) | — | N/A |

`run_tool()` catches `GatewayClientError` (tool_registry.py:231). With candidate fix, httpx errors become `GatewayClientError` → caught here.

### Functions with DIRECT try/except for GatewayClientError

| Function | Line | Client method | httpx path | Transport error caught? |
|---|---|---|---|---|
| `gateway_execute_argv` | 502 | `.execute_argv()` | `_post` | YES (candidate fix) |
| `gateway_apply_patch` | 545 | `.apply_patch()` | `_post` | YES (candidate fix) |
| `gateway_job_wait` | 641 | `.wait_job()` | `_get` | YES (candidate fix) |

### Functions WITHOUT adequate error handling

| Function | Line | Client method | httpx path | Transport error caught? |
|---|---|---|---|---|
| **`gateway_health`** | 193 | `.health()` | `_get` → `httpx.get()` | **NO** (master) / YES (candidate fix) |
| **`gateway_diagnostics_latency`** | 1151 | `._get()` | `httpx.get()` | YES — catches bare `Exception` (line 1158) |
| **`gateway_self_test`** | 1131 | via `run_self_test(client)` | See §3 | **NO** (master) / NO (candidate fix doesn't change self_test.py) |

---

## 3. Self-Test Path Analysis

### `gateway_self_test()` (gateway.py:1131)

```python
def gateway_self_test() -> dict[str, Any]:
    data = run_self_test(_server_client())  # NO try/except around this!
    ...
```

### `run_self_test(client)` (self_test.py:36)

The health check in self_test (lines 84-90):

```python
try:
    data = client.health()
    checks.append(check_result("health", "pass", ...))
except GatewayClientError as exc:
    checks.append(check_result("health", "fail", str(exc)))
```

**Only catches `GatewayClientError`.**

On master, when the gateway is unreachable:
1. `client.health()` → `_get("/health")` → `httpx.get()` → `httpx.ConnectError`
2. `httpx.ConnectError` is NOT a `GatewayClientError` subclass
3. The `except GatewayClientError` does NOT catch it
4. `httpx.ConnectError` propagates out of `run_self_test()` entirely
5. `gateway_self_test()` (gateway.py:1131) has no try/except either
6. **Raw transport exception hits the MCP framework → self_test tool fails with transport error**

**self_test is NOT resilient to transport failures on master.**

### What the candidate fix does NOT cover for self_test

The candidate fix wraps `GatewayClient._get()` to convert httpx errors → `GatewayClientError`. After the fix:
1. `client.health()` → `_get("/health")` → `httpx.get()` → `httpx.ConnectError`
2. `_transport_error()` wraps → `GatewayClientError("Gateway transport unavailable", body={code: "REMOTE_UNAVAILABLE"})`
3. `except GatewayClientError` in self_test.py:89 catches it ✓
4. Self-test reports `"health": "fail"` check

**The candidate fix DOES make self_test resilient** — indirectly, by fixing the root cause in `_get()`.

---

## 4. Coverage Gaps (NOT covered by candidate fix)

### Gap 1: `reconnect_session()` httpx calls

`_reconnect_session()` (gateway_client.py:177) makes a direct `httpx.post()` call (line 193) that is NOT wrapped by the candidate fix. On transport failure:
- `httpx.ConnectError` propagates raw from `_reconnect_session()`
- `_retry_on_session_not_found` decorator (line 223) catches `GatewayClientError` only — not `httpx.TransportError`
- Any tool using `@_retry_on_session_not_found` that triggers reconnection will fail with a raw transport error even after the candidate fix

**Not a production concern for the aggregate health case**, but a gap for session-reconnect paths.

### Gap 2: `disconnect()` — intentionally uncaught

`disconnect()` (gateway_client.py:210) wraps `_post()` in a bare `except Exception: pass`. By design: best-effort cleanup, never raises. No gap.

### Gap 3: `wait_job()` polling fallback

`wait_job()` (gateway_client.py:564) has a polling fallback loop (line 619-628) that calls `self.job_status()` and `self.job_result()` directly without try/except. If the gateway goes down DURING the polling loop (after the initial long-poll times out), `job_status()` will throw a raw `httpx.ConnectError`. The candidate fix does wrap `_get()`, so this is actually covered. **Not a gap after fix.**

### Gap 4: Agent adapter (`mcp_infra/adapters/agent.py`)

The agent adapter has its own `_server_client()` (line 44) and calls gateway methods through `run_tool()` wrappers. These are covered by the candidate fix (same `_get`/`_post` paths). **No additional gap.**

---

## 5. Summary

| Component | Master (5400dc2b) | After candidate fix (07c6633a) |
|---|---|---|
| `gateway_health()` transport failure | CRITICAL: raw httpx exception, aggregate health endpoint down | Fixed: returns `{status: "unreachable", ready: false}` |
| `self_test` health check | FAILS: raw httpx exception propagates | Fixed: reports `"health": "fail"` check |
| All `run_tool()`-wrapped tools (30+) | FAILS: raw httpx propagates through `run_tool` → `except Exception` re-raises | Fixed: `_get`/`_post` wrap in `GatewayClientError` → caught by `run_tool` |
| `_run_gateway()`-wrapped tools (5) | FAILS: same as above | Fixed: same mechanism |
| `diagnostics_latency` | SAFE: catches `Exception` | SAFE (no change needed) |
| `reconnect_session()` httpx.post | NOT FIXED: raw httpx on transport failure | NOT FIXED (separate path, not in candidate) |
| `disconnect()` | SAFE: catches `Exception: pass` | SAFE (no change needed) |

### Root cause

`GatewayClient._get()` and `GatewayClient._post()` make raw `httpx.get()`/`httpx.post()` calls without catching `httpx.RequestError` (the base class for all transport exceptions). Only HTTP-level errors (status ≥ 400) are caught and wrapped in `GatewayClientError`. Transport-level errors (DNS resolution failure, connection refused, timeout, network unreachable) escape as raw `httpx.TransportError` subclasses.

### Why the health path was uniquely vulnerable

`gateway_health()` is the **only** gateway adapter function that calls `_server_client()` methods **without** either `_run_gateway()`, `run_tool()`, or an explicit `try/except GatewayClientError`. Every other tool function in the adapter is wrapped by one of these error boundaries, which catch `GatewayClientError` and convert it to a structured tool error response. The health path was the sole unprotected call site.
