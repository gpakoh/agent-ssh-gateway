"""Regression tests for run_tool() discarding the actual tool result.

run_tool()'s final fallback branch used to run whenever fn() returned a
plain dict with no "ok" key -- e.g. build_command_result()'s
{"outcome", "exit_code", "stdout", "stderr", ...}, which is exactly what
git_status()/recent_commits()/git_diff_stat() and every SSH/search tool
built on run_project_command() return. That fallback discarded the real
data entirely and returned only the caller-supplied static success_text
(e.g. "Collected project git status.") as the result -- the actual
stdout was never seen by the MCP client.

Fixed: the raw dict itself becomes the result (success_text is kept as
supplementary meta, not a replacement), and a non-zero exit_code is
surfaced as a TOOL_EXECUTION_FAILED error rather than reported as
success.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

MCP_DIR = str(Path(__file__).resolve().parents[1] / "examples" / "mcp_server")
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)


def _fn(data: dict[str, Any]):
    def _inner() -> dict[str, Any]:
        return data
    return _inner


class TestRunToolPreservesRawResult:
    def test_stdout_is_preserved_not_discarded(self) -> None:
        from examples.mcp_server.server import run_tool

        raw = {
            "outcome": "passed",
            "exit_code": 0,
            "stdout": "M app/models.py\n",
            "stderr": "",
            "execution_duration_ms": 42,
            "job_id": None,
        }
        result = run_tool(
            tool="git_status",
            title="git status",
            fn=_fn(raw),
            success_text="Collected project git status.",
        )

        assert result["ok"] is True
        assert result["result"] == raw
        assert result["result"]["stdout"] == "M app/models.py\n"

    def test_success_text_kept_as_metadata_not_replacing_result(self) -> None:
        from examples.mcp_server.server import run_tool

        raw = {"outcome": "passed", "exit_code": 0, "stdout": "ok\n", "stderr": ""}
        result = run_tool(
            tool="recent_commits",
            title="recent commits",
            fn=_fn(raw),
            success_text="Collected project recent commits.",
        )

        assert result["result"] == raw
        assert result["meta"].get("success_text") == "Collected project recent commits."

    def test_nonzero_exit_code_is_reported_as_error_not_success(self) -> None:
        from examples.mcp_server.server import run_tool

        raw = {
            "outcome": "failed",
            "exit_code": 1,
            "stdout": "",
            "stderr": "fatal: not a git repository (or any parent up to mount point /)",
            "execution_duration_ms": 5,
        }
        result = run_tool(
            tool="git_status",
            title="git status",
            fn=_fn(raw),
            success_text="Collected project git status.",
        )

        assert result["ok"] is False
        assert result["error"]["code"] == "TOOL_EXECUTION_FAILED"

    def test_nonzero_exit_code_error_still_preserves_stdout_stderr(self) -> None:
        """Even when surfaced as an error, exit_code/stdout/stderr must stay
        available to the caller -- not be discarded in favor of a bare
        error message."""
        from examples.mcp_server.server import run_tool

        raw = {
            "outcome": "failed",
            "exit_code": 1,
            "stdout": "",
            "stderr": "fatal: not a git repository",
            "execution_duration_ms": 5,
        }
        result = run_tool(
            tool="git_status",
            title="git status",
            fn=_fn(raw),
            success_text="Collected project git status.",
        )

        assert result["result"]["exit_code"] == 1
        assert result["result"]["stderr"] == "fatal: not a git repository"

    def test_zero_exit_code_with_empty_stdout_is_still_success(self) -> None:
        """A clean run with no output (e.g. git status on a clean tree) must
        not be mistaken for a failure."""
        from examples.mcp_server.server import run_tool

        raw = {"outcome": "passed", "exit_code": 0, "stdout": "", "stderr": ""}
        result = run_tool(
            tool="git_status",
            title="git status",
            fn=_fn(raw),
            success_text="Collected project git status.",
        )

        assert result["ok"] is True
        assert result["result"] == raw

    def test_canonical_envelope_passthrough_unaffected(self) -> None:
        """A tool whose fn() already returns the canonical {"ok": ...}
        envelope must still pass through unchanged (pre-existing behavior,
        not touched by this fix)."""
        from examples.mcp_server.server import run_tool

        canonical = {
            "ok": True,
            "tool": "read_file",
            "result": {"content": "hello"},
            "error": None,
            "meta": {"duration_ms": 12.3},
        }
        result = run_tool(
            tool="read_file",
            title="read file",
            fn=_fn(canonical),
            success_text="unused",
        )

        assert result["result"] == {"content": "hello"}
        assert result["meta"]["duration_ms"] == 12.3

    def test_canonical_error_envelope_preserves_result_stdout_stderr(self) -> None:
        """Regression: a canonical {"ok": False} envelope that already
        carries a result (e.g. _run_uv_tool's error path with stdout/stderr)
        must keep that result. The old error branch rebuilt a bare
        tool_error without result, discarding stdout/stderr entirely."""
        from examples.mcp_server.server import run_tool

        envelope = {
            "ok": False,
            "tool": "run_mypy",
            "result": {
                "outcome": "error",
                "exit_code": 2,
                "stdout": "error: cannot find module 'app'\n",
                "stderr": "mypy: fatal error\n",
                "execution_duration_ms": 123,
                "job_id": "j2",
            },
            "error": {
                "code": "TOOL_EXECUTION_FAILED",
                "message": "mypy exit code 2",
                "retryable": False,
            },
            "meta": {
                "tool": "run_mypy",
                "duration_ms": 45.6,
                "redacted": False,
                "source": "gateway",
            },
        }
        result = run_tool(
            tool="run_mypy",
            title="run mypy",
            fn=_fn(envelope),
            success_text="Ran project mypy.",
        )

        assert result["ok"] is False
        assert result["error"]["code"] == "TOOL_EXECUTION_FAILED"
        assert result["error"]["message"] == "mypy exit code 2"
        assert result["result"] == envelope["result"]
        assert result["result"]["stdout"] == "error: cannot find module 'app'\n"
        assert result["result"]["stderr"] == "mypy: fatal error\n"
