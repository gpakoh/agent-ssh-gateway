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
- Set `MCP_OAUTH_GITEA_API_BASE=http://gitea:3000/api/v1` in `docker/.env` for the
  compose deployment shown in `docker/docker-compose.yml`.
- Do not point `MCP_OAUTH_GITEA_API_BASE` at `http://127.0.0.1:3005/...` inside the
  `mcp-oauth` container: container loopback is not the host's Gitea listener and
  live `gitea_*` MCP tools will fail with remote-unavailable errors.
- Keep `GITEA_FORWARDED_HOST` / `GITEA_FORWARDED_PROTO` aligned with the public Gitea
  origin when the client is expected to emit public-facing PR URLs.

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
