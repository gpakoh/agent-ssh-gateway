"""Tests for structured MCP tool result helpers."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"


def _import_tool_results():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.syspath_prepend(str(EXAMPLE_DIR))
    sys.modules.pop("tool_results", None)
    mod = importlib.import_module("tool_results")
    monkeypatch.undo()
    return mod


class TestTextResult:
    def test_shape(self):
        tr = _import_tool_results()
        result = tr.text_result(
            tool="gateway_health",
            title="Gateway health",
            text="ok",
            data={"status": "ok"},
        )

        assert result["content"][0]["type"] == "text"
        assert result["content"][0]["text"] == "ok"
        assert result["structuredContent"] == {"status": "ok"}
        assert result["_meta"]["agent_ssh_gateway_tool"] == "gateway_health"
        assert result["_meta"]["agent_ssh_gateway_title"] == "Gateway health"
        assert "isError" not in result

    def test_default_data_is_empty(self):
        tr = _import_tool_results()
        result = tr.text_result(
            tool="gateway_health",
            title="Gateway health",
            text="ok",
        )
        assert result["structuredContent"] == {}

    def test_no_isError(self):
        tr = _import_tool_results()
        result = tr.text_result(
            tool="gateway_health",
            title="Gateway health",
            text="ok",
        )
        assert "isError" not in result


class TestErrorResult:
    def test_shape(self):
        tr = _import_tool_results()
        result = tr.error_result(
            tool="gateway_execute_restricted",
            title="Restricted execute",
            error="denied",
        )

        assert result["isError"] is True
        assert result["content"][0]["type"] == "text"
        assert "denied" in result["content"][0]["text"]
        assert result["structuredContent"]["error"] == "denied"
        assert result["_meta"]["agent_ssh_gateway_tool"] == "gateway_execute_restricted"

    def test_with_data(self):
        tr = _import_tool_results()
        result = tr.error_result(
            tool="gateway_health",
            title="Health",
            error="timeout",
            data={"code": 504},
        )
        assert result["structuredContent"]["error"] == "timeout"
        assert result["structuredContent"]["code"] == 504
