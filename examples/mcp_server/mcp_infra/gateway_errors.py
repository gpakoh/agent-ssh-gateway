"""Gateway error classification for the MCP server package.

Moved verbatim from server.py (refactor #8): gateway error codes are
mapped onto the MCP tool layer's own ERROR_CODES vocabulary and turned
into clean, human-readable messages and hints.
"""

from __future__ import annotations

from typing import Any

from gateway_client import GatewayClientError
from tool_results import ERROR_CODES

# Maps the gateway's own error `code` values (from its structured
# {"detail": {"code": ..., "retryable": ...}} error body) onto this MCP
# tool layer's own ERROR_CODES vocabulary. Unmapped gateway codes still
# get their `retryable` flag honored (see _classify_gateway_error) —
# this map only controls the reported `code`, never `retryable`.
_GATEWAY_ERROR_CODE_MAP: dict[str, str] = {
    "INVALID_API_KEY": "AUTH_ERROR",
    "MASTER_KEY_REQUIRED": "AUTH_ERROR",
    # Generic 401 fallback from _auto_code (app/state.py's (401, "") entry) —
    # what SSHManagerError's AuthenticationError actually produces, since its
    # handler passes a deliberately generic message with no "api key"/"master
    # key" keyword for _auto_code to match on. Found by feeding this handler's
    # *real* response body through this *real* classifier (regression test),
    # not by re-mocking either side's assumed shape.
    "UNAUTHORIZED": "AUTH_ERROR",
    "SESSION_NOT_FOUND": "SESSION_NOT_FOUND",
    "FORBIDDEN": "PERMISSION_DENIED",
    "PROJECT_NOT_FOUND": "PROJECT_NOT_FOUND",
    "POLICY_DENIED": "PERMISSION_DENIED",
    "INVALID_INPUT": "INVALID_INPUT",
    "RATE_LIMITED": "RATE_LIMITED",
    # Same systematic audit: the gateway's own name for this is
    # RATE_LIMIT_EXCEEDED (both slowapi's 429 handler and SessionLimitError
    # produce it via app/state.py's (429, "") entry) — not "RATE_LIMITED".
    "RATE_LIMIT_EXCEEDED": "RATE_LIMITED",
    "TIMEOUT": "TIMEOUT",
    # TimeoutError's handler produces GATEWAY_TIMEOUT (app/state.py's
    # (504, "") entry), never the bare "TIMEOUT" this map already expected.
    "GATEWAY_TIMEOUT": "TIMEOUT",
    "WRITE_PERMISSION_DENIED": "PERMISSION_DENIED",
    # Reachable on every write-tool call against the gateway's own default
    # (WORKSPACE_READONLY=true) — not an edge case. app/routers/workspace.py
    # raises this via the normal HTTPException(detail=...) path (nested
    # shape, not the flat-body case above), so it always reached this map's
    # lookup, but the map itself just never had an entry for it.
    "WORKSPACE_READONLY": "PERMISSION_DENIED",
    # Both reachable on almost any malformed call — a 422 from FastAPI's own
    # request validation (missing/wrong-typed field, invalid JSON body) or a
    # 400 from a guardrail check (e.g. a blocked dangerous command pattern).
    # Both are flat-body responses (validation_exception_handler and several
    # HTTPException(400, ...) call sites bypass the nested-detail
    # convention the same way ssh_exception_handler does), so this was
    # falling all the way through to INTERNAL_ERROR — arguably the single
    # most commonly hit gap of this whole audit, since it fires on ordinary
    # mistakes while exploring a tool's parameters, not just rare failures.
    "VALIDATION_ERROR": "INVALID_INPUT",
    "BAD_REQUEST": "INVALID_INPUT",
}


def _gateway_error_message(exc: GatewayClientError) -> str:
    """Extract a clean, human-readable message from a GatewayClientError.

    str(exc) is "GET {path} failed: {status_code} {response.text}" (see
    GatewayClient._get/_post) -- the raw response body text verbatim,
    which for the gateway's own structured errors is a full serialized
    JSON blob (e.g. '{"detail": {"code": "JOB_NOT_FOUND", "message": "Job
    xyz not found", ...}}'), and the leading "GET {path}" then gets
    mangled to "[API]" by tool_error()'s own redaction. The gateway
    already computed a clean "message" inside that body -- use it.
    """
    if isinstance(exc.body, dict):
        detail = exc.body.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("message"), str) and detail["message"]:
            return detail["message"]
        if isinstance(exc.body.get("message"), str) and exc.body["message"]:
            return exc.body["message"]
    return str(exc)


def _gateway_error_hint(exc: GatewayClientError, code: str) -> str | None:
    """Extract the gateway's own per-error hint, when present.

    The gateway's per-error hints are written for the REST API surface
    (e.g. JOB_NOT_FOUND says "Use GET /api/jobs to list active jobs").
    There is no such MCP command, so job errors get an MCP-native hint
    instead of leaking a REST endpoint the caller cannot use.
    """
    if code == "JOB_NOT_FOUND":
        return "The job no longer exists (it may have expired); re-run the tool to start a new job, or call job_status/job_result with the id of a job returned by this run"
    if isinstance(exc.body, dict):
        detail = exc.body.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("hint"), str) and detail["hint"]:
            return detail["hint"]
    if code == "FILE_NOT_FOUND":
        return "The requested file does not exist at the specified path"
    if code == "WAIT_TIMEOUT":
        return "The command is still running server-side; call job_status/job_result with error.details.job_id to check on it or retrieve the final result once it completes."
    return None


def _classify_gateway_error(exc: GatewayClientError) -> tuple[str, bool]:
    """Classify a GatewayClientError into (error_code, retryable).

    Prefers the gateway's own structured error body — the gateway
    already computes `code`/`retryable` correctly server-side (e.g.
    INVALID_API_KEY is always retryable=false) — over guessing from the
    bare HTTP status code alone. Falls back to status-code heuristics
    only when the body is missing or not the expected shape (e.g. a
    non-JSON error page from an intermediate proxy).
    """
    status = exc.status_code
    msg = str(exc).lower()

    if status is None and not isinstance(exc.body, dict):
        # Client-side error raised locally by GatewayClient (missing session
        # id, missing project root, invalid project/path) — there was no HTTP
        # exchange, so the gateway's status-code heuristics below cannot
        # apply and must not classify this as an internal failure.
        return "INVALID_INPUT", False

    if isinstance(exc.body, dict) and exc.body.get("wait_timed_out"):
        return "WAIT_TIMEOUT", True

    detail: dict[str, Any] | None = None
    if isinstance(exc.body, dict):
        maybe_detail = exc.body.get("detail")
        if isinstance(maybe_detail, dict):
            detail = maybe_detail
        elif isinstance(exc.body.get("retryable"), bool):
            # Not every gateway handler wraps its error under "detail" — the
            # SSHManagerError handler (session/connection/exec errors) returns
            # a flat {message, code, retryable, hint, http_status} body. Use
            # it directly rather than falling through to the blunt
            # status-code-only heuristics below, which can't distinguish
            # "session not found" from "file not found" on a bare 404.
            detail = exc.body

        if detail is not None and isinstance(detail.get("retryable"), bool):
            gateway_retryable: bool = detail["retryable"]
            gateway_code = detail.get("code")
            mapped_code = _GATEWAY_ERROR_CODE_MAP.get(gateway_code) if gateway_code else None
            if mapped_code is not None:
                return mapped_code, gateway_retryable
            if gateway_code in ERROR_CODES:
                assert isinstance(gateway_code, str)
                return gateway_code, gateway_retryable
            if status == 404 and ("file not found" in msg or "cannot read" in msg):
                return "FILE_NOT_FOUND", gateway_retryable
            return "INTERNAL_ERROR", gateway_retryable

    if status == 404 and ("file not found" in msg or "cannot read" in msg):
        return "FILE_NOT_FOUND", False

    if status == 401:
        return "AUTH_ERROR", False
    if status == 403:
        return "PERMISSION_DENIED", False

    if status is not None and status >= 500:
        return "INTERNAL_ERROR", True

    if status == 400:
        return "INVALID_INPUT", False
    if status == 404:
        return "FILE_NOT_FOUND", False
    if status == 422:
        return "INVALID_INPUT", False

    return "INTERNAL_ERROR", True
