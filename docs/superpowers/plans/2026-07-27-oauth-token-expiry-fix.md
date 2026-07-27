# OAuth Persisted Token Expiry — Bugfix Plan

**Status:** PR1 (tests) landed `7b687c8`. PR2 (fix) landed — bug is
fixed. PR3 (docs/changelog/release) pending.
`docs/superpowers/specs/2026-07-27-public-mcp-oauth-decision.md` §3
("Persisted token expiry is not enforced on load"). Each PR below is a
separate, independently gated slice — none is authorized to run by
this plan itself, matching this repo's established small-sequential-PR
convention (see the Phase 16A/18B private-transport plans this
document mirrors in structure).

**Goal:** A persisted token's `expires_at` must actually be honored
after a process restart — an expired persisted token must not
authorize, and a revoked persisted token must stay rejected — without
touching public-connector work, OAuth UX, or anything not required to
fix this specific correctness gap.

**Non-goals:** public connector, reverse proxy, OAuth flow/consent UX
changes, destructive schema migration, anything from
`docs/superpowers/specs/2026-07-27-public-mcp-oauth-decision.md`'s
Option C/D wiring work.

---

## 1. Current behavior (read directly from source, not assumed)

- **Where a persisted token's expiry is *recorded*:**
  `examples/mcp_server/token_store.py`'s `StoredTokenEntry` dataclass
  has an `expires_at: str | None = None` field. `TokenStore.add()` /
  `_save()` persist it (or its absence) faithfully to the JSON store
  file, atomically, via the existing `fcntl`-locked
  tempfile-then-`os.replace` path — this part already works correctly
  and needs no change.
- **Where it is currently *set* (or not) when a token is created:**
  `scripts/mcp_token_cli.py`'s `_handle_create()` constructs
  `StoredTokenEntry(id=..., token_hash=..., name=..., profile=...,
  scopes=..., created_at=now)` — it **never passes `expires_at`**, so
  every token created via `mcp-token create` today persists with
  `expires_at=None` (infinite, by omission) already, at creation time.
  There is currently no CLI flag or code path anywhere in this repo
  that persists a *real*, finite `expires_at` value on a
  `StoredTokenEntry`. This matters for scoping the fix honestly: the
  bug described below is a **latent correctness gap in the reload
  path**, not something exploitable via any feature that exists today
  — it becomes a real, active problem the moment any future feature
  (a `--ttl` CLI flag, OAuth-issued-token persistence, etc.) starts
  setting a real expiry. It must still be fixed now, both because it
  is simply wrong and because §3 of the OAuth decision doc lists
  "verified token storage/rotation/revocation lifecycle" as a public-connector
  prerequisite that this gap would otherwise silently fail.
- **Where OAuth-issued (not CLI-created) tokens live:** confirmed by
  grep — `GatewayOAuthProvider.exchange_code_for_token()` and
  `refresh_access_token()` mint access/refresh tokens with real,
  finite `expires_at` values (`time.time() + 7200` / `+ 604800`) but
  write them **only** to the in-memory `self._tokens` dict; neither
  method ever calls `self._token_store.add(...)`. So today,
  dynamically-issued OAuth tokens are not persisted at all (lost on
  restart, which is a separate, already-documented limitation in the
  OAuth decision doc — not this bug, and not in scope for this fix).
  The persisted-entry path (`TokenStore` + `load_tokens()`) is
  exercised only by statically pre-registered/CLI-created tokens.
- **Where expiry is *lost* on reload:**
  `GatewayOAuthProvider.load_tokens()` (`examples/mcp_server/oauth_provider.py`)
  reads every non-revoked `StoredTokenEntry` from the store and calls
  `register_hashed_token(token_hash=entry.token_hash, profile=entry.profile,
  scopes=list(entry.scopes))` — note `entry.expires_at` is **not
  passed at all**. `register_hashed_token()`'s own signature has no
  `expires_at` parameter, and it unconditionally constructs
  `StoredToken(..., expires_at=float("inf"), ...)`. The persisted
  value (even if some future caller starts setting a real one) is
  silently discarded on every reload.
- **How this affects revoke/expiry/restart today:**
  - **Revocation**: unaffected by this bug — `revoke_token()` /
    `revoke_client_token()` delete the in-memory entry immediately and
    mark the persisted entry `revoked_at`, and `load_tokens()` already
    skips entries with `revoked_at is not None`. Revocation survives
    restart correctly today.
  - **Expiry**: not enforced across restart, for the reason above. If
    any future caller ever persists a finite `expires_at`, that token
    would incorrectly become "eternal" the moment the process reloads
    it, regardless of the stored value, unless it is separately
    revoked.
  - **In-memory-only OAuth tokens**: expiry *is* enforced correctly
    for the lifetime of a single process (`verify_access_token()` /
    `load_access_token()` both check `time.time() > stored.expires_at`)
    — this bug is specific to the reload path, not to the in-memory
    check itself, which is already correct.

## 2. Required fix

- **Persist expiry accurately**: no change needed to `TokenStore`
  itself — it already round-trips `expires_at` faithfully. The fix
  belongs entirely on the *read* side.
- **Reload expiry accurately**: `register_hashed_token()` must accept
  and apply a persisted `expires_at`, and `load_tokens()` must pass
  `entry.expires_at` through to it, converting the store's `str | None`
  ISO-8601-or-similar representation to the same numeric
  epoch-seconds form `StoredToken.expires_at` already uses everywhere
  else in the module (matching the existing `float("inf")` sentinel
  for "no expiry" so the rest of the class — `verify_access_token()`,
  `load_access_token()` — needs no changes at all).
- **Expired tokens must not authorize after restart**: a direct,
  behavioral consequence of the above — once `expires_at` survives
  reload, the existing `time.time() > stored.expires_at` checks
  already in `verify_access_token()`/`load_access_token()` correctly
  reject it. No new expiry-checking logic is needed, only correct data
  flowing into the structure that's already checked.
- **Revoked tokens remain revoked**: no change to this path; confirm
  with a regression test that the fix does not alter revocation
  behavior (see §3).
- **No token values in logs**: the fix touches no logging statements.
  Confirm by inspection/test that the parsing of a persisted
  `expires_at` string never gets printed verbatim in any error path
  (e.g. a malformed timestamp should fail closed and log only that
  parsing failed, never the raw token hash or value).
- **Backward compatibility for old persisted entries without expiry**:
  every entry persisted by every code path in this repo today has
  `expires_at=None` (see §1) — this must continue to mean "no expiry",
  mapped to the same `float("inf")` sentinel already used elsewhere,
  so existing token-store files require no migration and no entry
  silently stops working after this fix ships.

## 3. Tests to add

All in `tests/test_server_token_integration.py` (extending existing
coverage, e.g. `test_load_tokens_from_store`,
`test_load_tokens_skips_revoked`) and/or `tests/test_oauth_provider.py`,
following those files' existing fixture patterns — no new test file
needed for a change this narrowly scoped:

1. **Token expires before restart → rejected after reload.** Persist
   an entry with `expires_at` set to a timestamp in the past (relative
   to the test's mocked or real "now"), call `load_tokens()` on a
   fresh `GatewayOAuthProvider`, and assert `verify_access_token()` /
   `load_access_token()` return `None` for the corresponding raw
   token — i.e. reproduce the bug first (this test must fail against
   today's code, proving it's a real regression test, not a tautology)
   then pass against the fix.
2. **Token not expired before restart → accepted after reload, until
   its persisted expiry.** Persist an entry with a future
   `expires_at`, reload, confirm the token verifies successfully now,
   and — using a monkeypatched/frozen clock — confirm it stops
   verifying once that same future time is reached, without a second
   restart.
3. **Revoked token stays rejected after reload.** Persist an entry,
   revoke it via `TokenStore.revoke()` (or
   `GatewayOAuthProvider.revoke_token()` before a simulated restart),
   reload into a fresh provider instance, and confirm
   `load_tokens()`'s existing `revoked_at is not None` skip still
   applies — this is a non-regression test for behavior that already
   works, guarding against the fix accidentally changing it.
4. **Old token record without `expires_at` handled safely.** Load a
   `StoredTokenEntry` (or raw JSON dict) with `expires_at` absent/`None`
   — exactly the shape every entry created by
   `scripts/mcp_token_cli.py` has today — and confirm it still behaves
   as "never expires" after the fix, matching current (pre-fix)
   observed behavior for this specific case. This is the explicit
   backward-compatibility regression test.
5. **No raw token persisted or logged.** Confirm (already true today,
   regression-guard it) that fixing the expiry plumbing does not
   introduce any new place where a raw token — as opposed to its
   `sha256:` hash — is written to the store file or printed. Reuse the
   existing hygiene pattern from `tests/test_token_store.py`.
6. **DCR/PKCE flow unaffected.** Run the existing
   `test_authorization_code_flow`, `test_pkce_verification_*`, and
   DCR-adjacent tests in `tests/test_oauth_provider.py` unmodified
   after the fix — these exercise in-memory-only token issuance
   (never touching `TokenStore`), so they must remain green exactly as
   they are today, proving the fix is additive to the reload path only
   and does not touch the authorization-code/PKCE flow at all.

## 4. Scope

- No public connector work of any kind — this is a private-code
  correctness fix, independent of whether a public connector is ever
  built.
- No reverse proxy, no TLS, no Docker Compose/systemd changes.
- No OAuth UX changes — no new CLI flags, no new consent-screen
  behavior. (A future `--ttl` flag on `mcp-token create` is exactly
  the kind of feature that would make this bug immediately
  exploitable — worth flagging to whoever picks this up next, but
  explicitly **not** part of this fix's scope.)
- No destructive schema migration. The store file's on-disk shape does
  not change — `expires_at` is already a field on `StoredTokenEntry`
  and already round-trips through `TokenStore` correctly; only the
  in-memory reconstruction in `GatewayOAuthProvider` needs to actually
  use it.

## 5. Implementation slices

### PR1 — Tests reproducing the bug

**Files:**
- Modify: `tests/test_server_token_integration.py` (or
  `tests/test_oauth_provider.py`, whichever already has the closer
  fixture for constructing a `GatewayOAuthProvider` + `TokenStore`
  pair — decide at implementation time by reading both files' existing
  fixtures, don't guess) — add tests 1, 2, and 4 from §3.

**Tests to add/run:** the new tests themselves, run once to confirm
they **fail** against current `main` (proving they reproduce the real
bug, not a strawman), then committed in a failing state with a clear
note in the PR description that they are expected to fail until PR2 —
matching this repo's occasional practice of landing a red test first
when a fix is more than one line (or, if the team prefers not to land
known-red tests, PR1 and PR2 can be squashed into one PR — a call for
whoever implements this, not fixed here).

**Security invariants:** none introduced — test-only change.

**Rollback:** delete the new test functions; no other coupling.

**Out of scope:** the fix itself (PR2).

### PR2 — Minimal `TokenStore`/`register_hashed_token` fix

**Files:**
- Modify: `examples/mcp_server/oauth_provider.py` —
  `register_hashed_token()` gains an `expires_at: float | None = None`
  (or equivalent) parameter, applied to the constructed `StoredToken`
  instead of the current hardcoded `float("inf")`, defaulting to
  `float("inf")` when not provided (preserving every existing call
  site's behavior, including `register_static_token()`'s own
  unaffected `float("inf")` path, which is correct as-is and out of
  scope). `load_tokens()` converts `entry.expires_at` (the store's
  `str | None`) to the numeric form and passes it through.
- Modify: `examples/mcp_server/token_store.py` only if the
  string→epoch conversion is judged to belong there (e.g. a small
  `StoredTokenEntry.expires_at_epoch() -> float` helper) rather than
  inline in `oauth_provider.py` — a design call for whoever implements
  this, not fixed here; either location keeps the change minimal.

**Tests to add/run:** the PR1 tests (now passing), plus test 3, 5, 6
from §3, plus the full existing `tests/test_oauth_provider.py` +
`tests/test_server_token_integration.py` + `tests/test_token_store.py`
suites unmodified-and-green.

**Security invariants:**
- No raw token ever appears in the store file or logs (already true;
  regression-guarded by test 5).
- Revocation behavior unchanged (regression-guarded by test 3).
- No change to in-memory-only OAuth-issued token behavior
  (regression-guarded by test 6).
- Malformed/unparseable persisted `expires_at` values must fail
  closed — treat as "already expired" (reject) rather than "never
  expires" (accept), since silently granting infinite trust on a
  parse error is the wrong failure direction for a security-relevant
  field.

**Rollback:** revert the two-function change; no schema/data
migration to undo, since the on-disk format does not change.

**Out of scope:** any change to how tokens are *created* (no new
`--ttl` flag, no OAuth-token persistence) — this PR only fixes what
happens to an `expires_at` value that already exists on a persisted
entry, however that value got there.

### PR3 — Docs/changelog/release

**Files:**
- `CHANGELOG.md` — one entry describing the fix as a correctness bug
  fix (persisted-token expiry now enforced across restart), explicitly
  noting: (a) this is not a public-connector change, (b) no currently-shipped
  code path was actually setting a real expiry before this
  fix (per §1), so no currently-issued token's behavior changes as a
  result — this fix hardens the reload path for any future feature
  that does set one.
- `docs/superpowers/specs/2026-07-27-public-mcp-oauth-decision.md` —
  update §3's "Persisted token expiry is not enforced on load" bullet
  to reflect that this specific gap is now fixed (once PR2 has
  actually landed — this update happens in PR3, not before).
- `pyproject.toml` / `app/version.py` — version bump, following this
  repo's existing release-pack pattern exactly (see the
  `v0.1.5Xa0`/`v0.1.6Xa0` series of release commits for the exact
  format).

**Tests to add/run:** full validation matrix per §6 below; no new
tests in this PR (PR1/PR2 already cover the fix).

**Security invariants:** none new — this PR is docs/version metadata
only.

**Rollback:** revert the version/changelog/decision-doc-update
commit; no functional coupling, since PR2 already shipped
independently.

**Out of scope:** tagging, deploying — matching this repo's
established gate → release-pack → tag+deploy three-step pattern, never
combined without explicit instruction.

## 6. Validation commands (for whoever implements PR1/PR2/PR3)

```bash
ruff check .
python3 -m mypy examples/mcp_server/oauth_provider.py examples/mcp_server/token_store.py
pytest tests/test_oauth_provider.py tests/test_server_token_integration.py tests/test_token_store.py tests/test_mcp_token_cli.py -q
pytest -m "not host_smoke"
python3 scripts/check_public_hygiene.py
python3 scripts/check_no_hardcoded_secrets.py
```

## 7. Sequencing and dependencies

PR1 → PR2 → PR3, strictly sequential. PR2 must not be opened before
PR1's tests exist and are confirmed to fail against unfixed code (or,
if squashed, the combined PR's tests must be shown failing against a
pre-fix checkout before the fix commit, in the PR description). PR3
only after PR2 is merged and green. No PR in this plan is
pre-authorized by this document — each still needs its own explicit
"go" from the user, matching every other phase in this repo's history.

## 8. Acceptance criteria for "this bugfix done"

- A persisted token with a past `expires_at` does not authorize after
  a simulated process restart (fresh `GatewayOAuthProvider` +
  `load_tokens()`).
- A persisted token with a future `expires_at` authorizes until that
  time, then stops, without requiring a second restart to take effect.
- A revoked persisted token remains rejected after reload — unchanged
  from today.
- A persisted token with no `expires_at` (every entry created by any
  code path that exists in this repo today) continues to behave as
  "never expires" — unchanged from today.
- No raw token value appears in the store file or in any log line, at
  any point in this fix.
- `tests/test_oauth_provider.py`'s DCR/PKCE/authorization-code-flow
  tests pass unmodified.
