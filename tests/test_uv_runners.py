"""Tests for uv runner argv builder and target validation."""

import sys
from pathlib import Path

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "mcp_server"

sys.path.insert(0, str(EXAMPLE_DIR))
from mcp_client_tools import _build_uv_argv, _validate_targets  # noqa: E402


def test_build_ruff_argv():
    argv = _build_uv_argv("ruff", "/project", ["src/"])
    assert argv == [
        "uv", "run", "--frozen", "--directory", "/project", "--",
        "ruff", "check", "--", "src/",
    ]


def test_build_mypy_argv():
    argv = _build_uv_argv("mypy", "/project", ["src/main.py"])
    assert argv == [
        "uv", "run", "--frozen", "--directory", "/project", "--",
        "mypy", "--", "src/main.py",
    ]


def test_build_pytest_argv():
    argv = _build_uv_argv("pytest", "/project", ["tests/"])
    assert argv == [
        "uv", "run", "--frozen", "--directory", "/project", "--",
        "pytest", "--", "tests/",
    ]


def test_build_compileall_argv():
    argv = _build_uv_argv("compileall", "/project", ["src/"])
    assert argv == [
        "uv", "run", "--frozen", "--directory", "/project", "--",
        "python", "-m", "compileall", "--", "src/",
    ]


def test_invalid_target_with_traversal():
    with pytest.raises(ValueError, match="POLICY_DENIED"):
        _build_uv_argv("ruff", "/project", ["../outside"])


def test_invalid_target_absolute():
    result = _validate_targets("/project", ["/etc/passwd"])
    assert result == ["etc/passwd"]


class TestReadOnlyFallbackNoPathTraversal:
    """Bug 1: absolute target must not pass through _build_readonly_fallback_script."""

    def test_readonly_fallback_strips_absolute_targets(self):
        """Fallback script must convert /etc/passwd → <project_dir>/etc/passwd."""
        from mcp_client_tools import _build_readonly_fallback_script

        script = _build_readonly_fallback_script("pytest", "/project", ["/etc/passwd"])
        assert "/project/etc/passwd" in script
        last_line = [ln for ln in script.splitlines() if "pytest" in ln and "target_args" not in ln][-1]
        assert last_line.endswith("/project/etc/passwd 2>&1")

    def test_readonly_fallback_strips_absolute_targets_mypy(self):
        from mcp_client_tools import _build_readonly_fallback_script

        script = _build_readonly_fallback_script("mypy", "/project", ["/etc/shadow"])
        assert "/project/etc/shadow" in script

    def test_run_uv_tool_validates_targets_early(self, monkeypatch):
        """_run_uv_tool must validate targets before any SSH call."""
        from mcp_client_tools import _run_uv_tool

        call_log = []

        class FakeClient:
            def execute_raw(self, cmd, **kw):
                call_log.append(cmd)
                return {"job_id": "j1"}

            def wait_job(self, job_id, **kw):
                return {"exit_code": 0, "stdout": "", "stderr": ""}

        monkeypatch.setattr(
            "mcp_client_tools._resolve_project",
            lambda _: Path("/project"),
        )
        client = FakeClient()
        result = _run_uv_tool(client, "proj", "pytest", "run_pytest", target=["/etc/passwd"])

        assert result["ok"] is True
        assert "etc/passwd" in str(call_log)
        assert "/etc/passwd" not in str(call_log)


class _MockJobClient:
    """Mock gateway client that returns canned job responses.
    
    First execute_raw must be 'command -v uv' → exit 0 (uv present).
    Second execute_raw is the tool command → uses tool_exit_code.
    """

    def __init__(self, tool_exit_code: int, stdout: str = "", stderr: str = ""):
        self._calls: list[str] = []
        self._tool_exit = tool_exit_code
        self._stdout = stdout
        self._stderr = stderr

    def execute_raw(self, cmd: str, **kw) -> dict:
        self._calls.append(cmd)
        if cmd == "command -v uv":
            return {"job_id": "j0"}
        return {"job_id": "j1"}

    def wait_job(self, job_id: str, **kw) -> dict:
        if job_id == "j0":
            return {"exit_code": 0, "stdout": "/usr/bin/uv", "stderr": ""}
        return {
            "exit_code": self._tool_exit,
            "stdout": self._stdout,
            "stderr": self._stderr,
            "execution_duration_ms": 123,
            "job_id": "j1",
        }

    @property
    def calls(self) -> list[str]:
        return self._calls


class TestRunUvToolContract:
    """Regression tests for the _run_uv_tool response contract.
    
    exit_code=0 → tool_success(ok=True, result={outcome, exit_code, ...})
    exit_code=1 → tool_error(ok=False, result={outcome, exit_code, ...})
    """

    def _make_mock(self, exit_code=0, stdout="", stderr="", with_fallback=False):
        """Create a mock client with controlled tool exit_code.
        
        First execute_raw('command -v uv') → uv found.
        Second execute_raw(tool) → controlled exit_code + output.
        When with_fallback=True, also has execute_project_script.
        """
        class _Mock:
            def __init__(self):
                self._phase = 0
            def execute_raw(self, cmd, **kw):
                self._phase += 1
                return {"job_id": f"j{self._phase}"}
            def wait_job(self, job_id, **kw):
                if job_id == "j1":
                    return {"exit_code": 0, "stdout": "/usr/bin/uv", "stderr": ""}
                return {
                    "exit_code": exit_code,
                    "stdout": stdout,
                    "stderr": stderr,
                    "execution_duration_ms": 123,
                    "job_id": job_id,
                }
            def execute_project_script(self, proj, script, timeout_s=300):
                return {
                    "exit_code": exit_code,
                    "stdout": stdout,
                    "stderr": stderr,
                    "execution_duration_ms": 456,
                    "job_id": "j-fallback",
                }
        return _Mock()

    def test_pytest_success(self, monkeypatch):
        from mcp_client_tools import _run_uv_tool
        monkeypatch.setattr("mcp_client_tools._resolve_project", lambda _: Path("/project"))
        client = self._make_mock(exit_code=0, stdout="OK", stderr="")
        result = _run_uv_tool(client, "proj", "pytest", "run_pytest", target=["."])
        assert result["ok"] is True
        assert result["result"]["outcome"] == "passed"
        assert result["result"]["exit_code"] == 0
        assert result["result"]["stdout"] == "OK"
        assert result["error"] is None

    def test_pytest_failure(self, monkeypatch):
        from mcp_client_tools import _run_uv_tool
        monkeypatch.setattr("mcp_client_tools._resolve_project", lambda _: Path("/project"))
        client = self._make_mock(exit_code=1, stdout="FAILED_TEST", stderr="1 failed")
        result = _run_uv_tool(client, "proj", "pytest", "run_pytest", target=["."])
        assert result["ok"] is False
        assert result["result"]["outcome"] == "failed"
        assert result["result"]["exit_code"] == 1
        assert result["result"]["stdout"] == "FAILED_TEST"
        assert result["result"]["stderr"] == "1 failed"
        assert result["error"]["code"] == "CHECK_FAILED"

    def test_mypy_success(self, monkeypatch):
        from mcp_client_tools import _run_uv_tool
        monkeypatch.setattr("mcp_client_tools._resolve_project", lambda _: Path("/project"))
        client = self._make_mock(exit_code=0, stdout="Success: no issues")
        result = _run_uv_tool(client, "proj", "mypy", "run_mypy", target=["."])
        assert result["ok"] is True
        assert result["result"]["outcome"] == "passed"
        assert result["result"]["exit_code"] == 0

    def test_mypy_failure(self, monkeypatch):
        from mcp_client_tools import _run_uv_tool
        monkeypatch.setattr("mcp_client_tools._resolve_project", lambda _: Path("/project"))
        client = self._make_mock(exit_code=1, stdout="", stderr="found 3 errors")
        result = _run_uv_tool(client, "proj", "mypy", "run_mypy", target=["."])
        assert result["ok"] is False
        assert result["result"]["outcome"] == "failed"
        assert result["result"]["exit_code"] == 1
        assert result["result"]["stderr"] == "found 3 errors"
        assert result["error"]["code"] == "CHECK_FAILED"

    def test_validation_error_still_returns_ok_false(self, monkeypatch):
        """Validation errors (invalid target) must use tool_error, not break run_tool."""
        from mcp_client_tools import _run_uv_tool
        monkeypatch.setattr("mcp_client_tools._resolve_project", lambda _: Path("/project"))
        client = self._make_mock()
        result = _run_uv_tool(client, "proj", "pytest", "run_pytest", target=["../escape"])
        assert result["ok"] is False
        assert result["error"]["code"] == "POLICY_DENIED"

    def test_fallback_success_structure(self, monkeypatch):
        """Read-only fallback returns tool_success with same shape as normal path."""
        from mcp_client_tools import _run_uv_tool
        monkeypatch.setattr("mcp_client_tools._resolve_project", lambda _: Path("/project"))
        monkeypatch.setattr(
            "mcp_client_tools._build_readonly_fallback_script",
            lambda *a: "echo ok",
        )
        class _FB:
            def __init__(self):
                self._n = 0
            def execute_raw(self, cmd, **kw):
                self._n += 1
                return {"job_id": f"j{self._n}"}
            def wait_job(self, job_id, **kw):
                if job_id in ("j0", "j1"):
                    return {"exit_code": 0, "stdout": "/usr/bin/uv", "stderr": ""}
                return {"exit_code": 1, "stdout": "", "stderr": "read-only file system"}
            def execute_project_script(self, proj, script, timeout_s=300):
                return {"exit_code": 0, "stdout": "fallback OK", "stderr": "", "execution_duration_ms": 456, "job_id": "j-fb"}

        result = _run_uv_tool(_FB(), "proj", "pytest", "run_pytest", target=["."])
        assert result["ok"] is True
        assert result["result"]["outcome"] == "passed"
        assert result["result"]["exit_code"] == 0
        assert result["result"]["stdout"] == "fallback OK"

    def test_fallback_failure_structure(self, monkeypatch):
        """Read-only fallback failure returns tool_error with exit_code and outcome."""
        from mcp_client_tools import _run_uv_tool
        monkeypatch.setattr("mcp_client_tools._resolve_project", lambda _: Path("/project"))
        monkeypatch.setattr(
            "mcp_client_tools._build_readonly_fallback_script",
            lambda *a: "echo fail",
        )
        class _FB:
            def __init__(self):
                self._n = 0
            def execute_raw(self, cmd, **kw):
                self._n += 1
                return {"job_id": f"j{self._n}"}
            def wait_job(self, job_id, **kw):
                if job_id in ("j0", "j1"):
                    return {"exit_code": 0, "stdout": "/usr/bin/uv", "stderr": ""}
                return {"exit_code": 1, "stdout": "", "stderr": "read-only file system"}
            def execute_project_script(self, proj, script, timeout_s=300):
                return {"exit_code": 1, "stdout": "", "stderr": "2 failed", "execution_duration_ms": 456, "job_id": "j-fb"}

        result = _run_uv_tool(_FB(), "proj", "pytest", "run_pytest", target=["."])
        assert result["ok"] is False
        assert result["result"]["outcome"] == "failed"
        assert result["result"]["exit_code"] == 1
        assert result["result"]["stderr"] == "2 failed"
        assert result["error"]["code"] == "CHECK_FAILED"

    def test_run_tool_wraps_uv_failure_as_error_result(self, monkeypatch):
        """run_tool wrapper must set isError for tool_error from _run_uv_tool."""
        from mcp_client_tools import _run_uv_tool

        from examples.mcp_server.server import run_tool
        monkeypatch.setattr("mcp_client_tools._resolve_project", lambda _: Path("/project"))
        client = self._make_mock(exit_code=1, stdout="", stderr="2 failed")

        def _call():
            return _run_uv_tool(client, "proj", "pytest", "run_pytest", target=["."])

        result = run_tool(tool="run_pytest", title="Test", fn=_call, success_text="Done")
        assert result.get("isError") is True
        assert result["structuredContent"]["ok"] is False
        assert result["structuredContent"]["result"]["exit_code"] == 1


class TestMapUvExitCodeContract:
    """Direct unit tests for _map_uv_exit_code edge cases."""

    def test_pytest_no_tests_exit_code_5(self):
        from mcp_client_tools import _map_uv_exit_code
        outcome, error = _map_uv_exit_code("pytest", 5)
        assert outcome == "failed"
        assert error is None

    def test_pytest_unknown_exit_code_returns_error(self):
        from mcp_client_tools import _map_uv_exit_code
        outcome, error = _map_uv_exit_code("pytest", 3)
        assert outcome is None
        assert error == "TOOL_EXECUTION_FAILED"

    def test_ruff_failure_exit_code_1(self):
        from mcp_client_tools import _map_uv_exit_code
        outcome, error = _map_uv_exit_code("ruff", 1)
        assert outcome == "failed"
        assert error is None

    def test_ruff_unknown_exit_code_returns_error(self):
        from mcp_client_tools import _map_uv_exit_code
        outcome, error = _map_uv_exit_code("ruff", 2)
        assert outcome is None
        assert error == "TOOL_EXECUTION_FAILED"

    def test_pytest_success_exit_code_0(self):
        from mcp_client_tools import _map_uv_exit_code
        outcome, error = _map_uv_exit_code("pytest", 0)
        assert outcome == "passed"
        assert error is None

    def test_ruff_success_exit_code_0(self):
        from mcp_client_tools import _map_uv_exit_code
        outcome, error = _map_uv_exit_code("ruff", 0)
        assert outcome == "passed"
        assert error is None


class TestReadOnlyFallbackRejectsAbsoluteTargetsInRealFlow:
    """_run_uv_tool must pass sanitized (validated) targets to the readonly fallback.
    Absolute target like /etc/passwd must reach fallback as project_dir/etc/passwd."""

    def _make_fallback_mock(self):
        """Mock: first execute_raw fails with read-only, execute_project_script captures script."""
        class _FBMock:
            def __init__(self):
                self._n = 0
                self.fallback_script = None
            def execute_raw(self, cmd, **kw):
                self._n += 1
                return {"job_id": f"j{self._n}"}
            def wait_job(self, job_id, **kw):
                if job_id in ("j0", "j1"):
                    return {"exit_code": 0, "stdout": "/usr/bin/uv", "stderr": ""}
                return {"exit_code": 1, "stdout": "", "stderr": "read-only file system"}
            def execute_project_script(self, proj, script, timeout_s=300):
                self.fallback_script = script
                return {"exit_code": 0, "stdout": "", "stderr": "", "execution_duration_ms": 456, "job_id": "j-fb"}
        return _FBMock()

    def test_fallback_receives_sanitized_target_not_absolute(self, monkeypatch):
        from mcp_client_tools import _run_uv_tool
        monkeypatch.setattr("mcp_client_tools._resolve_project", lambda _: Path("/project"))
        mock = self._make_fallback_mock()
        _run_uv_tool(mock, "proj", "pytest", "run_pytest", target=["/etc/passwd"])
        assert mock.fallback_script is not None, "fallback must be triggered"
        last_line = [ln for ln in mock.fallback_script.splitlines() if "uv run pytest" in ln][-1]
        assert "/etc/passwd" not in last_line.split() or last_line.endswith("/project/etc/passwd 2>&1"), (
            f"fallback must not use raw absolute /etc/passwd, last line: {last_line}"
        )

    def test_fallback_receives_sanitized_target_relative_safe(self, monkeypatch):
        """Relative target must pass through unchanged."""
        from mcp_client_tools import _run_uv_tool
        monkeypatch.setattr("mcp_client_tools._resolve_project", lambda _: Path("/project"))
        mock = self._make_fallback_mock()
        _run_uv_tool(mock, "proj", "pytest", "run_pytest", target=["tests/"])
        assert mock.fallback_script is not None
        last_line = [ln for ln in mock.fallback_script.splitlines() if "uv run pytest" in ln][-1]
        assert "/project/tests" in last_line

    def test_fallback_receives_sanitized_target_with_traversal_blocked(self, monkeypatch):
        """../escape must be blocked before reaching fallback."""
        from mcp_client_tools import _run_uv_tool
        monkeypatch.setattr("mcp_client_tools._resolve_project", lambda _: Path("/project"))
        mock = self._make_fallback_mock()
        result = _run_uv_tool(mock, "proj", "pytest", "run_pytest", target=["../outside"])
        assert result["ok"] is False
        assert result["error"]["code"] == "POLICY_DENIED"
        assert mock.fallback_script is None, "fallback must NOT be called after POLICY_DENIED"

    def test_fallback_receives_sanitized_target_mypy(self, monkeypatch):
        """Same guarantee for mypy tool."""
        from mcp_client_tools import _run_uv_tool
        monkeypatch.setattr("mcp_client_tools._resolve_project", lambda _: Path("/project"))
        mock = self._make_fallback_mock()
        _run_uv_tool(mock, "proj", "mypy", "run_mypy", target=["/etc/shadow"])
        assert mock.fallback_script is not None
        last_line = [ln for ln in mock.fallback_script.splitlines() if "uv run mypy" in ln][-1]
        assert not any(w == "/etc/shadow" for w in last_line.split()), (
            f"fallback must not use raw absolute /etc/shadow, last line: {last_line}"
        )
        assert last_line.endswith("/project/etc/shadow 2>&1")


class TestReadOnlyFallbackTraversal:
    """_build_readonly_fallback_script relies on callers (_validate_targets) for path traversal
    protection. This documents that the function itself does NOT block '..' — the guarantee lives
    at the _run_uv_tool → _validate_targets layer."""

    def test_readonly_fallback_does_not_self_validate_traversal(self):
        from mcp_client_tools import _build_readonly_fallback_script
        script = _build_readonly_fallback_script("pytest", "/project", ["../outside"])
        assert "../outside" in script or "/project/../outside" in script


class TestProjectNotFound:
    """PROJECT_NOT_FOUND must be classified correctly, not as INTERNAL_ERROR."""

    def test_resolve_unknown_project_raises_project_not_found(self):
        from gateway_client import GatewayClientError
        from mcp_client_tools import _resolve_project

        with pytest.raises(GatewayClientError) as exc_info:
            _resolve_project("nonexistent_project")
        assert exc_info.value.status_code == 404
        assert exc_info.value.body is not None
        assert exc_info.value.body["detail"]["code"] == "PROJECT_NOT_FOUND"
        assert exc_info.value.body["detail"]["retryable"] is False

    def test_classify_gateway_error_project_not_found(self):
        from gateway_client import GatewayClientError
        from server import _classify_gateway_error

        exc = GatewayClientError(
            "PROJECT_NOT_FOUND: unknown project 'foo'",
            status_code=404,
            body={"detail": {"code": "PROJECT_NOT_FOUND", "retryable": False}},
        )
        code, retryable = _classify_gateway_error(exc)
        assert code == "PROJECT_NOT_FOUND"
        assert retryable is False

    def test_classify_gateway_error_real_internal_error(self):
        """A genuine 500 without structured body must remain INTERNAL_ERROR."""
        from gateway_client import GatewayClientError
        from server import _classify_gateway_error

        exc = GatewayClientError(
            "Internal server error",
            status_code=500,
            body={"detail": "oops"},
        )
        code, retryable = _classify_gateway_error(exc)
        assert code == "INTERNAL_ERROR"
        assert retryable is True

        exc_no_body = GatewayClientError(
            "connection refused",
            status_code=502,
        )
        code, retryable = _classify_gateway_error(exc_no_body)
        assert code == "INTERNAL_ERROR"
        assert retryable is True
