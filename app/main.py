"""FastAPI entry point for Web SSH Gateway."""

import json
import logging
import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, Header, Response, UploadFile, File, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, PlainTextResponse, HTMLResponse

from app.config import settings
from app.auth_middleware import auth_check, ws_auth_check, is_agent_token_valid
import secrets
from app.security import (
    limiter,
    rate_limit_mutation,
    sanitize_command,
    validate_path,
    SecretManager,
    AuditLogger,
    SessionSecurity,
    SECURITY_HEADERS,
)
from app.metrics import metrics
from app.redis_queue import RedisJobQueue
from app.circuit_breaker import CircuitBreakerRegistry
from app.distributed_lock import DistributedLock
from app.session_store import SessionStore
from app.bulk_operations_v2 import BulkOperationsManager
from app.models import (
    ConnectRequest,
    ConnectResponse,
    ExecuteRequest,
    ExecuteResponse,
    DisconnectRequest,
    DisconnectResponse,
    SessionsResponse,
    SessionInfo,
    HealthResponse,
    CapabilitiesResponse,
    AgentTokenResponse,
    AgentTokenRefreshResponse,
    ErrorResponse,
    SessionTimeoutRequest,
    SessionTimeoutResponse,
    SessionConfigResponse,
    JobRunRequest,
    JobRunResponse,
    JobStatusResponse,
    JobResultResponse,
    JobListResponse,
    FileEditRequest,
    FileEditResponse,
    FileReadRequest,
    FileReadResponse,
    PatchApplyRequest,
    PatchApplyResponse,
    ContextCreateRequest,
    ContextResponse,
    ContextListResponse,
    GitInfoResponse,
    GitInitRequest,
    GitCommitRequest,
    GitActionResponse,
    GitStatusResponse,
    GitDiffRequest,
    GitDiffResponse,
    ScaffoldRequest,
    ScaffoldResponse,
    FileEditWithContextRequest,
    FileEditWithContextResponse,
    ValidateRequest,
    ValidationReportResponse,
    ValidationStepResult,
    SmartContextStateResponse,
    TabStateResponse,
    OpenFileRequest,
    CloseFileRequest,
    UpdateCursorRequest,
    AddCommandRequest,
    AddSearchRequest,
    AddBookmarkRequest,
    RemoveBookmarkRequest,
    BatchExecuteRequest,
    BatchExecuteResponse,
    BatchReadRequest,
    BatchReadResponse,
    BatchOperationResultResponse,
    CodeSearchRequest,
    CodeSearchResponse,
    CodeSearchResultItem,
    CodeInsertRequest,
    CodeInsertResponse,
    CodeInsertSuggestion,
    CodeGenerateRequest,
    CodeGenerateResponse,
    CodeCompleteRequest,
    CodeCompleteResponse,
    CreateBackupRequest,
    RestoreBackupRequest,
    RecoveryActionResponse,
    BackupInfo,
    ListBackupsResponse,
    TemplateListResponse,
    TemplateInfo,
    TemplateGetRequest,
    TemplateRenderRequest,
    TemplateRenderResponse,
    DiffResponse,
    DiffLine,
    ProjectAnalyticsRequest,
    ProjectAnalyticsResponse,
    FileStats,
    CodeStats,
    GitStats,
    TestStats,
    DependencyStats,
    GlobalSearchRequest,
    GlobalSearchResponse,
    SearchMatchItem,
    GlobalReplaceRequest,
    GlobalReplaceResponse,
    ReplaceResultItem,
    FileTreeRequest,
    FileTreeResponse,
    FileTreeNode,
    ServerInfo,
    ServerListResponse,
    AddServerRequest,
    ConnectServerRequest,
    ServerConnectResponse,
    SnapshotInfo,
    CreateSnapshotRequest,
    RestoreSnapshotRequest,
    SnapshotListResponse,
    SnapshotActionResponse,
    WebhookConfigResponse,
    WebhookListResponse,
    CreateWebhookRequest,
    DeployRequest,
    DeployResponse,
    DeploymentInfo,
    ProjectStructureRequest,
    ProjectStructureResponse,
    FileMetadata,
    BatchEditRequest,
    BatchEditResponse,
    BatchEditResult,
    BulkExecuteRequest,
    BulkExecuteResult,
    BulkExecuteResponse,
    FileUploadRequest,
    FileUploadResponse,
    FileDownloadRequest,
    FileWriteRequest,
    FileWriteResponse,
    FileTreeRequest,
    FileTreeResponse,
    ASTRefactorRenameRequest,
    ASTRefactorRenameResponse,
    ASTRefactorFileResult,
    ASTRefactorExtractRequest,
    ASTRefactorExtractResponse,
    ASTAnalyzeRequest,
    ASTAnalyzeResponse,
    ValidationErrorResponse,
)
from app.ssh_manager import (
    SSHSessionManager,
    SSHManagerError,
    ConnectionError,
    AuthenticationError,
    SessionNotFoundError,
    TimeoutError,
    ExecutionError,
)
from app.job_manager import JobManager
from app.file_editor import FileEditor
from app.ast_refactor import ASTRefactor
from app.context_manager import ContextManager
from app.git_manager import GitStatus
from app.batch_operations import BatchOperationsManager
from app.code_intelligence import CodeIntelligence
from app.template_library import TemplateLibrary
from app.diff_generator import DiffGenerator
from app.project_analytics import ProjectAnalytics
from app.search_replace import GlobalSearchReplace
from app.file_tree import FileTreeExplorer
from app.server_manager import ServerManager, ServerStatus
from app.snapshot_manager import SnapshotManager
from app.webhook_manager import WebhookManager, WebhookType
from app.known_hosts import create_host_key_store, NullHostKeyStore, HostKeyStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lifespan — initializes globals in app.state
# ---------------------------------------------------------------------------

import app.state as state


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
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
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    state.audit_logger = AuditLogger()
    
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
    
    # Initialize Persistent Sessions If Configured
    state.session_store = None
    if settings.persistent_sessions_enabled and settings.database_url:
        try:
            state.session_store = SessionStore(settings.database_url)
            await state.session_store.connect()
            logger.info("Persistent Session Store Connected")

            # Restore active sessions from previous run
            active_sessions = await state.session_store.list_active_sessions()
            restored = 0
            failed = 0
            for sess in active_sessions:
                try:
                    creds = await state.session_store.get_session_credentials(sess["session_id"])
                    if creds:
                        await state.manager.restore_session(
                            session_id=sess["session_id"],
                            host=creds["host"],
                            port=creds.get("port", 22),
                            username=creds["username"],
                            password=creds.get("password"),
                            private_key=creds.get("private_key"),
                            key_passphrase=creds.get("key_passphrase"),
                        )
                        restored += 1
                except Exception as exc:
                    logger.warning("Failed to restore session %s: %s", sess["session_id"], exc)
                    failed += 1
            if restored:
                logger.info("Restored %d sessions from persistent storage (%d failed)", restored, failed)
        except Exception as exc:
            logger.warning("PostgreSQL not available: %s", exc)
    
    # Initialize Event Hook Components
    if settings.event_hooks_enabled and settings.database_url:
        try:
            from app.event_hook_store import EventHookStore
            from app.event_hook_delivery import DeliveryService

            state.event_hook_store = EventHookStore(settings.database_url)
            await state.event_hook_store.create_tables()

            state.delivery_service = DeliveryService(settings.database_url, instance_id=uuid.uuid4().hex)
            await state.delivery_service.create_tables()

            s = settings
            await state.delivery_service.start(
                poll_interval=s.event_hooks_poll_interval,
                connect_timeout=s.event_hooks_connect_timeout,
                read_timeout=s.event_hooks_read_timeout,
                max_attempts=s.event_hooks_max_attempts,
                retry_base_sec=s.event_hooks_retry_base_sec,
                retry_max_sec=s.event_hooks_retry_max_sec,
                lease_ttl=s.event_hooks_lease_ttl,
                retention_sent_days=s.event_hooks_retention_sent_days,
                retention_dead_days=s.event_hooks_retention_dead_days,
            )
            logger.info("Event hook delivery service started")
        except Exception as exc:
            logger.warning("Event hooks not available: %s", exc)

    logger.info("Security Components Initialized")
    logger.info("Swarm Mode Ready (redis Job Queue, Circuit Breaker, Distributed Locks)")

    logger.info("Web SSH Gateway started on %s:%d", settings.uvicorn_host, settings.uvicorn_port)
    yield
    
    # Graceful Shutdown: Drain Active Jobs
    logger.info("Starting Graceful Shutdown...")
    
    # Wait For Active Jobs To Complete (max 30s)
    if state.job_manager:
        active_jobs = [j for j in state.job_manager._jobs.values() if j.status == "running"]
        if active_jobs:
            logger.info("Waiting for %d active jobs to complete...", len(active_jobs))
            await asyncio.wait_for(
                state.job_manager.wait_for_all_jobs(),
                timeout=30.0
            )
    

    # Cleanup
    await state.context_manager.stop_cleanup_task()
    await state.job_manager.stop_cleanup_task()
    await state.manager.stop_cleanup_task()
    await state.manager.close_all()
    
    if state.redis_queue:
        await state.redis_queue.disconnect()
    if state.dist_lock:
        await state.dist_lock.disconnect()
    ds = getattr(state, 'delivery_service', None)
    if ds:
        await ds.close()
        logger.info("Event hook delivery service shut down")
    if state.session_store:
        await state.session_store.disconnect()
    if state.host_key_store:
        await state.host_key_store.disconnect()
    
    logger.info("Web SSH Gateway Shutdown Complete")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Web SSH Gateway",
    description="Execute SSH commands through a web browser",
    version="1.0.0",
    lifespan=lifespan,
    responses={
        422: {
            "model": ValidationErrorResponse,
            "description": "Request validation failed",
        }
    },
)

# Tags
TAGS_META = {
    "ssh": "SSH session management (connect, execute, disconnect)",
    "files": "File operations (read, edit, upload, download)",
    "jobs": "Background job execution and monitoring",
    "git": "Git repository operations",
    "context": "Development contexts with git awareness",
    "templates": "Code templates",
    "servers": "Saved server management",
    "snapshots": "Project snapshots for recovery",
    "webhooks": "CI/CD webhooks",
    "known-hosts": "Host key store management",
    "logs": "Remote log reading (journald, docker)",
    "help": "API help and endpoint discovery",
    "code": "Code intelligence (search, insert, complete)",
    "system": "System endpoints (health, metrics, config)",
}

def _path_tag(path: str) -> str:
    if path == "/" or path == "/health" or path == "/metrics":
        return "system"
    if path.startswith("/api/known-hosts"):
        return "known-hosts"
    if path.startswith("/api/logs"):
        return "logs"
    if path.startswith("/api/help"):
        return "help"
    if path.startswith("/api/servers"):
        return "servers"
    if path.startswith("/api/jobs"):
        return "jobs"
    if path.startswith("/api/file") or path.startswith("/api/batch") or path.startswith("/api/file"):
        return "files"
    if path.startswith("/api/ssh"):
        return "ssh"
    if path.startswith("/api/git"):
        return "git"
    if path.startswith("/api/context") or path.startswith("/api/validate"):
        return "context"
    if path.startswith("/api/templates"):
        return "templates"
    if path.startswith("/api/snapshots"):
        return "snapshots"
    if path.startswith("/api/webhooks"):
        return "webhooks"
    if path.startswith("/api/code") or path.startswith("/api/ast") or path.startswith("/api/refactor"):
        return "code"
    if path.startswith("/api/sdk"):
        return "system"
    if path.startswith("/api/search") or path.startswith("/api/replace"):
        return "code"
    if path.startswith("/api/project") or path.startswith("/api/analytics"):
        return "code"
    if path.startswith("/api/scaffold"):
        return "templates"
    if path.startswith("/api/recovery"):
        return "context"
    if path.startswith("/api/tree"):
        return "files"
    if path.startswith("/api/config"):
        return "system"
    if path.startswith("/api/circuit"):
        return "system"
    if path.startswith("/api/bulk"):
        return "files"
    return "system"

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

# Examples For Key Operations
EXAMPLES: dict[tuple[str, str], dict] = {
    ("/api/ssh/connect", "post"): {
        "summary": "Connect to SSH server",
        "value": {"host": "10.0.0.1", "port": 22, "username": "deploy", "password": "secret"},
    },
    ("/api/ssh/execute", "post"): {
        "summary": "Execute a command",
        "value": {"session_id": "abc123", "command": "ls -la", "timeout": 30},
    },
    ("/api/file/read", "post"): {
        "summary": "Read file content",
        "value": {"session_id": "abc123", "path": "/etc/hostname"},
    },
    ("/api/file/write", "post"): {
        "summary": "Write file content",
        "value": {"session_id": "abc123", "path": "/root/test.txt", "content": "hello", "mode": "write"},
    },
    ("/api/jobs/run", "post"): {
        "summary": "Start a background job",
        "value": {"session_id": "abc123", "command": "apt update", "timeout": 3600},
    },
    ("/api/context/create", "post"): {
        "summary": "Create a development context",
        "value": {"session_id": "abc123", "path": "/root/project", "name": "my_project"},
    },
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

# Structured Error Codes
ERROR_CODE_MAP: dict[tuple[int, str], str] = {
    (404, "session"): "SESSION_NOT_FOUND",
    (404, "pty"): "SESSION_NOT_FOUND",
    (404, "server"): "SERVER_NOT_FOUND",
    (404, "context"): "CONTEXT_NOT_FOUND",
    (404, "template"): "TEMPLATE_NOT_FOUND",
    (404, "job"): "JOB_NOT_FOUND",
    (404, "sdk"): "SDK_NOT_FOUND",
    (404, "webhook"): "WEBHOOK_NOT_FOUND",
    (404, "snapshot"): "SNAPSHOT_NOT_FOUND",
    (409, "already exists"): "ALREADY_EXISTS",
    (502, "connection"): "UPSTREAM_CONNECTION_FAILED",
    (502, ""): "BAD_GATEWAY",
    (504, ""): "GATEWAY_TIMEOUT",
    (401, ""): "UNAUTHORIZED",
    (400, ""): "BAD_REQUEST",
    (500, ""): "INTERNAL_ERROR",
    (422, ""): "VALIDATION_ERROR",
}

HINTS: dict[str, str] = {
    "SESSION_NOT_FOUND": "Create a session first via POST /api/ssh/connect",
    "SERVER_NOT_FOUND": "Use POST /api/servers to create one",
    "CONTEXT_NOT_FOUND": "Use POST /api/context/create to create a context",
    "TEMPLATE_NOT_FOUND": "Check available templates via GET /api/templates",
    "JOB_NOT_FOUND": "Use GET /api/jobs to list active jobs",
    "WEBHOOK_NOT_FOUND": "Use GET /api/webhooks to list registered webhooks",
    "SNAPSHOT_NOT_FOUND": "Use GET /api/snapshots to list snapshots",
    "SDK_NOT_FOUND": "Check that SDK was built and deployed",
    "ALREADY_EXISTS": "The resource already exists; pick a different identifier",
    "UPSTREAM_CONNECTION_FAILED": "The SSH server may be unreachable or refusing connections",
    "BAD_GATEWAY": "The upstream SSH server returned an error",
    "GATEWAY_TIMEOUT": "The upstream SSH server did not respond in time; retry may help",
    "UNAUTHORIZED": "Provide valid credentials (password or private key)",
    "BAD_REQUEST": "Check request parameters and try again",
    "INTERNAL_ERROR": "The server encountered an internal error; retry or contact support",
    "VALIDATION_ERROR": "Check the missing or invalid fields listed in errors[]",
    "RATE_LIMIT_EXCEEDED": "Reduce request frequency and retry after the indicated wait time",
}

RETRYABLE_CODES = {"BAD_GATEWAY", "GATEWAY_TIMEOUT", "INTERNAL_ERROR", "UPSTREAM_CONNECTION_FAILED", "RATE_LIMIT_EXCEEDED"}

def _auto_code(status_code: int, message: str) -> str:
    for (code, keyword), err_code in ERROR_CODE_MAP.items():
        if status_code == code and (not keyword or keyword in message.lower()):
            return err_code
    return "INTERNAL_ERROR"

def _hint(code: str) -> str:
    return HINTS.get(code, "")

def _err(status_code: int, message: str, *, code: str | None = None, retryable: bool | None = None, hint: str | None = None) -> dict:
    if code is None:
        code = _auto_code(status_code, message)
    if retryable is None:
        retryable = code in RETRYABLE_CODES
    if hint is None:
        hint = _hint(code)
    return {
        "message": message,
        "code": code,
        "retryable": retryable,
        "hint": hint,
        "http_status": status_code,
    }

def _set_errors(op: dict, path: str):
    tag = _path_tag(path)
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
    )

    schema["tags"] = [{"name": k, "description": v} for k, v in TAGS_META.items()]

    # Server Metadata For Codegen
    schema["servers"] = [{"url": "/", "description": "Web SSH Gateway API"}]

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
                            "message": {"type": "string", "description": "Human-readable error message"},
                            "code": {"type": "string", "description": "Machine-readable error code (e.g. SESSION_NOT_FOUND)"},
                            "retryable": {"type": "boolean", "description": "Whether the operation can be retried"},
                            "hint": {"type": "string", "description": "Guidance for resolving the error"},
                            "http_status": {"type": "integer", "description": "HTTP status code"},
                            "errors": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/ValidationFieldItem"},
                                "description": "Field-level validation errors (422 only)",
                            },
                            "total_errors": {"type": "integer", "description": "Total number of validation errors"},
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
                    "retryable": {"type": "boolean", "description": "Always false for validation errors"},
                    "hint": {"type": "string", "description": "Guidance to fix validation errors"},
                    "http_status": {"type": "integer", "description": "Always 422"},
                    "errors": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/ValidationFieldItem"},
                        "description": "Per-field validation errors",
                    },
                    "total_errors": {"type": "integer", "description": "Count of validation errors"},
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
                    "status": {"type": "string", "enum": ["started", "running", "completed", "cancelled"]},
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
                    "code": {"type": "string", "description": "Error code (e.g. TIMEOUT, CONNECTION_LOST)"},
                    "ts": {"type": "number", "description": "Unix timestamp"},
                },
            },
        },
    }

    schema["components"]["securitySchemes"] = {
        "ApiKeyHeader": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
    }

    # --- Default Response Headers ---
    COMMON_RESPONSE_HEADERS = {
        "X-Request-ID": {"schema": {"type": "string"}, "description": "Unique request identifier for tracing"},
        "X-RateLimit-Limit": {"schema": {"type": "integer"}, "description": "Rate limit ceiling (requests per window)"},
        "X-RateLimit-Remaining": {"schema": {"type": "integer"}, "description": "Requests remaining in current window"},
        "X-RateLimit-Reset": {"schema": {"type": "integer"}, "description": "Unix timestamp when rate limit resets"},
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
        ("/api/ssh/connect", "post"): {"session_id": "abc123", "host": "10.0.0.1", "port": 22, "username": "deploy"},
        ("/api/ssh/execute", "post"): {"session_id": "abc123", "exit_code": 0, "stdout": "total 42\n-rw-r--r-- 1 root root ...", "stderr": "", "duration_ms": 150},
        ("/api/context/create", "post"): {"context_id": "ctx_abc123", "name": "my_project", "path": "/root/project", "status": "ready"},
        ("/api/jobs/run", "post"): {"job_id": "job_abc123", "status": "queued"},
        ("/api/file/read", "post"): {"path": "/etc/hostname", "content": "my-server\n", "size": 10, "encoding": "utf-8"},
        ("/api/file/write", "post"): {"path": "/root/test.txt", "size": 5, "encoding": "utf-8"},
        ("/", "get"): {"service": "Web SSH Gateway", "version": "3.0.0", "status": "running"},
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
                return {"date-time": "2026-01-01T00:00:00Z", "uri": "https://example.com", "email": "user@example.com"}.get(schema_def["format"], "string")
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
            tag = _path_tag(path)
            op.setdefault("tags", [tag])

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
            _set_errors(op, path)

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
                        resp200.setdefault("content", {}).setdefault("application/json", {})["example"] = response_examples[key]

            # --- Parameter Descriptions ---
            for param in op.get("parameters", []):
                name = param.get("name", "")
                if name in PARAM_DESC and not param.get("description"):
                    param["description"] = PARAM_DESC[name]
                elif not param.get("description"):
                    param["description"] = name.replace("_", " ").title()

    # Security: /health And /api/capabilities Are Public; Everything Else Requires X-api-key
    for path, methods in schema.get("paths", {}).items():
        for method, op in methods.items():
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
    allow_headers=["*"],
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
    exc = await auth_check(request, settings)
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
    message = str(exc)
    raise HTTPException(status_code=status_code, detail=_err(status_code, message))


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
        
        field_errors.append({
            "field": field,
            "error": message,
            "type": error_type,
        })
    
    raise HTTPException(
        status_code=422,
        detail={
            "message": "Request validation failed",
            "code": "VALIDATION_ERROR",
            "retryable": False,
            "hint": "Check missing and invalid fields listed in errors[]",
            "http_status": 422,
            "errors": field_errors,
            "total_errors": len(field_errors),
        }
    )


from app.state import _err

# ---------------------------------------------------------------------------
# Router registration — route handlers live in app/routers/
# ---------------------------------------------------------------------------

from app.routers.ssh import router as ssh_router
from app.routers.files import router as files_router
from app.routers.jobs import router as jobs_router
from app.routers.git import router as git_router
from app.routers.context import router as context_router
from app.routers.system import router as system_router
from app.routers.logs import router as logs_router
from app.routers.templates import router as templates_router
from app.routers.event_hooks import router as event_hooks_router

app.include_router(ssh_router)
app.include_router(files_router)
app.include_router(jobs_router)
app.include_router(git_router)
app.include_router(context_router)
app.include_router(system_router)
app.include_router(logs_router)
app.include_router(templates_router)
app.include_router(event_hooks_router)

# Static files mount (after all router includes so static routes take precedence)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
