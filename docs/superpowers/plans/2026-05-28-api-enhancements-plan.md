# API Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development

**Goal:** Add 6 new API capabilities: port checker, session info, env inspect, config, SSH key upload, command templates

**Architecture:** New endpoints in existing routers (ssh.py, system.py) + one new router (templates.py). Each endpoint is self-contained — no shared state beyond existing SSH session manager.

**Tech Stack:** FastAPI, asyncio, Paramiko (SSH), Pydantic models

---

## File Structure

| File | Change |
|------|--------|
| `app/routers/ssh.py` | +port checker, +session info (extend), +env inspect, +SSH key upload |
| `app/routers/system.py` | +config endpoint |
| `app/routers/templates.py` | New file — command templates list + run |
| `app/models.py` | +SessionInfo, +SessionInfoResponse, +TemplateInfo, +TemplateRunRequest, +TemplateRunResponse |
| `app/main.py` | +templates router import/include, +templates tag in TAGS_META, +/api/logs in _path_tag, +/api/templates in _path_tag |
| `tests/` | Tests per feature |

---

### Task 1: Models

**File:** Modify `app/models.py`

Add models after existing session models (~line 72):

```python
class SessionInfo(BaseModel):
    session_id: str
    host: str
    port: int
    username: str
    connected_at: str
    last_command_at: str | None = None
    idle_seconds: float = 0.0


class SessionInfoResponse(BaseModel):
    sessions: list[SessionInfo]
    count: int


class TemplateInfo(BaseModel):
    id: str
    name: str
    description: str
    command: str
    params: list[dict] = []


class TemplateRunRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    template: str = Field(..., min_length=1)
    params: dict[str, str] = {}


class TemplateRunResponse(BaseModel):
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    duration: float = 0.0
```

---

### Task 2: Port Checker

**File:** Modify `app/routers/ssh.py` — add new endpoint + import `asyncio`

```python
@router.get("/api/ssh/check-port")
async def check_port(
    host: str = Query(..., description="Target hostname or IP"),
    port: int = Query(22, ge=1, le=65535, description="Target port"),
    timeout: float = Query(5.0, ge=0.5, le=30.0, description="Connection timeout in seconds"),
):
    """Check if a remote TCP port is reachable. No auth required."""
    start = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        elapsed = int((time.monotonic() - start) * 1000)
        return {"host": host, "port": port, "reachable": True, "duration_ms": elapsed}
    except (OSError, asyncio.TimeoutError, ConnectionError):
        elapsed = int((time.monotonic() - start) * 1000)
        return {"host": host, "port": port, "reachable": False, "duration_ms": elapsed}
```

Also add `import asyncio` at top of file (check if already there).

---

### Task 3: Session Info (extend existing)

**File:** Modify `app/routers/ssh.py` — replace existing `/api/ssh/sessions` endpoint

Find current endpoint (returns `{"sessions": list(manager._sessions.keys())}`) and replace:

```python
@router.get("/api/ssh/sessions", response_model=SessionInfoResponse)
async def list_sessions():
    """List all active SSH sessions with details."""
    sessions = []
    now = time.monotonic()
    for sid, rec in list(_state.manager._sessions.items()):
        connected = datetime.fromtimestamp(rec.created_at).isoformat() if rec.created_at else ""
        last_cmd = datetime.fromtimestamp(rec.last_activity).isoformat() if rec.last_activity else None
        idle = now - rec.last_activity if rec.last_activity else 0.0
        sessions.append(SessionInfo(
            session_id=sid,
            host=getattr(rec, "host", ""),
            port=getattr(rec, "port", 22),
            username=getattr(rec, "username", ""),
            connected_at=connected,
            last_command_at=last_cmd,
            idle_seconds=round(idle, 1),
        ))
    return SessionInfoResponse(sessions=sessions, count=len(sessions))
```

Add imports: `from datetime import datetime` at top.

---

### Task 4: Env Inspect

**File:** Modify `app/routers/ssh.py` — add new endpoint

```python
@router.get("/api/ssh/session/{session_id}/env")
async def session_env(
    session_id: str,
    prefix: str = Query(None, description="Filter env vars by prefix (e.g. PATH)"),
):
    """Read environment variables from an active SSH session."""
    result = await _state.manager.execute(session_id=session_id, command="printenv", timeout=10)
    if result["exit_code"] != 0:
        raise HTTPException(status_code=502, detail=_err(502, f"Failed to read env: {result['stderr']}"))

    env = {}
    for line in result["stdout"].splitlines():
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        if prefix and not key.startswith(prefix):
            continue
        env[key] = val
    return env
```

---

### Task 5: Config Endpoint

**File:** Modify `app/routers/system.py` — add new endpoint

```python
@router.get("/api/config")
async def get_config():
    """Return runtime configuration (secrets masked)."""
    from app.config import settings
    return {
        "session_timeout": settings.session_timeout,
        "cleanup_interval": settings.cleanup_interval,
        "ssh_default_timeout": settings.ssh_default_timeout,
        "max_sessions_per_ip": settings.max_sessions_per_ip,
        "rate_limit_requests": settings.rate_limit_requests,
        "rate_limit_window": settings.rate_limit_window,
        "persistent_sessions_enabled": settings.persistent_sessions_enabled,
        "known_hosts_store": settings.known_hosts_store or "null",
        "api_auth_enabled": settings.api_auth_enabled,
        "agent_token_enabled": bool(settings.agent_token),
        "agent_token_ttl": settings.agent_token_ttl,
        "read_only": settings.read_only if hasattr(settings, "read_only") else False,
    }
```

---

### Task 6: SSH Key Upload

**File:** Modify `app/routers/ssh.py` — add new endpoint

```python
@router.post("/api/ssh/keys")
async def upload_ssh_key(
    file: UploadFile = File(...),
):
    """Upload an SSH private key. Stored in /app/ssh_keys/."""
    content = await file.read()
    if len(content) > 64 * 1024:
        raise HTTPException(status_code=400, detail=_err(400, "Key file too large (max 64KB)"))
    try:
        text = content.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail=_err(400, "Key must be valid UTF-8 text"))

    if not text.startswith("-----BEGIN"):
        raise HTTPException(status_code=400, detail=_err(400, "Not a valid private key format"))

    keys_dir = "/app/ssh_keys"
    os.makedirs(keys_dir, exist_ok=True)
    name = file.filename or f"key-{uuid.uuid4().hex[:8]}.pem"
    fpath = os.path.join(keys_dir, name)

    with open(fpath, "w") as f:
        f.write(text)
    os.chmod(fpath, 0o600)

    return {"name": name, "path": fpath, "size": len(content)}
```

Add imports: `from fastapi import UploadFile, File`, `import uuid`, `import os`.

---

### Task 7: Command Templates Router

**File:** Create `app/routers/templates.py`

```python
"""Predefined command templates for SSH execution."""

import logging
from fastapi import APIRouter, HTTPException
from app import state as _state
from app.state import _err
from app.models import TemplateInfo, TemplateRunRequest, TemplateRunResponse
from app.security import sanitize_command

logger = logging.getLogger(__name__)
router = APIRouter()

TEMPLATES: list[TemplateInfo] = [
    TemplateInfo(id="deploy", name="Deploy service", description="Restart and check service status", command="systemctl restart {service} && systemctl status {service}", params=[{"name": "service", "type": "string", "required": True}]),
    TemplateInfo(id="healthcheck", name="Service health", description="Check if service is active", command="systemctl is-active --quiet {service} && echo 'active' || echo 'inactive'", params=[{"name": "service", "type": "string", "required": True}]),
    TemplateInfo(id="disk-usage", name="Disk usage", description="Show disk usage for a path", command="df -h {path}", params=[{"name": "path", "type": "string", "required": False}]),
    TemplateInfo(id="memory", name="Memory status", description="Show free memory", command="free -h", params=[]),
    TemplateInfo(id="docker-ps", name="Docker processes", description="List running containers", command="docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'", params=[]),
    TemplateInfo(id="docker-stats", name="Docker stats", description="Live container resource usage", command="docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'", params=[]),
    TemplateInfo(id="nginx-reload", name="Reload nginx", description="Test config and reload nginx", command="nginx -t && systemctl reload nginx", params=[]),
    TemplateInfo(id="uptime", name="System uptime", description="Show system uptime and load", command="uptime", params=[]),
    TemplateInfo(id="journal", name="Journal logs", description="Recent system logs (shortcut)", command="journalctl -n 30 --no-pager", params=[]),
]


@router.get("/api/templates", response_model=list[TemplateInfo])
async def list_templates():
    """List all predefined command templates."""
    return TEMPLATES


@router.post("/api/templates/run", response_model=TemplateRunResponse)
async def run_template(req: TemplateRunRequest):
    """Execute a command template with parameter substitution."""
    template = next((t for t in TEMPLATES if t.id == req.template), None)
    if not template:
        raise HTTPException(status_code=404, detail=_err(404, f"Template not found: {req.template}"))

    command = template.command
    for key, val in req.params.items():
        command = command.replace(f"{{{key}}}", val)

    try:
        command = sanitize_command(command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc)))

    result = await _state.manager.execute(session_id=req.session_id, command=command, timeout=30)
    return TemplateRunResponse(**result)
```

---

### Task 8: Wire Templates Router in main.py

**File:** Modify `app/main.py`

Add import after existing router imports:
```python
from app.routers.templates import router as templates_router
```

Add include after existing ones:
```python
app.include_router(templates_router)
```

Add to TAGS_META:
```python
"templates": "Predefined command templates",
```

Add to _path_tag (before the `if path.startswith("/api/templates"):` that was there before — actually need to check order):
The `if path.startswith("/api/scaffold"):` already returns "templates" for scaffold. Add also:
```python
if path.startswith("/api/templates"):
    return "templates"
```

---

### Task 9: Tests

**File:** `tests/test_port_checker.py` — test reachable/unreachable
**File:** `tests/test_session_info.py` — test list with mock sessions
**File:** `tests/test_env_inspect.py` — test env parsing
**File:** `tests/test_config.py` — test secrets masked
**File:** `tests/test_ssh_keys.py` — test upload validation
**File:** `tests/test_templates.py` — test list and run

---

### Task 10: Build, Deploy, Push

```bash
cd /media/1TB/Python/web_ssh/web-ssh-gateway
API_KEY=AFdvw9WxVNT5PTEle8PLGzDUUC8V5eA1yAeYOmrurPc docker compose -p web-ssh-gateway -f docker/docker-compose.yml build --no-cache web-ssh-gateway
API_KEY=AFdvw9WxVNT5PTEle8PLGzDUUC8V5eA1yAeYOmrurPc docker compose -p web-ssh-gateway -f docker/docker-compose.yml up -d --no-deps --force-recreate web-ssh-gateway
git add -A && git commit -m "feat: add port checker, session info, env inspect, config, ssh keys, command templates"
git push github master && git push gitea master
```
