# Changelog

## v0.2.1 - Quality baseline release

### Added

- Full `mypy app` gate with zero errors across 48 source files.
- CI/CD uses `mypy app --show-error-codes` as blocking step.

### Changed

- Type-annotated all remaining modules: `routers/system.py`, `routers/context.py`, `routers/git.py`, `search_replace.py`, `routers/files.py`, `template_library.py`, `ast_refactor.py`, `main.py`.
- `ssh_manager.py` `execute()` now returns `CommandResult` TypedDict instead of generic `dict[str, object]`.
- CI deploy health check bypasses HTTP proxy (`--noproxy '*'`).
- Runner `config.yaml` `NO_PROXY` expanded to cover Docker internal subnets.

### Fixed

- SQLAlchemy async session typing for mypy compatibility.
- `ast.FunctionDef` version-conditional `type_params` for Python 3.12.
- `redis.eval()` stub mismatch between redis-py 5.0 and 8.0.
- Deploy-backend health check failure caused by Squid proxy interception.

## v0.2.0 - Security hardening release

### Added

- Target host allowlist/denylist to prevent SSRF-style usage.
- Master-only agent token management.
- Scoped agent tokens.
- Session ownership checks.
- Command policy engine with `off`, `audit`, and `enforce` modes.
- Secret redaction for audit logs and event hook payloads.
- Credential hygiene with Pydantic `SecretStr`.
- Route authentication contract test.
- SSH key upload disabled by default.
- Event hook management protected by master auth.
- Distributed lock test compatibility with pytest 9.

### Changed

- SSH credentials are no longer stored in session metadata.
- Reconnect no longer attempts to reuse stored credentials.
- `.env.example` now documents all config aliases.
- CI/CD deploy flow uses Docker socket mode for trusted staging runner.

### Fixed

- Event hook tests updated for authenticated routes.
- Distributed lock async fixture fixed for pytest 9 compatibility.
- OpenAPI/runtime tests stabilized.
- Cross-test state pollution reduced.
