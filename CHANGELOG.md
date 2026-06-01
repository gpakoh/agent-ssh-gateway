# Changelog

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
