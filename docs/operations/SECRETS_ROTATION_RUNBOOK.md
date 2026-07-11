# Secrets Rotation Runbook

## Runtime env file location

| Variable | File | Source |
|----------|------|--------|
| `POSTGRES_PASSWORD` | `docker/.env` | `docker-compose.yml` via `${POSTGRES_PASSWORD:?...}` |
| `API_KEY` | `docker/.env` | `docker-compose.yml` via `${API_KEY:?...}` |
| `AGENT_TOKEN` | `docker/.env` | `docker-compose.yml` via `${AGENT_TOKEN:?...}` |
| `JWT_SECRET` | `docker/.env` | `docker-compose.yml` via `${JWT_SECRET:?...}` |

`docker/.env` is gitignored. All tracked compose files use `${VAR:?...}` syntax.

## Generating a new password

```bash
# Generate a strong random password (32+ bytes, base64)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Rotation procedure

1. Generate new value

```bash
NEW_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
```

2. Update runtime env without echoing the secret

```bash
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=${NEW_PASS}/" docker/.env
```

Or edit manually with a text editor that does not persist to shell history.

3. Verify the file was updated

```bash
grep -n '^POSTGRES_PASSWORD=' docker/.env
```

4. Restart the postgres service

```bash
docker compose -p web-ssh-gateway -f docker/docker-compose.yml up -d mcp-postgres --force-recreate
```

5. Verify the service is healthy

```bash
docker compose -p web-ssh-gateway -f docker/docker-compose.yml ps mcp-postgres
docker compose -p web-ssh-gateway -f docker/docker-compose.yml exec mcp-postgres pg_isready -U postgres
```

6. Verify old password no longer works

```bash
# Should fail with authentication error
PGPASSWORD=wrongpass docker compose -p web-ssh-gateway -f docker/docker-compose.yml exec -T mcp-postgres \
  psql -U postgres -c "SELECT 1"
```

7. Restart dependent services

```bash
docker compose -p web-ssh-gateway -f docker/docker-compose.yml up -d --no-deps \
  $(docker compose -p web-ssh-gateway -f docker/docker-compose.yml config --services | grep -v mcp-postgres)
```

## What NOT to do

- Do not write the secret to stdout or log files
- Do not commit the secret to any git branch (including feature branches)
- Do not paste the secret in chat, issue comments, or PR descriptions
- Do not share the secret between environments (dev/staging/prod must use different values)
- Do not skip verifying the old password stops working

## Verification checklist

- [ ] `docker/.env` contains the new password (in gitignored file only)
- [ ] `docker/docker-compose.yml` uses `${POSTGRES_PASSWORD:?...}` (no literal)
- [ ] `docker/.env.example` contains `POSTGRES_PASSWORD=change-me` (placeholder only)
- [ ] `python scripts/check_no_hardcoded_secrets.py` exits 0
- [ ] Postgres container shows `(healthy)` status
- [ ] Old password is rejected
