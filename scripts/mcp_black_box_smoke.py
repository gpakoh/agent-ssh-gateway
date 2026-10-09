#!/usr/bin/env python3
"""Authenticated black-box smoke for the deployed bearer-mode MCP server.

The deploy path runs this inside ``mcp-server`` after container health is green.
It performs a real authenticated ``initialize`` + ``tools/list`` handshake, and
can require one critical tool schema via ``MCP_SMOKE_REQUIRED_TOOL`` and
``MCP_SMOKE_REQUIRED_INPUTS``.

Only the read-only handshake is retried. A timeout/truncated response may happen
while a freshly-recreated container is becoming reachable, so a retry starts a
new MCP session instead of reusing an outcome we cannot prove. No mutating tool
call is ever replayed by this smoke check.

Exit code:
    0 - initialize + tools/list succeeded and any required schema is present
    1 - missing token, transport/protocol error, partial result, or schema drift
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import time
from typing import Any

TIMEOUT = float(os.environ.get("MCP_SMOKE_TIMEOUT", "10"))


class SmokeError(RuntimeError):
    """Deterministic protocol/schema failure; retrying would not help."""


class TransientSmokeError(SmokeError):
    """Read-only handshake failure that may be retried with a fresh session."""


def _retry_settings() -> tuple[int, float]:
    try:
        attempts = int(os.environ.get("MCP_SMOKE_ATTEMPTS", "2"))
        delay = float(os.environ.get("MCP_SMOKE_RETRY_DELAY", "0.25"))
    except ValueError as exc:
        raise SmokeError(f"invalid retry settings: {exc}") from exc
    if attempts < 1 or attempts > 5:
        raise SmokeError("MCP_SMOKE_ATTEMPTS must be between 1 and 5")
    if delay < 0 or delay > 10:
        raise SmokeError("MCP_SMOKE_RETRY_DELAY must be between 0 and 10 seconds")
    return attempts, delay


def _required_schema() -> tuple[str, tuple[str, ...]]:
    tool_name = os.environ.get("MCP_SMOKE_REQUIRED_TOOL", "").strip()
    raw_inputs = os.environ.get("MCP_SMOKE_REQUIRED_INPUTS", "")
    inputs = tuple(dict.fromkeys(item.strip() for item in raw_inputs.split(",") if item.strip()))
    if inputs and not tool_name:
        raise SmokeError("MCP_SMOKE_REQUIRED_INPUTS requires MCP_SMOKE_REQUIRED_TOOL")
    return tool_name, inputs


def _mcp_request(body: dict[str, Any], token: str, sid: str | None = None) -> tuple[dict[str, Any], str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    if sid:
        headers["Mcp-Session-Id"] = sid

    try:
        conn = http.client.HTTPConnection("127.0.0.1", 8087, timeout=TIMEOUT)
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        raise TransientSmokeError(f"connect failed: {exc}") from exc

    try:
        try:
            conn.request("POST", "/mcp", json.dumps(body), headers)
            resp = conn.getresponse()
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise TransientSmokeError(f"request failed: {exc}") from exc

        status = int(getattr(resp, "status", 200) or 200)
        if status >= 500:
            resp.close()
            raise TransientSmokeError(f"HTTP {status}")
        if status < 200 or status >= 300:
            resp.close()
            raise SmokeError(f"HTTP {status}")

        buf = b""
        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf += chunk
                if b"\n\n" in buf:
                    break
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            resp.close()
            raise TransientSmokeError(f"response read failed: {exc}") from exc

        ret_sid = resp.getheader("mcp-session-id", "")
        resp.close()

        raw = buf.decode("utf-8", errors="replace")
        payload: dict[str, Any] | None = None
        frame_terminated = b"\n\n" in buf or b"\r\n\r\n" in buf
        for line in raw.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                parsed = json.loads(line[5:])
            except (TypeError, ValueError) as exc:
                if not frame_terminated:
                    raise TransientSmokeError(
                        f"truncated MCP SSE JSON before frame completion: {exc}"
                    ) from exc
                raise SmokeError(f"malformed SSE JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise SmokeError("MCP response data must be a JSON object")
            payload = parsed
            break

        if payload is None:
            if not raw.strip():
                raise TransientSmokeError("MCP response ended before a payload")
            raise SmokeError("MCP response contained no data frame")
        if payload.get("jsonrpc") != "2.0" or payload.get("id") != body.get("id"):
            raise SmokeError("MCP response id/jsonrpc mismatch")
        return payload, ret_sid
    finally:
        conn.close()


def _validate_required_tool_schema(tools: list[Any]) -> None:
    tool_name, required_inputs = _required_schema()
    if not tool_name:
        return

    matches = [tool for tool in tools if isinstance(tool, dict) and tool.get("name") == tool_name]
    if len(matches) != 1:
        raise SmokeError(f"required tool {tool_name!r} missing or ambiguous")

    schema = matches[0].get("inputSchema")
    if not isinstance(schema, dict):
        raise SmokeError(f"required tool {tool_name!r} has no inputSchema object")
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise SmokeError(f"required tool {tool_name!r} has incomplete inputSchema")

    missing_properties = [name for name in required_inputs if name not in properties]
    missing_required = [name for name in required_inputs if name not in required]
    if missing_properties or missing_required:
        raise SmokeError(
            f"required tool {tool_name!r} schema mismatch: "
            f"missing properties={missing_properties}, missing required={missing_required}"
        )


def _run_handshake(token: str) -> None:
    result, sid = _mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "black-box-smoke", "version": "1.0"},
            },
        },
        token,
    )
    if not sid:
        raise SmokeError("no session ID in initialize response")
    if "error" in result:
        raise SmokeError(f"initialize error: {result['error']}")
    if not isinstance(result.get("result"), dict):
        raise SmokeError("initialize returned no result object")

    result2, _ = _mcp_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        token,
        sid=sid,
    )
    if "error" in result2:
        raise SmokeError(f"tools/list error: {result2['error']}")
    result_data = result2.get("result")
    if not isinstance(result_data, dict):
        raise SmokeError("tools/list returned no result object")
    tools = result_data.get("tools")
    if not isinstance(tools, list) or not tools:
        raise SmokeError("tools/list returned no tools")
    _validate_required_tool_schema(tools)


def main() -> int:
    token = os.environ.get("MCP_STREAMABLE_HTTP_BEARER_TOKEN", "").strip()
    if not token:
        print(
            "mcp_black_box_smoke: MCP_STREAMABLE_HTTP_BEARER_TOKEN not set",
            file=sys.stderr,
        )
        return 1

    try:
        attempts, retry_delay = _retry_settings()
        _required_schema()
    except SmokeError as exc:
        print(f"mcp_black_box_smoke: {exc}", file=sys.stderr)
        return 1

    for attempt in range(1, attempts + 1):
        try:
            _run_handshake(token)
            return 0
        except TransientSmokeError as exc:
            if attempt >= attempts:
                print(f"mcp_black_box_smoke: {exc}", file=sys.stderr)
                return 1
            print(
                f"mcp_black_box_smoke: transient attempt {attempt}/{attempts}: {exc}; "
                "reconnecting with a fresh MCP session",
                file=sys.stderr,
            )
            if retry_delay:
                time.sleep(retry_delay)
        except SmokeError as exc:
            print(f"mcp_black_box_smoke: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"mcp_black_box_smoke: {exc}", file=sys.stderr)
            return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
