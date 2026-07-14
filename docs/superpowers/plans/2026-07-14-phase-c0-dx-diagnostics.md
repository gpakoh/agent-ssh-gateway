# Phase C0 - DX diagnostics, no new write surface

Status: proposed agent work plan  
Date: 2026-07-14  
Repository: `agent-ssh-gateway`  
Baseline: `2f630fa` after Phase B read-only workspace tools

## Goal

Improve agent/operator diagnostics for authentication and SSH session lifecycle without expanding the gateway write/execution surface.

Phase C0 should make the common failure modes explicit:

- invalid or stale API key (`401`)
- expired or missing SSH session (`SESSION_NOT_FOUND`)
- quick SDK preflight before command execution
- one-shot helper operations that always connect and disconnect cleanly

## Non-goals

Do not add any new dangerous capability in this phase:

- no generic script execution endpoint
- no apply-patch endpoint
- no project write/edit endpoint
- no Docker/deploy helpers
- no shell command shape changes
- no permission model broadening
- no changes to Phase B read-only workspace security invariants

## Security invariants

All new endpoints and SDK helpers must preserve:

- existing API-key auth middleware
- no secret values in logs or response bodies
- no filesystem access outside existing endpoint behavior
- no command execution beyond already-existing SSH execute API
- stable, structured error responses for agents
- test coverage for invalid key/session cases

## Agent 1 - Auth/session diagnostic API

Scope:

- API routes and schemas for diagnostics only
- tests for success and failure paths
- docs if endpoint index/help is maintained in repo

Allowed files:

- `app/**` auth/session/API modules
- `tests/**` matching API tests
- endpoint/help docs if already used by the gateway

Tasks:

1. Add `GET /api/auth/check`.
   - Valid key returns HTTP 200 with a non-sensitive response, for example:
     ```json
     {"valid": true, "auth_mode": "api_key", "key_name": "default"}
     ```
   - Invalid/missing key returns current auth failure semantics, but with clearer hint text.
   - Do not return the API key, token prefix, hash, or any secret material.

2. Add `POST /api/session/check`.
   - Input: `{"session_id": "..."}`.
   - Live session returns:
     ```json
     {"valid": true, "session_id": "...", "status": "connected"}
     ```
   - Missing/expired session returns a non-500 structured response, for example:
     ```json
     {"valid": false, "code": "SESSION_NOT_FOUND", "hint": "Create a session via POST /api/ssh/connect"}
     ```
   - Do not execute remote shell commands as part of this check.

3. Improve `401` diagnostics.
   - Keep HTTP status and security posture.
   - Add a clear hint such as: `Provide X-API-Key header with a valid API key`.
   - Do not mention real configured keys.

Validation:

```bash
ruff check .
python3 -m mypy .
pytest -q tests/test_auth* tests/test_session* tests/test_api*  # adjust to actual test names
```

Report:

```text
Agent 1 - Auth/session diagnostics
Changed files:
Endpoints:
Tests:
Validation:
Security notes:
```

## Agent 2 - SDK diagnostics and quick helpers

Scope:

- Python SDK convenience methods only
- no server-side write surface additions
- tests for helper behavior with mocked HTTP/session calls

Allowed files:

- `sdk/ssh_gateway.py`
- SDK package exports if present
- SDK tests/docs/examples

Tasks:

1. Add `client.auth_check()`.
   - Calls `GET /api/auth/check`.
   - Returns structured dict/object.
   - Raises or returns a clear auth error consistently with existing SDK style.

2. Add `client.session_check(session_id=None)`.
   - Uses current client session id if omitted.
   - Calls `POST /api/session/check`.
   - Does not run a remote command.

3. Add `quick` helpers for one-shot operations.
   - `quick.run(host, username, password=None, private_key=None, command="...", port=22, base_url=..., api_key=...)`
   - `quick.read(host, username, password=None, private_key=None, path="...", port=22, base_url=..., api_key=...)`
   - Optional `quick.edit(...)` only if it delegates to an already-existing file edit endpoint and preserves existing auth/path restrictions.
   - Every helper must perform `connect -> operation -> disconnect` in a `finally` block.

4. Add examples in SDK docs or docstring.

Validation:

```bash
ruff check sdk tests
python3 -m mypy sdk
pytest -q tests/test_sdk* tests/test_quick*  # adjust to actual test names
```

Report:

```text
Agent 2 - SDK diagnostics and quick helpers
Changed files:
Methods/helpers:
Tests:
Validation:
Compatibility notes:
```

## Agent 4 - REST wiring and discoverability

Scope:

- route registration only
- `/api/help` / capabilities / endpoint index if that is the project convention
- HTTP smoke tests through the real app router

Non-goals:

- do not implement auth/session business logic
- do not modify SDK helpers
- do not add write/execute/apply_patch endpoints
- do not change authentication semantics beyond Agent 1 outputs

Allowed files:

- app entrypoint/router registration files, for example `app/main.py` or router registry modules
- help/capabilities endpoint metadata if maintained separately
- integration tests proving route reachability
- docs only when needed to describe discoverability

Tasks:

1. Verify or register `GET /api/auth/check` in the public API router.
2. Verify or register `POST /api/session/check` in the public API router.
3. Ensure `/api/help`, `/api/capabilities`, or the project endpoint index lists both endpoints if that is the existing convention.
4. Add or adjust integration smoke tests proving both endpoints are reachable through the app router.
5. Confirm invalid auth still returns HTTP 401 and does not leak configured keys, token prefixes, hashes, or session secrets.
6. Confirm no new write/execute/project-edit route appears in help/capabilities as part of Phase C0.

Validation:

```bash
ruff check .
python3 -m mypy .
pytest -q tests/test_auth* tests/test_session* tests/test_app* tests/test_api*  # adjust to actual test names
```

Report:

```text
Agent 4 - REST wiring and discoverability
Changed files:
Registered routes:
Help/capabilities updated:
HTTP smoke tests:
Security notes:
Non-goals preserved:
```

## Agent 3 - Arbiter/docs/integration pass

Scope:

- verify Agent 1 and Agent 2 integration
- update docs for Phase C0
- do not add new API behavior beyond accepted patches

Allowed files:

- docs/API/help docs if present
- `README.md` / SDK docs if existing convention uses them
- tests only if fixing documentation-test mismatch

Tasks:

1. Verify endpoint behavior manually or with integration tests:
   - valid auth check
   - invalid auth check
   - live session check
   - expired/missing session check

2. Verify SDK helpers:
   - `auth_check()` before connect
   - `session_check()` after connect
   - `quick.run()` disconnects on success and failure

3. Confirm Phase B read-only workspace endpoints still pass.

4. Record final validation note.

Validation gate:

```bash
ruff check .
python3 -m mypy .
pytest -q tests/test_workspace* tests/test_app_workspace* tests/test_auth* tests/test_session* tests/test_sdk* tests/test_quick*
```

If full `pytest -q` still hits pre-existing collection errors from `jwt/cryptography` warnings, classify them explicitly and do not claim full-suite green.

Report:

```text
Agent 3 - Phase C0 arbiter
Repo status:
Changed files:
Endpoint checks:
SDK checks:
Workspace regression:
Full pytest status:
Decision: ready to push yes/no
```

## Acceptance criteria

Phase C0 is complete when:

- `GET /api/auth/check` exists and is tested
- `POST /api/session/check` exists and is tested
- SDK exposes `auth_check()` and `session_check()`
- quick helper(s) exist with guaranteed disconnect in `finally`
- no new script/apply_patch/project-write endpoint is introduced
- workspace Phase B tests remain green
- ruff and mypy are green
- known unrelated full-suite collection errors are documented, not hidden

## Recommended commit split

Use small commits:

1. `api: add auth and session diagnostics`
2. `sdk: add diagnostics and quick helpers`
3. `docs: record phase c0 diagnostics`

Do not tag a release in this phase unless explicitly requested.
