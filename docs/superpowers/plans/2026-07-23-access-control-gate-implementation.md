# Phase 12B — Agent Access Approval Gate Implementation Plan

> **Status:** Ready for subagent execution
> **Spec:** `docs/superpowers/specs/2026-07-23-access-control-gate-design.md`
> **Date:** 2026-07-23
> **Goal:** Make Telegram Allow/Deny buttons enforce real gateway access policy for `(actor_fingerprint, source_ip)` tuples.

---

## Non-Negotiable Semantics

- Access key is `(actor_fingerprint, source_ip)`.
- `actor_fingerprint` is the existing 12-character SHA-256 prefix, never a raw token.
- Redis key is `ac:{sha256(actor_fingerprint:source_ip)[:16]}`; raw IPs and raw fingerprints must not appear in Redis keys.
- `master` is exempt by default unless `ACCESS_CONTROL_ENFORCE_MASTER=true`.
- `denied` blocks SSH connect, execute, execute-argv, execute-stream, PTY, jobs, bulk, and batch.
- `pending` allows SSH connect, but command execution is capped to `readonly` or `testlint`.
- `pending` blocks PTY because an interactive shell cannot be profile-capped.
- Notifier callback tokens are not authentication; gateway admin API auth is still `require_master_key`.
- Dry-run notifier must not call Telegram `getUpdates`.
- No raw command, path, host, username, token, or session credentials in alerts, callbacks, Redis keys, logs, docs, or tests.

---

## Slice 1 / Agent 1 — Access Control Core

**Objective:** Add the gateway-owned access-control engine with in-memory hot path and optional Redis backing. No router enforcement yet.

### Files

| File | Action |
|------|--------|
| `app/access_control.py` | New |
| `app/config.py` | Add access-control env settings |
| `app/state.py` | Add `access_control_store` |
| `app/main.py` | Initialize store and cleanup task |
| `.env.example` | Add commented access-control settings |
| `tests/test_access_control.py` | New |
| `tests/test_access_control_redis.py` | New or extend with mocked Redis |

### Implementation Tasks

- [ ] Add `AccessDecision`, `AccessPolicyResult`, `AccessDeniedError`, `AccessPendingApprovalError`.
- [ ] Add `make_access_key_hash(actor_fingerprint, source_ip) -> str`, returning 16 hex chars.
- [ ] Add in-memory store with `set_decision()`, `get_decision()`, `cleanup_expired()`, `recent()`.
- [ ] Add TTL defaults: pending 900s, allowed 86400s, denied 86400s.
- [ ] Add Redis best-effort persistence using `ACCESS_CONTROL_REDIS_URL or REDIS_URL`.
- [ ] Add startup load of non-expired Redis entries into memory.
- [ ] Add `capped_profile(requested)` preserving `readonly`/`testlint`, downgrading others to `readonly`.
- [ ] Add `resolve_access_policy(identity, source_ip, requested_profile, operation, enforce_master=False)`.
- [ ] Ensure Redis failure logs warning but never blocks request flow.

### Tests

- [ ] Unknown tuple returns `pending` with capped profile.
- [ ] `allowed` returns requested profile unchanged.
- [ ] `denied` raises `AccessDeniedError`.
- [ ] Master is `exempt` by default.
- [ ] Master enforced when `enforce_master=True`.
- [ ] `capped_profile("ops") == "readonly"`.
- [ ] `capped_profile("testlint") == "testlint"`.
- [ ] Expired decisions are treated as pending.
- [ ] Redis keys contain only `ac:` + 16 hex chars.
- [ ] Redis write/load works with mocked Redis.
- [ ] Redis unavailable falls back to memory and logs warning.

### Gates

```bash
ruff check app/access_control.py tests/test_access_control.py tests/test_access_control_redis.py
python3 -m mypy app/access_control.py app/config.py app/state.py app/main.py
pytest tests/test_access_control.py tests/test_access_control_redis.py -q
python3 scripts/check_public_hygiene.py
```

---

## Slice 2 / Agent 2 — Gateway Enforcement + Admin Endpoint

**Objective:** Wire access decisions into real gateway behavior and expose a master-key admin decision endpoint.

### Files

| File | Action |
|------|--------|
| `app/routers/admin_access.py` | New |
| `app/main.py` | Include admin router |
| `app/api_help.py` | Document endpoint and error codes |
| `app/routers/ssh.py` | Enforce connect, execute, argv, WS execute, PTY |
| `app/routers/jobs.py` | Enforce jobs/run and bulk/execute |
| `app/routers/batch.py` | Enforce batch execute |
| `app/ssh_manager.py` | Store `source_ip`, add disconnect helper |
| `tests/test_admin_access.py` | New |
| `tests/test_access_gate.py` | New |

### Implementation Tasks

- [ ] Add `POST /api/admin/access-control/decision`, protected by `require_master_key`.
- [ ] Accept only `actor_fingerprint`, `source_ip`, `decision`, `reason`, `ttl_seconds`, `request_id`.
- [ ] Normalize request decision `allow|deny` into stored state `allowed|denied`.
- [ ] Emit structured audit event `access_control.decision`.
- [ ] On deny, call internal `disconnect_sessions_for_actor_source()`.
- [ ] Add `source_ip` to `SessionRecord` and pass trusted-proxy-aware source IP from `ssh_connect()`.
- [ ] Add WebSocket source-IP helper matching `get_client_ip()` behavior.
- [ ] Enforce `denied` on `POST /api/ssh/connect`.
- [ ] Enforce profile capping before `evaluate_command_policy()` on `execute`, `execute-argv`, `WS execute/stream`, `jobs/run`, `bulk/execute`, `batch/execute`.
- [ ] Enforce `pending` block on `WS /api/ssh/pty/{session_id}/stream`.
- [ ] Return `403` with `ACCESS_DENIED` or `ACCESS_PENDING_APPROVAL` where applicable.
- [ ] Update legacy and structured audit details to include access state/profile where useful, without leaking raw command/path/host.

### Tests

- [ ] Admin endpoint rejects missing auth and agent token.
- [ ] Admin endpoint accepts master key and stores decision.
- [ ] Admin endpoint deny kills matching sessions only.
- [ ] Denied actor+IP cannot connect.
- [ ] Pending actor+IP can connect.
- [ ] Pending actor command runs under readonly/testlint cap.
- [ ] Pending actor PTY is rejected with `ACCESS_PENDING_APPROVAL`.
- [ ] Denied actor cannot use execute, execute-argv, WS execute, jobs/run, bulk/execute, batch/execute.
- [ ] Allowed actor passes through original profile.
- [ ] Master exempt by default.
- [ ] `ACCESS_CONTROL_ENFORCE_MASTER=true` enforces master.
- [ ] Trusted proxy source IP is used for HTTP routes.
- [ ] Trusted proxy source IP is used for WebSocket routes.
- [ ] `/api/help` documents endpoint and error codes truthfully.

### Gates

```bash
ruff check app/routers/admin_access.py app/routers/ssh.py app/routers/jobs.py app/routers/batch.py app/ssh_manager.py tests/test_admin_access.py tests/test_access_gate.py
python3 -m mypy app/routers/admin_access.py app/routers/ssh.py app/routers/jobs.py app/routers/batch.py app/ssh_manager.py
pytest tests/test_admin_access.py tests/test_access_gate.py tests/test_api_help_docs.py -q
python3 scripts/check_public_hygiene.py
```

---

## Slice 3 / Agent 3 — Notifier Buttons → Gateway Decisions

**Objective:** Make Telegram buttons call the gateway admin endpoint and remove notifier-local access authority.

### Files

| File | Action |
|------|--------|
| `app/notifier/actions.py` | Keep opaque callback tokens |
| `app/notifier/access.py` | Delete or stop using; gateway owns state |
| `app/notifier/callbacks.py` | Rewrite to call gateway admin API |
| `app/notifier/get_updates.py` | Add `run_forever()` if missing |
| `app/notifier/telegram.py` | Add `answer_callback_query()` and `edit_message_text()` |
| `app/notifier/service.py` | Attach inline buttons for configured event types |
| `app/notifier/config.py` | Add `action_event_types` |
| `app/notifier/__main__.py` | Start callback poller only in real-send mode |
| `.env.example` | Add commented action event config |
| `docs/operations/NOTIFIER.md` | Document operator flow |
| `tests/test_notifier_actions.py` | Extend |
| `tests/test_notifier_callbacks.py` | Rewrite/extend |
| `tests/test_notifier_buttons.py` | New |

### Implementation Tasks

- [ ] Add `GATEWAY_NOTIFIER_ACTION_EVENT_TYPES`, default `command.deny,workspace.readonly_block`.
- [ ] Attach buttons only when event type is configured and event has `actor_fingerprint + source_ip`.
- [ ] Button labels: `Allow actor+IP`, `Deny actor+IP`.
- [ ] Keep callback token opaque and 1-hour TTL.
- [ ] Callback handler posts to `/api/admin/access-control/decision` with notifier gateway API key.
- [ ] Callback handler calls `answerCallbackQuery`.
- [ ] Callback handler edits the original Telegram message to remove buttons and show decided state.
- [ ] Callback handler sends or edits safe follow-up text with actor fingerprint prefix and source IP only.
- [ ] Start `CallbackPoller` only when `settings.can_send_telegram` is true.
- [ ] Dry-run smoke must remain text-only/no `getUpdates`.
- [ ] Remove notifier-local `record_decision()` usage from runtime.

### Tests

- [ ] Buttons attach to `command.deny` and `workspace.readonly_block` by default.
- [ ] Buttons do not attach to digest events unless configured.
- [ ] Buttons do not attach when actor/source fields are absent.
- [ ] Callback posts correct payload to gateway admin endpoint.
- [ ] Callback uses master API key header.
- [ ] Callback token is not accepted as auth.
- [ ] Callback answers query and edits message.
- [ ] Expired callback token does not call gateway.
- [ ] Dry-run notifier does not start poller or call `getUpdates`.
- [ ] Alert/follow-up text contains no raw command/path/host/token/session credentials.

### Gates

```bash
ruff check app/notifier tests/test_notifier_actions.py tests/test_notifier_callbacks.py tests/test_notifier_buttons.py
python3 -m mypy app/notifier
pytest tests/test_notifier*.py -q
python3 scripts/check_public_hygiene.py
```

---

## Slice 4 / Agent 4 — Final Gate + Release Readiness

**Objective:** Verify the whole chain end-to-end before any tag/deploy.

### Required Checks

- [ ] `ruff check .`
- [ ] `python3 -m mypy app/`
- [ ] `pytest -m "not host_smoke"`
- [ ] `python3 scripts/check_public_hygiene.py`
- [ ] `python3 scripts/check_no_hardcoded_secrets.py`
- [ ] Verify no `tg-bot-service` paths changed.
- [ ] Verify notifier overlay is still opt-in and dry-run-first.
- [ ] Verify main compose does not auto-start notifier.
- [ ] Verify dry-run smoke does not call Telegram `getUpdates`.

### Behavior Matrix

| Scenario | Expected |
|----------|----------|
| Unknown agent+IP connects | Session created |
| Unknown agent+IP runs `ls` | Allowed under `readonly` |
| Unknown agent+IP runs write command | `403 ACCESS_PENDING_APPROVAL` or policy deny under capped profile |
| Unknown agent+IP opens PTY | `403`/WS close with `ACCESS_PENDING_APPROVAL` |
| Operator clicks Allow | Gateway decision stored, actor+IP gets normal profile |
| Operator clicks Deny | Gateway decision stored, active sessions killed |
| Denied actor+IP reconnects | `403 ACCESS_DENIED` |
| Master key request | Exempt unless `ACCESS_CONTROL_ENFORCE_MASTER=true` |
| Redis down | Gateway continues with memory state and warning |

### No Release Until

- Gateway enforcement exists and is tested.
- Notifier buttons call gateway admin endpoint.
- Hygiene and secret scanners pass.
- CI green on Python 3.11 and 3.12.

---

## Recommended Dispatch Order

1. **Agent 1:** Slice 1 only. Commit after tests pass.
2. **Gate:** Review `access_control.py` semantics before router wiring.
3. **Agent 2:** Slice 2 only. Commit after endpoint/enforcement tests pass.
4. **Gate:** Empirically test pending/denied/allowed on real routers.
5. **Agent 3:** Slice 3 only. Commit after notifier tests pass.
6. **Agent 4:** Slice 4 final gate. No tag/deploy.

