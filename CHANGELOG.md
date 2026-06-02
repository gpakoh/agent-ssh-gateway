# Changelog

All notable changes to this project will be documented in this file.

This project follows semantic versioning where practical, but the public API is not considered stable before v1.0.0.

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
