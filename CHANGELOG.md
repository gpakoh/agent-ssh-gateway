# Changelog

All notable changes to this project will be documented in this file.

This project follows semantic versioning where practical, but the public API is not considered stable before v1.0.0.

## [Unreleased]

## [0.1.19-alpha] - 2026-06-26

### Added

- **`MCP_EXTRA_TOKENS_FILE` env var**: file-based JSON token→profile mapping, overrides `MCP_EXTRA_TOKENS_JSON` on conflict. Avoids shell escaping issues. (Session 122)
- **Enforce smoke `tools/list` flow**: now performs MCP initialize and passes `Mcp-Session-Id` before testing `tools/list`. (Session 122)

### Fixed

- **Enforce smoke token authentication**: generated Bearer tokens were not being sent (profile names were sent instead). Fixed dict key/value order in token→profile iteration. (Session 122)
- **Scope validation test**: `mcp:admin` is now a valid scope (fail-closed), test was updated to check with `mcp:invalid` instead. (Session 122)

### Verified

- pytest: 709 passed
- Enforce smoke: 14/14
- Fleet healthcheck: 6/6

## [0.1.18-alpha] - 2026-06-26

### Added

- **Tool-level scope enforcement for MCP tools.** 5 access profiles (`viewer`, `operator`, `agent-runner`, `infra`, `full`) with 10 capability scopes. Enforced at the OAuthProxyMiddleware level in the main gateway MCP adapter. (Session 120–121)
- **`tool_scopes.py`**: `ACCESS_PROFILES` map (5 profiles × 10 scopes), `TOOL_SCOPES` map (88 tools), `FLEET_ROUTE_SCOPES`, `get_profile_scopes()`, `check_tool_scope()`, `get_tool_scope()`, `get_highest_profile()` helpers.
- **`OAuthProxyMiddleware._get_token_scopes()`**: resolves token scopes from `GatewayOAuthProvider.verify_access_token()`. (Session 119)
- **`OAuthProxyMiddleware._check_tool_scope()`**: per-request scope check for `tools/call` and fleet routes; supports `off`, `audit`, `enforce` modes via `MCP_SCOPE_ENFORCEMENT`. (Session 119)
- **`MCP_SCOPE_ENFORCEMENT` env var**: tri-state (`off` / `audit` / `enforce`). Audit logs `SCOPE_ALLOWED`/`SCOPE_DENIED` to stderr without blocking. (Session 119)
- **`MCP_DEFAULT_ACCESS_PROFILE` env var**: default profile for tokens without explicit scopes (default: `operator`). (Session 119)
- **`MCP_EXTRA_TOKENS_JSON` env var**: JSON mapping `token → profile` for pre-registering scoped test/service tokens. (Session 120)
- **Fail-closed model**: unknown tools default to `mcp:admin` scope, accessible only by `full` profile. (Session 119)
- **`scripts/mcp_enforce_smoke.py`**: reusable profile-based enforce smoke test tool. (Session 120)

### Changed

- **`OAuthProxyMiddleware.proxy_request()`** now checks tool scopes for `tools/call` and fleet routes. (Session 119)
- **Fleet route paths** use prefix match (`/mcp/gitea` → `mcp:repo`). (Session 119)
- **Healthcheck token** pre-registered with all 10 scopes + infinite expiry. (Session 119)
- **`mcp:execute`** scope added to separate `gateway_execute_restricted` from `mcp:read`. (Session 120)
- **OAuth tokens** now carry access profile scopes in audit and enforce modes. (Session 120)
- **Production deployed with `MCP_SCOPE_ENFORCEMENT=enforce`** and `MCP_DEFAULT_ACCESS_PROFILE=full` for full-capability private ChatGPT. (Session 121)
- **`SUPPORTED_SCOPES`** in `oauth_provider.py` includes all 10 scopes: `mcp:read`, `mcp:project`, `mcp:handoff`, `mcp:agent-run`, `mcp:execute`, `mcp:repo`, `mcp:docker`, `mcp:postgres`, `mcp:docs`, `mcp:admin`.

### Tests

- 36 scope enforcement tests in `test_scope_enforcement.py`: profile integrity, scope logic, tool extraction, fleet routes, fail-closed. (Session 119)
- 148 MCP tests + 36 scope tests = 184 total. `make check` clean. (Session 119)
- Enforce mode smoke: all 5 profiles verified (viewer/operator → Docker 403, infra/full → Docker 200, healthcheck full profile OK, tools/list not blocked, denied → 403 JSON-RPC error). (Session 120)

## [0.1.17-alpha] - 2026-06-26

### Changed

- **OAuth-only MCP hardening.** Removed mixed auth mode. Made OAuth the default MCP auth mode (`MCP_AUTH_MODE=oauth`).
- **Token mode preserved as emergency rollback.** `MCP_AUTH_MODE=token` now requires `MCP_PUBLIC_TOKEN` (raises `ValueError` if empty). Token pre-registered in `GatewayOAuthProvider` — not anonymous. Accepts Bearer header or `?mcp_token=` query param.
- **Mixed mode removed.** (Session 115)
- **`OAuthProxyMiddleware`** (renamed from `MixedAuthMiddleware`): token mode accepts both Bearer and `?mcp_token=`; oauth mode strictly Bearer-only, rejects `?mcp_token=`. (Session 115)
- **Fleet adapters (5×)**: updated to accept Bearer header via shared `extract_auth_token()`; removed `TokenAuthMiddleware`. (Session 115)
- **Healthcheck**: switched from `?mcp_token=` query param to `Authorization: Bearer`. (Session 115)
- **All `.env.example` files**: `MCP_AUTH_MODE=oauth` default, token mode documented as rollback. (Session 115)

### Tests

- 144 MCP tests passing: 30 auth tests (oauth + token mode), 17 oauth provider tests, 97 tool/mode/handoff tests. 0 E402, 0 ruff errors. (Session 115)

## [0.1.16-alpha] - 2026-06-25

### Added

- **OAuth Phase 2 — FastMCP native Auth.** Three-layer auth architecture: `token` (legacy, default), `oauth` (FastMCP-native OAuth 2.1 DCR + PKCE), `mixed` (proxy auth for agents + FastMCP OAuth for clients). (Session 113)
- **`GatewayOAuthProvider`** — async OAuth 2.1 provider implementing FastMCP `AuthorizationServerProtocol`: PKCE S256, DCR, auth code flow, refresh tokens, scope validation, token revocation. 17 tests. (Session 113)
- **`mixed` auth mode** — MCP_PUBLIC_TOKEN pre-registered as opaque access token in `GatewayOAuthProvider`; FastMCP auth layer disabled (proxy handles `mcp_token` → Bearer injection). (Session 113)
- **`oauth` auth mode** — full FastMCP-native OAuth with `AuthSettings` configured; `issuer_url`, `resource_server_url`, DCR with `mcp:read`/`mcp:write` scopes. (Session 113)
- **`MCP_AUTH_MODE` env var** — tri-state: `token` (default, no change), `mixed` (proxy auth + FastMCP OAuth provider ready), `oauth` (FastMCP-native OAuth). (Session 113)

### Tests

- 24 tests for OAuth Phase 2: 7 `test_mcp_server.py` (auth mode config, provider init, token registration, FastMCP settings), 17 `test_oauth_provider.py` (PKCE, DCR, auth code, tokens, revocation, scopes).

### Changed

- `proxy.py`: `mcp_token` query → Bearer header injection now routes through `GatewayOAuthProvider.verify_access_token()` in mixed mode; no token → 401. (Session 113)
- `server.py`: auth config block reorganized — `MCP_AUTH_MODE` switch selects between token (default, no auth), oauth (FastMCP-native), and mixed (proxy auth + provider pre-loaded). (Session 113)
- `/etc/agent-ssh-gateway-mcp.env`: `MCP_AUTH_MODE=mixed` (was: token-only, no MCP_AUTH_MODE set). (Session 113)
- Healthcheck: 6/6 adapters healthy, 85 Gateway tools available. (Session 113)

## [0.1.15-alpha] - 2026-06-25

### Added

- **Mimo local execution bootstrap** — default model switched to local Ollama (`ollama-gen/gemma4:26b`), configurable via `MIMO_DEFAULT_MODEL` env var. (Session 109.5)
- **NO_PROXY/no_proxy bypass** — `MIMO_EXTRA_NO_PROXY` env var adds local targets (`192.168.1.103`, `10.10.10.x`, etc.) to `NO_PROXY`/`no_proxy` before `mimo run`, preventing proxy-blocked connections. (Session 109.5)
- **Mimo 403/proxy blocker resolved** — Xiaomi free-tier endpoint (`api.xiaomimimo.com`) reverted to `Illegal access` from all infra IPs; local Ollama path unblocked via NO_PROXY. No interactive login, no paid keys, no secrets in repo. (Session 109.5)
- **Mimo real execution smoke** — 11/11 guards + full task lifecycle validated end-to-end via Mimo in linked worktree. `agent-status.md`, `agent-report.md`, `implementation-diff.patch` all produced correctly. Exit 0. (Session 110)

### Tests

- 33 tests for `test_mcp_mimo.py` (was 29): default model, env override, explicit model, NO_PROXY exports, local IP coverage.

### Changed

- `mimo_tools.py`: `project_run_mimo()` model fallback from hardcoded string to `MIMO_DEFAULT_MODEL` env → `ollama-gen/gemma4:26b`; `_build_mimo_script()` exports `NO_PROXY`/`no_proxy` before Mimo execution.
- CI: Gitea CI Run #199 green (`b2f5c00`, healthcheck SSE fix) and #200 green (`652c86d`, Mimo defaults).

## [0.1.14-alpha] - 2026-06-25

### Added

- **Agent Handoff v2** — 6 new MCP tools for managing agent tasks: `write_agent_task`, `read_agent_status`, `read_agent_report`, `read_agent_diff`, `list_agent_tasks`, `archive_agent_task`. Task lifecycle: `created → running → needs-review | failed`. Structured `task.json` + `current-plan.md` contract with `agent`, `allowed_files`, `forbidden_files`, `commit_allowed`, `push_allowed` fields. (Sessions 99–102)
- **OpenCode runner** — `project_run_opencode` MCP tool (`gateway_project_run_opencode`) executes handoff tasks via OpenCode CLI (`--dangerously-skip-permissions`). Exit code → `needs-review` / `failed` status. `agent-report.md` and `implementation-diff.patch` auto-generated. (Sessions 103)
- **Mimo runner** — `project_run_mimo` MCP tool (`gateway_project_run_mimo`, chatgpt mode only) executes handoff tasks in disposable git worktrees via Mimo CLI. 11 pre-flight guards: task.json validation, agent check, worktree isolation, `MCP_GATEWAY_WORKTREE_ROOT` enforcement, linked worktree detection, binary discovery. Designed for `--dangerously-skip-permissions` safety. (Session 104)
- **Parallel two-agent dry-run** — validated end-to-end orchestration: ChatGPT coordinates OpenCode (real artifact task) + Mimo (worktree task) independently with no cross-contamination, independent status/report/diff, full cleanup, `make check` green. (Session 106)

### Tests

- 210 tests for `test_mcp_mimo.py` — model validation, command construction, guard logic, result mapping, registration.
- 87 tests for `test_mcp_opencode.py` — registration, mode visibility, shell execution, error handling.
- 121 tests for `test_opencode_runner_wrapper.py` — dry-run, binary discovery, task validation, diff capture.
- 153 tests for `test_agent_tasks.py` — write/read/list/archive, path guards, task_id validation.
- `make check` target extended with 6 test files (90 tests, all passing).
- CI: skipif guards for missing `mcp` package and missing `opencode` binary.

### Changed

- `opencode_runner_wrapper.py`: hardened heredoc, permissions, timeout handling (commit `c01b59a`).
- `mimo_tools.py`: all 11 guards execute in shell script on SSH target — binary discovery (`$MIMO_BIN`, `command -v`, `/root/.mimocode/bin/mimo`) handled inside shell, not in Python.
- `tool_modes.py`: `gateway_project_run_mimo` registered under chatgpt mode.
- `server.py`: both `project_run_opencode` and `project_run_mimo` registered with `assert_handoff_write_allowed()`.

### Docs

- Design spec: `docs/superpowers/specs/2026-06-25-mimo-runner-design.md`.
- Implementation plan: `docs/superpowers/plans/2026-06-25-mimo-runner-implementation.md`.
- AGENTS.md: Agent Handoff v2 lifecycle, OpenCode runner, Mimo runner, parallel orchestration.
- README: Agent Handoff v2 section with OpenCode + Mimo parallel orchestration diagram and examples.
- Runbook: `docs/operations/AGENT_HANDOFF_RUNBOOK.md`.

## [0.1.13-alpha] - 2026-06-24

### Added

- **Unified Gateway MCP Fleet endpoint** exposing 77 tools through a single `/mcp` ChatGPT App — no more separate adapter apps.
- Aggregated Context7 documentation tools (2) into the main Gateway MCP schema.
- Aggregated Docker read-only tools (7) into the main Gateway MCP schema.
- Aggregated Postgres read-only tools (6) into the main Gateway MCP schema.
- Added `asyncpg` to the remote MCP virtual environment for Postgres adapter support.

### Changed

- Extended `tool_modes.py` chatgpt mode to include the full unified fleet toolset.
- Updated MCP fleet healthcheck expectations from 62 tools to 77 tools.
- Simplified ChatGPT integration model: one app, one endpoint, one schema, no separate adapter apps required.

### Fixed

- Reduced ChatGPT action cache and namespace conflicts by consolidating adapter tools behind the main Gateway endpoint.

## [0.1.12-alpha] - 2026-06-24

### Added

- **Fleet healthcheck** (`scripts/mcp_fleet_healthcheck.py`): one-shot diagnostics for all 6 adapters — systemd, env file security, MCP tools/list, nginx route verification.
- **Operations runbook** (`docs/operations/MCP_FLEET_RUNBOOK.md`): adapter reference, service management, troubleshooting.
- **README/AGENTS.md operations pass**: healthcheck section with example output, agent instructions to run healthcheck first.

## [0.1.11-alpha] - 2026-06-24

### Added

- **Docker read-only MCP adapter** (`fleet/docker_server.py`): 7 safe tools (ps, images, inspect, logs, stats, compose_ps, compose_services). Read-only subprocess wrapper, shell=False.
- **Postgres read-only MCP adapter** (`fleet/postgres_server.py`): 6 tools (health, list_schemas, list_tables, describe_table, select, vector_status) for rag_vectordb. SQL guardrails: multi-statement ban, DDL/DML block, LIMIT 1000 wrapping, system schema block.
- **mcp_readonly DB user**: nosuperuser, read-only, statement_timeout=30s.
- **Public endpoints**: /mcp/docker, /mcp/postgres.

## [0.1.10-alpha] - 2026-06-22

### Added

- **62-tool Gateway MCP fleet**: main gateway (62 tools), GitHub (8), Gitea (12), Context7.
- **Phase 2 — Project-scoped local code tools**: 16 new `gateway_project_*` endpoints for safe file read, text search, file find, directory tree, git diff/cached, pytest, ruff, mypy, remotes, branch, commit head, handoff read/write/status.
- **GitHub read-only remote MCP adapter** (`fleet/github_server.py`): 8 read-only tools (repo info, commits, branches, file contents, search code, PR list, issues, user info) via fine-grained PAT.
- **Gitea read-only + CI/CD remote MCP adapter** (`fleet/gitea_server.py`): 12 tools (repo info, branches, file tree/read, issues, PRs, CI/CD runs, jobs, workflows, commit search).
- Introduced `.ai-bridge` handoff protocol: `current-plan.md` write with `MCP_GATEWAY_WRITE_MODE` guard.
- Added `MCP_GATEWAY_TOOL_MODE` visibility filter (`minimal`, `standard`, `full`, `chatgpt`).
- Added `MCP_PUBLIC_TOKEN` auth for Streamable HTTP/SSE public endpoint.
- Added `AGENTS.md` agent reference documentation for Phase 2 workflow.
- New CI runner: `runner-docker-e2e` dedicated to compose/E2E/smoke jobs.

### Changed

- `ssh_strict_host_key_checking` now controls Paramiko `AutoAddPolicy` vs `RejectPolicy`.
- `SecretManager` now requires `master_key` (raises `ValueError` if empty).
- `session_store.py` passes `settings.encryption_key` to `SecretManager` (fixes credential recovery on restart).
- `KNOWN_HOSTS_STORE=file` removed from docker-compose.yml (restores `NullHostKeyStore` default).

### Fixed

- Context7 adapter: increased httpx timeout 5→120s, AsyncExitStack lifecycle, stale subprocess retry with session reset.
- Ruff lint: `UP038` (union isinstance), `F841` (unused variable), `E402` (import ordering with load_dotenv).
- 571 tests passing, 1 skipped, 0 failures.

## [0.1.8-alpha] - 2026-06-19

### Added

- Added an experimental MCP server layer for AI coding agents.
- Added MCP tool visibility modes: `minimal`, `standard`, and `full`.
- Added structured MCP tool outputs with `content`, `structuredContent`, and `_meta`.
- Added `gateway_self_test` diagnostics with pass/warn/fail checks.
- Added guarded `.ai-bridge` handoff mode with `MCP_GATEWAY_WRITE_MODE`.

### Changed

- Added OpenCode MCP setup documentation.
- Improved MCP example safety boundaries and tool registration.
- Added Redis close/aclose compatibility across redis-py versions.

### Tests

- Test suite: 545 passed, 1 skipped.

## [0.1.7-alpha] - 2026-06-16

### Added

- Added an experimental RLM gateway auditor with controlled read-only subagents (disabled by default, `RLM_ENABLE_SUBAGENTS=1` or `--enable-subagents`).
- Added command allowlist/denylist enforcement for all root-agent commands in the RLM auditor profile (19 allowed prefixes, 16 denied patterns).
- Added gateway connectivity, auth, and session health checks via `--dry-run` mode — works without RLM/OpenAI.
- Added `.env.example` and setup documentation for the RLM auditor example.

### Changed

- The RLM auditor root-agent now uses `gateway_execute_restricted()` with allowlist validation instead of raw `gateway_execute()`.
- WebSocket stream timeout raised from 30s to 600s for long-running interactive sessions.
- Docker Compose networks changed to `external: true`; gateway container now attaches to `proxmox_macvlan` with static IP `10.10.10.145` for direct LXC→container routing behind nginx.
- `docs/RLM_ADAPTER_EXPERIMENT.md` updated with the v0–v3 implementation milestone table.

### Docs

- Added safety boundary warnings to `examples/rlm_gateway_auditor/README.md`: experimental, not a sandbox, use scoped token, no write/deploy commands allowed.

## [0.1.6-alpha] - 2026-06-06

### Added

- Added local Web UI authentication with first-admin registration, JWT login, logout, and auth-gated frontend initialization.

### Fixed

- `/api/auth/*`, `/static/*`, and `/` are now public paths — middleware no longer blocks registration/login/verify with 403 when `API_AUTH_ENABLED=true`.
- Auth middleware falls back to JWT Bearer token verification when no `X-API-Key` header is present, enabling the web UI login flow.

### Changed

- `SSH_KEY_UPLOAD_DIR` is now configurable via env var (default `./ssh_keys`).

### Tests

- SSH key upload test uses `tmp_path` instead of repo working directory — fixes `PermissionError` in CI runner.

### Security

- Added fail-closed `JWT_SECRET` validation — gateway refuses to start with an empty secret.
- Public registration is automatically disabled after the first user is created.
- Registration guarded by `asyncio.Lock` to prevent race conditions on first-user creation.
- JWT payload enforces `type: "web-ui"` to prevent cross-context token reuse.
- Parent directory for `AUTH_DB_PATH` is created automatically on startup.
- Frontend fetch wrapper uses `Headers` API instead of raw object access for safe Bearer token injection.
- Register link hidden from the UI when an admin already exists.
- Added 15 auth tests covering: check (empty/after register), register (first/second/weak/mismatch/login-gate), login (valid/invalid/nonexistent), verify (valid/missing/invalid/expired), protected endpoint with JWT.

## [0.1.5-alpha] - 2026-06-05

### Changed

- Refined README positioning for the project as an OpenAPI-first SSH control plane for AI agents.
- Migrated settings configuration to the Pydantic v2 `SettingsConfigDict` style.
- Replaced deprecated Redis `close()` usage with `aclose()` in `redis_queue.py` and `distributed_lock.py`.

## [0.1.4-alpha] - 2026-06-05

### Added

- Added optional command output redaction for secrets (tokens, passwords, API keys) in command responses. Opt-in via `COMMAND_OUTPUT_REDACTION_ENABLED` setting or per-request `redact_output` parameter.
- Redaction applies to sync execute (`POST /api/ssh/execute`), job result (`GET /api/jobs/{job_id}/result`), and job SSE stream (`GET /api/jobs/{job_id}/stream` and `/events` alias).
- Raw job output is not mutated — redaction is applied on the response/stream side only.

### Tests

- Added 13 tests covering output redaction: sync execute (4), job result (4), SSE stream (5).

## [0.1.3-alpha] - 2026-06-05

### Added

- Added `async_mode` to `POST /api/ssh/execute` to start long-running commands through the existing job manager without changing the default synchronous behavior. Async commands can be tracked via `GET /api/jobs/{job_id}/status`.
- Added async execute job lifecycle coverage — async mode now creates a job that can be inspected through the existing jobs API, supporting the full `exec → job_id → status` flow.

### Tests

- Added 6 tests covering async mode: sync backward compat, async job creation, command policy bypass prevention, cross-tenant isolation, and E2E execute → status flow.

## [0.1.2-alpha] - 2026-06-04

### Security

- Added path validation (`validate_path`) for project tree, project file structure, and file watch endpoints to reject traversal attacks.
- Added session ownership checks for AST rename, extract, analyze, bulk read, and bulk edit file endpoints — agent tokens can no longer operate on cross-tenant sessions through these routes.
- Reduced secret exposure: `sanitize_command()` no longer logs raw passwords, tokens, or API keys to the warning log.
- Added `SSH_STRICT_HOST_KEY_CHECKING` env var — when enabled, uses `RejectPolicy` instead of `AutoAddPolicy`.
- `SecretManager` now requires an explicit `master_key` — removed automatic random key generation that made encrypted sessions unrecoverable after restart.
- Event hook delivery safety: `assert` replaced with `RuntimeError` for invariant violations.
- Added 22 regression tests covering path traversal rejection (12) and file endpoint ownership enforcement (10).

### Changed

- `auth_middleware.py`: `AuthIdentity` migrated from `dataclass` to `TypedDict`; `X-Forwarded-For` parsing added for CIDR checks behind proxies.
- `pyproject.toml`: `paramiko` import guarded with `TYPE_CHECKING` to silence mypy import-untyped.
- `session_store.py`: fixed `SecretManager` instantiation — now correctly passes `settings.encryption_key` instead of creating a random key on every call.
- `ROUTER_ARCHITECTURE.md`: updated with router lifecycle and security model.

## [0.1.1-alpha] - 2026-06-04

### Changed

- Refactored the API routing layer into dedicated feature routers without changing public API paths or response contracts.
- Extracted API help generation from the system router into `app/api_help.py`.
- Extracted feature-specific routes from `app/routers/system.py`:
  - known-hosts management
  - server inventory and connection routes
  - snapshot management
  - webhook and deployment routes
  - batch execution
  - global search and replace
  - code search/generation/completion
  - project analytics and file tree inspection
- Reduced `app/routers/system.py` to lightweight system/meta GET endpoints only.
- Added router ownership documentation in `docs/ROUTERS.md`.

### Tests

- Verified route uniqueness, route authorization, and OpenAPI contract checks.
- Full test suite remains green: 435 passed.

### Compatibility

- No public API path changes.
- No authentication behavior changes.
- No response model changes.

## [0.1.0-alpha] - 2026-06-02

### Added

- FastAPI-based SSH gateway API.
- Master and agent token authentication model.
- Scope-based access control for agent tokens.
- SSH session lifecycle endpoints.
- Command execution over HTTP.
- Command execution over WebSocket stream.
- Interactive PTY WebSocket stream.
- File operations over SSH sessions (read, edit, write, upload, download, patch).
- File watch WebSocket support.
- Session ownership checks for HTTP and WebSocket session-bound operations.
- Target CIDR allow/deny restrictions.
- Command policy engine with off, audit, and enforce modes.
- Secret redaction for audit logs and event hook payloads.
- Private key upload disabled by default.
- Event hook management for workflow automation.
- Python project metadata via pyproject.toml.
- MIT license.
- Security model and threat assumptions in SECURITY.md.

### Fixed

- Agent tokens can no longer access sessions owned by other tokens through HTTP session-bound endpoints.
- Agent tokens can no longer access sessions owned by other tokens through WebSocket execute, PTY, and file watch streams.
- Duplicate API route registrations were removed.
- Ruff checks pass for app and tests modules.

### Changed

- Public Docker Compose example simplified for open-source use.
- Production-specific infrastructure artifacts removed from the repository.

### Known limitations

- Early MVP / alpha release.
- Public API may change before v1.0.0.
- Not intended to be exposed directly to the public Internet.
- Not a full multi-tenant enterprise isolation system.
- No guarantee of protection if the master token or host OS is compromised.
