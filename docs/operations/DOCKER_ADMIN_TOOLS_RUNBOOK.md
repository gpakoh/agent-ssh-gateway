# Docker Admin Tools Runbook

Practical operations for `mcp:docker:admin` tools — exec, run, rmi, volume_rm, and admin-expanded prune/compose_down.

**See also**: [`MCP_FLEET_RUNBOOK.md`](./MCP_FLEET_RUNBOOK.md) — adapter reference,
service management, nginx, iptables.
[`MCP_OPERATOR_RUNBOOK.md`](./MCP_OPERATOR_RUNBOOK.md) — day-to-day MCP operations.

---

## 1. Scope model

Two Docker-related scopes — flat, no inheritance:

| Scope | Grants | Typical tools |
|-------|--------|---------------|
| `mcp:docker` | Read + write on disposable containers | `docker_ps`, `docker_logs`, `docker_start`, `docker_rm`, `docker_prune` (container/image/network), `docker_compose_down` (no volumes) |
| `mcp:docker:admin` | Dangerous operations | `docker_exec`, `docker_run`, `docker_rmi`, `docker_volume_rm`, `docker_prune` (volume/system), `docker_compose_down --volumes` |

A token with `mcp:docker:admin` CANNOT call `mcp:docker` tools unless it ALSO has `mcp:docker` explicitly. Both must be listed.

### Profiles

| Profile | `mcp:docker` | `mcp:docker:admin` |
|---------|-------------|-------------------|
| `infra` | ✅ | ✅ |
| `full` | ✅ | ✅ |
| `viewer` | — | — |
| `operator` | — | — |
| `agent-runner` | — | — |

---

## 2. Confirmation workflow

All admin tools require a two-step confirmation:

```
Step 1: Call the tool -> returns confirmation_required with a confirm_token
Step 2: Call docker_confirm with that token -> executes the operation
```

- Token: 128-bit random, `secrets.token_urlsafe(16)`
- TTL: 60 seconds
- Single-use: consumed before execution, cannot replay
- Comparison: `hmac.compare_digest` (timing-safe)

```text
Caller:  docker_run(image="alpine:3.20", command=["echo", "ok"])
Server:  {ok: false, confirmation_required: true, confirm_token: "abc...", summary: "Run alpine:3.20: echo ok", ttl_seconds: 60}

Caller:  docker_confirm(confirm_token="abc...")
Server:  {ok: true, stdout: "ok\n", exit_code: 0}
```

---

## 3. `docker_exec` — command execution in existing container

### Positive smoke

```text
1. Create disposable container (outside MCP):
   docker run -d --name mcp-admin-smoke-exec alpine:3.20 sleep 300

2. Call via MCP:
   docker_exec(container="mcp-admin-smoke-exec", command=["echo", "ok"])
   → confirmation_required

3. Confirm:
   docker_confirm(token="...")
   → stdout: "ok\n", exit_code: 0

4. Cleanup:
   docker_rm(container="mcp-admin-smoke-exec", force=true)
   → confirmation_required → docker_confirm → container removed
```

### Denylist

Checked **before** confirmation token creation. The following are blocked:

| Pattern | Example | Error code |
|---------|---------|-----------|
| Environment dump | `env`, `printenv` | `DOCKER_EXEC_COMMAND_BLOCKED` |
| Shadow files | `cat /etc/shadow`, `cat /etc/gshadow` | `DOCKER_EXEC_COMMAND_BLOCKED` |
| Process environ | `cat /proc/*/environ` | `DOCKER_EXEC_COMMAND_BLOCKED` |
| SSH keys | `cat <ssh-key-path>`, `ls /.ssh/` | `DOCKER_EXEC_COMMAND_BLOCKED` |
| Shell launcher | `sh -c "whoami"`, `bash -c "ls"`, `ash -c "id"` | `DOCKER_EXEC_COMMAND_BLOCKED` |
| Empty argv | `[]` | `INVALID_INPUT` |
| Non-printable chars | binary/control characters in argv | `INVALID_INPUT` |
| Invalid container name | special chars in container name | `INVALID_INPUT` |

```text
docker_exec(container="mcp-admin-smoke-exec", command=["env"])
→ ok=false
→ error.code = "DOCKER_EXEC_COMMAND_BLOCKED"
→ No confirmation token created
→ No container operation performed
```

### Safety notes

- The denylist is a **guardrail**, not a security boundary
- `docker_exec` requires both `mcp:docker:admin` scope AND confirmation
- Container name is validated against `^[a-zA-Z0-9][a-zA-Z0-9_.-]*$`
- Timeout: 5-300 seconds (default 30)
- Docker daemon may return `DOCKER_EXEC_CONTAINER_NOT_FOUND` if container does not exist

---

## 4. `docker_run` — create and run a disposable container

### Fail-closed behavior

`docker_run` is **disabled by default**. The `MCP_DOCKER_RUN_ALLOWED_IMAGES` environment variable must be set:

```bash
export MCP_DOCKER_RUN_ALLOWED_IMAGES="alpine:3.20,ubuntu:22.04"
```

If the variable is unset or empty:

```text
docker_run(image="alpine:3.20", command=["echo", "ok"])
→ ok=false
→ error.code = "DOCKER_RUN_ALLOWLIST_NOT_CONFIGURED"
→ Hint: Set MCP_DOCKER_RUN_ALLOWED_IMAGES with comma-separated image:tag entries
```

### Positive smoke

```text
1. Ensure MCP_DOCKER_RUN_ALLOWED_IMAGES includes the target image

2. Call:
   docker_run(
     image="alpine:3.20",
     command=["echo", "ok"],
     container_name="mcp-admin-smoke-run"
   )
   → confirmation_required

3. Confirm:
   docker_confirm(token="...")
   → stdout: "ok\n", exit_code: 0

4. Container auto-removed (--rm). Verify:
   docker ps -a --filter name=mcp-admin-smoke-run → (no output)
```

### Safety notes

- Container runs with `--rm` — removed automatically after exit
- Image tag validated against `^[a-z0-9]+([._/-][a-z0-9]+)*(:[a-zA-Z0-9._-]+)?$`
- Container name validated against `^[a-zA-Z0-9][a-zA-Z0-9_.-]*$`
- argv validated against same denylist as `docker_exec`
- Timeout: 5-600 seconds (default 60)
- Optional `container_name` — if omitted, Docker auto-generates

---

## 5. `docker_rmi` — remove one or more images

### Safe usage

Only disposable/test tags. Never remove production images through this tool.

```text
1. Tag a disposable reference:
   docker tag alpine:3.20 mcp-admin-smoke-img:session166

2. Call:
   docker_rmi(images=["mcp-admin-smoke-img:session166"])
   → confirmation_required

3. Confirm:
   docker_confirm(token="...")
   → images removed

4. Verify:
   docker image ls mcp-admin-smoke-img:session166 → (no output)
```

### Constraints

- 1-5 images per call
- Each image ref validated against `^[a-z0-9]+([._/-][a-z0-9]+)*(:[a-zA-Z0-9._-]+)?$`
- Error `DOCKER_RMI_INVALID_REFERENCE` for bad format
- Error `DOCKER_RMI_FAILED` if Docker daemon rejects (e.g., image in use)

---

## 6. `docker_volume_rm` — remove one or more volumes

### Safe usage

Only disposable/test volumes. Never remove production data volumes.

```text
1. Create a disposable volume:
   docker volume create mcp-admin-smoke-vol

2. Call:
   docker_volume_rm(volumes=["mcp-admin-smoke-vol"])
   → confirmation_required

3. Confirm:
   docker_confirm(token="...")
   → volumes removed

4. Verify:
   docker volume ls | grep mcp-admin-smoke-vol → (no output)
```

### Constraints

- 1-5 volumes per call
- Each volume name validated against `^[a-zA-Z0-9][a-zA-Z0-9_.-]*$`
- Error `DOCKER_VOLUME_RM_INVALID_NAME` for bad format

---

## 7. Admin expansion on existing tools

### `docker_prune` — volume and system types

With `mcp:docker:admin` scope, `docker_prune` accepts two additional types:

| Type | Scope required | Description |
|------|---------------|-------------|
| `container` | `mcp:docker` | Remove stopped containers |
| `image` | `mcp:docker` | Remove unused images |
| `network` | `mcp:docker` | Remove unused networks |
| `volume` | `mcp:docker:admin` | Remove unused volumes |
| `system` | `mcp:docker:admin` | Remove all unused resources |

Without admin scope:
```text
docker_prune(type="volume")
→ ok=false
→ error.code = "DOCKER_ADMIN_SCOPE_REQUIRED"
```

### `docker_compose_down` — volumes flag

Without `mcp:docker:admin` scope:
```text
docker_compose_down(volumes=true)
→ ok=false
→ error.code = "DOCKER_ADMIN_SCOPE_REQUIRED"
```

---

## 8. Disposable smoke procedure

Repeatable procedure to verify admin tools are functioning after deploy or config change:

```bash
# 1. Create exec target
docker run -d --name mcp-admin-smoke-exec alpine:3.20 sleep 300

# 2. Create disposable volume
docker volume create mcp-admin-smoke-vol

# 3. Create disposable image tag (if alpine:3.20 is available)
docker tag alpine:3.20 mcp-admin-smoke-img:smoke 2>/dev/null || docker pull alpine:3.20 && docker tag alpine:3.20 mcp-admin-smoke-img:smoke

# 4. Run smoke via MCP (one at a time, confirm each):
#    docker_exec → mcp-admin-smoke-exec → echo ok
#    docker_volume_rm → mcp-admin-smoke-vol
#    docker_rmi → mcp-admin-smoke-img:smoke
#    (docker_run requires MCP_DOCKER_RUN_ALLOWED_IMAGES)

# 5. Cleanup any leftovers
docker rm -f mcp-admin-smoke-exec 2>/dev/null
docker rmi mcp-admin-smoke-img:smoke 2>/dev/null
```

### Operations NOT allowed in normal smoke

- `docker_prune(type="volume")` — only in dedicated admin session
- `docker_prune(type="system")` — only in dedicated admin session
- `docker_compose_down(volumes=true)` — only on disposable compose projects
- `docker_rmi` on production images
- `docker_volume_rm` on production volumes
- `docker_exec` with `--privileged` containers or production containers

---

## 9. Cleanup checklist

After an admin smoke session:

- [ ] `mcp-admin-smoke-exec` container removed
- [ ] `mcp-admin-smoke-run` container removed (auto via --rm)
- [ ] `mcp-admin-smoke-vol` volume removed
- [ ] `mcp-admin-smoke-img:*` tags removed
- [ ] `docker ps -a | grep mcp-admin-smoke` returns nothing
- [ ] `docker volume ls | grep mcp-admin-smoke` returns nothing
- [ ] `docker image ls | grep mcp-admin-smoke` returns nothing

---

## 10. Error code reference

| Code | Tool(s) | Meaning |
|------|---------|---------|
| `DOCKER_ADMIN_SCOPE_REQUIRED` | prune, compose_down | Admin scope needed for type/flag |
| `DOCKER_RUN_ALLOWLIST_NOT_CONFIGURED` | docker_run | MCP_DOCKER_RUN_ALLOWED_IMAGES not set |
| `DOCKER_RUN_IMAGE_NOT_ALLOWED` | docker_run | Image not in allowlist |
| `DOCKER_RUN_IMAGE_INVALID` | docker_run | Image tag format invalid |
| `DOCKER_EXEC_COMMAND_BLOCKED` | docker_exec, docker_run | argv matched denylist |
| `DOCKER_EXEC_CONTAINER_NOT_FOUND` | docker_exec | Container does not exist |
| `DOCKER_RMI_INVALID_REFERENCE` | docker_rmi | Image ref format invalid or 1-5 constraint |
| `DOCKER_RMI_FAILED` | docker_rmi | Docker daemon rejected removal |
| `DOCKER_VOLUME_RM_INVALID_NAME` | docker_volume_rm | Volume name invalid or 1-5 constraint |
| `DOCKER_VOLUME_RM_FAILED` | docker_volume_rm | Docker daemon rejected removal |
| `INVALID_INPUT` | exec, run, rmi, volume_rm | Validation failure |
| `SCOPE_DENIED` | all | Token lacks required scope |
| `CONFIRMATION_REQUIRED` | all admin | One-time token needed |
| `CONFIRMATION_INVALID` | docker_confirm | Token not found/invalid |
| `CONFIRMATION_EXPIRED` | docker_confirm | Token TTL exceeded (60s) |
