# Docker Compose build: read-only filesystem diagnostics

This document covers the `docker_compose_build` operation in the SSH Gateway
MCP Docker adapter. It does not authorize a new Docker write, daemon restart,
cache pruning, permission change, or privileged mount. The existing Compose
operation still requires its normal approval and allowlisted project root.

## Safe diagnostic contract

A confirmed Compose build that fails with an **explicit read-only filesystem
write error** returns `DOCKER_COMMAND_FAILED` with
`failure_class=read_only_fs` and one conservative `write_surface`. The
adapter never returns the raw failing Docker stderr, source path or environment
variables in this case. Only a path associated with an explicit failed write
in the same bounded diagnostic line can select a surface; unrelated build
output does not. Multiple conflicting failures remain `unclassified`.
Only a failure at the beginning of a line, or an explicit
`ERROR: failed to solve:` / `Error response from daemon:` prefix, can select
a surface. Recognized prefixed direct syscall errors (such as
`ERROR: failed to solve: mkdir /var/lib/buildkit/cache: read-only file system`)
are classified without echoing the path. Dockerfile `RUN echo` or unrelated
progress output containing error-like text is **not** authoritative evidence.

| `write_surface` | Evidence required | Permitted follow-up |
| --- | --- | --- |
| `client_buildx_home` | Write failure under `~/.docker/buildx` or managed runner `/tmp/mcp-buildx` | Use the already configured digest-pinned managed Compose runner with writable tmpfs Buildx state |
| `builder_cache` | Write failure under `~/.cache/buildx` or `~/.cache/buildkit` | Review the approved builder cache configuration, then provide only a scoped writable cache |
| `builder_state` | Write failure under `/var/lib/buildkit` or `/var/lib/docker/buildkit` | Ask the builder operator to inspect its dedicated state volume and mount |
| `daemon_namespace` | Write failure under recognized Docker daemon state roots | Ask the Docker daemon operator to verify its configured writable data-root |
| `compose_context` | Write failure within the **known allowlisted** Compose project root | Keep the source bind read-only; redirect build-generated output to an approved writable location |
| `unclassified` | Unknown, unsupported or conflicting evidence | Inspect approved mount configuration; never guess a writable host path |

Unknown daemon data-root locations, misleading text that is not a write error,
paths outside the allowlisted Compose project, and path traversal are not
assumed to be any known surface. This avoids unsafe automatic remediation.

## Already sanctioned isolated Buildx state

When an operator configures `MCP_COMPOSE_RUNNER_IMAGE` with an immutable
digest and the requested project root is approved, the existing
`DockerClient._compose_base_argv` launches the pinned Compose runner with:

- a read-only root filesystem and read-only project bind;
- a separate writable `/tmp` tmpfs (64 MiB);
- `BUILDX_CONFIG=/tmp/mcp-buildx`;
- a **read-only** Docker registry-auth volume, when configured;
- restricted capabilities, no-new-privileges, and no network.

This is an existing, scoped writable *client-side Buildx state*, not blanket
write permission to the Docker daemon's cache or data-root. Never change a
registry credential volume from read-only to writable just to fix a Buildx
failure. If 64 MiB is insufficient or the daemon's own storage is read-only,
this client tmpfs alone is not a remedy: review the operator-approved runner
and daemon contracts before making changes.

## Acceptance / recovery boundary

Run the Docker adapter and confirmation regression tests in an isolated
candidate. Qualify the actual operational error only against an authorized
staging/production Compose project with an already approved Docker build
confirmation; do not intentionally make the production daemon or source
read-only to manufacture a smoke test. Record the sanitized surface and the
actual immutable release SHA in the relevant Gitea Issue. If the surface is
`unclassified`, do not alter filesystem permissions or attempt a blind
rerun. This diagnostic improvement does not resolve every Docker storage
failure or replace dedicated operator recovery procedures.
