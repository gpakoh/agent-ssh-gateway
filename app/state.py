"""Global application state — initialized in lifespan()."""

from typing import Optional

from app.ssh_manager import SSHSessionManager
from app.job_manager import JobManager
from app.file_editor import FileEditor
from app.context_manager import ContextManager
from app.batch_operations import BatchOperationsManager
from app.code_intelligence import CodeIntelligence
from app.search_replace import GlobalSearchReplace
from app.file_tree import FileTreeExplorer
from app.server_manager import ServerManager
from app.snapshot_manager import SnapshotManager
from app.webhook_manager import WebhookManager
from app.project_analytics import ProjectAnalytics
from app.security import SecretManager, AuditLogger
from app.redis_queue import RedisJobQueue
from app.circuit_breaker import CircuitBreakerRegistry
from app.distributed_lock import DistributedLock
from app.session_store import SessionStore
from app.bulk_operations_v2 import BulkOperationsManager

manager: Optional[SSHSessionManager] = None
job_manager: Optional[JobManager] = None
file_editor: Optional[FileEditor] = None
context_manager: Optional[ContextManager] = None
batch_manager: Optional[BatchOperationsManager] = None
code_intelligence: Optional[CodeIntelligence] = None
search_replace: Optional[GlobalSearchReplace] = None
file_tree: Optional[FileTreeExplorer] = None
server_manager: Optional[ServerManager] = None
snapshot_manager: Optional[SnapshotManager] = None
webhook_manager: Optional[WebhookManager] = None
analytics: Optional[ProjectAnalytics] = None
secret_manager: Optional[SecretManager] = None
audit_logger: Optional[AuditLogger] = None
redis_queue: Optional[RedisJobQueue] = None
circuit_breakers: Optional[CircuitBreakerRegistry] = None
dist_lock: Optional[DistributedLock] = None
session_store: Optional[SessionStore] = None
bulk_ops: Optional[BulkOperationsManager] = None

# ---------------------------------------------------------------------------
# Error helpers (shared across routers)
# ---------------------------------------------------------------------------

RETRYABLE_CODES = {"BAD_GATEWAY", "GATEWAY_TIMEOUT", "INTERNAL_ERROR", "UPSTREAM_CONNECTION_FAILED", "RATE_LIMIT_EXCEEDED"}

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
