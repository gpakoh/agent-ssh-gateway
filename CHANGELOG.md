# Changelog

All notable changes to this project will be documented in this file.

This project follows semantic versioning where practical, but the public API is not considered stable before v1.0.0.

## [Unreleased]

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
- Docker Compose networks changed to `external: true`; gateway container now attaches to `proxmox_macvlan` with static IP `10.0.0.145` for direct LXC→container routing behind nginx.
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
