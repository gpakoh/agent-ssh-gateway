"""Test helpers for canonical MCP tool response envelope assertions."""

from __future__ import annotations

from typing import Any


def assert_tool_envelope(
    response: dict[str, Any],
    *,
    ok: bool | None = None,
    tool: str | None = None,
    source: str | None = None,
    dangerous: bool | None = None,
    has_result: bool = True,
    has_error: bool | None = None,
    check_meta_contract: bool = False,
) -> None:
    """Assert that *response* follows the canonical MCP tool response envelope."""
    assert isinstance(response, dict), f"expected dict, got {type(response).__name__}"

    assert "ok" in response, "response missing 'ok'"
    assert isinstance(response["ok"], bool), (
        f"'ok' must be bool, got {type(response['ok']).__name__}"
    )

    assert "tool" in response, "response missing 'tool'"
    assert isinstance(response["tool"], str), (
        f"'tool' must be str, got {type(response['tool']).__name__}"
    )

    assert "result" in response, "response missing 'result'"

    assert "error" in response, "response missing 'error'"

    assert "meta" in response, "response missing 'meta'"
    assert isinstance(response["meta"], dict), (
        f"'meta' must be dict, got {type(response['meta']).__name__}"
    )

    if ok is not None:
        assert response["ok"] == ok, f"expected ok={ok}, got {response['ok']}"

    if tool is not None:
        assert response["tool"] == tool, f"expected tool={tool!r}, got {response['tool']!r}"

    if has_error is not None:
        if has_error:
            assert response["error"] is not None, "expected error field to be non-None"
        else:
            assert response["error"] is None, f"expected error=None, got {response['error']!r}"
    elif response["ok"]:
        assert response["error"] is None, (
            f"ok=true response should have error=None, got {response['error']!r}"
        )
    else:
        assert response["error"] is not None, "ok=false response should have non-None error"
        for key in ("code", "message", "retryable"):
            assert key in response["error"], f"error missing '{key}'"

    if source is not None:
        assert response["meta"].get("source") == source, (
            f"expected source={source!r}, got {response['meta'].get('source')!r}"
        )

    if dangerous is not None:
        assert response["meta"].get("dangerous") == dangerous, (
            f"expected dangerous={dangerous!r}, got {response['meta'].get('dangerous')!r}"
        )

    if not has_result:
        assert response["result"] is None, f"expected result=None, got {response['result']!r}"

    if check_meta_contract:
        meta = response["meta"]
        assert "contract_version" in meta, "meta missing 'contract_version'"
        assert isinstance(meta["contract_version"], str), (
            f"contract_version must be str, got {type(meta['contract_version']).__name__}"
        )
        assert "request_id" in meta, "meta missing 'request_id'"
        assert isinstance(meta["request_id"], str), (
            f"request_id must be str, got {type(meta['request_id']).__name__}"
        )
        assert len(meta["request_id"]) > 0, "request_id must not be empty"
        assert "duration_ms" in meta, "meta missing 'duration_ms'"
        assert isinstance(meta["duration_ms"], int | float), (
            f"duration_ms must be numeric, got {type(meta['duration_ms']).__name__}"
        )
        assert meta["duration_ms"] >= 0, f"duration_ms must be >= 0, got {meta['duration_ms']}"
        assert "truncated" in meta, "meta missing 'truncated'"
        assert isinstance(meta["truncated"], bool), (
            f"truncated must be bool, got {type(meta['truncated']).__name__}"
        )
        assert "warnings" in meta, "meta missing 'warnings'"
        assert isinstance(meta["warnings"], list), (
            f"warnings must be list, got {type(meta['warnings']).__name__}"
        )


def assert_docker_envelope(
    response: dict[str, Any],
    *,
    ok: bool | None = None,
    tool: str | None = None,
    dangerous: bool | None = None,
    has_result: bool = True,
    has_error: bool | None = None,
) -> None:
    """Assert Docker tool canonical envelope with source='docker'."""
    return assert_tool_envelope(
        response,
        ok=ok,
        tool=tool,
        source="docker",
        dangerous=dangerous,
        has_result=has_result,
        has_error=has_error,
    )
