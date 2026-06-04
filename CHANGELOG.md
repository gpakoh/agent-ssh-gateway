# Changelog

All notable changes to this project will be documented in this file.

This project follows semantic versioning where practical, but the public API is not considered stable before v1.0.0.

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
