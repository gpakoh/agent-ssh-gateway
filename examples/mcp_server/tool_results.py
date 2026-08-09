import re
import time
import uuid
from typing import Any

CONTRACT_VERSION = "1"

# Patterns to redact in error messages
_INTERNAL_PATH_RE = re.compile(r"(?<!['\w])/(?:media|root|app|home|tmp|var|etc|opt|usr|mnt|data)\S*")
_API_ENDPOINT_RE = re.compile(r"(?:GET|POST|PUT|DELETE|PATCH)\s+/api/\S+")


def _redact_error_message(message: str) -> tuple[str, bool]:
    """Strip internal paths and API endpoints from error messages.

    Returns (redacted_message, was_redacted).
    """
    original = message
    message = _INTERNAL_PATH_RE.sub("[PATH]", message)
    message = _API_ENDPOINT_RE.sub("[API]", message)
    return message, message != original

ERROR_CODES = {
    "TOOL_NOT_FOUND",
    "CONTAINER_NOT_FOUND",
    "SESSION_NOT_FOUND",
    "AUTH_ERROR",
    "POLICY_VIOLATION",
    "RATE_LIMITED",
    "TIMEOUT",
    "DEPENDENCY_MISSING",
    "INVALID_INPUT",
    "INTERNAL_ERROR",
    "FILE_NOT_FOUND",
    "CONFIRM_TOKEN_INVALID",
    "CONFIRM_TOKEN_EXPIRED",
    "CONFIRM_TOKEN_CONSUMED",
    "CONFIRM_SCOPE_DENIED",
    "DOCKER_COMMAND_FAILED",
    "DOCKER_ADMIN_SCOPE_REQUIRED",
    "DOCKER_EXEC_COMMAND_BLOCKED",
    "DOCKER_EXEC_CONTAINER_NOT_FOUND",
    "DOCKER_EXEC_TIMEOUT",
    "DOCKER_RUN_ALLOWLIST_NOT_CONFIGURED",
    "DOCKER_RUN_IMAGE_NOT_ALLOWED",
    "DOCKER_RUN_IMAGE_INVALID",
    "DOCKER_RUN_CONTAINER_CREATE_FAILED",
    "DANGEROUS_PERMISSIONS_BLOCKED",
    "DOCKER_RUN_TIMEOUT",
    "DOCKER_RMI_INVALID_REFERENCE",
    "DOCKER_RMI_FAILED",
    "DOCKER_VOLUME_RM_INVALID_NAME",
    "DOCKER_VOLUME_RM_FAILED",
    "TOOL_EXECUTION_FAILED",
    "POLICY_DENIED",
    "WAIT_TIMEOUT",
    "JOB_NOT_FOUND",
    "PERMISSION_DENIED",
    "CHECK_FAILED",
    "PROJECT_NOT_FOUND",
    "READ_ERROR",
    "PATTERN_NOT_FOUND",
    "SCAN_ERROR",
    "REMOTE_API_ERROR",
    "SECRET_PATH_DENIED",
    "FILE_READ_ERROR",
    "WORKSPACE_READONLY",
}

SAFE_SOURCE_VALUES = {
    "gateway",
    "docker",
    "postgres",
    "gitea",
    "github",
    "context7",
    "agent",
    "unknown",
}


def _now_ms() -> float:
    return time.monotonic()


def _make_meta(tool_name: str | None = None) -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "tool": tool_name or "unknown",
        "request_id": str(uuid.uuid4()),
        "duration_ms": 0,
        "truncated": False,
        "warnings": [],
    }


def tool_success(
    tool: str,
    result: Any = None,
    *,
    tool_name: str | None = None,
    duration_ms: float | None = None,
    redacted: bool = False,
    truncated: bool = False,
    source: str = "unknown",
    **extra_meta: Any,
) -> dict[str, Any]:
    effective_tool = tool_name or tool
    meta = _make_meta(effective_tool)
    meta["redacted"] = bool(redacted)
    meta["truncated"] = bool(truncated)
    meta["source"] = source if source in SAFE_SOURCE_VALUES else "unknown"
    if duration_ms is not None:
        meta["duration_ms"] = round(duration_ms, 1)
    meta.update(extra_meta)

    return {
        "ok": True,
        "tool": tool,
        "result": result,
        "error": None,
        "meta": meta,
    }


def validate_pagination(
    value: int,
    name: str,
    *,
    min_value: int = 1,
    max_value: int = 500,
) -> None:
    """Raise ValueError if a pagination parameter (per_page/limit/offset/etc.)
    is out of range.

    P2 audit finding: negative/zero per_page, limit, and offset arguments
    were accepted and passed straight through to list slicing (e.g.
    tools_list[offset:offset+limit]) or a remote API call -- not a crash,
    but silently produced confusing results (a negative offset selects
    from the end of the list; a negative/zero limit truncates or empties
    it) with no indication anything was wrong. Call sites that already
    map a bare ValueError to an INVALID_INPUT tool_error (gitea/github
    list tools via _remote_api_error, tools_manifest via _run_gateway)
    can just call this directly; others (docker_ps/images/stats) catch
    ValueError separately to return INVALID_INPUT instead of their
    subprocess-failure error code.
    """
    if value < min_value or value > max_value:
        raise ValueError(f"{name} must be between {min_value} and {max_value}, got {value}")


def tool_error(
    tool: str = "",
    code: str = "INTERNAL_ERROR",
    message: str = "An unexpected error occurred",
    *,
    tool_name: str | None = None,
    result: Any = None,
    retryable: bool = False,
    hint: str | None = None,
    details: dict[str, Any] | None = None,
    duration_ms: float | None = None,
    redacted: bool = False,
    truncated: bool = False,
    source: str = "unknown",
    **extra_meta: Any,
) -> dict[str, Any]:
    if code not in ERROR_CODES:
        code = "INTERNAL_ERROR"

    effective_tool = tool_name or tool
    meta = _make_meta(effective_tool)
    meta["truncated"] = bool(truncated)
    meta["source"] = source if source in SAFE_SOURCE_VALUES else "unknown"
    if duration_ms is not None:
        meta["duration_ms"] = round(duration_ms, 1)
    meta.update(extra_meta)

    raw_message = str(message)
    redacted_message, was_redacted = _redact_error_message(raw_message)
    meta["redacted"] = bool(redacted) or was_redacted
    if was_redacted:
        meta.setdefault("warnings", [])
        meta["warnings"].append("Error message redacted for security")

    error: dict[str, Any] = {
        "code": code,
        "message": redacted_message,
        "retryable": bool(retryable),
    }
    if hint is not None:
        error["hint"] = str(hint)
    if details is not None:
        error["details"] = details

    return {
        "ok": False,
        "tool": tool,
        "result": result,
        "error": error,
        "meta": meta,
    }


def build_command_result(
    outcome: str,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
    execution_duration_ms: int | None = None,
    job_id: str | None = None,
    timestamps: dict | None = None,
) -> dict:
    result = {
        "outcome": outcome,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "execution_duration_ms": execution_duration_ms,
        "job_id": job_id,
    }
    if timestamps:
        result["timestamps"] = timestamps
    return result


# Legacy helpers — kept for backward compatibility.
# Use tool_success() / tool_error() for new code.


def text_result(
    *,
    tool: str,
    title: str,
    text: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a structured MCP-compatible tool result."""
    structured = data or {}
    return {
        "content": [
            {
                "type": "text",
                "text": text,
            }
        ],
        "structuredContent": structured,
        "_meta": {
            "agent_ssh_gateway_tool": tool,
            "agent_ssh_gateway_title": title,
        },
    }


def error_result(
    *,
    tool: str,
    title: str,
    error: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a structured MCP-compatible error result."""
    structured = {
        "error": error,
        **(data or {}),
    }
    return {
        "isError": True,
        "content": [
            {
                "type": "text",
                "text": f"Error: {error}",
            }
        ],
        "structuredContent": structured,
        "_meta": {
            "agent_ssh_gateway_tool": tool,
            "agent_ssh_gateway_title": title,
        },
    }


def normalize_tool_result(
    tool: str,
    value: Any,
    *,
    source: str = "unknown",
    **extra_meta: Any,
) -> dict[str, Any]:
    """Wrap an arbitrary return value into the canonical envelope.

    Handles common cases:
    - ``dict`` with ``"ok"`` key → assumed already canonical, returned as-is.
    - ``dict`` with ``"error"`` key → wrapped as tool_error.
    - ``str`` starting with ``"error:"`` or ``"Error:"`` → wrapped as tool_error.
    - Everything else → wrapped as tool_success with result=value.
    """
    if isinstance(value, dict) and "ok" in value:
        return value

    if isinstance(value, dict) and "error" in value:
        return tool_error(
            tool=tool,
            message=str(value["error"]),
            result=value.get("result"),
            source=source,
            **extra_meta,
        )

    if isinstance(value, str) and value.lower().startswith("error:"):
        return tool_error(
            tool=tool,
            code="INTERNAL_ERROR",
            message=value,
            source=source,
            **extra_meta,
        )

    return tool_success(
        tool=tool,
        result=value,
        source=source,
        **extra_meta,
    )
