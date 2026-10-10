# Deployment Overlays

How the gateway compose system separates public generic config from live private config.

## Architecture

```
docker/docker-compose.yml          ← tracked, generic, public
docker/docker-compose.live.yml     ← gitignored, live-specific, private
docker/docker-compose.notifier.yml ← tracked, opt-in overlay
docker/.env                        ← gitignored, live secrets + paths
docker/.env.example                ← tracked, placeholder template
```

## What Goes Where

### Tracked (public repo)

| File | Purpose |
|------|---------|
| `docker/docker-compose.yml` | Generic compose: services, health checks, security hardening. Uses env var placeholders for host-specific values. |
| `docker/docker-compose.notifier.yml` | Optional notifier sidecar overlay. Dry-run by default. |
| `docker/.env.example` | Template showing required env vars with placeholder values. |
| `docker/docker-compose.live.example.yml` | Template showing live overlay structure (no real IPs/paths). |

### Gitignored (private, local only)

| File | Purpose |
|------|---------|
| `docker/docker-compose.live.yml` | Live overlay: macvlan network, real workspace mount, static IP. |
| `docker/.env` | Live secrets (API_KEY, JWT_SECRET, etc.) + workspace paths. |

## Deploy Commands

### Generic (no live overlay)

```bash
docker compose -f docker/docker-compose.yml up -d --build
```

### With live overlay (real deployment)

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.live.yml up -d --build
```

### With notifier overlay

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.notifier.yml up -d
```

## Preflight Check

```bash
python3 scripts/compose_live_preflight.py
```

Verifies:
- `docker/docker-compose.live.yml` exists locally and is gitignored
- `docker/.env` exists locally and is gitignored
- Main compose is generic (no hardcoded host IPs/paths)
- Rendered compose has readonly mounts where expected

## Live MCP OAuth Notes

- `mcp-oauth` must use an internal Gitea API base, not the host loopback URL.
- Set `GITEA_API_BASE=http://gitea:3000/api/v1` in `docker/.env` for the
  compose deployment shown in `docker/docker-compose.yml`.
- Do not point `GITEA_API_BASE` at `http://127.0.0.1:3005/...` inside the
  `mcp-oauth` container: container loopback is not the host's Gitea listener and
  live `gitea_*` MCP tools will fail with remote-unavailable errors.
- Keep `GITEA_FORWARDED_HOST` / `GITEA_FORWARDED_PROTO` aligned with the public Gitea
  origin when the client is expected to emit public-facing PR URLs.
- If registered project Git remotes use an additional internal Gitea hostname or IP,
  set `GITEA_TRUSTED_REMOTE_HOSTS` to a comma/space-separated allowlist of those hosts.
  The control plane uses this only to recognize the repository identity; authenticated
  fetches are re-resolved through the Gitea API and do not reuse checkout credentials.

## Adding a New Private Value

1. Add placeholder to `docker/.env.example` with descriptive comment
2. Add env var reference to `docker/docker-compose.yml` using `${VAR:-default}` syntax
3. Set real value in `docker/.env` (gitignored)
4. Run `python3 scripts/compose_live_preflight.py` to verify

For required secrets referenced via `${VAR:?message}` in compose, deploys will fail at
compose-interpolation time if the key is absent from `docker/.env`.

## Common Mistakes

| Mistake | Why it's bad | Fix |
|---------|-------------|-----|
| Putting real IPs in `docker-compose.yml` | Leaks infra to public repo | Move to `.env` + overlay |
| Committing `docker/.env` | Secrets in git history | Add to `.gitignore`, rotate secrets |
| Committing `docker-compose.live.yml` | Leaks network topology | Add to `.gitignore` |
| Using `docker compose -e` | Not supported, silent failure | Use `.env` file or env vars |

## Compose access to root-owned deployment secrets

The admin OAuth service can set `MCP_COMPOSE_RUNNER_IMAGE` to an exact
`repository@sha256:digest` MCP server image. Registry deployment passes the same
verified MCP image digest to this setting, including rollback.

For an explicit allowed project directory, Compose then runs in a short-lived
helper as UID 0 with no capabilities, no network, a read-only root filesystem,
a bounded tmpfs and only that project directory mounted read-only at its host
path plus the existing Docker socket. This allows Compose to read a root-owned
0600 `.env` without changing ownership or permissions or returning its contents.
The daemon still creates service volumes at their original host paths.

Existing admin scope checks, pending-action confirmation, service binding,
operation receipts and output redaction still apply. No helper is used for an
implicit project directory or when the setting is empty. Mutable image tags are
rejected. Compose builds that need sources outside the selected project need a
separately prepared image; the helper does not mount a parent workspace.

For private registries, the production deploy job owns an operator Docker
config volume named `mcp-compose-registry-auth`. After its normal registry
login, CI resolves the exact just-published MCP image to a registry digest,
uses that immutable image only as a Docker CLI helper, and rotates the volume
with `docker login --password-stdin` from the shared
`infra-quart/secrets/registry.token` file (mounted read-only into the deploy
job). The deploy job exports only
`MCP_COMPOSE_DOCKER_CONFIG_VOLUME=mcp-compose-registry-auth` to Compose, so the
recreated `mcp-oauth` receives the non-secret selector while the registry token
never becomes an MCP tool argument, Compose variable, tracked file, or
container environment value.

The pinned Compose helper later mounts that volume read-only at
`/run/mcp-docker-config` and sets `DOCKER_CONFIG` to that directory. The volume
must contain Docker client `config.json`; it is deliberately not mounted into
`mcp-oauth` itself, only into the short-lived pinned Compose helper when a
validated project directory is used.

For a manual/non-CI deployment, provision or rotate the same operator-owned
volume outside ChatGPT/MCP using an approved digest-pinned Docker CLI image and
`docker login --password-stdin`, reading the token from the shared
`infra-quart/secrets/registry.token` file, then set the selector in the private
`docker/.env`, for example:

```bash
docker volume create mcp-compose-registry-auth
printf '%s' "$REGISTRY_TOKEN" | docker run --rm -i \
  --user 0:0 \
  -e DOCKER_CONFIG=/docker-config \
  -v mcp-compose-registry-auth:/docker-config \
  --entrypoint /usr/bin/docker "$MCP_COMPOSE_RUNNER_IMAGE" \
  login "$REGISTRY" --username "$REGISTRY_USER" --password-stdin
```

Then set `MCP_COMPOSE_DOCKER_CONFIG_VOLUME=mcp-compose-registry-auth` in
`docker/.env` and recreate `mcp-oauth`.

Candidate preparation scans only deterministic ids that can belong to the
requested project and branch. Missing or unsafe metadata in that lineage still
fails closed with `CANDIDATE_LINEAGE_SCAN_FAILED`, the candidate id and a repair
action; unrelated abandoned clones no longer block all registered projects.
