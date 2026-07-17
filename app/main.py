"""FastAPI entry point for agent-ssh-gateway."""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import app.build_info as build_info
import app.state as state
from app.agent_token_store import AgentTokenStore
from app.auth_middleware import auth_check, is_ip_allowed, parse_cidrs
from app.batch_operations import BatchOperationsManager
from app.bulk_operations_v2 import BulkOperationsManager
from app.circuit_breaker import CircuitBreakerRegistry
from app.code_intelligence import CodeIntelligence
from app.config import settings
from app.context_manager import ContextManager
from app.distributed_lock import DistributedLock
from app.file_editor import FileEditor
from app.file_tree import FileTreeExplorer
from app.job_manager import JobManager
from app.known_hosts import NullHostKeyStore, create_host_key_store
from app.models import (
    ValidationErrorResponse,
)
from app.project_analytics import ProjectAnalytics
from app.redis_queue import RedisJobQueue
from app.routers.auth import router as auth_identity_router
from app.search_replace import GlobalSearchReplace
from app.security import (
    SECURITY_HEADERS,
    AuditLogger,
    limiter,
)
from app.server_manager import ServerManager
from app.session_store import SessionStore
from app.snapshot_manager import SnapshotManager
from app.ssh_manager import (
    AuthenticationError,
    ConnectionError,
    ExecutionError,
    SessionNotFoundError,
    SSHManagerError,
    SSHSessionManager,
    TimeoutError,
)
from app.state import _err
from app.user_auth import init_auth_db
from app.user_auth import router as auth_router
from app.version import APP_VERSION
from app.webhook_manager import WebhookManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lifespan — Initializes Globals In App.state
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    build_info.set_started_at()
    await init_auth_db()
    state.host_key_store = create_host_key_store(settings)
    if not isinstance(state.host_key_store, NullHostKeyStore):
        logger.info("Host key store initialized: %s", type(state.host_key_store).__name__)

    state.manager = SSHSessionManager(
        session_timeout=settings.session_timeout,
        cleanup_interval=settings.cleanup_interval,
        host_key_store=state.host_key_store,
    )
    await state.manager.start_cleanup_task()

    state.job_manager = JobManager(ssh_manager=state.manager)
    await state.job_manager.start_cleanup_task()

    state.file_editor = FileEditor(ssh_manager=state.manager)

    state.context_manager = ContextManager(ssh_manager=state.manager)
    await state.context_manager.start_cleanup_task()

    state.batch_manager = BatchOperationsManager(
        ssh_manager=state.manager,
        file_editor=state.file_editor,
        context_manager=state.context_manager,
    )

    state.code_intelligence = CodeIntelligence(
        ssh_manager=state.manager,
        file_editor=state.file_editor,
    )

    state.search_replace = GlobalSearchReplace(
        ssh_manager=state.manager,
        file_editor=state.file_editor,
    )

    state.file_tree = FileTreeExplorer(ssh_manager=state.manager)

    state.server_manager = ServerManager()

    state.snapshot_manager = SnapshotManager(
        ssh_manager=state.manager,
        context_manager=state.context_manager,
    )

    state.webhook_manager = WebhookManager(
        ssh_manager=state.manager,
        job_manager=state.job_manager,
    )

    state.analytics = ProjectAnalytics(ssh_manager=state.manager)

    # Initialize Security Components
    if settings.persistent_sessions_enabled and not settings.encryption_key:
        raise RuntimeError(
            "PERSISTENT_SESSIONS_ENABLED=true requires ENCRYPTION_KEY to be set. "
            'Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    state.audit_logger = AuditLogger()

    # Initialize structured audit event logger (JSONL + ring buffer)
    from app.audit import AuditEventLogger as _AuditEventLogger
    state.event_audit_logger = _AuditEventLogger(
        log_path=settings.audit_log_path,
        recent_limit=settings.audit_recent_limit,
    )

    # Initialize Swarm Components
    state.redis_queue = RedisJobQueue(settings.redis_url)
    state.circuit_breakers = CircuitBreakerRegistry()
    state.dist_lock = DistributedLock(settings.redis_url)
    state.bulk_ops = BulkOperationsManager(max_concurrency=50)

    try:
        await state.redis_queue.connect()
        await state.dist_lock.connect()
        logger.info("Redis Components Connected")
    except Exception as exc:
        logger.warning("Redis not available: %s", exc)

    # Initialize Agent Token Store (requires Redis)
    state.agent_token_store = AgentTokenStore(settings.redis_url)
    try:
        await state.agent_token_store.connect()
        if settings.agent_token and state.agent_token_store.connected:
            await state.agent_token_store.set_token(
                token=settings.agent_token,
                ttl=settings.agent_token_ttl,
                scopes=settings.agent_token_scopes,
            )
    except Exception as exc:
        logger.warning("AgentTokenStore not available (agent token rotation will fail): %s", exc)

    # Initialize Persistent Sessions If Configured
    state.session_store = None
    if settings.persistent_sessions_enabled and settings.database_url:
        try:
            state.session_store = SessionStore(settings.database_url)
            await state.session_store.connect()
            logger.info("Persistent Session Store Connected")

            # Restore Active Sessions From Previous Run
            allowed_nets = parse_cidrs(settings.allowed_client_cidrs)
            active_sessions = await state.session_store.list_active_sessions()
            restored = 0
            failed = 0
            for sess in active_sessions:
                try:
                    creds = await state.session_store.get_session_credentials(sess["session_id"])
                    if creds and is_ip_allowed(creds.get("host", ""), allowed_nets):
                        _ = await state.manager.create_session(
                            host=creds["host"],
                            port=creds.get("port", 22),
                            username=creds["username"],
                            password=creds.get("password"),
                            private_key=creds.get("private_key"),
                            key_passphrase=creds.get("key_passphrase"),
                        )

                        await state.session_store.deactivate_session(sess["session_id"])
                        restored += 1
                except Exception as exc:
                    logger.warning("Failed to restore session %s: %s", sess["session_id"], exc)
                    failed += 1
            if restored:
                logger.info(
                    "Restored %d sessions from persistent storage (%d failed)", restored, failed
                )
        except Exception as exc:
            logger.warning("PostgreSQL not available: %s", exc)

    # Initialize Event Hook Components
    if settings.event_hooks_enabled:
        try:
            if not settings.database_url:
                raise RuntimeError("EVENT_HOOKS_ENABLED=true requires DATABASE_URL")

            from app.event_hook_delivery import DeliveryService
            from app.event_hook_store import EventHookStore

            state.event_hook_store = EventHookStore(settings.database_url)
            await state.event_hook_store.create_tables()

            state.delivery_service = DeliveryService(
                settings.database_url, instance_id=uuid.uuid4().hex
            )
            await state.delivery_service.create_tables()

            s = settings
            await state.delivery_service.start(
                poll_interval=s.event_hooks_poll_interval,
                connect_timeout=s.event_hooks_timeout_connect,
                read_timeout=s.event_hooks_timeout_read,
                max_attempts=s.event_hooks_max_attempts,
                retry_base_sec=s.event_hooks_retry_base_sec,
                retry_max_sec=s.event_hooks_retry_max_sec,
                lease_ttl=s.event_hooks_lease_ttl,
                retention_sent_days=s.event_hooks_retention_sent_days,
                retention_dead_days=s.event_hooks_retention_dead_days,
            )
            logger.info("Event Hook Delivery Service Started")
        except Exception:
            logger.exception("Event hooks are enabled but failed to initialize")
            raise

    logger.info("Security Components Initialized")
    logger.info("Swarm Mode Ready (redis Job Queue, Circuit Breaker, Distributed Locks)")

    logger.info("agent-ssh-gateway started on %s:%d", settings.uvicorn_host, settings.uvicorn_port)
    yield

    # Graceful Shutdown: Drain Active Jobs
    logger.info("Starting Graceful Shutdown...")

    # Wait For Active Jobs To Complete (max 30s)
    if state.job_manager:
        active_jobs = [j for j in state.job_manager._jobs.values() if j.status == "running"]
        if active_jobs:
            logger.info("Waiting for %d active jobs to complete...", len(active_jobs))
            await asyncio.wait_for(state.job_manager.wait_for_all_jobs(), timeout=30.0)

    # Cleanup
    await state.context_manager.stop_cleanup_task()
    await state.job_manager.stop_cleanup_task()
    await state.manager.stop_cleanup_task()

    sessions = await state.manager.list_sessions()
    if sessions:
        tasks = [
            asyncio.wait_for(state.manager.disconnect(s.session_id), timeout=5.0) for s in sessions
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for sid, err in zip([s.session_id for s in sessions], results, strict=True):
            if err:
                logger.warning("Force close session %s: %s", sid, err)

    ws_count = len(state.active_websockets)
    if ws_count:
        logger.info("Closing %d active WebSocket connections...", ws_count)
        for ws in list(state.active_websockets):
            try:
                await ws.close(code=1001, reason="Server shutting down")
            except Exception:
                pass
        state.active_websockets.clear()

    if state.redis_queue:
        await state.redis_queue.disconnect()
    if state.dist_lock:
        await state.dist_lock.disconnect()
    if state.agent_token_store:
        await state.agent_token_store.disconnect()
    ds = getattr(state, "delivery_service", None)
    if ds:
        await ds.close()
        logger.info("Event Hook Delivery Service Shut Down")
    if state.session_store:
        await state.session_store.disconnect()
    if state.host_key_store:
        await state.host_key_store.disconnect()

    logger.info("agent-ssh-gateway Shutdown Complete")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="agent-ssh-gateway",
    description=(
        "API-first SSH gateway for AI agents, CI/CD, and infrastructure teams.\n\n"
        "## Authentication\n"
        "All endpoints (except `/health` and `/api/capabilities`) require `X-API-Key` header.\n"
        "- **Master API key** — full access to all endpoints.\n"
        "- **Agent token** — scoped access for automation (create via `POST /api/agent/token`).\n\n"
        "See `GET /api/help` for a quick-start guide with curl examples."
    ),
    version=APP_VERSION,
    lifespan=lifespan,
    responses={
        422: {
            "model": ValidationErrorResponse,
            "description": "Request validation failed",
        }
    },
    openapi_tags=[
        {
            "name": "ssh",
            "description": "SSH session management (connect, execute, disconnect). Requires scope: `ssh:connect` | `ssh:execute` | `ssh:disconnect` for agent tokens.",
        },
        {
            "name": "files",
            "description": "File operations (read, edit, upload, download). Requires scope: `ssh:files` for agent tokens.",
        },
        {
            "name": "jobs",
            "description": "Background job execution and monitoring. Requires scope: `jobs:run` | `jobs:read` for agent tokens.",
        },
        {"name": "git", "description": "Git repository operations. Master key only."},
        {
            "name": "context",
            "description": "Development contexts with git awareness. Master key only.",
        },
        {"name": "templates", "description": "Code templates. Master key only."},
        {"name": "servers", "description": "Saved server management. Master key only."},
        {"name": "snapshots", "description": "Project snapshots for recovery. Master key only."},
        {"name": "webhooks", "description": "CI/CD webhooks. Master key only."},
        {"name": "known-hosts", "description": "Host key store management. Master key only."},
        {"name": "logs", "description": "Remote log reading (journald, docker). Master key only."},
        {
            "name": "auth",
            "description": "Authentication diagnostics (whoami). Requires scope: `auth:read` for agent tokens.",
        },
        {
            "name": "help",
            "description": "API help and endpoint discovery. Accessible with any valid API key.",
        },
        {
            "name": "code",
            "description": "Code intelligence (search, insert, complete). Master key only.",
        },
        {
            "name": "system",
            "description": "System endpoints (health, metrics, config). Public: `/health`, `/api/capabilities`. Master key: rest.",
        },
    ],
)


ERROR_SCHEMA_REF = "#/components/schemas/ErrorResponse"

TAG_ERROR_CODES = {
    "ssh": [400, 401, 404, 500, 502, 504],
    "files": [400, 404, 500],
    "jobs": [404, 500],
    "git": [400, 404, 500],
    "context": [400, 404, 500],
    "templates": [400, 404, 500],
    "servers": [400, 404, 409, 500],
    "snapshots": [404, 500],
    "webhooks": [400, 404, 500],
    "code": [400, 404, 500],
    "system": [500],
}

ERROR_DESC = {
    400: "Bad request",
    401: "Unauthorized",
    404: "Not found",
    409: "Conflict",
    429: "Too many requests",
    500: "Internal server error",
    502: "Bad gateway",
    504: "Gateway timeout",
}


def _set_errors(op: dict):
    tag = (op.get("tags") or ["system"])[0]
    codes = TAG_ERROR_CODES.get(tag, [])
    for code in codes:
        if str(code) not in op.setdefault("responses", {}):
            op["responses"][str(code)] = {
                "description": ERROR_DESC.get(code, "Error"),
                "content": {"application/json": {"schema": {"$ref": ERROR_SCHEMA_REF}}},
            }


def custom_openapi():
    from fastapi.openapi.utils import get_openapi

    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )

    # Server Metadata For Codegen
    schema["servers"] = [{"url": "/", "description": "agent-ssh-gateway API"}]

    # --- Error Response Schemas With Agent-friendly Format ---
    schema["components"]["schemas"]["ErrorResponse"] = {
        "type": "object",
        "properties": {
            "detail": {
                "oneOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string",
                                "description": "Human-readable error message",
                            },
                            "code": {
                                "type": "string",
                                "description": "Machine-readable error code (e.g. SESSION_NOT_FOUND)",
                            },
                            "retryable": {
                                "type": "boolean",
                                "description": "Whether the operation can be retried",
                            },
                            "hint": {
                                "type": "string",
                                "description": "Guidance for resolving the error",
                            },
                            "http_status": {"type": "integer", "description": "HTTP status code"},
                            "errors": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/ValidationFieldItem"},
                                "description": "Field-level validation errors (422 only)",
                            },
                            "total_errors": {
                                "type": "integer",
                                "description": "Total number of validation errors",
                            },
                        },
                    },
                ],
            },
        },
    }
    schema["components"]["schemas"]["ValidationFieldItem"] = {
        "type": "object",
        "properties": {
            "field": {"type": "string", "description": "Field name that failed validation"},
            "error": {"type": "string", "description": "Human-readable validation error"},
            "type": {"type": "string", "description": "Machine-readable error type"},
        },
    }
    schema["components"]["schemas"]["ValidationErrorResponse"] = {
        "type": "object",
        "properties": {
            "detail": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Error summary"},
                    "code": {"type": "string", "description": "Always VALIDATION_ERROR"},
                    "retryable": {
                        "type": "boolean",
                        "description": "Always false for validation errors",
                    },
                    "hint": {"type": "string", "description": "Guidance to fix validation errors"},
                    "http_status": {"type": "integer", "description": "Always 422"},
                    "errors": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/ValidationFieldItem"},
                        "description": "Per-field validation errors",
                    },
                    "total_errors": {
                        "type": "integer",
                        "description": "Count of validation errors",
                    },
                },
            },
        },
    }

    # --- SSE Event Schema ---
    schema["components"]["schemas"]["SSEEvent"] = {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["status", "stdout", "stderr", "exit", "error"],
                "description": "Event type discriminator",
            },
            "data": {"type": "string", "description": "Event payload as JSON string"},
        },
        "discriminator": {"propertyName": "type"},
        "oneOf": [
            {
                "$ref": "#/components/schemas/SSEStatusEvent",
                "description": "Job status update (started/running/completed/cancelled)",
            },
            {
                "$ref": "#/components/schemas/SSEStdoutEvent",
                "description": "Stdout output chunk",
            },
            {
                "$ref": "#/components/schemas/SSEStderrEvent",
                "description": "Stderr output chunk",
            },
            {
                "$ref": "#/components/schemas/SSEExitEvent",
                "description": "Job exit code",
            },
            {
                "$ref": "#/components/schemas/SSEErrorEvent",
                "description": "Job-level error (timeout, connection lost)",
            },
        ],
    }
    schema["components"]["schemas"]["SSEStatusEvent"] = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["status"], "description": "Event type"},
            "data": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["started", "running", "completed", "cancelled"],
                    },
                    "job_id": {"type": "string"},
                    "ts": {"type": "number", "description": "Unix timestamp"},
                },
            },
        },
    }
    schema["components"]["schemas"]["SSEStdoutEvent"] = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["stdout"], "description": "Event type"},
            "data": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Stdout text chunk"},
                    "ts": {"type": "number", "description": "Unix timestamp"},
                },
            },
        },
    }
    schema["components"]["schemas"]["SSEStderrEvent"] = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["stderr"], "description": "Event type"},
            "data": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Stderr text chunk"},
                    "ts": {"type": "number", "description": "Unix timestamp"},
                },
            },
        },
    }
    schema["components"]["schemas"]["SSEExitEvent"] = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["exit"], "description": "Event type"},
            "data": {
                "type": "object",
                "properties": {
                    "code": {"type": "integer", "description": "Exit code (0 = success)"},
                    "ts": {"type": "number", "description": "Unix timestamp"},
                },
            },
        },
    }
    schema["components"]["schemas"]["SSEErrorEvent"] = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["error"], "description": "Event type"},
            "data": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Error message"},
                    "code": {
                        "type": "string",
                        "description": "Error code (e.g. TIMEOUT, CONNECTION_LOST)",
                    },
                    "ts": {"type": "number", "description": "Unix timestamp"},
                },
            },
        },
    }

    schema["components"]["securitySchemes"] = {
        "ApiKeyHeader": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": (
                "Master API key (full access) **or** agent token (scoped access).\n\n"
                "- **Master key**: set via `API_KEY` env var, has access to all endpoints.\n"
                "- **Agent token**: created via `POST /api/agent/token` with a master key.\n"
                "  Supports scopes: `ssh:connect`, `ssh:execute`, `ssh:disconnect`, "
                "`ssh:files`, `ssh:port-check`, `jobs:read`, `jobs:run`.\n\n"
                "Also accepted as `Authorization: Bearer <key>` header."
            ),
        },
        "MutualTLS": {
            "type": "http",
            "scheme": "mutual",
            "description": "mTLS client certificate authentication via X-SSL-Client-Cert header (configured in nginx)",
        },
    }

    # --- Default Response Headers ---
    COMMON_RESPONSE_HEADERS = {
        "X-Request-ID": {
            "schema": {"type": "string"},
            "description": "Unique request identifier for tracing",
        },
        "X-RateLimit-Limit": {
            "schema": {"type": "integer"},
            "description": "Rate limit ceiling (requests per window)",
        },
        "X-RateLimit-Remaining": {
            "schema": {"type": "integer"},
            "description": "Requests remaining in current window",
        },
        "X-RateLimit-Reset": {
            "schema": {"type": "integer"},
            "description": "Unix timestamp when rate limit resets",
        },
    }

    content_type_map = {
        "/": {"get": "text/html"},
        "/metrics": {"get": "text/plain"},
        "/api/sdk/download": {"get": "text/x-python"},
        "/api/jobs/{job_id}/stream": {"get": "text/event-stream"},
        "/api/jobs/{job_id}/events": {"get": "text/event-stream"},
        "/api/file/raw": {"get": "text/plain"},
        "/api/file/download": {"get": "application/octet-stream"},
    }

    # Param Description Overrides By Name
    PARAM_DESC: dict[str, str] = {
        "session_id": "Active SSH session identifier returned from POST /api/ssh/connect",
        "server_id": "Target server identifier",
        "job_id": "Background job identifier returned from POST /api/jobs/run",
        "context_id": "Development context identifier returned from POST /api/context/create",
        "template_id": "Template identifier from GET /api/templates",
        "webhook_id": "Webhook identifier from GET /api/webhooks",
        "snapshot_id": "Snapshot identifier from GET /api/snapshots",
        "deployment_id": "Deployment identifier from GET /api/webhooks/{webhook_id}/deployments",
        "api_key": "API key for authentication",
        "format": "Output format (json, text)",
        "path": "Absolute file path on the remote server",
        "timeout": "Operation timeout in seconds",
        "force": "Force operation even if destructive",
        "recursive": "Process directories recursively",
    }

    response_examples: dict[tuple[str, str], dict] = {
        ("/api/ssh/connect", "post"): {
            "session_id": "abc123",
            "host": "10.0.0.1",
            "port": 22,
            "username": "deploy",
        },
        ("/api/ssh/execute", "post"): {
            "session_id": "abc123",
            "exit_code": 0,
            "stdout": "total 42\n-rw-r--r-- 1 root root ...",
            "stderr": "",
            "duration_ms": 150,
        },
        ("/api/context/create", "post"): {
            "context_id": "ctx_abc123",
            "name": "my_project",
            "path": "/root/project",
            "status": "ready",
        },
        ("/api/jobs/run", "post"): {"job_id": "job_abc123", "status": "queued"},
        ("/api/file/read", "post"): {
            "path": "/etc/hostname",
            "content": "my-server\n",
            "size": 10,
            "encoding": "utf-8",
        },
        ("/api/file/write", "post"): {"path": "/root/test.txt", "size": 5, "encoding": "utf-8"},
        ("/", "get"): {"service": "agent-ssh-gateway", "version": APP_VERSION, "status": "running"},
    }

    # Helper To Generate Example From JSON Schema
    def _gen_example(schema_def: dict) -> object:
        if "example" in schema_def:
            return schema_def["example"]
        if "default" in schema_def:
            return schema_def["default"]
        if schema_def.get("type") == "string":
            if "enum" in schema_def:
                return schema_def["enum"][0]
            if "format" in schema_def:
                return {
                    "date-time": "2026-01-01T00:00:00Z",
                    "uri": "https://example.com",
                    "email": "user@example.com",
                }.get(schema_def["format"], "string")
            return "string"
        if schema_def.get("type") == "integer":
            return 0
        if schema_def.get("type") == "number":
            return 0.0
        if schema_def.get("type") == "boolean":
            return True
        if schema_def.get("type") == "array":
            items = schema_def.get("items", {})
            item = _gen_example(items) if items else "string"
            return [item]
        if schema_def.get("type") == "object":
            props = schema_def.get("properties", {})
            return {k: _gen_example(v) for k, v in props.items()}
        if "oneOf" in schema_def:
            return _gen_example(schema_def["oneOf"][0])
        if "anyOf" in schema_def:
            return _gen_example(schema_def["anyOf"][0])
        if "$ref" in schema_def:
            ref_name = schema_def["$ref"].rsplit("/", 1)[-1]
            referenced = schema.get("components", {}).get("schemas", {}).get(ref_name, {})
            return _gen_example(referenced) if referenced else "string"
        return "string"

    for path, methods in schema.get("paths", {}).items():
        for method, op in methods.items():
            # Deprecate Old Query-param Upload Endpoint
            if path == "/api/file/upload" and method == "post":
                op["deprecated"] = True

            # Content Types For Non-json Endpoints
            if path in content_type_map and method in content_type_map[path]:
                ct = content_type_map[path][method]
                resp = op.setdefault("responses", {}).setdefault("200", {})
                resp["content"] = {ct: {}}

            # 422 References Validationerrorresponse
            for code, resp in op.get("responses", {}).items():
                if code == "422":
                    resp["content"] = {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ValidationErrorResponse"}
                        }
                    }

            # Add Extra Error Codes
            _set_errors(op)

            # Add Response Headers To All Responses
            for resp in op.get("responses", {}).values():
                resp.setdefault("headers", {}).update(COMMON_RESPONSE_HEADERS)

            # --- Request Body Examples (auto-generated For All Body Ops) ---
            req_body = op.get("requestBody", {}).get("content", {}).get("application/json", {})
            if req_body.get("schema"):
                if "example" not in req_body:
                    req_body["example"] = _gen_example(req_body["schema"])

            # Multipart Form-data Example
            mp = op.get("requestBody", {}).get("content", {}).get("multipart/form-data", {})
            if mp.get("schema") and "example" not in mp:
                mp["example"] = _gen_example(mp["schema"])

            # --- Response 200 Examples ---
            key = (path, method)
            if key in response_examples:
                resp200 = op.get("responses", {}).get("200", {})
                if resp200.get("content", {}).get("application/json", {}) is not None:
                    ct_content = resp200.get("content", {}).get("application/json")
                    if ct_content and "example" not in ct_content:
                        ct_content["example"] = response_examples[key]
                    elif ct_content is None:
                        resp200.setdefault("content", {}).setdefault("application/json", {})[
                            "example"
                        ] = response_examples[key]

            # --- Parameter Descriptions ---
            for param in op.get("parameters", []):
                name = param.get("name", "")
                if name in PARAM_DESC and not param.get("description"):
                    param["description"] = PARAM_DESC[name]
                elif not param.get("description"):
                    param["description"] = name.replace("_", " ").title()

    # Security: /health And /api/capabilities Are Public; Everything Else Requires X-api-key
    for path, _methods in schema.get("paths", {}).items():
        for _, op in _methods.items():
            if path in ("/health", "/api/capabilities"):
                continue
            op["security"] = [{"ApiKeyHeader": []}]

    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi

# Rate Limiting
app.state.limiter = limiter

# CORS (restrict In Production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-API-Key", "X-Request-ID"],
)


@app.middleware("http")
async def security_headers_middleware(request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers[header] = value
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    exc = await auth_check(request, settings, state.agent_token_store)
    if exc is not None:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Exception Handler
# ---------------------------------------------------------------------------


@app.exception_handler(SSHManagerError)
async def ssh_exception_handler(request, exc: SSHManagerError):
    """Convert SSH manager exceptions to structured HTTP responses."""
    status_map = {
        ConnectionError: 502,
        AuthenticationError: 401,
        SessionNotFoundError: 404,
        TimeoutError: 504,
        ExecutionError: 500,
    }
    status_code = status_map.get(type(exc), 500)
    return JSONResponse(
        status_code=status_code,
        content=_err(status_code, str(exc)),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    """Convert Pydantic validation errors to clear field-specific messages."""
    errors = exc.errors()
    field_errors = []

    for error in errors:
        loc = error.get("loc", [])
        field = ".".join(str(x) for x in loc if x != "body")
        msg = error.get("msg", "")
        error_type = error.get("type", "")

        # Create Human-readable Message
        if "missing" in error_type or "required" in error_type:
            message = f"Field '{field}' is required but was not provided"
        elif "type_error" in error_type:
            input_val = error.get("input", "")
            message = f"Field '{field}' has invalid type. Expected: {msg}, got: {type(input_val).__name__ if input_val else 'none'}"
        elif "value_error" in error_type:
            message = f"Field '{field}' validation failed: {msg}"
        elif "min_length" in error_type or "max_length" in error_type:
            message = f"Field '{field}' length validation failed: {msg}"
        else:
            message = f"Field '{field}': {msg}"

        field_errors.append(
            {
                "field": field,
                "error": message,
                "type": error_type,
            }
        )

    return JSONResponse(
        status_code=422,
        content={
            "message": "Request validation failed",
            "code": "VALIDATION_ERROR",
            "retryable": False,
            "hint": "Check missing and invalid fields listed in errors[]",
            "http_status": 422,
            "errors": field_errors,
            "total_errors": len(field_errors),
        },
    )


# ---------------------------------------------------------------------------
# Router Registration — Route Handlers Live In App/routers/
# ---------------------------------------------------------------------------

from app.routers.batch import router as batch_router  # noqa: E402
from app.routers.code import router as code_router  # noqa: E402
from app.routers.context import router as context_router  # noqa: E402
from app.routers.diagnostics import router as diagnostics_router  # noqa: E402
from app.routers.event_hooks import router as event_hooks_router  # noqa: E402
from app.routers.files import router as files_router  # noqa: E402
from app.routers.git import router as git_router  # noqa: E402
from app.routers.jobs import router as jobs_router  # noqa: E402
from app.routers.known_hosts import router as known_hosts_router  # noqa: E402
from app.routers.logs import router as logs_router  # noqa: E402
from app.routers.project_inspection import router as project_inspection_router  # noqa: E402
from app.routers.search_replace import router as search_replace_router  # noqa: E402
from app.routers.servers import router as servers_router  # noqa: E402
from app.routers.snapshots import router as snapshots_router  # noqa: E402
from app.routers.ssh import router as ssh_router  # noqa: E402
from app.routers.system import router as system_router  # noqa: E402
from app.routers.templates import router as templates_router  # noqa: E402
from app.routers.webhooks import router as webhooks_router  # noqa: E402
from app.routers.workspace import router as workspace_router  # noqa: E402

app.include_router(batch_router)
app.include_router(diagnostics_router)
app.include_router(code_router)
app.include_router(project_inspection_router)
app.include_router(ssh_router)
app.include_router(files_router)
app.include_router(jobs_router)
app.include_router(git_router)
app.include_router(context_router)
app.include_router(search_replace_router)
app.include_router(servers_router)
app.include_router(snapshots_router)
app.include_router(webhooks_router)
app.include_router(known_hosts_router)
app.include_router(system_router)
app.include_router(logs_router)
app.include_router(templates_router)
app.include_router(event_hooks_router)
app.include_router(auth_router)
app.include_router(auth_identity_router)
app.include_router(workspace_router)

# Static Files Mount (after All Router Includes So Static Routes Take Precedence)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
