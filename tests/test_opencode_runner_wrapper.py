"""Tests for OpenCode runner wrapper — opencode_runner_wrapper module."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from scripts.opencode_runner_wrapper import (
    build_result_summary,
    find_opencode_bin,
    resolve_project_root,
    run_wrapper,
    validate_task_id,
    write_task_file,
)


class TestValidateTaskId:
    def test_valid_ids(self):
        for tid in [
            "2026-06-25-fix-auth-opencode",
            "a12345678901",
            "fix-test-flake-auth-mimo",
        ]:
            validate_task_id(tid)

    def test_invalid_ids(self):
        for tid in ["", "too-short", "UPPERCASE", "has spaces", "\xe4", None]:
            with pytest.raises((ValueError, TypeError)):
                validate_task_id(tid)


class TestFindOpencodeBin:
    def test_returns_given_path_if_file_exists(self):
        with tempfile.NamedTemporaryFile() as f:
            result = find_opencode_bin(f.name)
            assert result == f.name

    def test_returns_default_when_none_given(self):
        result = find_opencode_bin(None)
        assert result == "/root/.opencode/bin/opencode"


class TestResolveProjectRoot:
    def test_uses_env_var(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_PROJECT_ROOT", "/tmp/projects")
        result = resolve_project_root("my-app")
        assert result == "/tmp/projects/my-app"

    def test_falls_back_to_cwd_when_empty(self):
        result = resolve_project_root("")
        assert Path(result) == Path.cwd()


class TestWriteTaskFile:
    def test_creates_file_in_task_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_task_file(tmp, "a12345678901", "test.txt", "hello")
            assert Path(path).exists()
            assert Path(path).read_text() == "hello"
            assert "a12345678901" in path


class TestRunWrapperDryRun:
    def test_dry_run_does_not_execute(self):
        result = run_wrapper(
            task_id="b23456789012",
            project="",
            dry_run=True,
        )
        assert result["status"] == "dry-run"
        assert result["exit_code"] is None
        assert "[DRY-RUN]" in result["stdout"]

    def test_invalid_task_id_raises(self):
        with pytest.raises(ValueError):
            run_wrapper(
                task_id="bad",
                project="",
                dry_run=True,
            )

    def test_nonexistent_project_root_fails(self):
        result = run_wrapper(
            task_id="c34567890123",
            project="/nonexistent/project_xyz",
            dry_run=True,
        )
        assert result["status"] == "failed"


class TestBuildResultSummary:
    def test_creates_markdown(self):
        summary = build_result_summary(
            task_id="d45678901234",
            run_result={
                "status": "completed",
                "exit_code": 0,
                "stdout": "done",
                "stderr": "",
                "started_at": "2026-01-01T00:00:00",
                "finished_at": "2026-01-01T00:01:00",
                "timed_out": False,
            },
            command="opencode run test",
            opencode_bin="/usr/bin/opencode",
        )
        assert "d45678901234" in summary
        assert "completed" in summary
        assert "0" in summary
        assert "done" in summary
