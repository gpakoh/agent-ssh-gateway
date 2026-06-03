"""System, server, snapshot, webhook, search, code intelligence, analytics, tree, and batch routes."""

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from app import state as _state
from app.auth_middleware import (
    VALID_AGENT_SCOPES,
    AuthIdentity,
    is_agent_token_valid,
    require_any_auth,
    require_master_key,
)
from app.config import settings
from app.metrics import metrics
from app.models import (
    AddServerRequest,
    BatchExecuteRequest,
    BatchExecuteResponse,
    BatchOperationResultResponse,
    CapabilitiesResponse,
    CodeCompleteRequest,
    CodeCompleteResponse,
    CodeGenerateRequest,
    CodeGenerateResponse,
    CodeInsertRequest,
    CodeInsertResponse,
    CodeInsertSuggestion,
    CodeSearchRequest,
    CodeSearchResponse,
    CodeSearchResultItem,
    CodeStats,
    ConnectServerRequest,
    CreateSnapshotRequest,
    CreateWebhookRequest,
    DependencyStats,
    DeployRequest,
    DeployResponse,
    FileStats,
    FileTreeNode,
    FileTreeRequest,
    FileTreeResponse,
    GitStats,
    GlobalReplaceRequest,
    GlobalReplaceResponse,
    GlobalSearchRequest,
    GlobalSearchResponse,
    HealthResponse,
    KnownHostCheckResponse,
    KnownHostLookupResponse,
    ProjectAnalyticsRequest,
    ProjectAnalyticsResponse,
    ReplaceResultItem,
    RestoreSnapshotRequest,
    SearchMatchItem,
    ServerConnectResponse,
    ServerInfo,
    ServerListResponse,
    SnapshotActionResponse,
    SnapshotInfo,
    SnapshotListResponse,
    TestStats,
    WebhookConfigResponse,
    WebhookListResponse,
)
from app.security import validate_target_host
from app.server_manager import ServerStatus
from app.state import _err
from app.version import APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Health & System
# ---------------------------------------------------------------------------


@router.get("/health", tags=["system"], response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    redis_ok = _state.redis_queue is not None and _state.redis_queue._redis is not None
    pg_ok = _state.session_store is not None
    return HealthResponse(
        status="ok" if redis_ok or not settings.redis_url else "degraded",
        redis=redis_ok,
        postgres=pg_ok,
        # Redis/Postgres are optional integrations for this alpha release,
        # so the service can be ready even when they are not configured.
        ready=True,
    )


@router.get("/api/capabilities", tags=["system"], response_model=CapabilitiesResponse)
async def get_capabilities():
    """Return API capabilities and environment information.

    Unauthenticated — used by agents to discover server settings.
    """
    servers = _state.server_manager.list_servers() if _state.server_manager else []
    server_count = len(servers)
    hint = ""
    if server_count == 0:
        hint = "No servers configured. Create one via POST /api/servers or connect directly with POST /api/ssh/connect"
    return CapabilitiesResponse(
        version=APP_VERSION,
        auth_mode="api_key" if settings.api_auth_enabled else "none",
        session_timeout=settings.session_timeout,
        cleanup_interval=settings.cleanup_interval,
        ssh_default_timeout=settings.ssh_default_timeout,
        max_sessions_per_ip=settings.max_sessions_per_ip,
        rate_limit_requests=settings.rate_limit_requests,
        rate_limit_window=settings.rate_limit_window,
        server_count=server_count,
        agent_token_enabled=bool(await is_agent_token_valid(settings, settings.agent_token, _state.agent_token_store)),
        agent_token_ttl=settings.agent_token_ttl,
        hint=hint,
    )


@router.get("/api/config", tags=["system"])
async def get_config(_identity: AuthIdentity = Depends(require_master_key)):
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
        "read_only": getattr(settings, "read_only", False),
    }


@router.get("/api/help", tags=["help"])
async def api_help(request: Request, _identity: AuthIdentity = Depends(require_any_auth)):
    """API reference: auth requirements, quick-start examples, and all endpoints.

    Accessible with any valid API key (master key or agent token).
    """
    from app.version import APP_VERSION

    openapi = request.app.openapi()
    paths = openapi.get("paths", {})
    schemas = openapi.get("components", {}).get("schemas", {})
    known_tags = {t["name"] for t in (request.app.openapi_tags or [])}

    def _resolve(s: dict) -> dict:
        if "$ref" in s:
            name = s["$ref"].split("/")[-1]
            resolved = schemas.get(name, {})
            if resolved.get("properties"):
                return resolved
        return s

    # -----------------------------------------------------------------------
    # Scope → endpoint mapping (built from require_scope dependencies)
    # -----------------------------------------------------------------------
    scope_routes: dict[str, list[dict]] = {}
    master_only: list[dict] = []
    public_endpoints: list[dict] = []

    for path, methods in paths.items():
        for method, details in methods.items():
            if method == "parameters":
                continue
            tags = details.get("tags", ["other"])
            tag = next((t for t in tags if t in known_tags), tags[0] if tags else "other")
            if path in ("/health", "/api/capabilities"):
                public_endpoints.append({"method": method.upper(), "path": path, "summary": details.get("summary", "")})
                continue
            scope = details.get("x-scope", "")
            if scope:
                scope_routes.setdefault(scope, []).append({
                    "method": method.upper(), "path": path, "tag": tag,
                    "summary": details.get("summary", ""),
                })
            else:
                master_only.append({
                    "method": method.upper(), "path": path, "tag": tag,
                    "summary": details.get("summary", ""),
                })

    # -----------------------------------------------------------------------
    # Quick-start examples
    # -----------------------------------------------------------------------
    examples = [
        {
            "title": "Connect to a remote host",
            "endpoint": "POST /api/ssh/connect",
            "scope": "ssh:connect",
            "curl": '''curl -s -X POST http://localhost:8085/api/ssh/connect \\
  -H "X-API-Key: $API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"host":"192.168.1.100","username":"root","password":"secret","port":22}' ''',
            "response": '{"session_id":"ses_abc123","host":"192.168.1.100","port":22,"username":"root"}',
        },
        {
            "title": "Execute a command",
            "endpoint": "POST /api/ssh/execute",
            "scope": "ssh:execute",
            "curl": '''curl -s -X POST http://localhost:8085/api/ssh/execute \\
  -H "X-API-Key: $API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"session_id":"ses_abc123","command":"ls -la /tmp"}' ''',
            "response": '{"exit_code":0,"stdout":"total 42\\n-rw-r--r-- 1 root root ...","stderr":"","duration_ms":150}',
        },
        {
            "title": "Read a remote file",
            "endpoint": "POST /api/file/read",
            "scope": "ssh:files",
            "curl": '''curl -s -X POST http://localhost:8085/api/file/read \\
  -H "X-API-Key: $API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"session_id":"ses_abc123","path":"/etc/hostname"}' ''',
            "response": '{"path":"/etc/hostname","content":"my-server\\n","size":10,"encoding":"utf-8"}',
        },
        {
            "title": "Edit a remote file",
            "endpoint": "PATCH /api/file/edit",
            "scope": "ssh:files",
            "curl": '''curl -s -X PATCH http://localhost:8085/api/file/edit \\
  -H "X-API-Key: $API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"session_id":"ses_abc123","path":"/tmp/test.txt","content":"hello world"}' ''',
            "response": '{"path":"/tmp/test.txt","size":11,"encoding":"utf-8"}',
        },
        {
            "title": "Run a background job",
            "endpoint": "POST /api/jobs/run",
            "scope": "jobs:run",
            "curl": '''curl -s -X POST http://localhost:8085/api/jobs/run \\
  -H "X-API-Key: $API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"session_id":"ses_abc123","command":"sleep 10 && echo done"}' ''',
            "response": '{"job_id":"job_abc123","status":"queued"}',
        },
        {
            "title": "Save a server for quick reconnect",
            "endpoint": "POST /api/servers",
            "scope": "master_key",
            "curl": '''curl -s -X POST http://localhost:8085/api/servers \\
  -H "X-API-Key: $MASTER_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"id":"prod-web-1","name":"Production Web 1","host":"10.0.0.1","port":22,"username":"deploy","description":"Primary web server"}' ''',
            "response": '{"id":"prod-web-1","name":"Production Web 1","host":"10.0.0.1","port":22,"username":"deploy","description":"Primary web server"}',
        },
        {
            "title": "List saved servers",
            "endpoint": "GET /api/servers",
            "scope": "master_key",
            "curl": '''curl -s http://localhost:8085/api/servers -H "X-API-Key: $MASTER_KEY"''',
            "response": '{"servers":[{"id":"prod-web-1","name":"Production Web 1","host":"10.0.0.1","port":22,"username":"deploy","status":"unknown"}],"count":1}',
        },
    ]

    # -----------------------------------------------------------------------
    # Group all endpoints by tag (existing behaviour)
    # -----------------------------------------------------------------------
    groups: dict[str, list[dict]] = {}
    for path, methods in paths.items():
        for method, details in methods.items():
            if method == "parameters":
                continue
            tags = details.get("tags", ["other"])
            tag = next((t for t in tags if t in known_tags), tags[0] if tags else "other")
            entry = {"method": method.upper(), "path": path, "summary": details.get("summary", "")}
            params = []
            for p in details.get("parameters", []):
                params.append(_clean_param(p.get("name", ""), p.get("in", "query"), p.get("schema", {}), p.get("required", False), p.get("description", "")))
            body = details.get("requestBody", {})
            if body:
                content = body.get("content", {})
                for _, media_body in content.items():
                    schema = _resolve(media_body.get("schema", {}))
                    props = schema.get("properties", {})
                    required_set = set(schema.get("required", []))
                    for pname, pdetails in props.items():
                        params.append(_clean_param(pname, "body", pdetails, pname in required_set, pdetails.get("description", "")))
            if params:
                entry["params"] = params
            groups.setdefault(tag, []).append(entry)

    return {
        "service": "agent-ssh-gateway",
        "version": APP_VERSION,
        "authentication": {
            "header": "X-API-Key",
            "also_accepted": "Authorization: Bearer <key>",
            "types": {
                "master_key": {
                    "description": "Full access to all endpoints. Set via API_KEY env var.",
                    "access": "all endpoints including admin (config, tokens, webhooks, etc.)",
                    "how_to_use": 'Pass as X-API-Key header: -H "X-API-Key: <master-key>"',
                },
                "agent_token": {
                    "description": "Scoped token for agent/automation use. Create via POST /api/agent/token (requires master key).",
                    "access": "endpoints listed under agent_scopes below",
                    "how_to_use": 'Same header: -H "X-API-Key: <agent-token>"',
                    "create_command": "curl -X POST http://localhost:8085/api/agent/token -H 'X-API-Key: $MASTER_KEY' -H 'Content-Type: application/json' -d '{\"scopes\":[\"ssh:execute\",\"ssh:files\"],\"ttl\":3600}'",
                },
            },
            "available_scopes": sorted(VALID_AGENT_SCOPES),
            "scope_endpoints": scope_routes,
            "master_only_count": len(master_only),
        },
        "quick_start": {
            "title": "3-step quick start",
            "steps": [
                "1. **Set your API key** — export it or pass as `X-API-Key` header. Use the master key (set via `API_KEY` env var) or create an agent token via `POST /api/agent/token`.",
                "2. **Connect to a host** — `POST /api/ssh/connect` with `{host, username, password/private_key}`. Returns a `session_id`.",
                "3. **Execute a command** — `POST /api/ssh/execute` with `{session_id, command}`. Or read a file: `POST /api/file/read` with `{session_id, path}`.",
            ],
            "walkthrough": [
                {
                    "step": 1,
                    "action": "Save your API key",
                    "detail": "Set it as an environment variable or pass in every request header.",
                    "curl": 'export API_KEY="<your-master-key-or-agent-token>"',
                },
                {
                    "step": 2,
                    "action": "Connect to a remote host",
                    "detail": "Replace host, username, and password with your server credentials.",
                    "endpoint": "POST /api/ssh/connect",
                    "scope": "ssh:connect",
                    "curl": 'curl -s -X POST http://localhost:8085/api/ssh/connect -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d \'{"host":"192.168.1.100","username":"root","password":"your-password","port":22}\'',
                    "expect": '{"session_id":"ses_abc123","status":"connected","message":"SSH session established successfully"}',
                },
                {
                    "step": 3,
                    "action": "Run a command on the connected host",
                    "detail": "Use the session_id from step 2. Try `uname -a` or `ls /tmp`.",
                    "endpoint": "POST /api/ssh/execute",
                    "scope": "ssh:execute",
                    "curl": 'curl -s -X POST http://localhost:8085/api/ssh/execute -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d \'{"session_id":"ses_abc123","command":"uname -a"}\'',
                    "expect": '{"exit_code":0,"stdout":"Linux hostname 6.8.0 ... x86_64 GNU/Linux\\n","stderr":"","duration_ms":120}',
                },
                {
                    "step": 4,
                    "action": "Read a file from the connected host",
                    "detail": "Use the same session_id to read remote files.",
                    "endpoint": "POST /api/file/read",
                    "scope": "ssh:files",
                    "curl": 'curl -s -X POST http://localhost:8085/api/file/read -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d \'{"session_id":"ses_abc123","path":"/etc/hostname"}\'',
                    "expect": '{"path":"/etc/hostname","content":"my-server\\n","size":10,"encoding":"utf-8"}',
                },
            ],
            "next_steps": [
                "See the `examples` section below for more operations (file edit, background jobs).",
                "Create an agent token with limited scope: `POST /api/agent/token` (requires master key).",
                "Save servers for quick reconnection: `POST /api/servers`.",
            ],
        },
        "server_onboarding": {
            "title": "Saved servers vs direct connect",
            "no_servers_configured": {
                "message": "No saved servers yet — two ways to get started:",
                "options": [
                    {
                        "label": "Connect directly (quick, no setup)",
                        "how_to": "POST /api/ssh/connect with host, username, and password/private_key. No prior setup needed — works immediately.",
                        "when_to_use": "One-off SSH sessions, troubleshooting, or when you don't need to reuse the server address.",
                    },
                    {
                        "label": "Save a server (for reuse)",
                        "how_to": "POST /api/servers with id, name, host, username. Then connect via POST /api/servers/{id}/connect.",
                        "when_to_use": "Frequent access to the same server, CI/CD, team-shared server inventory.",
                    },
                ],
                "action_summary": "No servers? Use POST /api/ssh/connect now, or save one for later with POST /api/servers.",
            },
            "create_example": {
                "endpoint": "POST /api/servers",
                "body": '{"id":"prod-web-1","name":"Production Web 1","host":"10.0.0.1","port":22,"username":"deploy"}',
                "note": "Master key required. Saved servers can be connected with a single endpoint call.",
            },
            "lifecycle": [
                {"step": "Create", "action": "POST /api/servers — register a server by id, host, username"},
                {"step": "Connect", "action": "POST /api/servers/{id}/connect — start an SSH session to the saved server"},
                {"step": "Execute", "action": "POST /api/ssh/execute — run commands via the session_id from the connect step"},
                {"step": "Disconnect", "action": "POST /api/ssh/disconnect — clean up when done"},
            ],
        },
        "file_workflow": {
            "title": "File operations for agents",
            "overview": "These operations let you read and modify files on a remote host through an active SSH session.",
            "prerequisite": "You need a valid session_id from POST /api/ssh/connect.",
            "steps": [
                {
                    "step": 1,
                    "action": "Read the file",
                    "endpoint": "POST /api/file/read",
                    "body": '{"session_id":"ses_abc123","path":"/var/www/app/config.py"}',
                    "response": '{"path":"/var/www/app/config.py","content":"DEBUG = False\\nPORT = 8080\\n","size":32,"encoding":"utf-8"}',
                    "notes": "Returns the full file content. No size limit on reads. Path must not contain `..` or `~`.",
                },
                {
                    "step": 2,
                    "action": "Edit the file",
                    "endpoint": "PATCH /api/file/edit",
                    "body": '{"session_id":"ses_abc123","path":"/var/www/app/config.py","operations":[{"type":"replace","old":"DEBUG = False","new":"DEBUG = True"}]}',
                    "response": '{"path":"/var/www/app/config.py","operations_applied":1,"changed":true,"success":true}',
                    "notes": "Supports operation types: replace, insert_after, insert_before, delete, append, create. Max file write size: 500 KB.",
                },
                {
                    "step": 3,
                    "action": "Verify the result",
                    "endpoint": "POST /api/ssh/execute",
                    "body": '{"session_id":"ses_abc123","command":"cat /var/www/app/config.py"}',
                    "response": '{"exit_code":0,"stdout":"DEBUG = True\\nPORT = 8080\\n","stderr":"","duration_ms":100}',
                    "notes": "Use execute to confirm the change took effect.",
                },
            ],
            "forbidden_paths": [
                "/etc/passwd", "/etc/shadow", "/etc/hosts", "/etc/crontab",
                "/root/.ssh", "/root/.bash_history", "/var/log/auth.log",
                "/usr/bin", "/proc", "/sys", "/dev", "/boot",
            ],
            "limitations": {
                "max_write_size": "500 KB (content encoded as base64 via heredoc)",
                "max_upload_size": "10 MB (base64-encoded, via POST /api/file/upload)",
                "encoding": "UTF-8 only",
                "path_restrictions": "No directory traversal (.. or ~). Forbidden paths listed above.",
            },
        },
        "context_workflow": {
            "title": "Development contexts for structured editing",
            "overview": "A context tracks a working directory with git awareness. Open files within a context to keep cursor positions, edit with validation, and auto-commit changes.",
            "prerequisite": "You need a valid session_id from POST /api/ssh/connect.",
            "steps": [
                {
                    "step": 1,
                    "action": "Create a context",
                    "endpoint": "POST /api/context/create",
                    "body": '{"session_id":"ses_abc123","path":"/var/www/app","name":"my-app","auto_commit":true}',
                    "response": '{"context_id":"ctx_abc123","name":"my-app","path":"/var/www/app","session_id":"ses_abc123","branch":"main","files_opened":[],"status":"ready"}',
                    "notes": "A context binds a session to a working directory. auto_commit creates git commits on each edit.",
                },
                {
                    "step": 2,
                    "action": "Open a file in the context",
                    "endpoint": "POST /api/context/file/open",
                    "body": '{"context_id":"ctx_abc123","path":"/var/www/app/config.py"}',
                    "response": '{"status":"opened","path":"/var/www/app/config.py"}',
                    "notes": "Registers the file in the context so cursor and edit state are tracked.",
                },
                {
                    "step": 3,
                    "action": "Update cursor position",
                    "endpoint": "POST /api/context/cursor",
                    "body": '{"context_id":"ctx_abc123","path":"/var/www/app/config.py","line":5,"column":1}',
                    "response": '{"status":"updated","path":"/var/www/app/config.py","line":5,"column":1}',
                    "notes": "Helps the agent remember where it left off. Line and column are 1-indexed.",
                },
                {
                    "step": 4,
                    "action": "Edit a file within the context",
                    "endpoint": "PATCH /api/context/file/edit",
                    "body": '{"context_id":"ctx_abc123","path":"/var/www/app/config.py","operations":[{"type":"replace","old":"DEBUG = False","new":"DEBUG = True"}],"run_validation":false}',
                    "response": '{"path":"/var/www/app/config.py","operations_applied":1,"changed":true,"success":true,"git_commit":"abc123def"}',
                    "notes": "If auto_commit was set on the context, each edit creates a git commit automatically.",
                },
                {
                    "step": 5,
                    "action": "Close the file",
                    "endpoint": "POST /api/context/file/close",
                    "body": '{"context_id":"ctx_abc123","path":"/var/www/app/config.py"}',
                    "response": '{"status":"closed","path":"/var/www/app/config.py"}',
                    "notes": "Removes the file from the context's open files list. The cursor position is preserved in context state.",
                },
            ],
            "edit_operations": {
                "replace": {"description": "Find exact text and replace it", "fields": {"old": "str (required)", "new": "str (required)"}},
                "insert_after": {"description": "Insert text after a matching line", "fields": {"after": "str (required)", "text": "str (required)"}},
                "insert_before": {"description": "Insert text before a matching line", "fields": {"before": "str (required)", "text": "str (required)"}},
                "delete": {"description": "Delete matching line(s)", "fields": {"old": "str (required)", "count": "int (optional, 0 = all)"}},
                "append": {"description": "Append text to the end of the file", "fields": {"text": "str (required)"}},
                "create": {"description": "Create a new file with content", "fields": {"text": "str (required)"}},
            },
            "tips": [
                "Use cursor to bookmark where you are — especially useful when switching between files.",
                "Context edits with auto_commit=true produce git commits, making changes auditable.",
                "Run POST /api/context/create once per working directory, then reuse the context_id.",
                "Close files when done to keep the context's open_files list lean.",
            ],
        },
        "bulk_workflow": {
            "title": "Bulk & Batch Operations",
            "overview": "Perform multiple file reads, edits, or command executions in a single API call. Bulk endpoints run operations concurrently (where possible); batch endpoints process sequentially in a single transaction.",
            "prerequisite": "You need a valid session_id from POST /api/ssh/connect.",
            "bulk_vs_batch": {
                "when_to_use": {
                    "bulk_read": "Read up to 20 files concurrently. Each file is independent — you don't need all-or-nothing semantics. Best for inspecting multiple configs, logs, or source files at once.",
                    "bulk_edit": "Edit multiple files in one request. Processed sequentially for safety — each file's edits are applied before moving to the next. Best for coordinated changes like updating config values across several files.",
                    "bulk_execute": "Run up to 100 independent commands concurrently. Best for collecting system info (uptime, disk, memory) or running parallel health checks. Results are collected per-command; one failure doesn't stop others.",
                    "batch_execute": "A single transaction with mixed operations (read → edit → execute → create). Best for multi-step workflows where each step depends on the previous. Supports continue_on_error, optional git commits, and validation.",
                },
                "key_differences": [
                    "bulk/read reads N files in one call → saves N round-trips vs file/read (1 file per call)",
                    "bulk/edit edits N files in one call → vs file/edit (1 file per call with same operation format)",
                    "bulk/execute runs N commands concurrently → vs jobs/run (1 command, supports background/async)",
                    "batch/execute is a transaction (mixed ops, git, validation) → vs bulk/execute (simple concurrent commands only)",
                    "bulk endpoints use scope ssh:files or jobs:run; batch/execute requires master key",
                ],
            },
            "examples": [
                {
                    "endpoint": "POST /api/bulk/read",
                    "title": "Read multiple files concurrently",
                    "description": "Returns all file contents in a single response. Each file read is independent — errors are captured per-file without failing the whole request.",
                    "request_body": {
                        "session_id": "ses_abc123",
                        "paths": [
                            "/etc/hostname",
                            "/etc/os-release",
                            "/proc/uptime",
                        ],
                    },
                    "response": {
                        "files": {
                            "/etc/hostname": "web-server-01\n",
                            "/etc/os-release": "PRETTY_NAME=\"Ubuntu 24.04 LTS\"\nNAME=\"Ubuntu\"\n...",
                            "/proc/uptime": "482931.20 1234567.89\n",
                        },
                        "errors": {},
                    },
                    "notes": "max 20 paths per request. Reads use asyncio.gather with semaphore(20) — independent files read in parallel.",
                },
                {
                    "endpoint": "POST /api/bulk/edit",
                    "title": "Edit multiple files",
                    "description": "Apply different edit operations to different files in one request. Files are processed sequentially; each file can have multiple operations. Errors in one file don't stop other files from being edited.",
                    "request_body": {
                        "session_id": "ses_abc123",
                        "files": [
                            {
                                "path": "/var/www/app/config.py",
                                "operations": [
                                    {"type": "replace", "old": "DEBUG = False", "new": "DEBUG = True"},
                                    {"type": "insert_after", "after": "PORT = 8080", "text": "HOST = '0.0.0.0'\n"},
                                ],
                            },
                            {
                                "path": "/var/www/app/settings.py",
                                "operations": [
                                    {"type": "replace", "old": "TIMEOUT = 30", "new": "TIMEOUT = 60"},
                                    {"type": "append", "text": "\n# Added by bulk edit\nMAX_RETRIES = 3\n"},
                                ],
                            },
                        ],
                    },
                    "response": {
                        "results": [
                            {"path": "/var/www/app/config.py", "success": True, "operations_applied": 2, "changed": True, "error": None},
                            {"path": "/var/www/app/settings.py", "success": True, "operations_applied": 2, "changed": True, "error": None},
                        ],
                        "total_files": 2,
                        "files_changed": 2,
                        "total_operations": 4,
                    },
                    "notes": "Operation types: replace, insert_after, insert_before, delete, append, create. Sequential per-file — errors in one file don't block others.",
                },
                {
                    "endpoint": "POST /api/bulk/execute",
                    "title": "Run multiple commands concurrently",
                    "description": "Execute independent commands in parallel. Each command's result includes stdout, stderr, exit code, and duration. Commands that fail are collected in results — other commands continue running.",
                    "request_body": {
                        "session_id": "ses_abc123",
                        "commands": [
                            "uptime",
                            "df -h /",
                            "free -m",
                        ],
                    },
                    "response": {
                        "results": [
                            {"command": "uptime", "success": True, "exit_code": 0, "stdout": " 12:34:56 up 10 days, ...", "stderr": "", "duration": 0.12},
                            {"command": "df -h /", "success": True, "exit_code": 0, "stdout": "Filesystem ...\n/dev/sda1 ...", "stderr": "", "duration": 0.08},
                            {"command": "free -m", "success": True, "exit_code": 0, "stdout": "               total  used  free  ...", "stderr": "", "duration": 0.09},
                        ],
                        "total_commands": 3,
                        "successful": 3,
                        "failed": 0,
                        "total_duration": 0.29,
                    },
                    "notes": "max 100 commands. Concurrent execution via semaphore(10). Rate limited: 10 requests/minute.",
                },
                {
                    "endpoint": "POST /api/batch/execute",
                    "title": "Multi-step transaction (advanced)",
                    "description": "A single transaction combining multiple operation types: read, edit, create, delete, rename, copy, execute. Supports git auto-commit, optional validation, and continue_on_error.",
                    "notes": "Requires master key. Uses context_id (not session_id). See /api/help context_workflow for context creation. Max 50 operations per transaction.",
                },
            ],
            "full_scenario": {
                "title": "Typical agent scenario: inspect → fix → verify",
                "overview": "Three-step workflow: read config files, apply fixes, confirm the result. Each step uses a bulk or batch endpoint to minimize round-trips.",
                "steps": [
                    {
                        "step": 1,
                        "action": "Read config files",
                        "endpoint": "POST /api/bulk/read",
                        "body": '{"session_id":"ses_abc123","paths":["/etc/nginx/nginx.conf","/etc/nginx/sites-enabled/default","/var/log/nginx/error.log"]}',
                        "expected": "Three files returned. errors dict is empty. Each file content accessible by path key.",
                        "notes": "Read all relevant files in one call. If a file doesn't exist, it appears in errors with the reason — other files still return normally.",
                    },
                    {
                        "step": 2,
                        "action": "Apply fixes to multiple files",
                        "endpoint": "POST /api/bulk/edit",
                        "body": '{"session_id":"ses_abc123","files":[{"path":"/etc/nginx/nginx.conf","operations":[{"type":"replace","old":"worker_connections 768;","new":"worker_connections 1024;"}]},{"path":"/etc/nginx/sites-enabled/default","operations":[{"type":"replace","old":"listen 80 default_server;","new":"listen 8080 default_server;"}]}]}',
                        "expected": "Both files edited successfully. results array shows success: true for each. total_operations = 2.",
                        "notes": "Edits are sequential — if nginx.conf fails, default is still attempted. Check each result individually.",
                    },
                    {
                        "step": 3,
                        "action": "Verify with concurrent commands",
                        "endpoint": "POST /api/bulk/execute",
                        "body": '{"session_id":"ses_abc123","commands":["nginx -t","curl -s -o /dev/null -w \'%{http_code}\' http://localhost:8080","echo done"]}',
                        "expected": "nginx -t exits 0 (config OK), curl returns 200, echo prints 'done'. summary: 3/3 successful.",
                        "notes": "Commands run concurrently. nginx -t validates config; curl confirms the server responds on the new port.",
                    },
                ],
                "summary": "3 API calls replaced up to 8 individual requests (3 reads + 2 edits + 3 commands). Agent gets all results in 3 structured responses.",
            },
            "limitations": {
                "max_paths_per_bulk_read": 20,
                "max_commands_per_bulk_execute": 100,
                "max_operations_per_batch_execute": 50,
                "rate_limit_bulk_execute": "10 requests per minute (HTTP 429 after burst)",
                "note": "Limits are enforced server-side. Exceeding them returns a validation error before any execution.",
            },
        },
        "jobs_streaming": {
            "title": "Job execution & live streaming",
            "overview": "Long-running commands can be started as background jobs and monitored in real time via SSE streaming. The stream delivers stdout/stderr chunks as they arrive, without polling.",
            "when_to_use": {
                "jobs_run": "Start a command as a background job. The command runs asynchronously — the response returns immediately with a job_id. Best for commands that take >5s, deployment scripts, or any operation you want to monitor without blocking.",
                "jobs_stream": "Connect to the SSE event stream for a running job. Each chunk of stdout/stderr is delivered as it arrives. Use this for real-time visibility — the terminal equivalent of watching a command execute.",
                "jobs_status": "Lightweight check — returns job_id, status, progress, and duration. No output data. Best for quick health checks or when you only need to know if a job finished.",
                "jobs_events": "Alias for /stream. Same SSE event stream.",
                "jobs_result": "Full result after completion — includes accumulated stdout, stderr, exit_code, duration, error_message. Best for retrieving the complete output of a finished job.",
            },
            "example": {
                "step_1_start": {
                    "action": "Start a long-running job",
                    "endpoint": "POST /api/jobs/run",
                    "body": '{"session_id":"ses_abc123","command":"apt-get update && apt-get upgrade -y","timeout":300}',
                    "response": '{"job_id":"job_abc123","status":"pending","message":"Job started"}',
                    "notes": "Returns immediately with a job_id. The command runs asynchronously on the SSH host.",
                },
                "step_2_monitor": {
                    "action": "Stream live output (SSE)",
                    "endpoint": "GET /api/jobs/{job_id}/stream",
                    "notes": "Connect to this endpoint to receive events in real time. Each event is a JSON line prefixed with data:. See event format below.",
                    "event_format": {
                        "status": '{"type":"status","status":"running"}',
                        "stdout": '{"type":"stdout","data":"Reading package lists...\\n"}',
                        "stderr": '{"type":"stderr","data":"W: Some index files failed to download.\\n"}',
                        "exit": '{"type":"exit","exit_code":0}',
                        "error": '{"type":"error","error":"Connection lost"}',
                    },
                    "stream_notes": [
                        "Events are SSE-formatted: data: <json>\\n\\n",
                        "Keepalive pings (:keepalive\\n\\n) every 1s when idle.",
                        "Stream ends when the job reaches a terminal state (completed/failed/cancelled).",
                        "Maximum stream duration: 3600s (1 hour).",
                        "Rate limited: 20 requests per minute.",
                    ],
                },
                "step_3_check_status": {
                    "action": "Quick status check (no output)",
                    "endpoint": "GET /api/jobs/{job_id}/status",
                    "response": '{"job_id":"job_abc123","status":"running","progress":{},"duration":12.5}',
                    "notes": "Lightweight alternative to stream. Returns current status and elapsed duration without stdout/stderr.",
                },
                "step_4_get_result": {
                    "action": "Retrieve final result",
                    "endpoint": "GET /api/jobs/{job_id}/result",
                    "response": '{"job_id":"job_abc123","session_id":"ses_abc123","command":"apt-get update...","status":"completed","stdout":"...","stderr":"...","exit_code":0,"duration":45.2,"error_message":null}',
                    "notes": "Available after job reaches a terminal state. Returns complete stdout/stderr (capped at 10 MB per stream).",
                },
            },
            "sse_events": {
                "title": "SSE Event Types",
                "events": [
                    {"type": "status", "fields": {"status": "pending | running | completed | failed | cancelled"}, "description": "Job status change. Emitted on state transitions."},
                    {"type": "stdout", "fields": {"data": "string — stdout chunk"}, "description": "A chunk of stdout output. Delivered as it arrives from the SSH process. Each chunk is up to 4 KB."},
                    {"type": "stderr", "fields": {"data": "string — stderr chunk"}, "description": "A chunk of stderr output. Same delivery model as stdout."},
                    {"type": "exit", "fields": {"exit_code": "int — process exit code"}, "description": "Process exit. Only emitted if the command completes normally."},
                    {"type": "error", "fields": {"error": "string — error message"}, "description": "Fatal error (SSH disconnect, timeout, crash). Job transitions to failed status."},
                ],
            },
            "stream_vs_poll": {
                "title": "Stream vs Polling",
                "differences": [
                    "Stream delivers events in real time — no delay between output production and delivery.",
                    "Status polling only tells you the current state, not what the job is producing.",
                    "Polling is useful when you don't control the client (e.g., simple CI scripts) or when SSE connections are blocked.",
                    "Stream uses one long-lived HTTP connection. Polling uses many short requests — more server load at high frequency.",
                ],
                "recommendation": "Use stream for interactive monitoring. Use status/result for post-hoc analysis or when SSE is unavailable.",
            },
        },
        "git_workflow": {
            "title": "Git safe flow — inspect, backup, commit, restore",
            "overview": "A safe git workflow: review changes before committing, create backups before risky operations, restore if something goes wrong. All git endpoints require the master API key.",
            "safe_flow": [
                {"step": 1, "action": "Check status", "endpoint": "GET /api/git/simple-status", "purpose": "See which files are modified, staged, or untracked. Returns branch name and file lists. Lightweight — 2 SSH calls."},
                {"step": 2, "action": "Review diff", "endpoint": "POST /api/git/diff", "purpose": "See the actual changes line by line before deciding what to do. Returns the unified diff output."},
                {"step": 3, "action": "Create backup", "endpoint": "POST /api/git/backup", "purpose": "Stash current changes with a named backup. Safe to run even if nothing changed — creates a git stash entry you can restore later."},
                {"step": 4, "action": "Commit or restore", "endpoint": "POST /api/git/commit or POST /api/git/restore", "purpose": "Commit your reviewed changes, or restore the backup if something went wrong during the process."},
            ],
            "when_to_use": {
                "git_simple_status": "Quick overview — what branch, what files changed, what's staged. No context needed, just session + path.",
                "git_status": "Full context-aware status — includes remote URL, last commit hash, and can_commit flag. Requires a context.",
                "git_diff": "Review actual changes before committing. Use with cached=true for staged diff, cached=false for working tree diff.",
                "git_backup": "Before any risky operation (large edit, bulk edit, restore). Creates a named git stash entry. Idempotent — safe to call multiple times.",
                "git_commit": "Save reviewed changes with a message. Use files param to commit only specific files. Requires status to be clean or staged.",
                "git_restore": "Undo changes by popping the most recent stash backup. Restores files to the state when the backup was created.",
                "recovery_backup": "Alias for git/backup with request body instead of query params. Same underlying mechanism.",
                "recovery_restore": "Alias for git/restore with request body instead of query params. Same underlying mechanism.",
                "recovery_backups": "List available stash backups with their names and timestamps. Useful before deciding which backup to restore.",
            },
            "examples": [
                {
                    "endpoint": "GET /api/git/simple-status",
                    "title": "Quick status check",
                    "description": "Returns branch, modified/staged/untracked file lists. No context required.",
                    "request": "GET /api/git/simple-status?session_id=ses_abc123&path=/var/www/app",
                    "response": '{"branch":"main","clean":false,"modified":["config.py","src/main.py"],"staged":[],"untracked":["new_feature.py"],"ahead":0,"behind":0}',
                    "notes": "clean=true means no changes. Check modified vs staged — staged files are already git add'd.",
                },
                {
                    "endpoint": "POST /api/git/diff",
                    "title": "Review changes",
                    "description": "Get the full unified diff for the working tree. Shows every changed line with +/- markers.",
                    "body": '{"session_id":"ses_abc123","path":"/var/www/app","cached":false}',
                    "response": '{"path":"/var/www/app","diff":"diff --git a/config.py b/config.py\\n--- a/config.py\\n+++ b/config.py\\n@@ -1,3 +1,4 @@\\n DEBUG = False\\n+DEBUG = True\\n PORT = 8080\\n","files_changed":1}',
                    "notes": "cached=true for staged diff (git diff --cached). cached=false (default) for working tree diff. Large diffs are truncated at 10 MB.",
                },
                {
                    "endpoint": "POST /api/git/backup",
                    "title": "Create a named backup",
                    "description": "Stashes current changes with a name. Safe to call at any point — creates a new entry in the stash stack.",
                    "request": "POST /api/git/backup?context_id=ctx_abc123&backup_name=before_bulk_edit",
                    "response": '{"success":true,"message":"Backup created: before_bulk_edit","hash":null}',
                    "notes": "Backup names help identify the stash later. Use GET /api/recovery/backups?context_id=ctx_abc123 to list all backups.",
                },
                {
                    "endpoint": "POST /api/git/commit",
                    "title": "Commit changes",
                    "description": "Commits changes with a message. Optionally specify which files to commit — otherwise commits all tracked changes.",
                    "body": '{"context_id":"ctx_abc123","message":"fix: enable debug mode","files":["config.py"]}',
                    "response": '{"success":true,"message":"Commit created: abc123def","hash":"abc123def"}',
                    "notes": "Commits are local only (no push). Use files param to commit specific files. Files must be either staged or tracked with changes.",
                },
                {
                    "endpoint": "POST /api/git/restore",
                    "title": "Restore from backup",
                    "description": "Pops the most recent stash entry. Restores working tree to the state when the backup was created.",
                    "request": "POST /api/git/restore?context_id=ctx_abc123",
                    "response": '{"success":true,"message":"Backup restored","hash":null}',
                    "notes": "Restore is destructive — overwrites current working tree changes. Always create a backup first. If merge conflicts occur, the response includes a hint to resolve them manually.",
                },
            ],
            "backup_recovery_aliases": {
                "title": "Backup/recovery endpoints (request-body aliases)",
                "note": "POST /api/recovery/backup and /api/recovery/restore are request-body equivalents of the query-param git/backup and git/restore. Use whichever matches your client style.",
                "recovery_backup_body": '{"context_id":"ctx_abc123","name":"before_risky_edit"}',
                "recovery_restore_body": '{"context_id":"ctx_abc123","backup_id":null}',
                "recovery_list": "GET /api/recovery/backups?context_id=ctx_abc123 — lists all stash backups",
            },
            "safety_notes": [
                "Always run git/simple-status before committing — know what you're committing.",
                "Always create a backup (git/backup) before risky operations (bulk edit, restore).",
                "Restore overwrites working tree changes — it cannot be undone. Backup first.",
                "Commits are local. There is no git push endpoint — commits stay on the remote server.",
                "Git endpoints require master key — agent tokens cannot access git operations.",
                "Large diffs are truncated at 10 MB. For very large changes, check individual file diffs.",
            ],
        },
        "navigation_workflow": {
            "title": "Project navigation — tree, search, context",
            "overview": "Navigate a project without manually walking a directory tree. Use project endpoints to inspect structure, search to find code, and contexts to track a working session with bookmarks and cursor positions.",
            "prerequisite": "You need a valid session_id from POST /api/ssh/connect for project endpoints. Search and context endpoints require the master API key.",
            "sections": [
                {
                    "name": "project_tree",
                    "title": "Project tree & structure",
                    "endpoints": [
                        {"endpoint": "GET /api/project/tree", "scope": "ssh:files", "description": "Quick flat listing of files and directories. Returns type, path, size. Good for getting a birds-eye view of a project directory. Query params: session_id, path (default '.'), max_depth (1—10, default 3)."},
                        {"endpoint": "POST /api/project/structure", "scope": "master_key", "description": "Full project structure with metadata (permissions, modified_at, git_status) and a nested tree representation. Input: {session_id, path, include_git_status?, max_depth?}."},
                        {"endpoint": "POST /api/tree", "scope": "master_key", "description": "Recursive file tree with nested children. Supports hidden files and configurable depth. Input: {session_id, path, depth?, show_hidden?, max_files?}."},
                    ],
                    "which_to_use": {
                        "quick_lookup": "GET /api/project/tree — low overhead, flat list, scope-based (agent tokens work)",
                        "detailed_analysis": "POST /api/project/structure — full metadata + git status, requires master key",
                        "deep_tree": "POST /api/tree — recursive tree with nesting, requires master key",
                    },
                },
                {
                    "name": "code_search",
                    "title": "Search across files & code",
                    "endpoints": [
                        {"endpoint": "POST /api/search/global", "scope": "master_key", "description": "Text search across all project files. Supports regex and glob file patterns. Returns match count, affected files, and per-match line/column/content with optional context lines. Input: {session_id, path, query, file_pattern?, use_regex?, case_sensitive?, context_lines?}."},
                        {"endpoint": "POST /api/code/search", "scope": "master_key", "description": "Language-aware code search. Filters by file extension. Supports 0—10 context lines. Input: {session_id, path, query, language?, context_lines?}. Language is a file extension (e.g. 'py', 'js', 'rs')."},
                    ],
                    "which_to_use": {
                        "find_text": "POST /api/search/global — grep-style, text + regex, any file pattern",
                        "find_code": "POST /api/code/search — language-filtered, useful for finding classes/functions in a specific language",
                    },
                },
                {
                    "name": "context_system",
                    "title": "Contexts — bookmarks, cursor, session tracking",
                    "endpoints": [
                        {"endpoint": "GET /api/context/list", "scope": "master_key", "description": "List all active contexts. Optional filter by session_id query param. Returns array of ContextResponse with name, path, branch, open files, and bookmark count."},
                        {"endpoint": "POST /api/context/create", "scope": "master_key", "description": "Create a new development context. Binds a session to a working directory. Supports auto_commit (git commit on each edit) and auto_validate. Input: {session_id, path, name?, branch?, auto_commit?, auto_validate?}."},
                        {"endpoint": "GET /api/context/{context_id}", "scope": "master_key", "description": "Get context details including open files, bookmarks, smart state (tabs, command history, search history)."},
                        {"endpoint": "POST /api/context/bookmark", "scope": "master_key", "description": "Add a bookmark at a specific file and line. Bookmark notes help remember why this location is important. Input: {context_id, path, line, note?}."},
                        {"endpoint": "POST /api/context/cursor", "scope": "master_key", "description": "Update cursor position in an open file. Line and column are 1-indexed. Helps agents remember where they left off. Input: {context_id, path, line, column?}."},
                        {"endpoint": "POST /api/context/file/open", "scope": "master_key", "description": "Register a file as open in the context. Tracks cursor position, edit history, and tab state. Input: {context_id, path}."},
                    ],
                },
            ],
            "examples": [
                {
                    "endpoint": "GET /api/project/tree",
                    "title": "Quick project tree",
                    "description": "Flat list of all files and directories up to 3 levels deep. Returns type (file/directory), path, and size (files only).",
                    "request": "GET /api/project/tree?session_id=ses_abc123&path=/var/www/app&max_depth=3",
                    "response": '{"items":[{"type":"directory","path":".","size":null},{"type":"file","path":"config.py","size":1024},{"type":"file","path":"main.py","size":4096}],"count":15}',
                    "notes": "Hidden dirs (*.git, node_modules, __pycache__, venv) are excluded. max_depth controls how deep to recurse — 1 = current dir only.",
                },
                {
                    "endpoint": "POST /api/search/global",
                    "title": "Search across all files",
                    "description": "Grep-style text search. Returns every match with file path, line number, column, and matching content line. Supports context_lines for surrounding lines.",
                    "body": '{"session_id":"ses_abc123","path":"/var/www/app","query":"DEBUG","file_pattern":"*.py","context_lines":2}',
                    "response": '{"query":"DEBUG","total_count":3,"files_affected":["/var/www/app/config.py","/var/www/app/settings.py"],"matches":[{"path":"/var/www/app/config.py","line":5,"column":1,"content":"DEBUG = True"},{"path":"/var/www/app/config.py","line":8,"column":1,"content":"# Toggle debug mode"},{"path":"/var/www/app/settings.py","line":3,"column":1,"content":"DEBUG_LEVEL = 2"}]}',
                    "notes": "Use file_pattern to limit search scope (*.py, *.js, *.json). use_regex=true enables regex search. context_lines=N adds surrounding lines. Search is synchronous and blocks until complete.",
                },
                {
                    "endpoint": "POST /api/context/create",
                    "title": "Create a working context",
                    "description": "Binds a session to a working directory. All subsequent file operations can reference the context instead of repeating session_id + path.",
                    "body": '{"session_id":"ses_abc123","path":"/var/www/app","name":"fix-auth","auto_commit":true}',
                    "response": '{"context_id":"ctx_abc123","name":"fix-auth","path":"/var/www/app","session_id":"ses_abc123","branch":"main","files_opened":[],"status":"ready"}',
                    "notes": "auto_commit creates a git commit after each context-aware edit. Name helps identify the context in listings.",
                },
                {
                    "endpoint": "POST /api/context/bookmark",
                    "title": "Bookmark a file location",
                    "description": "Marks a specific file and line with an optional note. Bookmarks persist in the context state and appear in /api/context/{id} response under smart_state.bookmarks.",
                    "body": '{"context_id":"ctx_abc123","path":"/var/www/app/config.py","line":5,"note":"need to enable debug mode"}',
                    "response": '{"status":"added","bookmark":{"context_id":"ctx_abc123","path":"/var/www/app/config.py","line":5,"note":"need to enable debug mode","id":"bm_1"}}',
                    "notes": "Bookmarks help agents remember important locations. Use multiple bookmarks to track various points of interest. Remove with DELETE /api/context/bookmark?context_id=...&path=...&line=...",
                },
                {
                    "endpoint": "POST /api/context/cursor",
                    "title": "Set cursor position",
                    "description": "Remembers where the agent was working in a file. Useful when switching between files or resuming work after a break. Line and column are 1-indexed.",
                    "body": '{"context_id":"ctx_abc123","path":"/var/www/app/config.py","line":15,"column":1}',
                    "response": '{"status":"updated","path":"/var/www/app/config.py","line":15,"column":1}',
                    "notes": "Cursor position is stored per-file within the context. Retrieve it via GET /api/context/{id} — look at smart_state.tabs[] for cursor per open file.",
                },
            ],
            "full_scenario": {
                "title": "Typical agent flow: search → open → bookmark → edit",
                "overview": "Find code, register it in a context, mark important locations, and edit — all without manually walking the project tree.",
                "steps": [
                    {
                        "step": 1,
                        "action": "Search for the code to change",
                        "endpoint": "POST /api/search/global",
                        "body": '{"session_id":"ses_abc123","path":"/var/www/app","query":"DEBUG","file_pattern":"*.py","context_lines":2}',
                        "expected": "3 matches in 2 files. Each match shows path, line, column, and content line. Context lines show surrounding code.",
                        "notes": "The response tells you exactly which files need changing and what the current code looks like.",
                    },
                    {
                        "step": 2,
                        "action": "Create a context for this work",
                        "endpoint": "POST /api/context/create",
                        "body": '{"session_id":"ses_abc123","path":"/var/www/app","name":"debug-config-fix"}',
                        "expected": "context_id returned. Context tracks working directory, git branch, and all subsequent file activity.",
                        "notes": "One context per task. Reuse the same context_id for all operations in this session.",
                    },
                    {
                        "step": 3,
                        "action": "Open the target file in the context",
                        "endpoint": "POST /api/context/file/open",
                        "body": '{"context_id":"ctx_abc123","path":"/var/www/app/config.py"}',
                        "expected": "File registered as opened in the context. Cursor tracking begins for this file.",
                        "notes": "Opening a file registers it in the context's tab list. You can track cursor, scroll position, and edit history per file.",
                    },
                    {
                        "step": 4,
                        "action": "Set a bookmark at the key line",
                        "endpoint": "POST /api/context/bookmark",
                        "body": '{"context_id":"ctx_abc123","path":"/var/www/app/config.py","line":5,"note":"enable debug here"}',
                        "expected": "Bookmark created. It appears in the context state under smart_state.bookmarks.",
                        "notes": "Bookmarks help you remember important locations. Use notes to explain why the location matters.",
                    },
                    {
                        "step": 5,
                        "action": "Navigate to the bookmark and edit",
                        "endpoint": "PATCH /api/context/file/edit",
                        "body": '{"context_id":"ctx_abc123","path":"/var/www/app/config.py","operations":[{"type":"replace","old":"DEBUG = False","new":"DEBUG = True"}]}',
                        "expected": "Edit applied. If auto_commit was set, a git commit is created. Response includes diff and commit hash.",
                        "notes": "The context-aware edit endpoint integrates with auto_commit and auto_validation. Results are captured in context history.",
                    },
                ],
                "summary": "5 API calls: search to find, create to prepare, open to register, bookmark to mark, edit to fix. Agent never manually walks the file tree — search and context keep it oriented.",
            },
            "tips": [
                "Start with GET /api/project/tree or POST /api/search/global to orient yourself in an unfamiliar project.",
                "Create a context once per task and reuse the context_id — it remembers file state, bookmarks, and cursor positions.",
                "Use POST /api/context/cursor to mark where you left off editing, especially when switching between multiple files.",
                "Bookmarks are lightweight — create them freely. Remove with DELETE /api/context/bookmark.",
                "Search is synchronous. For very large codebases, narrow the search with file_pattern or language.",
                "Project tree excludes hidden dirs (node_modules, __pycache__, .git, venv) to keep results clean.",
            ],
        },
        "templating_workflow": {
            "title": "Templates, scaffold & code generation",
            "overview": "Start new files and modules from pre-built templates, scaffold Python classes with test files, or generate code from natural language instructions. All endpoints require the master API key.",
            "prerequisite": "Template render and scaffold need a session_id. Code generate works without a session. Code complete needs a session_id and a target file path.",
            "sections": [
                {
                    "name": "template_library",
                    "title": "Code templates — list, inspect, render",
                    "overview": "Pre-written code templates for common patterns: FastAPI endpoints, Pydantic models, Python classes, functions, tests, Docker Compose services, Nginx configs, GitHub Actions workflows.",
                    "endpoints": [
                        {"endpoint": "GET /api/templates", "scope": "master_key", "description": "List all available code templates. Returns id, name, description, and language for each template. 8 built-in templates cover Python, YAML, and Nginx."},
                        {"endpoint": "GET /api/templates/{template_id}", "scope": "master_key", "description": "Get template details with full source code and default parameters. Use this to preview what will be generated before rendering."},
                        {"endpoint": "POST /api/templates/render", "scope": "master_key", "description": "Render a template with custom parameters and save the output to a file on the remote host. Supports auto_commit. Input: {context_id, template_id, params, target_path, auto_commit?}."},
                    ],
                },
                {
                    "name": "command_templates",
                    "title": "Command templates — predefined SSH commands",
                    "overview": "Parameterised SSH command templates for common tasks: deploy, healthcheck, disk usage, docker stats, nginx reload, journal logs. Params are substituted into {placeholders} before execution.",
                    "endpoints": [
                        {"endpoint": "GET /api/command-templates", "scope": "master_key", "description": "List all predefined command templates. Each template has id, name, description, and a command string with {param} placeholders."},
                        {"endpoint": "POST /api/templates/run", "scope": "master_key", "description": "Execute a command template with parameter substitution. Params dict is used to replace {key} placeholders in the template command. Input: {session_id, template, params?}."},
                    ],
                },
                {
                    "name": "scaffold",
                    "title": "Scaffold — generate project structures",
                    "overview": "Generate multi-file project structures from a single request. Currently supports Python class scaffolding (class file + test file).",
                    "endpoints": [
                        {"endpoint": "POST /api/scaffold/python-class", "scope": "master_key", "description": "Create a Python class with an optional test file. Specify class name, methods, and target module directory. Creates both class file (lowercase name) and test file (test_ prefix) in one call. Input: {session_id, module_path, class_name, methods?, include_test?}."},
                    ],
                    "conflict_handling": {
                        "note": "Scaffold uses write_file which overwrites existing files. There is no built-in conflict detection — check file existence first via POST /api/file/read or include in a context to track state.",
                        "options": {
                            "overwrite": "Default behaviour — existing files are replaced. Always happens when you call scaffold again with the same target.",
                            "skip": "Not implemented server-side. Check manually: read target file first, then decide if you want to scaffold.",
                            "diff_first": "Use POST /api/git/diff (before scaffolding in a git repo) or GET /api/project/tree to check if target files exist.",
                        },
                    },
                },
                {
                    "name": "code_generation",
                    "title": "Code generation & completion",
                    "overview": "Generate or complete code from natural language instructions without writing boilerplate by hand.",
                    "endpoints": [
                        {"endpoint": "POST /api/code/generate", "scope": "master_key", "description": "Generate code from a natural language instruction. No session required — works standalone. Returns generated code with language label and explanation. Input: {instruction, language?}."},
                        {"endpoint": "POST /api/code/complete", "scope": "master_key", "description": "Suggest code completion for a partially written file. Uses CodeIntelligence to match the existing code context. Input: {session_id, path, partial_code, language?}. Returns completion text with surrounding context for verification."},
                    ],
                },
            ],
            "examples": [
                {
                    "endpoint": "GET /api/templates",
                    "title": "Browse available templates",
                    "description": "List all 8 built-in code templates with their ID, name, description, and language. Use the ID to fetch details or render a specific template.",
                    "request": "GET /api/templates",
                    "response": '{"templates":[{"id":"fastapi_endpoint","name":"FastAPI Endpoint","description":"FastAPI route handler","language":"python"},{"id":"pydantic_model","name":"Pydantic Model","description":"Pydantic data model","language":"python"}],"count":8}',
                    "notes": "Template IDs: fastapi_endpoint, pydantic_model, class, function, test, docker_compose_service, nginx_config, github_actions.",
                },
                {
                    "endpoint": "GET /api/templates/{template_id}",
                    "title": "Preview a template before rendering",
                    "description": "Get the full template source code with default parameters. Inspect the {placeholder} variables and customise before calling render.",
                    "request": "GET /api/templates/fastapi_endpoint",
                    "response": '{"id":"fastapi_endpoint","name":"FastAPI Endpoint","description":"FastAPI route handler","language":"python","code":"@router.{method}(\"{path}\")\\nasync def {handler_name}({params}):\\n    \"\"\"{description}\"\"\"\\n    return {response}\\n","default_params":{"method":"get","path":"/items/{item_id}","handler_name":"get_item","params":"item_id: int","description":"Get item by ID","response":"{\"item_id\": item_id}"}}',
                    "notes": "The code field contains the template with {placeholders}. default_params provides sensible defaults for a quick render.",
                },
                {
                    "endpoint": "POST /api/templates/render",
                    "title": "Render a template to a file",
                    "description": "Render a FastAPI endpoint template with custom parameters and save it to a target file on the remote host. Optionally auto-commit.",
                    "body": '{"context_id":"ctx_abc123","template_id":"fastapi_endpoint","params":{"method":"get","path":"/users/{user_id}","handler_name":"get_user","params":"user_id: int","description":"Get user by ID","response":"{\\"user_id\\": user_id}"},"target_path":"/var/www/app/routes/users.py","auto_commit":false}',
                    "response": '{"success":true,"template_id":"fastapi_endpoint","target_path":"/var/www/app/routes/users.py","code":"@router.get(\"/users/{user_id}\")\\nasync def get_user(user_id: int):\\n    \"\"\"Get user by ID\"\"\"\\n    return {\"user_id\": user_id}\\n","git_commit":null}',
                    "notes": "The rendered code is returned in the response so you can verify it. Set auto_commit=true for automatic git commit. The file is written via SSH heredoc.",
                },
                {
                    "endpoint": "POST /api/templates/run",
                    "title": "Run a command template",
                    "description": "Execute a predefined command template with parameter substitution. Replace {service} with your actual service name.",
                    "body": '{"session_id":"ses_abc123","template":"healthcheck","params":{"service":"nginx"}}',
                    "response": '{"stdout":"● nginx.service - nginx\\nLoaded: loaded (/lib/systemd/system/nginx.service; enabled)\\nActive: active (running)\\n...","stderr":"","exit_code":0,"duration":1.2}',
                    "notes": "Available templates: deploy, healthcheck, disk-usage, memory, docker-ps, docker-stats, nginx-reload, uptime, journal. Use GET /api/command-templates to see all.",
                },
                {
                    "endpoint": "POST /api/scaffold/python-class",
                    "title": "Generate a Python class + tests",
                    "description": "Scaffold a complete Python class file and optional test file. Both are created in a single API call on the remote host.",
                    "body": '{"session_id":"ses_abc123","module_path":"app/services","class_name":"UserService","methods":["get_user","create_user","delete_user"],"include_test":true}',
                    "response": '{"success":true,"files_created":["app/services/user_service.py","app/services/test_user_service.py"],"message":"Created UserService class with 3 methods"}',
                    "notes": "class_name must be PascalCase (^[A-Z][a-zA-Z0-9_]*$). The class file is lowercase(class_name).py. The test file is test_lowercase(class_name).py. Methods become async methods with NotImplementedError stubs. Use include_test=false to skip test file generation.",
                },
                {
                    "endpoint": "POST /api/code/generate",
                    "title": "Generate code from description",
                    "description": "Turn a natural language description into code. No SSH session needed — works standalone. Returns code with language and explanation.",
                    "body": '{"instruction":"FastAPI route that returns a list of users from a database","language":"python"}',
                    "response": '{"code":"from fastapi import APIRouter, Depends\\nfrom sqlalchemy.ext.asyncio import AsyncSession\\n\\nrouter = APIRouter()\\n\\n@router.get(\"/users\")\\nasync def list_users(db: AsyncSession = Depends(get_db)):\\n    \"\"\"Return all users.\"\"\"\\n    result = await db.execute(select(User))\\n    return result.scalars().all()\\n","language":"python","explanation":"Generated code for: FastAPI route that returns a list of users from a database"}',
                    "notes": "Code is generated server-side (no external LLM call). The implementation uses CodeIntelligence.generate_code(). For project-specific context, use code/insert instead.",
                },
            ],
            "full_scenario": {
                "title": "From template to working code in 4 calls",
                "overview": "Browse templates, pick one, render it to a file, then verify. No manual file creation needed.",
                "steps": [
                    {
                        "step": 1,
                        "action": "Browse available templates",
                        "endpoint": "GET /api/templates",
                        "expected": "8 templates returned. Pick an ID that matches your need (e.g. fastapi_endpoint for a new API route).",
                        "notes": "Use the response to decide which template suits your task.",
                    },
                    {
                        "step": 2,
                        "action": "Preview the template",
                        "endpoint": "GET /api/templates/fastapi_endpoint",
                        "expected": "Full template code with {placeholders} and default_params. Copy the defaults or customise them for your use case.",
                        "notes": "The preview shows exactly what will be rendered. Adjust the params dict accordingly.",
                    },
                    {
                        "step": 3,
                        "action": "Render the template to a file",
                        "endpoint": "POST /api/templates/render",
                        "body": '{"context_id":"ctx_abc123","template_id":"fastapi_endpoint","params":{"method":"get","path":"/items","handler_name":"list_items","params":"","description":"List all items","response":"[{\\"id\\": 1, \\"name\\": \\"item\\"}]"},"target_path":"/var/www/app/routes/items.py"}',
                        "expected": "success: true. The response includes the rendered code — verify it looks correct. The file is saved on the remote host.",
                        "notes": "Set auto_commit=true to automatically create a git commit with the new file.",
                    },
                    {
                        "step": 4,
                        "action": "Verify the file was created",
                        "endpoint": "POST /api/file/read",
                        "body": '{"session_id":"ses_abc123","path":"/var/www/app/routes/items.py"}',
                        "expected": "File content matches what was rendered. The new route is ready to be used.",
                        "notes": "You can also run a syntax check: POST /api/ssh/execute with 'python -m py_compile /var/www/app/routes/items.py'.",
                    },
                ],
                "summary": "4 API calls: browse → preview → render → verify. No file system navigation, no manual typing — the agent goes from nothing to a working file in seconds.",
            },
            "tips": [
                "Browse /api/templates before rendering — you might find a template that saves more time than writing from scratch.",
                "Use /api/templates/{id} to preview the template and see the {placeholder} variables before you render.",
                "Scaffold creates both a class file and test file in one call — use include_test=false if you only want the class.",
                "Code generate works without an SSH session — ideal for preparation or offline planning.",
                "Before scaffolding, check if files exist via POST /api/file/read or GET /api/project/tree to avoid accidental overwrites.",
                "Use git/backup before scaffold/render to create a restore point, especially in production repositories.",
            ],
        },
        "observability_workflow": {
            "title": "Observability — analytics, logs, metrics, webhooks",
            "overview": "Understand system state, inspect logs, monitor performance, and integrate with external services via webhooks. All endpoints in this section require the master API key unless noted otherwise.",
            "prerequisite": "Logs and analytics need an active SSH session (session_id) to run remote commands. Metrics and webhook listing work standalone.",
            "sections": [
                {
                    "name": "project_analytics",
                    "title": "Project analytics",
                    "overview": "Comprehensive project metrics: file counts by extension, code statistics (LOC, classes, functions), git history (commits, branches, contributors), test coverage, and dependency status. All data is collected via SSH commands on the remote host.",
                    "endpoints": [
                        {"endpoint": "POST /api/analytics", "scope": "master_key", "description": "Analyze a project on the remote host. Input: {session_id, path}. Returns files (total, extensions), code (LOC, classes, functions), git (is_git_repo, commits, branches, contributors), tests (test_files, total_tests, has_tests), dependencies (requirements_count, has_pyproject, outdated_packages)."},
                    ],
                },
                {
                    "name": "metrics_and_circuit_breaker",
                    "title": "Prometheus metrics & circuit breaker",
                    "overview": "Prometheus-format metrics endpoint measuring all gateways operations: request count/latency, SSH connections, job queue depth, circuit breaker states, file operations, event hook deliveries. Circuit breaker stats give per-host state (closed/open/half-open) with failure/success counts.",
                    "endpoints": [
                        {"endpoint": "GET /metrics", "scope": "master_key", "description": "Prometheus exposition format (text/plain). Includes counters for requests, SSH connections, commands, jobs, file ops, hook deliveries; histograms for latency; gauges for queue depth, circuit breaker state, active locks."},
                        {"endpoint": "GET /api/circuit-breaker/stats", "scope": "master_key", "description": "Per-host circuit breaker state. Returns dict keyed by host with state (closed/open/half_open), failure_count, success_count, last_failure_time, half_open_calls."},
                    ],
                },
                {
                    "name": "logs",
                    "title": "Logs — journald & Docker",
                    "overview": "Read systemd journal logs or Docker container logs from the remote host. Both support filtering by recency, count, and severity/container. Returns raw stdout from the SSH command.",
                    "endpoints": [
                        {"endpoint": "GET /api/logs/journal", "scope": "master_key", "description": "Read systemd journal logs. Query params: session_id (required), unit (systemd unit name, e.g. nginx/sshd), lines (1–5000, default 50), priority (emerg/alert/crit/err/warning/notice/info/debug), since (time range: '1h', '30m', ISO date)."},
                        {"endpoint": "GET /api/logs/docker", "scope": "master_key", "description": "Read Docker container logs. Query params: session_id (required), container (name or ID, required), lines (1–5000, default 100), since (time range: '5m', '1h'), timestamps (boolean, default false)."},
                    ],
                    "when_to_use": {
                        "journal": "system-level logs: service failures, SSH auth attempts, system errors. Use unit filter to narrow to a specific service.",
                        "docker": "Container-level logs: application output, startup errors, runtime warnings. Use since filter to focus on recent activity.",
                    },
                },
                {
                    "name": "event_hooks",
                    "title": "Event hooks — event-driven notifications",
                    "overview": "Event hooks are PostgreSQL-backed webhook endpoints that receive session and command lifecycle events (session.connected, session.disconnected, command.started, command.completed, command.failed). Deliveries use an outbox pattern with automatic retry, HMAC signing, and dead-letter queue for failed deliveries.",
                    "endpoints": [
                        {"endpoint": "GET /api/event-hooks", "scope": "master_key", "description": "List all registered event hooks. Returns id, url, events (list of subscribed event types), session_id filter, include_output, is_active, created_at, updated_at."},
                        {"endpoint": "POST /api/event-hooks", "scope": "master_key", "description": "Register a new event hook. Input: {url (HTTPS), events (array, e.g. ['command.completed', 'session.connected']), session_id?, headers?, secret?, include_output?}. The URL receives POST requests with JSON payloads on matching events."},
                        {"endpoint": "PATCH /api/event-hooks/{hook_id}", "scope": "master_key", "description": "Update an event hook. All fields optional: url, events, session_id, headers, secret, include_output, is_active."},
                        {"endpoint": "DELETE /api/event-hooks/{hook_id}", "scope": "master_key", "description": "Delete an event hook. Returns {deleted: true}."},
                    ],
                    "event_types": [
                        {"type": "session.connected", "description": "SSH session established"},
                        {"type": "session.disconnected", "description": "SSH session ended"},
                        {"type": "command.started", "description": "Command execution began"},
                        {"type": "command.completed", "description": "Command exited successfully (exit_code=0)"},
                        {"type": "command.failed", "description": "Command exited with non-zero code"},
                    ],
                },
                {
                    "name": "ci_cd_webhooks",
                    "title": "CI/CD webhooks — auto-deployment",
                    "overview": "Webhooks managed by the WebhookManager. These are CI/CD deployment triggers (not event-driven hooks). A webhook stores a deploy command and target path; triggering it runs the deploy as a background job. Storage is in-memory (not persisted across restarts).",
                    "endpoints": [
                        {"endpoint": "GET /api/webhooks", "scope": "master_key", "description": "List all CI/CD webhooks. Returns id, name, webhook_type (github/gitea/generic), target_path, deploy_command, context_id, notify_url, enabled."},
                        {"endpoint": "POST /api/webhooks", "scope": "master_key", "description": "Create a new webhook. Input: {name, target_path, deploy_command, context_id, webhook_type?, secret?, notify_url?}. Returns the created webhook config."},
                        {"endpoint": "POST /api/webhooks/{webhook_id}/deploy", "scope": "master_key", "description": "Manually trigger deployment. Input: {session_id}. Runs 'cd target_path && deploy_command' as a background job. Returns job_id for tracking."},
                        {"endpoint": "GET /api/webhooks/{webhook_id}/deployments", "scope": "master_key", "description": "List deployment history for a webhook. Returns array with id, webhook_id, webhook_name, status (pending/running/success/failed), timestamp, payload."},
                        {"endpoint": "DELETE /api/webhooks/{webhook_id}", "scope": "master_key", "description": "Delete a webhook and its deployment history."},
                    ],
                },
            ],
            "examples": [
                {
                    "endpoint": "POST /api/analytics",
                    "title": "Analyse a project",
                    "description": "Get comprehensive metrics for a project on the remote host. Returns file stats, code stats, git info, test coverage, and dependency status in a single response.",
                    "body": '{"session_id":"ses_abc123","path":"/var/www/app"}',
                    "response": '{"project_path":"/var/www/app","files":{"total_files":156,"total_directories":12,"extensions":{".py":89,".js":34,".json":15,".yaml":8,".md":6,".css":4}},"code":{"python_lines_of_code":12450,"classes":34,"functions":187},"git":{"is_git_repo":true,"total_commits":342,"branches":3,"contributors":2,"last_commit":"2025-06-01"},"tests":{"test_files":15,"total_tests":317,"has_tests":true},"dependencies":{"requirements_count":45,"has_pyproject":true,"outdated_packages":3}}',
                    "notes": "Analytics runs find, grep, git log, and pytest --collect-only on the remote host. For large projects, allow 5–15s for collection. Outdated packages requires pip list --outdated.",
                },
                {
                    "endpoint": "GET /metrics",
                    "title": "View Prometheus metrics",
                    "description": "Returns all gateway metrics in Prometheus text format. Key metrics: HTTP requests total, request latency histogram, active SSH connections, job queue depth, circuit breaker states.",
                    "request": "GET /metrics",
                    "response_preview": "ssh_gateway_requests_total{method=\"POST\",endpoint=\"/api/ssh/connect\",status=\"200\"} 42\nssh_gateway_ssh_connections_active 3.0\nssh_gateway_queue_depth{queue=\"pending\"} 0\nssh_gateway_circuit_breaker_state{host=\"10.0.0.1\"} 0",
                    "notes": "Formatter: Prometheus exposition. Parse with promtool or any Prometheus client library. The /api/circuit-breaker/stats endpoint gives a friendlier JSON view of circuit breaker states.",
                },
                {
                    "endpoint": "GET /api/logs/journal",
                    "title": "Read systemd journal logs",
                    "description": "Fetch the last 20 lines of nginx service logs from the remote host. Use unit filter for specific services, priority for error-level filtering.",
                    "request": "GET /api/logs/journal?session_id=ses_abc123&unit=nginx&lines=20&priority=err",
                    "response": '{"stdout":"Jun 01 12:34:56 web-server nginx[1234]: 2025/06/01 12:34:56 [error] ... connect() failed (111: Connection refused)...","stderr":"","exit_code":0,"duration":0.45}',
                    "notes": "Uses journalctl --no-pager on the remote host. Supported priorities: emerg, alert, crit, err, warning, notice, info, debug. Since accepts relative ('1h', '30m') or absolute dates.",
                },
                {
                    "endpoint": "GET /api/logs/docker",
                    "title": "Read Docker container logs",
                    "description": "Fetch the last 30 lines of a container named 'web-app' with timestamps. Use container name or ID.",
                    "request": "GET /api/logs/docker?session_id=ses_abc123&container=web-app&lines=30&timestamps=true&since=5m",
                    "response": '{"stdout":"2025-06-01T12:34:56Z [INFO] Server started on port 8080\\n2025-06-01T12:35:10Z [WARN] Memory usage high: 85%","stderr":"","exit_code":0,"duration":0.32}',
                    "notes": "Uses docker logs on the remote host. Container must be running. Since supports duration suffixes (5m, 1h) or ISO timestamps.",
                },
                {
                    "endpoint": "POST /api/webhooks",
                    "title": "Create a CI/CD webhook",
                    "description": "Register a deployment webhook that runs a deploy command when triggered.",
                    "body": '{"name":"my-app-deploy","webhook_type":"generic","target_path":"/var/www/app","deploy_command":"git pull && pip install -r requirements.txt && systemctl restart app","context_id":"ctx_abc123"}',
                    "response": '{"id":"wh_a1b2c3d4","name":"my-app-deploy","webhook_type":"generic","target_path":"/var/www/app","deploy_command":"git pull && pip install -r requirements.txt && systemctl restart app","context_id":"ctx_abc123","notify_url":null,"enabled":true}',
                    "notes": "Webhooks are stored in memory (not persisted across restarts). Trigger deployment via POST /api/webhooks/{id}/deploy with {session_id}. The deploy runs as a background job.",
                },
                {
                    "endpoint": "POST /api/webhooks/{webhook_id}/deploy",
                    "title": "Trigger a deployment",
                    "description": "Manually trigger the deploy command for a webhook. Runs as a background job — track progress via the returned job_id.",
                    "body": '{"session_id":"ses_abc123"}',
                    "response": '{"status":"deploying","job_id":"job_xyz789","message":"Deploy started for my-app-deploy"}',
                    "notes": "The deploy runs via job_manager.create_job(). Check GET /api/jobs/{job_id}/result for completion. Deployment history is available at GET /api/webhooks/{id}/deployments.",
                },
            ],
            "full_scenario": {
                "title": "Diagnose a problem from metrics to fix",
                "overview": "Check system health, find the issue in logs, then take action. Six API calls from observation to resolution.",
                "steps": [
                    {
                        "step": 1,
                        "action": "Check system metrics",
                        "endpoint": "GET /metrics",
                        "expected": "Prometheus text with current request counts, active connections, queue depth. Look for elevated error rates or connection drops.",
                        "notes": "Parse metrics to identify anomalies — high latency, queued jobs, circuit breaker states.",
                    },
                    {
                        "step": 2,
                        "action": "Check circuit breaker stats for blocked hosts",
                        "endpoint": "GET /api/circuit-breaker/stats",
                        "expected": "Per-host breaker states. Any host in 'open' state means connections are blocked due to repeated failures.",
                        "notes": "Open breakers auto-recover after 60s. Half-open means recovery is being tested.",
                    },
                    {
                        "step": 3,
                        "action": "Read journal logs for the failing service",
                        "endpoint": "GET /api/logs/journal",
                        "request": "GET /api/logs/journal?session_id=ses_abc123&unit=nginx&lines=30&priority=err",
                        "expected": "Error-level journal entries for nginx. Look for connection refused, bind failures, or permission errors.",
                        "notes": "Use priority=err to filter out noise. Adjust lines and unit to narrow the search.",
                    },
                    {
                        "step": 4,
                        "action": "Run project analytics to assess scope",
                        "endpoint": "POST /api/analytics",
                        "body": '{"session_id":"ses_abc123","path":"/var/www/app"}',
                        "expected": "Project metrics: file counts, LOC, git state, test coverage. High outdated_packages count suggests dependency issues.",
                        "notes": "Combine analytics with logs to understand if the problem is code, config, or dependency related.",
                    },
                    {
                        "step": 5,
                        "action": "Create a webhook for automated deploy",
                        "endpoint": "POST /api/webhooks",
                        "body": '{"name":"fix-deploy","webhook_type":"generic","target_path":"/var/www/app","deploy_command":"git pull && pip install -r requirements.txt && systemctl restart app","context_id":"ctx_abc123"}',
                        "expected": "Webhook config with id. Save the id for future deploys.",
                        "notes": "Webhooks enable one-click deploys from the UI or automated CI/CD triggers.",
                    },
                    {
                        "step": 6,
                        "action": "Deploy the fix",
                        "endpoint": "POST /api/webhooks/{webhook_id}/deploy",
                        "body": '{"session_id":"ses_abc123"}',
                        "expected": "Deploy started as background job. job_id returned. Track via GET /api/jobs/{job_id}/result.",
                        "notes": "Monitor deployment progress. Check journal logs again to confirm the fix resolved the issue.",
                    },
                ],
                "summary": "6 API calls: metrics → circuit breaker → journal logs → analytics → create webhook → deploy. Full diagnostic-to-resolution pipeline without manual server access.",
            },
            "tips": [
                "Start with GET /metrics for a broad view, then drill down into specific logs or breaker stats.",
                "Use journal logs with priority=err when debugging — info-level logs are too verbose for most diagnostics.",
                "For Docker containers, add timestamps=true to correlate log events with system events.",
                "Webhooks are in-memory — recreate them after a server restart. Event hooks are persisted in PostgreSQL.",
                "Analytics runs commands on the remote host — it may take 5–15s. The response size is typically 1–3 KB.",
                "Circuit breaker stats are useful for detecting flaky target hosts. A host flapping open/closed suggests intermittent network issues.",
            ],
        },
        "recovery_workflow": {
            "title": "Recovery — backups, snapshots & known hosts",
            "overview": "Create restore points before making changes and roll back when something goes wrong. Two systems exist: git stash backups (lightweight, per-context) and snapshots (standalone, per-project). All endpoints require master API key.",
            "prerequisite": "Backups need a context_id (created via POST /api/context/create). Snapshots need a context_id with a valid session. Known hosts are managed automatically by the gateway but can be inspected.",
            "sections": [
                {
                    "name": "recovery_backups",
                    "title": "Git stash backups — quick rollback",
                    "overview": "Backups use git stash under the hood. They save working tree changes to a named stash entry. Restoring pops the most recent stash. Backups are per-context (bound to a working directory). Each backup has a name and a stash index. List available backups to see what's saved.",
                    "when_to_use": "Before any risky operation: bulk edit, template render, scaffold, global replace. Create a backup first, do the operation, verify, and if something went wrong restore.",
                    "endpoints": [
                        {"endpoint": "POST /api/recovery/backup", "scope": "master_key", "description": "Create a named backup. Input: {context_id, name? (default auto_backup)}. Stashes current working tree changes as a git stash entry. Safe to call multiple times — each call creates a new entry."},
                        {"endpoint": "GET /api/recovery/backups", "scope": "master_key", "description": "List all stash backups for a context. Query: context_id. Returns array with id (stash index like stash@{0}), name (stash message), created_at (server timestamp)."},
                        {"endpoint": "POST /api/recovery/restore", "scope": "master_key", "description": "Restore the most recent stash backup. Input: {context_id, backup_id?}. Overwrites current working tree with stashed files. This is destructive — current uncommitted changes are lost."},
                    ],
                    "recovery_vs_git": {
                        "note": "POST /api/recovery/backup and /api/recovery/restore are aliases for POST /api/git/backup and /api/git/restore with request-body inputs instead of query params. The recovery variants are preferred for agent workflows because they use cleaner JSON bodies.",
                    },
                },
                {
                    "name": "snapshots",
                    "title": "Snapshots — standalone project state",
                    "overview": "Snapshots capture the state of specific files in a project directory. Unlike backups (which use git stash), snapshots are managed by the SnapshotManager and store file contents independently of git. They can include a description, file list, and reference the git commit before the snapshot was taken. Snapshots persist across context deletion.",
                    "when_to_use": "When you need a named restore point with a description and file inventory. Snapshots are better than backups when you want to know exactly which files were saved and why.",
                    "endpoints": [
                        {"endpoint": "POST /api/snapshots", "scope": "master_key", "description": "Create a snapshot. Input: {context_id, name, description?}. Returns snapshot_id and message with file count. Creates a point-in-time copy of all files in the context's working directory."},
                        {"endpoint": "GET /api/snapshots", "scope": "master_key", "description": "List snapshots for a context. Query: context_id. Returns array with id, name, description, created_at, files (filenames), git_commit_before, size_bytes."},
                        {"endpoint": "POST /api/snapshots/restore", "scope": "master_key", "description": "Restore project state from a snapshot. Input: {context_id, snapshot_id}. Restores all files in the snapshot to their state at capture time. Returns success and restored files count."},
                        {"endpoint": "DELETE /api/snapshots/{snapshot_id}", "scope": "master_key", "description": "Delete a snapshot. Query: context_id. Removes the snapshot from storage. Cannot be undone."},
                    ],
                },
                {
                    "name": "known_hosts",
                    "title": "Known hosts — SSH host key management",
                    "overview": "The gateway tracks SSH host keys for connected servers. Known hosts are stored either in a file (OpenSSH format) or PostgreSQL depending on configuration. The system auto-accepts or rejects unknown hosts based on the SSH_STRICT_HOST_KEY_CHECKING setting.",
                    "endpoints": [
                        {"endpoint": "GET /api/known-hosts", "scope": "master_key", "description": "List all known hosts. Returns host, port, key_type, fingerprint for each entry. Useful for auditing which servers the gateway has connected to."},
                        {"endpoint": "DELETE /api/known-hosts/{host}", "scope": "master_key", "description": "Remove a specific host from known hosts. Path: host. Useful after a host key rotation to force re-acceptance on next connection."},
                        {"endpoint": "DELETE /api/known-hosts", "scope": "master_key", "description": "Clear all known hosts. Resets the known hosts store to empty. Next connections to any host will be treated as unknown."},
                    ],
                },
            ],
            "examples": [
                {
                    "endpoint": "POST /api/recovery/backup",
                    "title": "Create a named backup",
                    "description": "Stash current changes before a risky operation. Name helps identify the backup in the list. Safe to call even if there are no changes — creates an empty stash entry.",
                    "body": '{"context_id":"ctx_abc123","name":"before_bulk_edit_configs"}',
                    "response": '{"success":true,"message":"Backup \'before_bulk_edit_configs\' created","backup_id":"before_bulk_edit_configs"}',
                    "notes": "Backups use git stash. If there are no uncommitted changes, the stash is empty but still created. List backups to verify.",
                },
                {
                    "endpoint": "GET /api/recovery/backups",
                    "title": "List available backups",
                    "description": "See all stash backups for a context. Each backup has an id (stash@{N}) and name. The created_at is the server timestamp when the list was fetched.",
                    "request": "GET /api/recovery/backups?context_id=ctx_abc123",
                    "response": '{"backups":[{"id":"stash@{0}","name":"before_bulk_edit_configs","created_at":1717450000.0,"files_changed":[]}],"count":1}',
                    "notes": "If no backups exist, count=0 and backups=[]. Create one with POST /api/recovery/backup before any risky operation.",
                },
                {
                    "endpoint": "POST /api/recovery/restore",
                    "title": "Restore from backup",
                    "description": "Pop the most recent stash entry and restore working tree files. WARNING: overwrites current uncommitted changes. Always create a fresh backup before restoring.",
                    "body": '{"context_id":"ctx_abc123"}',
                    "response": '{"success":true,"message":"Backup restored successfully","backup_id":null,"restored_files":["all_stashed_files"]}',
                    "notes": "Restore is destructive — current working tree changes are overwritten. Use backup_id to restore a specific backup (if supported). Ensure a backup exists by listing first.",
                },
                {
                    "endpoint": "GET /api/snapshots",
                    "title": "List snapshots for a context",
                    "description": "See all snapshots with their metadata. Each snapshot shows name, description, file list, git commit before capture, and size.",
                    "request": "GET /api/snapshots?context_id=ctx_abc123",
                    "response": '{"snapshots":[{"id":"snap_abc123","name":"pre-refactor","context_id":"ctx_abc123","created_at":1717450000.0,"files":["src/main.py","src/config.py","tests/test_main.py"],"description":"Before big refactor","git_commit_before":"abc123def","size_bytes":40960}],"count":1}',
                    "notes": "If no snapshots exist, count=0. Create one with POST /api/snapshots. Each snapshot captures all files in the context's working directory.",
                },
                {
                    "endpoint": "POST /api/snapshots/restore",
                    "title": "Restore from snapshot",
                    "description": "Restore all files from a snapshot. The response shows how many files were restored. WARNING: overwrites current versions of the same files.",
                    "body": '{"context_id":"ctx_abc123","snapshot_id":"snap_abc123"}',
                    "response": '{"success":true,"message":"Restored 3 of 3 files","snapshot_id":"snap_abc123","restored_files":[]}',
                    "notes": "Restore is file-level: files in the snapshot are written back. Files not in the snapshot are untouched. Create a backup first if you want a rollback point before restoring.",
                    "warning": "Overwrites current file contents. Use POST /api/recovery/backup first to create a recovery point before restoring a snapshot.",
                },
                {
                    "endpoint": "GET /api/known-hosts",
                    "title": "List known SSH hosts",
                    "description": "See all SSH host keys the gateway has learned. Each entry shows host, port, key type, and fingerprint.",
                    "request": "GET /api/known-hosts",
                    "response": '{"hosts":[{"host":"192.168.1.100","port":22,"key_type":"ssh-ed25519","fingerprint":"SHA256:abc123..."},{"host":"10.0.0.1","port":22,"key_type":"ssh-rsa","fingerprint":"SHA256:def456..."}]}',
                    "notes": "Hosts are added automatically on successful SSH connection. Clear with DELETE /api/known-hosts if host keys have changed and you need to re-accept them.",
                },
            ],
            "full_scenario": {
                "title": "Safe edit cycle: backup → edit → snapshot → restore",
                "overview": "Before making changes, create a recovery point. After changes are verified, capture a snapshot. If something goes wrong later, restore from either the backup or snapshot.",
                "steps": [
                    {
                        "step": 1,
                        "action": "Create a backup before editing",
                        "endpoint": "POST /api/recovery/backup",
                        "body": '{"context_id":"ctx_abc123","name":"before_config_edit"}',
                        "expected": "Backup created. Working tree changes are stashed. If the edit goes wrong, this backup can restore the original state.",
                        "notes": "Always backup before any destructive or risky operation. The name helps identify the backup later.",
                    },
                    {
                        "step": 2,
                        "action": "Make your edits",
                        "endpoint": "PATCH /api/context/file/edit",
                        "body": '{"context_id":"ctx_abc123","path":"/var/www/app/config.py","operations":[{"type":"replace","old":"DEBUG = False","new":"DEBUG = True"}]}',
                        "expected": "File edited. Changes are visible in the working tree.",
                        "notes": "Edit within a context so the snapshot captures the full project state including the edit.",
                    },
                    {
                        "step": 3,
                        "action": "Verify the changes work",
                        "endpoint": "POST /api/ssh/execute",
                        "body": '{"session_id":"ses_abc123","command":"python -c \\"import config; print(config.DEBUG)\\""}',
                        "expected": "Output shows DEBUG = True. The edit is working.",
                        "notes": "Verify before snapshotting. If the edit broke something, restore from the backup instead.",
                    },
                    {
                        "step": 4,
                        "action": "Capture a snapshot of the verified state",
                        "endpoint": "POST /api/snapshots",
                        "body": '{"context_id":"ctx_abc123","name":"after_debug_enable","description":"Enabled debug mode in config"}',
                        "expected": "Snapshot created. The verified state is now saved independently of git. Can be restored at any time.",
                        "notes": "Snapshots are standalone — they don't depend on git history. Use descriptive names and descriptions for easy identification.",
                    },
                    {
                        "step": 5,
                        "action": "List available recovery points",
                        "endpoint": "GET /api/recovery/backups + GET /api/snapshots",
                        "expected": "Both backup and snapshot appear in their respective lists. The backup references the pre-edit state, the snapshot references the post-edit verified state.",
                        "notes": "Use backups for quick rollback during active editing. Use snapshots for permanent checkpoints that survive beyond the current session.",
                    },
                ],
                "summary": "5 steps: backup → edit → verify → snapshot → list. Full recovery cycle: a restore point before the change and a verified checkpoint after.",
            },
            "tips": [
                "Always create a backup (POST /api/recovery/backup) before bulk edits, template renders, or scaffolding.",
                "Use descriptive backup names like 'before_refactor_auth' so you can identify them in the list.",
                "Snapshots are better for permanent checkpoints — they survive context deletion and include file lists and descriptions.",
                "After restoring a snapshot, create a fresh backup if you plan to make more changes (the restore overwrites the working tree).",
                "Known hosts accumulate over time. Periodically audit GET /api/known-hosts and remove stale entries.",
                "If a host key changes unexpectedly (man-in-the-middle warning), use DELETE /api/known-hosts/{host} to clear it, then reconnect.",
                "Empty backup list means no recovery points exist. Create one before your next risky operation.",
            ],
        },
        "ssh_trust_workflow": {
            "title": "SSH Trust Flow — safe host key verification",
            "overview": "Before establishing an SSH connection, the gateway checks whether the remote host's key is known. This section documents the three trust states, how the UI behaves in each, and how to manage known hosts manually.",
            "important": "The preflight endpoint GET /api/known-hosts/check returns only 'known' or 'unknown'. It does NOT attempt to detect 'changed' — that requires a real SSH handshake. 'changed' is only reported after a failed Connect attempt.",
            "sections": [
                {
                    "name": "trust_states",
                    "title": "Three trust states",
                    "overview": "The gateway maintains a store of SSH host keys (file or PostgreSQL). Each (host,port) pair has exactly one state at any time.",
                    "states": [
                        {"state": "known", "meaning": "The host:port has been seen before and its key matches what's stored. No action needed.", "ui": "Green: 'Trusted'. Connect works normally.", "icon": "🟢"},
                        {"state": "unknown", "meaning": "The host:port has never been seen before. This is normal for first connections.", "ui": "Yellow: 'Host not in known-hosts yet'. Connect still works — the key will be stored on success.", "icon": "🟡"},
                        {"state": "changed", "meaning": "The host presented a key that differs from what's stored. This is a potential MITM attack and requires manual intervention.", "ui": "Red: 'Host key CHANGED'. Connect is blocked until the entry is deleted via Recovery > Known Hosts.", "icon": "🔴"},
                    ],
                    "note": "'changed' is never returned by the preflight endpoint. It only appears after a real SSH connection attempt fails with a key mismatch. Once triggered, the UI remembers the state until the entry is deleted.",
                },
                {
                    "name": "host_key_store",
                    "title": "How known-hosts work",
                    "overview": "The store is configured via KNOWN_HOSTS_STORE env var ('file', 'postgres', or empty/null for no-op). On first connection to a host, the key is automatically added. On subsequent connections, the key is verified against the stored copy.",
                    "settings": [
                        {"setting": "KNOWN_HOSTS_STORE='file'", "description": "Uses an OpenSSH-format file (default: known_hosts in working dir). Paramiko's HostKeys handles read/write."},
                        {"setting": "KNOWN_HOSTS_STORE='postgres'", "description": "Uses the ssh_host_keys table in PostgreSQL. Supports concurrent access across gateway instances."},
                        {"setting": "SSH_STRICT_HOST_KEY_CHECKING=true", "description": "Unknown hosts are rejected instead of auto-accepted. Combine with manual key management via known-hosts API."},
                    ],
                },
                {
                    "name": "preflight_check",
                    "title": "Check Trust — what it does and doesn't do",
                    "overview": "The Check Trust button in the UI calls GET /api/known-hosts/check?host=X&port=Y. It looks up the (host,port) pair in the store and returns 'known' if found, 'unknown' if not.",
                    "what_it_does": [
                        "Returns 'known' — host:port exists in the store. The key was previously trusted.",
                        "Returns 'unknown' — host:port not found. First connection or entry was deleted.",
                    ],
                    "what_it_does_not_do": [
                        "It does NOT attempt to connect to the host.",
                        "It does NOT compare keys (no key is available before connection).",
                        "It does NOT return 'changed'. That state is impossible to determine without an active SSH handshake.",
                    ],
                    "recommendation": "Always use Check Trust before connecting to a sensitive host. If unknown, verify the host fingerprint out-of-band before proceeding.",
                },
                {
                    "name": "recovery_actions",
                    "title": "Known-hosts management (Recovery panel)",
                    "overview": "The Recovery panel has a Known Hosts sub-block with three actions: View (show fingerprint), Delete (remove one host:port entry), Clear All (remove all entries). All actions require master API key.",
                    "endpoints": [
                        {"endpoint": "GET /api/known-hosts", "scope": "master_key", "description": "List all known hosts. Returns host, port, key_type, fingerprint for each entry."},
                        {"endpoint": "GET /api/known-hosts/{host}?port=Y", "scope": "master_key", "description": "Lookup single host:port entry. Returns full record or 404. Lookup is by (host,port) pair."},
                        {"endpoint": "GET /api/known-hosts/check?host=X&port=Y", "scope": "master_key", "description": "Preflight trust check. Returns 'known' or 'unknown'. Never 'changed'."},
                        {"endpoint": "DELETE /api/known-hosts/{host}?port=Y", "scope": "master_key", "description": "Delete specific host:port entry. Port defaults to 22. Use after host key rotation."},
                        {"endpoint": "DELETE /api/known-hosts", "scope": "master_key", "description": "Clear all known hosts. Resets the store. All hosts become unknown on next connect."},
                    ],
                },
            ],
            "examples": [
                {
                    "endpoint": "GET /api/known-hosts/check",
                    "title": "Preflight: check if a host is trusted",
                    "description": "Before connecting, check if the host:port has a stored key. This is the 'Check Trust' action.",
                    "request": "GET /api/known-hosts/check?host=192.168.1.100&port=22",
                    "response": '{"status":"known","host":"192.168.1.100","port":22}',
                    "notes": "Returns 'known' if the (host,port) pair exists in the store, 'unknown' if not. Never returns 'changed'.",
                },
                {
                    "endpoint": "GET /api/known-hosts/{host}",
                    "title": "Lookup a specific host entry by host:port",
                    "description": "Get full details of a stored host key. Returns 404 if not found.",
                    "request": "GET /api/known-hosts/192.168.1.100?port=22",
                    "response": '{"host":"192.168.1.100","port":22,"key_type":"ssh-ed25519","fingerprint":"SHA256:abc123..."}',
                    "notes": "Lookup is by (host,port) pair. Port defaults to 22. Use exact port your connection uses.",
                },
                {
                    "endpoint": "DELETE /api/known-hosts/{host}",
                    "title": "Delete a specific host entry",
                    "description": "Remove a host:port entry. Use this after a legitimate host key rotation.",
                    "request": "DELETE /api/known-hosts/192.168.1.100?port=22",
                    "response": '{"deleted":1,"host":"192.168.1.100","port":22}',
                    "notes": "Deletes by (host,port) pair. If you have entries for the same host on different ports, only the matching one is removed.",
                },
                {
                    "endpoint": "DELETE /api/known-hosts",
                    "title": "Clear all known hosts",
                    "description": "Remove all entries from the store. All hosts become unknown on next connection.",
                    "request": "DELETE /api/known-hosts",
                    "response": '{"deleted":5}',
                    "notes": "This is destructive and irreversible. After this, every host will be treated as 'unknown' on first connect.",
                },
            ],
            "full_scenario": {
                "title": "End-to-end: Unknown host → Connect → Trust → Changed key → Block → Recovery",
                "overview": "A complete cycle from first connection through key rotation and recovery.",
                "steps": [
                    {
                        "step": 1,
                        "action": "Preflight: check trust before connecting",
                        "endpoint": "GET /api/known-hosts/check?host=10.0.0.5&port=2222",
                        "expected": '{"status":"unknown"}',
                        "notes": "Host is unknown. This is expected for first connection. Verify fingerprint out-of-band if this is a production server.",
                    },
                    {
                        "step": 2,
                        "action": "Connect — first time, key is stored",
                        "endpoint": "POST /api/ssh/connect",
                        "body": '{"host":"10.0.0.5","port":2222,"username":"deploy","password":"***"}',
                        "expected": "Connection successful. Host key is automatically stored in the store.",
                        "notes": "On success, the gateway stores the host key via store(). Next check will return 'known'.",
                    },
                    {
                        "step": 3,
                        "action": "Preflight: confirm host is now trusted",
                        "endpoint": "GET /api/known-hosts/check?host=10.0.0.5&port=2222",
                        "expected": '{"status":"known"}',
                        "notes": "Host is now trusted. The UI shows green indicator.",
                    },
                    {
                        "step": 4,
                        "action": "Connect fails — key changed",
                        "endpoint": "POST /api/ssh/connect",
                        "body": '{"host":"10.0.0.5","port":2222,"username":"deploy","password":"***"}',
                        "expected": "Connection fails with 'Host key changed — possible MITM attack'.",
                        "notes": "The gateway's KnownHostsPolicy detected a key mismatch. The connection is rejected with SSHException.",
                    },
                    {
                        "step": 5,
                        "action": "UI blocks Connect — shows red warning",
                        "endpoint": "(UI only)",
                        "expected": "Connect button is disabled. Red banner: 'Host key CHANGED — possible MITM attack. Remove entry via Recovery > Known Hosts'.",
                        "notes": "The UI detected 'changed' from the error message. User must resolve before retrying.",
                    },
                    {
                        "step": 6,
                        "action": "User investigates — view the stored key fingerprint",
                        "endpoint": "GET /api/known-hosts/10.0.0.5?port=2222",
                        "expected": '{"host":"10.0.0.5","port":2222,"key_type":"ssh-ed25519","fingerprint":"SHA256:old_fingerprint..."}',
                        "notes": "The stored fingerprint does not match what the server is now presenting. Admin should verify the new fingerprint out-of-band.",
                    },
                    {
                        "step": 7,
                        "action": "Admin confirms the key change is legitimate — deletes stale entry",
                        "endpoint": "DELETE /api/known-hosts/10.0.0.5?port=2222",
                        "expected": '{"deleted":1}',
                        "notes": "Entry removed. Next connection attempt will treat the host as 'unknown' and store the new key.",
                    },
                    {
                        "step": 8,
                        "action": "Reconnect — new key is stored",
                        "endpoint": "POST /api/ssh/connect",
                        "body": '{"host":"10.0.0.5","port":2222,"username":"deploy","password":"***"}',
                        "expected": "Connection successful. New key is stored. Trust restored.",
                        "notes": "The cycle is complete: unknown → connect → trust → changed → delete → reconnect → trust.",
                    },
                ],
                "summary": "8 steps: preflight unknown → connect (store) → confirm known → changed detected → blocked → inspect → delete → reconnect. Full trust lifecycle with safe recovery.",
            },
            "tips": [
                "Always run Check Trust before connecting to a production server. 'unknown' is safe but worth verifying.",
                "If a host key changed unexpectedly, do NOT delete the entry blindly. Verify the new fingerprint out-of-band first.",
                "The preflight endpoint never returns 'changed'. Changed is only detected during an actual SSH handshake.",
                "Delete by (host,port) pair, not by host alone. A server on port 2222 and the same server on port 22 have separate entries.",
                "Use Clear All sparingly — it removes every stored key and every host becomes 'unknown'.",
                "Known hosts are stored per gateway instance. If you run multiple gateways, they share the store only if using KNOWN_HOSTS_STORE=postgres.",
                "Check Trust requires the host and port. If connecting through a jump host, check the jump host, not the target.",
                "The Recovery panel's Known Hosts sub-block shows the action log in the terminal. Use View to confirm a fingerprint before deleting.",
            ],
        },
        "public_endpoints": public_endpoints,
        "examples": examples,
        "endpoints": groups,
    }


def _clean_param(name: str, location: str, schema: dict, required: bool, description: str) -> dict:
    ptype = schema.get("type", "string")
    if not ptype and "$ref" in schema:
        ptype = schema["$ref"].split("/")[-1]
    if isinstance(ptype, list):
        ptype = ptype[0] if ptype else "string"
    p = {"name": name, "in": location, "type": ptype, "required": required}
    desc = (description or "").strip().split(".")[0].strip()
    if desc:
        p["desc"] = desc
    return p


@router.get("/metrics", tags=["system"], response_class=PlainTextResponse)
async def prometheus_metrics(_identity: AuthIdentity = Depends(require_master_key)):
    """Prometheus metrics endpoint."""
    return Response(content=metrics.get_metrics(), media_type="text/plain")


@router.get("/api/sdk/download", tags=["system"], response_class=PlainTextResponse)
async def download_sdk(_identity: AuthIdentity = Depends(require_master_key)):
    """Download Python SDK.

    Note: auth is handled by the global middleware.
    """
    sdk_path = PROJECT_ROOT / "sdk" / "ssh_gateway.py"
    if not sdk_path.exists():
        raise HTTPException(status_code=404, detail=_err(404, "SDK not found"))
    content = sdk_path.read_text()
    return Response(
        content=content,
        media_type="text/x-python",
        headers={
            "Content-Disposition": "attachment; filename=ssh_gateway.py"
        }
    )


@router.get("/api/circuit-breaker/stats", tags=["system"])
async def circuit_breaker_stats(_identity: AuthIdentity = Depends(require_master_key)):
    """Get circuit breaker statistics."""
    return await _state.circuit_breakers.get_all_stats()


@router.get("/", tags=["system"], response_class=HTMLResponse)
async def root():
    """Serve the web terminal UI.

    Protected by global auth middleware — requires a valid X-API-Key header
    (master key or agent token) when API auth is enabled.

    See GET /api/help for the REST API reference.
    """
    return FileResponse("app/static/index.html")


# ---------------------------------------------------------------------------
# Server Management
# ---------------------------------------------------------------------------


@router.get("/api/servers", tags=["servers"], response_model=ServerListResponse)
async def list_servers(_identity: AuthIdentity = Depends(require_master_key)):
    """List all configured servers."""
    servers = _state.server_manager.list_servers()
    return ServerListResponse(
        servers=[ServerInfo(**_state.server_manager.to_dict(s)) for s in servers],
        count=len(servers),
    )


@router.post("/api/servers", tags=["servers"])
async def add_server(req: AddServerRequest, _identity: AuthIdentity = Depends(require_master_key)):
    """Add a new server."""
    existing = _state.server_manager.get_server(req.id)
    if existing:
        raise HTTPException(status_code=409, detail=_err(409, f"Server with ID '{req.id}' already exists"))
    
    server = _state.server_manager.add_server(
        server_id=req.id,
        name=req.name,
        host=req.host,
        port=req.port,
        username=req.username,
        description=req.description,
        tags=req.tags,
    )
    return _state.server_manager.to_dict(server)


@router.delete("/api/servers/{server_id}", tags=["servers"])
async def delete_server(server_id: str, _identity: AuthIdentity = Depends(require_master_key)):
    """Remove a server."""
    if not _state.server_manager.get_server(server_id):
        raise HTTPException(status_code=404, detail=_err(404, f"Server {server_id} not found"))
    _state.server_manager.remove_server(server_id)
    return {"status": "removed", "server_id": server_id}


@router.post("/api/servers/{server_id}/connect", tags=["servers"], response_model=ServerConnectResponse)
async def connect_server(server_id: str, req: ConnectServerRequest, _identity: AuthIdentity = Depends(require_master_key)):
    """Connect to a server and return session."""
    server = _state.server_manager.get_server(server_id)
    if not server:
        raise HTTPException(status_code=404, detail=_err(404, f"Server {server_id} not found"))
    
    try:
        validate_target_host(
            server.host,
            settings.allowed_target_cidrs,
            settings.denied_target_cidrs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=_err(403, str(exc))) from exc

    try:
        _password = req.password.get_secret_value() if req.password else None
        _private_key = req.private_key.get_secret_value() if req.private_key else None
        session_id = await _state.manager.create_session(
            host=server.host,
            port=server.port,
            username=server.username,
            password=_password,
            private_key=_private_key,
        )
        
        _state.server_manager.update_server_status(
            server_id,
            ServerStatus.ONLINE,
            session_id=session_id,
        )
        
        return ServerConnectResponse(
            server_id=server_id,
            session_id=session_id,
            status="connected",
            message=f"Connected to {server.name}",
        )
    except Exception as exc:
        _state.server_manager.update_server_status(server_id, ServerStatus.ERROR)
        raise HTTPException(status_code=502, detail=_err(502, f"Connection failed: {exc}")) from exc


# ---------------------------------------------------------------------------
# Snapshot System
# ---------------------------------------------------------------------------


@router.post("/api/snapshots", tags=["snapshots"], response_model=SnapshotActionResponse)
async def create_snapshot(req: CreateSnapshotRequest, _identity: AuthIdentity = Depends(require_master_key)):
    """Create a snapshot of current project state."""
    ctx = await _state.context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))
    
    try:
        snapshot = await _state.snapshot_manager.create_snapshot(
            session_id=ctx.session_id,
            context_id=req.context_id,
            name=req.name,
            description=req.description,
        )
        
        return SnapshotActionResponse(
            success=True,
            message=f"Snapshot '{snapshot.name}' created with {len(snapshot.files)} files",
            snapshot_id=snapshot.id,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_err(500, f"Snapshot creation failed: {exc}")) from exc


@router.get("/api/snapshots", tags=["snapshots"])
async def list_snapshots(context_id: str, _identity: AuthIdentity = Depends(require_master_key)):
    """List all snapshots for context."""
    ctx = await _state.context_manager.get_context(context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))
    
    snapshots = await _state.snapshot_manager.list_snapshots(ctx.session_id, context_id)
    
    return SnapshotListResponse(
        snapshots=[
            SnapshotInfo(
                id=s.id,
                name=s.name,
                context_id=s.context_id,
                created_at=s.created_at,
                files=s.files,
                description=s.description,
                git_commit_before=s.git_commit_before,
                size_bytes=s.size_bytes,
            )
            for s in snapshots
        ],
        count=len(snapshots),
    )


@router.post("/api/snapshots/restore", tags=["snapshots"], response_model=SnapshotActionResponse)
async def restore_snapshot(req: RestoreSnapshotRequest, _identity: AuthIdentity = Depends(require_master_key)):
    """Restore project from snapshot."""
    ctx = await _state.context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))
    
    try:
        result = await _state.snapshot_manager.restore_snapshot(
            session_id=ctx.session_id,
            context_id=req.context_id,
            snapshot_id=req.snapshot_id,
        )
        
        return SnapshotActionResponse(
            success=result["success"],
            message=f"Restored {result['restored_files']} of {result['total_files']} files",
            snapshot_id=req.snapshot_id,
            restored_files=result["restored_files"],
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_err(500, f"Restore failed: {exc}")) from exc


@router.delete("/api/snapshots/{snapshot_id}", tags=["snapshots"])
async def delete_snapshot(snapshot_id: str, context_id: str, _identity: AuthIdentity = Depends(require_master_key)):
    """Delete a snapshot."""
    ctx = await _state.context_manager.get_context(context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))
    
    success = await _state.snapshot_manager.delete_snapshot(
        session_id=ctx.session_id,
        context_id=context_id,
        snapshot_id=snapshot_id,
    )
    
    return {"status": "deleted" if success else "not_found", "snapshot_id": snapshot_id}


# ---------------------------------------------------------------------------
# CI/CD Webhooks
# ---------------------------------------------------------------------------


@router.post("/api/webhooks", tags=["webhooks"])
async def create_webhook(req: CreateWebhookRequest, _identity: AuthIdentity = Depends(require_master_key)):
    """Create a new webhook for auto-deployment."""
    config = _state.webhook_manager.add_webhook(
        name=req.name,
        webhook_type=req.webhook_type,
        secret=req.secret,
        target_path=req.target_path,
        deploy_command=req.deploy_command,
        context_id=req.context_id,
        notify_url=req.notify_url,
    )
    
    return WebhookConfigResponse(
        id=config.id,
        name=config.name,
        webhook_type=config.webhook_type.value,
        target_path=config.target_path,
        deploy_command=config.deploy_command,
        context_id=config.context_id,
        notify_url=config.notify_url,
        enabled=config.enabled,
    )


@router.get("/api/webhooks", tags=["webhooks"], response_model=WebhookListResponse)
async def list_webhooks(_identity: AuthIdentity = Depends(require_master_key)):
    """List all webhooks."""
    configs = _state.webhook_manager.list_webhooks()
    return WebhookListResponse(
        webhooks=[
            WebhookConfigResponse(
                id=c.id,
                name=c.name,
                webhook_type=c.webhook_type.value,
                target_path=c.target_path,
                deploy_command=c.deploy_command,
                context_id=c.context_id,
                notify_url=c.notify_url,
                enabled=c.enabled,
            )
            for c in configs
        ],
        count=len(configs),
    )


@router.delete("/api/webhooks/{webhook_id}", tags=["webhooks"])
async def delete_webhook(webhook_id: str, _identity: AuthIdentity = Depends(require_master_key)):
    """Delete a webhook."""
    success = _state.webhook_manager.remove_webhook(webhook_id)
    return {"status": "deleted" if success else "not_found", "webhook_id": webhook_id}


@router.post("/api/webhooks/{webhook_id}/deploy", tags=["webhooks"], response_model=DeployResponse)
async def deploy_webhook(webhook_id: str, req: DeployRequest, _identity: AuthIdentity = Depends(require_master_key)):
    """Manually trigger deployment."""
    result = await _state.webhook_manager.execute_deploy(
        session_id=req.session_id,
        webhook_id=webhook_id,
    )
    
    return DeployResponse(
        status=result["status"],
        job_id=result.get("job_id"),
        message=result.get("message", ""),
    )


@router.get("/api/webhooks/{webhook_id}/deployments", tags=["webhooks"])
async def webhook_deployments(webhook_id: str, _identity: AuthIdentity = Depends(require_master_key)):
    """List deployment history."""
    deployments = _state.webhook_manager.get_deployments(webhook_id)
    return {
        "deployments": deployments,
        "count": len(deployments),
    }


# ---------------------------------------------------------------------------
# Global Search & Replace
# ---------------------------------------------------------------------------


@router.post("/api/search/global", tags=["code"], response_model=GlobalSearchResponse)
async def global_search(req: GlobalSearchRequest, _identity: AuthIdentity = Depends(require_master_key)):
    """Search across all project files."""
    matches = await _state.search_replace.search(
        session_id=req.session_id,
        path=req.path,
        query=req.query,
        file_pattern=req.file_pattern,
        use_regex=req.use_regex,
        case_sensitive=req.case_sensitive,
        context_lines=req.context_lines,
    )
    
    files_affected = list(set(m.path for m in matches))
    
    return GlobalSearchResponse(
        query=req.query,
        matches=[
            SearchMatchItem(
                path=m.path,
                line=m.line,
                column=m.column,
                content=m.content,
            )
            for m in matches
        ],
        total_count=len(matches),
        files_affected=files_affected,
    )


@router.post("/api/replace/global", tags=["code"], response_model=GlobalReplaceResponse)
async def global_replace(req: GlobalReplaceRequest, _identity: AuthIdentity = Depends(require_master_key)):
    """Replace across all project files."""
    results = await _state.search_replace.replace(
        session_id=req.session_id,
        path=req.path,
        search_query=req.search,
        replace_with=req.replace,
        file_pattern=req.file_pattern,
        use_regex=req.use_regex,
        case_sensitive=req.case_sensitive,
        dry_run=req.dry_run,
    )
    
    total_replacements = sum(r.replacements_count for r in results)
    files_modified = sum(1 for r in results if r.replacements_count > 0)
    
    git_commit = None
    if not req.dry_run and req.auto_commit and req.context_id and files_modified > 0:
        commit_result = await _state.context_manager.commit_changes(
            req.context_id,
            f"Global replace: '{req.search}' -> '{req.replace}'",
            [r.path for r in results if r.replacements_count > 0]
        )
        if commit_result.get("success"):
            git_commit = commit_result.get("hash")
    
    return GlobalReplaceResponse(
        search=req.search,
        replace=req.replace,
        results=[
            ReplaceResultItem(
                path=r.path,
                replacements_count=r.replacements_count,
                success=r.success,
                error=r.error,
            )
            for r in results
        ],
        total_replacements=total_replacements,
        files_modified=files_modified,
        dry_run=req.dry_run,
        git_commit=git_commit,
    )


# ---------------------------------------------------------------------------
# Code Intelligence
# ---------------------------------------------------------------------------


@router.post("/api/code/search", tags=["code"], response_model=CodeSearchResponse)
async def code_search(req: CodeSearchRequest, _identity: AuthIdentity = Depends(require_master_key)):
    """Search for code pattern in project."""
    results = await _state.code_intelligence.search_code(
        session_id=req.session_id,
        path=req.path,
        query=req.query,
        language=req.language,
        context_lines=req.context_lines,
    )
    
    return CodeSearchResponse(
        query=req.query,
        results=[
            CodeSearchResultItem(
                path=r.path,
                line=r.line,
                column=r.column,
                content=r.content,
            )
            for r in results
        ],
        count=len(results),
    )


@router.post("/api/code/insert", tags=["code"], response_model=CodeInsertResponse)
async def code_insert(req: CodeInsertRequest, _identity: AuthIdentity = Depends(require_master_key)):
    """Intelligently insert code based on natural language instruction."""
    ctx = await _state.context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))
    
    suggestion = await _state.code_intelligence.find_insertion_point(
        session_id=ctx.session_id,
        path=req.path,
        instruction=req.instruction,
        language=req.language,
    )
    
    if not suggestion:
        raise HTTPException(status_code=400, detail=_err(400, "Could not find insertion point"))
    
    try:
        result = await _state.file_editor.edit_file(
            ctx.session_id,
            req.path,
            [{"type": "insert_after", "after": suggestion.insert_after, "text": suggestion.code}],
        )
        
        git_commit = None
        if req.auto_commit and result.get("success"):
            commit_result = await _state.context_manager.commit_changes(
                req.context_id,
                f"AI: {req.instruction}",
                [req.path]
            )
            if commit_result.get("success"):
                git_commit = commit_result.get("hash")
        
        return CodeInsertResponse(
            success=result.get("success", False),
            path=req.path,
            suggestion=CodeInsertSuggestion(
                insert_after=suggestion.insert_after,
                code=suggestion.code,
                explanation=suggestion.explanation,
                line_number=suggestion.line_number,
            ),
            applied=result.get("success", False),
            git_commit=git_commit,
        )
    except Exception as exc:
        logger.error("Code insertion failed: %s", exc)
        return CodeInsertResponse(
            success=False,
            path=req.path,
            suggestion=CodeInsertSuggestion(
                insert_after=suggestion.insert_after,
                code=suggestion.code,
                explanation=suggestion.explanation,
                line_number=suggestion.line_number,
            ),
            applied=False,
        )


@router.post("/api/code/generate", tags=["code"], response_model=CodeGenerateResponse)
async def code_generate(req: CodeGenerateRequest, _identity: AuthIdentity = Depends(require_master_key)):
    """Generate code based on natural language instruction."""
    code = await _state.code_intelligence.generate_code(
        session_id="",
        instruction=req.instruction,
        language=req.language,
    )
    
    return CodeGenerateResponse(
        code=code,
        language=req.language,
        explanation=f"Generated code for: {req.instruction}",
    )


@router.post("/api/code/complete", tags=["code"], response_model=CodeCompleteResponse)
async def code_complete(req: CodeCompleteRequest, _identity: AuthIdentity = Depends(require_master_key)):
    """Suggest code completion."""
    completion = await _state.code_intelligence.suggest_completion(
        session_id=req.session_id,
        path=req.path,
        partial_code=req.partial_code,
        language=req.language,
    )
    
    return CodeCompleteResponse(
        completion=completion,
        context=req.partial_code[-100:] if len(req.partial_code) > 100 else req.partial_code,
    )


# ---------------------------------------------------------------------------
# Analytics & File Tree
# ---------------------------------------------------------------------------


@router.post("/api/analytics", tags=["code"], response_model=ProjectAnalyticsResponse)
async def run_analytics(req: ProjectAnalyticsRequest, _identity: AuthIdentity = Depends(require_master_key)):
    """Analyze project and return metrics."""
    metrics_data = await _state.analytics.analyze_project(
        session_id=req.session_id,
        path=req.path,
    )
    
    return ProjectAnalyticsResponse(
        project_path=metrics_data["project_path"],
        files=FileStats(**metrics_data["files"]),
        code=CodeStats(**metrics_data["code"]),
        git=GitStats(**metrics_data["git"]),
        tests=TestStats(**metrics_data["tests"]),
        dependencies=DependencyStats(**metrics_data["dependencies"]),
    )


@router.post("/api/tree", tags=["files"], response_model=FileTreeResponse)
async def get_file_tree_v2(req: FileTreeRequest, _identity: AuthIdentity = Depends(require_master_key)):
    """Get directory tree structure."""
    tree = await _state.file_tree.get_tree(
        session_id=req.session_id,
        path=req.path,
        depth=req.depth,
        show_hidden=req.show_hidden,
        max_files=req.max_files,
    )
    
    def count_files(node) -> tuple[int, int]:
        files = 0
        dirs = 0
        if node.type == "file":
            files = 1
        elif node.type == "directory":
            dirs = 1
            for child in node.children:
                f, d = count_files(child)
                files += f
                dirs += d
        return files, dirs
    
    total_files, total_dirs = count_files(tree)
    
    return FileTreeResponse(
        root=FileTreeNode(**_state.file_tree.node_to_dict(tree)),
        total_files=total_files,
        total_directories=total_dirs,
    )


# ---------------------------------------------------------------------------
# Batch Execute
# ---------------------------------------------------------------------------


@router.post("/api/batch/execute", tags=["files"], response_model=BatchExecuteResponse)
async def batch_execute(req: BatchExecuteRequest, request: Request, _identity: AuthIdentity = Depends(require_master_key)):
    """Execute multiple file operations in a single transaction."""
    
    ctx = await _state.context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))

    operations = []
    for op in req.operations:
        op_dict = {
            "type": op.type,
            "path": op.path,
            "continue_on_error": op.continue_on_error,
        }
        if op.operations:
            op_dict["operations"] = [o.model_dump() for o in op.operations]
        if op.content:
            op_dict["content"] = op.content
        if op.new_path:
            op_dict["new_path"] = op.new_path
        if op.dest_path:
            op_dict["dest_path"] = op.dest_path
        if op.command:
            op_dict["command"] = op.command
        operations.append(op_dict)

    result = await _state.batch_manager.execute_batch(
        session_id=ctx.session_id,
        context_id=req.context_id,
        operations=operations,
        auto_commit=req.auto_commit,
        commit_message=req.commit_message,
        run_validation=req.run_validation,
        transaction_id=str(uuid.uuid4())[:8],
    )

    return BatchExecuteResponse(
        transaction_id=result.transaction_id,
        overall_success=result.overall_success,
        summary=result.summary,
        total_duration=result.total_duration,
        operations=[
            BatchOperationResultResponse(
                operation=op.operation,
                path=op.path,
                success=op.success,
                output=op.output,
                error=op.error,
                duration=op.duration,
                lines_changed=op.lines_changed,
            )
            for op in result.operations
        ],
        git_commit=result.git_commit,
        validation_result=result.validation_result,
    )


# ---------------------------------------------------------------------------
# Known Hosts
# ---------------------------------------------------------------------------


@router.get("/api/known-hosts", tags=["known-hosts"])
async def list_known_hosts(_identity: AuthIdentity = Depends(require_master_key)):
    entries = await _state.host_key_store.list_keys()
    return {"hosts": entries}


@router.get("/api/known-hosts/check", tags=["known-hosts"])
async def check_known_host(
    host: str = Query(..., min_length=1),
    port: int = Query(22, ge=1, le=65535),
    _identity: AuthIdentity = Depends(require_master_key),
):
    """Preflight trust check — returns 'known' or 'unknown'. Never returns 'changed'.

    IMPORTANT: lookups are by (host,port) pair — not by host alone.
    'changed' cannot be detected without a real SSH handshake.
    """
    entry = await _state.host_key_store.get_host(host, port)
    return KnownHostCheckResponse(
        status="known" if entry else "unknown",
        host=host,
        port=port,
    )


@router.get("/api/known-hosts/{host}", tags=["known-hosts"])
async def lookup_known_host(
    host: str,
    port: int = Query(22, ge=1, le=65535),
    _identity: AuthIdentity = Depends(require_master_key),
):
    """Lookup a single host:port entry. Returns 404 if not found.

    IMPORTANT: lookups are by (host,port) pair — not by host alone.
    """
    entry = await _state.host_key_store.get_host(host, port)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Host {host}:{port} not found in known-hosts")
    return KnownHostLookupResponse(**entry)


@router.delete("/api/known-hosts/{host}", tags=["known-hosts"])
async def delete_known_host(
    host: str,
    port: int = Query(22, ge=1, le=65535),
    _identity: AuthIdentity = Depends(require_master_key),
):
    """Delete a specific host:port entry from known hosts.

    IMPORTANT: deletes by (host,port) pair — use port=22 if not specified.
    """
    count = await _state.host_key_store.delete_host(host, port)
    if count == 0:
        raise HTTPException(status_code=404, detail=f"No known hosts found for {host}:{port}")
    return {"deleted": count, "host": host, "port": port}


@router.delete("/api/known-hosts", tags=["known-hosts"])
async def clear_known_hosts(_identity: AuthIdentity = Depends(require_master_key)):
    count = await _state.host_key_store.delete_all()
    return {"deleted": count}
