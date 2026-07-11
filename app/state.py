"""Global application state — initialized in lifespan()."""

from app.batch_operations import BatchOperationsManager
from app.bulk_operations_v2 import BulkOperationsManager
from app.circuit_breaker import CircuitBreakerRegistry
from app.code_intelligence import CodeIntelligence
from app.context_manager import ContextManager
from app.distributed_lock import DistributedLock
from app.event_hook_delivery import DeliveryService
from app.event_hook_store import EventHookStore
from app.file_editor import FileEditor
from app.file_tree import FileTreeExplorer
from app.job_manager import JobManager
from app.known_hosts import HostKeyStore
from app.project_analytics import ProjectAnalytics
from app.redis_queue import RedisJobQueue
from app.search_replace import GlobalSearchReplace
from app.security import AuditLogger, SecretManager
from app.server_manager import ServerManager
from app.session_store import SessionStore
from app.snapshot_manager import SnapshotManager
from app.ssh_manager import SSHSessionManager
from app.webhook_manager import WebhookManager

manager: SSHSessionManager | None = None
job_manager: JobManager | None = None
file_editor: FileEditor | None = None
context_manager: ContextManager | None = None
batch_manager: BatchOperationsManager | None = None
code_intelligence: CodeIntelligence | None = None
search_replace: GlobalSearchReplace | None = None
file_tree: FileTreeExplorer | None = None
server_manager: ServerManager | None = None
snapshot_manager: SnapshotManager | None = None
webhook_manager: WebhookManager | None = None
analytics: ProjectAnalytics | None = None
secret_manager: SecretManager | None = None
audit_logger: AuditLogger | None = None
redis_queue: RedisJobQueue | None = None
circuit_breakers: CircuitBreakerRegistry | None = None
dist_lock: DistributedLock | None = None
session_store: SessionStore | None = None
host_key_store: HostKeyStore | None = None
bulk_ops: BulkOperationsManager | None = None
event_hook_store: EventHookStore | None = None
delivery_service: DeliveryService | None = None
from fastapi import WebSocket  # noqa: E402

from app.agent_token_store import AgentTokenStore  # noqa: E402

agent_token_store: AgentTokenStore | None = None
active_websockets: set[WebSocket] = set()


def get_agent_token_store() -> AgentTokenStore:
    if agent_token_store is None:
        raise RuntimeError("AgentTokenStore not initialized")
    return agent_token_store


# ---------------------------------------------------------------------------
# Error Helpers (shared Across Routers)
# ---------------------------------------------------------------------------

RETRYABLE_CODES = {
    "BAD_GATEWAY",
    "GATEWAY_TIMEOUT",
    "INTERNAL_ERROR",
    "UPSTREAM_CONNECTION_FAILED",
    "RATE_LIMIT_EXCEEDED",
}

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
    (404, "cannot read"): "FILE_NOT_FOUND",
    (404, "file not found"): "FILE_NOT_FOUND",
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
    "FILE_NOT_FOUND": "The requested file does not exist at the specified path",
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


def _err(
    status_code: int,
    message: str,
    *,
    code: str | None = None,
    retryable: bool | None = None,
    hint: str | None = None,
) -> dict:
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
