import time
import uuid
from typing import Any

CONTRACT_VERSION = "1"

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
    "DOCKER_COMMAND_FAILED",
    "DOCKER_ADMIN_SCOPE_REQUIRED",
    "DOCKER_EXEC_COMMAND_BLOCKED",
    "DOCKER_EXEC_CONTAINER_NOT_FOUND",
    "DOCKER_EXEC_TIMEOUT",
    "DOCKER_RUN_ALLOWLIST_NOT_CONFIGURED",
    "DOCKER_RUN_IMAGE_NOT_ALLOWED",
    "DOCKER_RUN_IMAGE_INVALID",
    "DOCKER_RUN_CONTAINER_CREATE_FAILED",
    "DOCKER_RUN_TIMEOUT",
    "DOCKER_RMI_INVALID_REFERENCE",
    "DOCKER_RMI_FAILED",
    "DOCKER_VOLUME_RM_INVALID_NAME",
    "DOCKER_VOLUME_RM_FAILED",
    "TOOL_EXECUTION_FAILED",
    "POLICY_DENIED",
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


def tool_error(
    tool: str,
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
    meta["redacted"] = bool(redacted)
    meta["truncated"] = bool(truncated)
    meta["source"] = source if source in SAFE_SOURCE_VALUES else "unknown"
    if duration_ms is not None:
        meta["duration_ms"] = round(duration_ms, 1)
    meta.update(extra_meta)

    error: dict[str, Any] = {
        "code": code,
        "message": str(message),
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
