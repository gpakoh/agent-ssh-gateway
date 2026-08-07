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
    assert argv[:7] == [
        "uv", "run", "--frozen", "--directory", "/project", "--",
        "python",
    ]
    assert argv[7] == "-c"
    walk_code = argv[8]
    assert "os.walk" in walk_code, "compileall must use the pruning walk, not -m compileall"
    assert "-m" not in walk_code
    assert argv[-2:] == ["--", "src/"]


def test_build_compileall_argv_prunes_service_dirs():
    from mcp_client_tools import _build_compileall_walk_code

    argv = _build_uv_argv("compileall", "/project", ["."])
    assert argv[-2:] == ["--", "."]
    code = _build_compileall_walk_code()
    assert "dirnames[:]" in code, "walk must prune dirs at the descent level"
    assert "os.walk" in code
    for path in (
        ".venv",
        ".git",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".benchmarks",
    ):
        assert path in code, f"excluded dir {path} must be baked into the walk"
    assert "_LIMIT" in code and "truncated" in code, "walk output must be capped"


def test_compileall_walk_code_prunes_dirs_and_reports_errors(tmp_path, monkeypatch, capsys):
    """Run the generated walk code for real (no uv): excluded service dirs
    must never be compiled, and a syntax error must surface with the real
    message and a failing exit code."""
    from mcp_client_tools import _build_compileall_walk_code

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ok.py").write_text("x = 1\n")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "bad.py").write_text("def f(:\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "x.py").write_text("y = \n")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "z.py").write_text("z = \n")

    code = compile(_build_compileall_walk_code(), "walk", "exec")

    monkeypatch.setattr("sys.argv", ["-c", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        exec(code, {})
    out = capsys.readouterr().out
    assert exc.value.code == 0, out
    assert "ok.py" in out and "Compiling" in out
    assert "bad.py" not in out, "walk descended into .venv"
    assert "x.py" not in out, "walk descended into .git"
    assert "z.py" not in out, "walk descended into node_modules"

    (tmp_path / "broken.py").write_text("def broken(:\n")
    monkeypatch.setattr("sys.argv", ["-c", str(tmp_path / "broken.py")])
    with pytest.raises(SystemExit) as exc:
        exec(code, {})
    out = capsys.readouterr().out
    assert exc.value.code == 1, out
    assert "broken.py" in out
    assert "failed" in out.lower() or "error" in out.lower()


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

    def test_readonly_fallback_copies_uv_lock_and_syncs_frozen(self):
        """Regression: the fallback script copied only pyproject.toml, so
        `uv sync --extra dev` re-resolved dependencies on every first sync
        instead of honoring the project's uv.lock — non-deterministic
        versions, and the temp project could drift from what the real
        project pins. uv.lock must be copied alongside pyproject.toml and
        the sync must use --frozen so the lockfile semantics hold.
        """
        from mcp_client_tools import _build_readonly_fallback_script

        script = _build_readonly_fallback_script("pytest", "/project", ["tests"])

        sync_lines = [ln for ln in script.splitlines() if "uv sync" in ln]
        assert sync_lines, "expected a uv sync invocation"
        frozen_assign = [
            ln
            for ln in script.splitlines()
            if "uv.lock" in ln and "FROZEN=--frozen" in ln
        ]
        assert frozen_assign, (
            "uv sync must honor the project lockfile: when uv.lock exists "
            f"FROZEN must be --frozen; got {sync_lines}"
        )
        assert any("$FROZEN" in ln for ln in sync_lines), (
            f"uv sync must consume $FROZEN; got {sync_lines}"
        )
        assert "uv.lock" in script, "script must reference uv.lock"

    def test_readonly_fallback_uv_lock_change_triggers_resync(self):
        """The stamp diff-check must also cover uv.lock: a lockfile change
        (e.g. after `uv lock`/`uv add`) must invalidate the cached venv even
        when pyproject.toml is untouched.
        """
        from mcp_client_tools import _build_readonly_fallback_script

        script = _build_readonly_fallback_script("pytest", "/project", ["tests"])
        assert "uv.lock" in script
        assert "NEED_SYNC=1" in script
        lock_check = [ln for ln in script.splitlines() if "uv.lock" in ln and "diff" in ln]
        assert lock_check, "uv.lock must participate in the NEED_SYNC diff check"

    def test_readonly_fallback_tmp_root_scoped_by_project_path(self):
        """Regression: tmp_root was keyed only by basename, so two projects
        that happen to share a name (/work/a/proj and /home/b/proj) collided
        in /tmp/.mcp-test/proj — the second reused the first's venv and,
        because symlinks are only created when the destination is missing,
        ran the first project's code instead of its own. tmp_root must be
        scoped by a stable hash of the full project path.
        """
        from mcp_client_tools import _build_readonly_fallback_script

        def tmp_root(script):
            for ln in script.splitlines():
                if ln.startswith("mkdir -p /tmp/.mcp-test/"):
                    return ln.split()[-1]
            raise AssertionError("no tmp_root in script")

        root_a1 = tmp_root(_build_readonly_fallback_script("pytest", "/work/a/proj", ["tests"]))
        root_b = tmp_root(_build_readonly_fallback_script("pytest", "/home/b/proj", ["tests"]))
        root_a2 = tmp_root(_build_readonly_fallback_script("pytest", "/work/a/proj", ["tests"]))
        assert root_a1 == root_a2, "same project path must map to a stable tmp_root"
        assert root_a1 != root_b, "same basename from different parents must not collide"
        assert "proj" in root_a1, "tmp_root should stay human-readable"
        assert root_a1.startswith("/tmp/.mcp-test/")

    def test_readonly_fallback_tmp_root_scoped_by_remote_identity(self):
        """Regression (audit P0-3): the cache key must include the remote
        UID/user identity. Two SSH identities sharing the same project path
        (e.g. the read-only sshd container) previously reused one shared
        /tmp/.mcp-test/<name>-<hash> venv — a chmod'd/foreign interpreter
        then killed the other user's run_tests with Permission denied. The
        script must derive the identity on the target and embed it in
        tmp_root.
        """
        from mcp_client_tools import _build_readonly_fallback_script

        script = _build_readonly_fallback_script("pytest", "/project", ["tests"])
        assert '_MCP_UID="$(id -u)"' in script
        assert '_MCP_USER="$(id -un)"' in script
        root_lines = [
            ln for ln in script.splitlines() if ln.startswith("mkdir -p /tmp/.mcp-test/")
        ]
        assert root_lines, "expected tmp_root mkdir"
        root = root_lines[0].split()[-1]
        assert "-u${_MCP_UID}-${_MCP_USER}" in root

    def test_readonly_fallback_rebuilds_broken_venv(self):
        """Regression (audit P0-3): a stale/broken cached venv (dangling
        interpreter symlink, unreadable base python, partial sync from a
        killed run) must be detected and dropped before resync — the poison
        must not persist past the next invocation.
        """
        from mcp_client_tools import _build_readonly_fallback_script

        script = _build_readonly_fallback_script("pytest", "/project", ["tests"])
        probe = [
            ln for ln in script.splitlines() if ".venv/bin/python3" in ln and "! -x" in ln
        ]
        assert probe, "expected interpreter health probe"
        cleanup = [ln for ln in script.splitlines() if "rm -rf" in ln and ".venv" in ln]
        assert cleanup, "broken .venv must be removed before resync"
        assert "rm -f $STAMP" in script, "stamp must be cleared with a broken venv"

    def test_readonly_fallback_symlinks_use_scoped_tmp_root(self):
        """The app/tests symlinks must be created inside the scoped tmp_root
        so a project never links the previous tenant's code from a
        basename-colliding directory.
        """
        from mcp_client_tools import _build_readonly_fallback_script

        script = _build_readonly_fallback_script("pytest", "/home/b/proj", ["tests"])
        roots = [
            ln.split()[-1]
            for ln in script.splitlines()
            if ln.startswith("mkdir -p /tmp/.mcp-test/")
        ]
        assert len(roots) == 1
        root = roots[0]
        symlinks = [ln for ln in script.splitlines() if "ln -sf " in ln]
        assert symlinks, "expected app/tests symlinks"
        for ln in symlinks:
            idx = ln.index("ln -sf ")
            src, dst = ln[idx:].split()[2:4]
            assert dst.startswith(root + "/"), (
                f"symlink dst {dst} escapes scoped root {root}: {ln}"
            )
            assert "mcp-test/proj-" in root

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

    def test_mypy_exit_2_preserves_stdout_stderr_through_run_tool(self, monkeypatch):
        """Regression: run_mypy exit 2 (TOOL_EXECUTION_FAILED) must keep
        stdout/stderr in the result all the way through run_tool. The
        wrapper used to rebuild a bare tool_error without result, hiding
        the reason a diagnostic tool failed."""
        from mcp_client_tools import run_mypy

        from examples.mcp_server.server import run_tool
        monkeypatch.setattr("mcp_client_tools._resolve_project", lambda _: Path("/project"))
        client = self._make_mock(
            exit_code=2,
            stdout="error: cannot find module 'app'\n",
            stderr="mypy: fatal error\n",
        )

        result = run_tool(
            tool="run_mypy",
            title="run mypy",
            fn=lambda: run_mypy(client, "proj", ["."]),
            success_text="Ran project mypy.",
        )

        assert result.get("ok") is False
        assert result.get("error", {}).get("code") == "TOOL_EXECUTION_FAILED"
        assert result.get("error", {}).get("message") == "mypy exit code 2"
        assert result["result"]["exit_code"] == 2
        assert result["result"]["stdout"] == "error: cannot find module 'app'\n"
        assert result["result"]["stderr"] == "mypy: fatal error\n"

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

    def test_compileall_readonly_fallback_uses_uv_run_no_project_not_uvx(self, monkeypatch):
        """Regression: compileall isn't an installable PyPI tool, so its
        read-only fallback must not go through the uvx --from <tool> path
        (that always fails with "compileall was not found in the package
        registry", hiding the real syntax error) — it must run via
        `uv run --no-project python3 -m compileall` instead, since the SSH
        target has no system python3, only the interpreter uv manages.
        """
        from mcp_client_tools import _run_uv_tool
        monkeypatch.setattr("mcp_client_tools._resolve_project", lambda _: Path("/project"))

        class _FB:
            def __init__(self):
                self._n = 0
                self.argv_calls: list[list[str]] = []
                self.argv_kwargs: list[dict] = []

            def execute_raw(self, cmd, **kw):
                self._n += 1
                return {"job_id": f"j{self._n}"}

            def wait_job(self, job_id, **kw):
                if job_id == "j1":
                    return {"exit_code": 0, "stdout": "/usr/bin/uv", "stderr": ""}
                return {"exit_code": 1, "stdout": "", "stderr": "Read-only file system (os error 30)"}

            def execute_argv(self, argv, **kw):
                self.argv_calls.append(argv)
                self.argv_kwargs.append(kw)
                return {
                    "exit_code": 1,
                    "stdout": "***   File \"broken.py\", line 1\nSyntaxError: '(' was never closed\n",
                    "stderr": "",
                    "execution_duration_ms": 12,
                    "job_id": "j-fb",
                }

        client = _FB()
        result = _run_uv_tool(client, "proj", "compileall", "run_compileall", target=["broken.py"])

        assert len(client.argv_calls) == 1
        argv = client.argv_calls[0]
        assert "uv" in argv and "run" in argv and "--no-project" in argv
        # Regression: reported live as a 504 at ~30s — a cold uv interpreter
        # cache needs more than execute_argv's bare 30s default to download
        # and extract a full CPython build before compileall even starts.
        assert client.argv_kwargs[0].get("timeout_s") == 300
        assert "python3" in argv
        assert "uvx" not in argv
        assert "--from" not in argv  # uvx's package-resolution flag never appears

        assert result["ok"] is False
        assert result["result"]["outcome"] == "failed"
        assert "SyntaxError" in result["result"]["stdout"]
        assert result["error"]["code"] == "CHECK_FAILED"

    def test_compileall_readonly_fallback_success(self, monkeypatch):
        from mcp_client_tools import _run_uv_tool
        monkeypatch.setattr("mcp_client_tools._resolve_project", lambda _: Path("/project"))

        class _FB:
            def __init__(self):
                self._n = 0

            def execute_raw(self, cmd, **kw):
                self._n += 1
                return {"job_id": f"j{self._n}"}

            def wait_job(self, job_id, **kw):
                if job_id == "j1":
                    return {"exit_code": 0, "stdout": "/usr/bin/uv", "stderr": ""}
                return {"exit_code": 1, "stdout": "", "stderr": "Read-only file system (os error 30)"}

            def execute_argv(self, argv, **kw):
                return {
                    "exit_code": 0,
                    "stdout": "Compiling 'clean.py'...\n",
                    "stderr": "",
                    "execution_duration_ms": 8,
                    "job_id": "j-fb",
                }

        result = _run_uv_tool(_FB(), "proj", "compileall", "run_compileall", target=["clean.py"])
        assert result["ok"] is True
        assert result["result"]["outcome"] == "passed"
        assert result["result"]["exit_code"] == 0

    def test_run_tool_wraps_uv_failure_as_error_result(self, monkeypatch):
        """run_tool wrapper returns canonical error for _run_uv_tool failure."""
        from mcp_client_tools import _run_uv_tool

        from examples.mcp_server.server import run_tool
        monkeypatch.setattr("mcp_client_tools._resolve_project", lambda _: Path("/project"))
        client = self._make_mock(exit_code=1, stdout="", stderr="2 failed")

        def _call():
            return _run_uv_tool(client, "proj", "pytest", "run_pytest", target=["."])

        result = run_tool(tool="run_pytest", title="Test", fn=_call, success_text="Done")
        assert result.get("ok") is False
        assert result.get("error", {}).get("code") == "CHECK_FAILED"
        assert result.get("error", {}).get("retryable") is False


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

    def _classify(self, exc):
        from examples.mcp_server.server import _classify_gateway_error

        return _classify_gateway_error(exc)

    def test_classify_gateway_error_project_not_found(self):
        from gateway_client import GatewayClientError

        exc = GatewayClientError(
            "PROJECT_NOT_FOUND: unknown project 'foo'",
            status_code=404,
            body={"detail": {"code": "PROJECT_NOT_FOUND", "retryable": False}},
        )
        code, retryable = self._classify(exc)
        assert code == "PROJECT_NOT_FOUND"
        assert retryable is False

    def test_classify_gateway_error_real_internal_error(self):
        """A genuine 500 without structured body must remain INTERNAL_ERROR."""
        from gateway_client import GatewayClientError

        exc = GatewayClientError(
            "Internal server error",
            status_code=500,
            body={"detail": "oops"},
        )
        code, retryable = self._classify(exc)
        assert code == "INTERNAL_ERROR"
        assert retryable is True

        exc_no_body = GatewayClientError(
            "connection refused",
            status_code=502,
        )
        code, retryable = self._classify(exc_no_body)
        assert code == "INTERNAL_ERROR"
        assert retryable is True


class TestRunPytestMcpSchemaTargetArray:
    """Regression: the MCP schema for run_pytest/run_ruff/run_mypy must
    accept an array of targets, matching the implementation's
    ``target: list[str] | str | None``. Before the fix the schema only
    allowed a single string, so an agent passing several files got
    ``pytest exit code 4`` (multiple paths packed into one argument).
    """

    def _schema(self, tool_name: str) -> dict:
        import importlib
        import os
        from unittest.mock import patch

        import examples.mcp_server.server as srv

        env = dict(os.environ)
        env["MCP_GATEWAY_TOOL_MODE"] = "mcp_client"
        env.pop("MCP_CLIENT_SAFE_MODE", None)
        with patch.dict(os.environ, env):
            importlib.reload(srv)
        params = srv.mcp._tool_manager._tools[tool_name].parameters
        return params.get("properties", {}).get("target", {})

    def test_run_pytest_target_accepts_array(self):
        target = self._schema("run_pytest")
        assert "array" in str(target.get("anyOf", [])) or target.get("type") == "array"

    def test_run_ruff_target_accepts_array(self):
        target = self._schema("run_ruff")
        assert "array" in str(target.get("anyOf", [])) or target.get("type") == "array"

    def test_run_mypy_target_accepts_array(self):
        target = self._schema("run_mypy")
        assert "array" in str(target.get("anyOf", [])) or target.get("type") == "array"


class TestExecutionDurationMsConversion:
    """Regression (audit item 2): run_pytest/run_ruff/run_mypy reported
    execution_duration_ms=null because the gateway's /wait endpoint returns
    job.to_dict() with ``duration`` in seconds and no
    ``execution_duration_ms`` key — only the client's polling fallback
    converted units. All build_command_result call sites must convert.
    """

    def test_helper_prefers_explicit_ms(self):
        from mcp_client_tools import _execution_duration_ms

        assert _execution_duration_ms({"execution_duration_ms": 123, "duration": 9.9}) == 123

    def test_helper_converts_seconds_to_ms(self):
        from mcp_client_tools import _execution_duration_ms

        assert _execution_duration_ms({"duration": 12.345}) == 12345

    def test_helper_returns_none_without_duration(self):
        from mcp_client_tools import _execution_duration_ms

        assert _execution_duration_ms({"stdout": ""}) is None

    def test_run_uv_tool_success_carries_execution_duration_ms(self, monkeypatch):
        """wait_job returns only seconds-valued duration — the envelope's
        build_command_result must still expose execution_duration_ms."""
        from mcp_client_tools import _run_uv_tool

        class FakeClient:
            def execute_raw(self, cmd, **kw):
                return {"job_id": "j1"}

            def wait_job(self, job_id, **kw):
                return {"exit_code": 0, "stdout": "", "stderr": "", "duration": 2.5}

        monkeypatch.setattr(
            "mcp_client_tools._resolve_project",
            lambda _: Path("/project"),
        )
        monkeypatch.setattr(
            "mcp_client_tools._validate_targets",
            lambda proj, targets: targets,
        )
        result = _run_uv_tool(FakeClient(), "proj", "pytest", "run_pytest", target=["tests/"])

        assert result["ok"] is True
        assert result["result"]["execution_duration_ms"] == 2500

    def test_run_uv_tool_failure_carries_execution_duration_ms(self, monkeypatch):
        from mcp_client_tools import _run_uv_tool

        class FakeClient:
            def execute_raw(self, cmd, **kw):
                if "command -v uv" in cmd:
                    return {"job_id": "check1"}
                return {"job_id": "j1"}

            def wait_job(self, job_id, **kw):
                if job_id == "check1":
                    return {"exit_code": 0, "stdout": "/usr/bin/uv", "stderr": ""}
                return {"exit_code": 4, "stdout": "", "stderr": "2 failed", "duration": 1.25}

        monkeypatch.setattr(
            "mcp_client_tools._resolve_project",
            lambda _: Path("/project"),
        )
        monkeypatch.setattr(
            "mcp_client_tools._validate_targets",
            lambda proj, targets: targets,
        )
        result = _run_uv_tool(FakeClient(), "proj", "pytest", "run_pytest", target=["tests/"])

        assert result["ok"] is False
        assert result["result"]["execution_duration_ms"] == 1250


class TestAsyncRunTestsReadonlyFallback:
    """Regression (run_tests exit 2): async run_tests on a read-only
    workspace must detect the broken venv upfront and submit the writable
    temp-project script asynchronously instead of returning the job_id of a
    plain `uv run --frozen` that dies before pytest (failed to remove
    .venv/.lock on a read-only FS).
    """

    def _client(self, venv_ok: bool, fallback_job: str = "j-fallback"):
        calls: list[tuple[str, str]] = []

        class FakeClient:
            def execute_raw(self, cmd, **kw):
                calls.append(("execute_raw", cmd))
                if cmd == "command -v uv":
                    return {"job_id": "j-check"}
                if cmd.startswith("test -x "):
                    return {"job_id": "j-probe"}
                return {"job_id": "j-run"}

            def wait_job(self, job_id, **kw):
                if job_id == "j-check":
                    return {"exit_code": 0, "stdout": "/usr/bin/uv", "stderr": ""}
                if job_id == "j-probe":
                    return {
                        "exit_code": 0 if venv_ok else 1,
                        "stdout": "",
                        "stderr": "",
                    }
                return {"exit_code": 0, "stdout": "", "stderr": ""}

            def execute_project_script_async(self, project, script):
                calls.append(("execute_project_script_async", project))
                return {"job_id": fallback_job}

        return FakeClient(), calls

    def test_broken_venv_submits_fallback_script_async(self, monkeypatch):
        from mcp_client_tools import _run_uv_tool

        monkeypatch.setattr(
            "mcp_client_tools._resolve_project",
            lambda _: Path("/project"),
        )
        client, calls = self._client(venv_ok=False)
        result = _run_uv_tool(
            client,
            "proj",
            "pytest",
            "run_tests",
            target=["."],
            async_submit=True,
        )

        assert result["ok"] is True
        assert result["result"]["status"] == "running"
        assert result["result"]["job_id"] == "j-fallback"
        assert any(
            kind == "execute_project_script_async" for kind, _ in calls
        ), "broken venv must submit the fallback script"
        assert any(
            "Read-only workspace detected" in w for w in result["meta"].get("warnings", [])
        )

    def test_broken_venv_fallback_script_contains_pytest(self, monkeypatch):
        from mcp_client_tools import _run_uv_tool

        monkeypatch.setattr(
            "mcp_client_tools._resolve_project",
            lambda _: Path("/project"),
        )
        captured: dict[str, str] = {}

        class CapturingClient:
            def execute_raw(self, cmd, **kw):
                if cmd == "command -v uv":
                    return {"job_id": "j-check"}
                if cmd.startswith("test -x "):
                    return {"job_id": "j-probe"}
                return {"job_id": "j-run"}

            def wait_job(self, job_id, **kw):
                if job_id == "j-check":
                    return {"exit_code": 0, "stdout": "/usr/bin/uv", "stderr": ""}
                if job_id == "j-probe":
                    return {"exit_code": 1, "stdout": "", "stderr": ""}
                return {"exit_code": 0, "stdout": "", "stderr": ""}

            def execute_project_script_async(self, project, script):
                captured["script"] = script
                return {"job_id": "j-fallback"}

        result = _run_uv_tool(
            CapturingClient(),
            "proj",
            "pytest",
            "run_tests",
            target=["."],
            async_submit=True,
        )

        assert result["result"]["job_id"] == "j-fallback"
        assert "uv run pytest" in captured["script"]
        assert "/tmp/.mcp-test" in captured["script"]

    def test_healthy_venv_still_returns_plain_job_id(self, monkeypatch):
        from mcp_client_tools import _run_uv_tool

        monkeypatch.setattr(
            "mcp_client_tools._resolve_project",
            lambda _: Path("/project"),
        )
        client, calls = self._client(venv_ok=True)
        result = _run_uv_tool(
            client,
            "proj",
            "pytest",
            "run_tests",
            target=["."],
            async_submit=True,
        )

        assert result["result"]["job_id"] == "j-run"
        assert not any(
            kind == "execute_project_script_async" for kind, _ in calls
        ), "healthy venv must keep the plain uv run path"
