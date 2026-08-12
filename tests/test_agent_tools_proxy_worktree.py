"""Tests for agent_tools._build_opencode_script — live-proxy lease/report
loop and per-worker git worktree isolation.

Mirrors quart-platform/opencode-adapter's proxy pattern: lease a live
proxy from the provider before the run (GET /proxy?format=provider), give
it back on rate limit (POST /proxy/report -> provider cooldown). Pure
script-building logic, no real opencode binary — same style as
test_opencode_runner_argv.py.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from examples.mcp_server.agent_tools import (
    PROXY_LIMIT_MARKERS,
    _build_opencode_script,
    _isolated_worktree_error,
    _parent_prerun_snapshot_script_lines,
    _proxy_report_script_lines,
    _read_task_json,
    _supervisor_postrun_script_lines,
)

TD = ".ai-bridge/tasks/a12345678901"
TASK_ID = "a12345678901"
PROVIDER = "http://proxy-parser-worker:8080/proxy?format=provider"


class TestReadTaskJsonLenient:
    """task.json is optional metadata for run_opencode (worktree_path).
    Corrupt/unrelated stdout must not crash the run; run_agent still
    fails closed because it treats {} as "task.json not found"."""

    def test_corrupt_stdout_returns_empty(self):
        rc = MagicMock(return_value={"exit_code": 0, "stdout": "# Plan", "stderr": ""})
        assert _read_task_json(rc, "proj", TASK_ID) == {}

    def test_empty_stdout_returns_empty(self):
        rc = MagicMock(return_value={"exit_code": 0, "stdout": "", "stderr": ""})
        assert _read_task_json(rc, "proj", TASK_ID) == {}

    def test_valid_json_parsed(self):
        import json

        rc = MagicMock(
            return_value={
                "exit_code": 0,
                "stdout": json.dumps({"worktree_path": "/wt/x"}),
                "stderr": "",
            }
        )
        assert _read_task_json(rc, "proj", TASK_ID) == {"worktree_path": "/wt/x"}


class TestProxyReportScriptLines:
    def test_report_url_derived_from_provider_base(self):
        lines = "\n".join(_proxy_report_script_lines(PROVIDER, "5"))
        assert "http://proxy-parser-worker:8080/proxy/report" in lines

    def test_report_contract_has_proxy_and_retry_after(self):
        lines = "\n".join(_proxy_report_script_lines(PROVIDER, "5"))
        assert "proxy_url" in lines
        assert "retry_after_seconds" in lines
        assert "json.dumps" in lines

    def test_all_limit_markers_in_grep_pattern(self):
        lines = "\n".join(_proxy_report_script_lines(PROVIDER, "5"))
        for marker in PROXY_LIMIT_MARKERS:
            assert marker in lines

    def test_report_is_best_effort(self):
        lines = "\n".join(_proxy_report_script_lines(PROVIDER, "5"))
        assert "except Exception" in lines

    def test_retry_after_fallback_default(self):
        lines = "\n".join(_proxy_report_script_lines(PROVIDER, "5"))
        assert "RETRY_AFTER=300" in lines


class TestBuildOpencodeScriptProxy:
    def test_fetch_and_report_wired_when_env_set(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_PROXY_PROVIDER_URL", PROVIDER)
        script = _build_opencode_script(TD, TASK_ID, None, project_root="/srv/proj")
        assert "PROXYFETCH_EOF" in script
        assert "PROXYREPORT_EOF" in script
        assert "proxy/report" in script
        assert "RATE_LIMITED=1" in script

    def test_no_proxy_lines_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)
        script = _build_opencode_script(TD, TASK_ID, None, project_root="/srv/proj")
        assert "PROXYFETCH_EOF" not in script
        assert "proxy/report" not in script
        assert "RATE_LIMITED=1" not in script

    def test_opencode_output_captured_for_detection(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_PROXY_PROVIDER_URL", PROVIDER)
        script = _build_opencode_script(TD, TASK_ID, None, project_root="/srv/proj")
        assert '> "$td/opencode-output.log" 2>&1' in script
        assert 'cat "$td/opencode-output.log"' in script

    def test_rate_limited_status_wins_over_failed(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_PROXY_PROVIDER_URL", PROVIDER)
        script = _build_opencode_script(TD, TASK_ID, None, project_root="/srv/proj")
        assert 'echo "Status: rate-limited" > "$td/agent-status.md"' in script


class TestBuildOpencodeScriptWorktree:
    def test_worktree_added_when_path_provided(self):
        script = _build_opencode_script(
            TD, TASK_ID, None, project_root="/srv/proj", worktree_path="/srv/proj/.ai-bridge/worktrees/a12345678901"
        )
        assert "git worktree add --detach" in script
        assert 'cd "$wt" || exit 1' in script

    def test_relative_worktree_resolved_against_project_root(self):
        script = _build_opencode_script(
            TD, TASK_ID, None, project_root="/srv/proj", worktree_path="../agent-worktrees/a12345678901"
        )
        assert "wt='/srv/agent-worktrees/a12345678901'" in script

    def test_no_worktree_lines_without_path(self):
        script = _build_opencode_script(TD, TASK_ID, None, project_root="/srv/proj")
        assert "git worktree add" not in script

    def test_td_absolute_in_worktree_mode(self):
        script = _build_opencode_script(
            TD, TASK_ID, None, project_root="/srv/proj", worktree_path="/srv/proj/wt"
        )
        assert "td='/srv/proj/.ai-bridge/tasks/a12345678901'" in script

    def test_td_absolute_with_project_root_without_worktree(self):
        script = _build_opencode_script(TD, TASK_ID, None, project_root="/srv/proj")
        assert "td='/srv/proj/.ai-bridge/tasks/a12345678901'" in script


class TestIsolatedWorktreeGuard:
    def test_parent_checkout_is_forbidden(self):
        error = _isolated_worktree_error("/srv/proj", ".")
        assert error is not None
        assert "parent checkout" in error

    def test_missing_worktree_is_forbidden_for_resolved_project(self):
        assert _isolated_worktree_error("/srv/proj", None) is not None

    def test_distinct_worktree_is_allowed(self):
        assert _isolated_worktree_error("/srv/proj", "../agent-worktrees/t1") is None

    def test_registryless_context_keeps_legacy_behavior(self):
        assert _isolated_worktree_error(None, None) is None


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _init_git_repo(root: Path) -> str:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Agent Runner Tests")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    (root / ".gitignore").write_text("task-artifacts/\n", encoding="utf-8")
    _git(root, "add", "base.txt", ".gitignore")
    _git(root, "commit", "-q", "-m", "base")
    return _git(root, "rev-parse", "HEAD")


def _run_supervisor_postrun(
    root: Path,
    *,
    base_head: str,
    allowed_files: list[str],
    forbidden_files: list[str] | None = None,
    required_checks: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    td = root / "task-artifacts"
    td.mkdir(exist_ok=True)
    script = "\n".join(
        [
            f"td={shlex.quote(str(td))}",
            f"BASE_HEAD={shlex.quote(base_head)}",
            "RC=0",
            *_supervisor_postrun_script_lines(
                allowed_files,
                forbidden_files or [],
                required_checks or [],
            ),
            "exit $FINAL_RC",
        ]
    )
    result = subprocess.run(
        ["sh", "-c", script],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, td


class TestSupervisorPostrunEvidence:
    def test_untracked_file_is_in_evidence_without_mutating_real_index(self, tmp_path):
        base_head = _init_git_repo(tmp_path)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "new.md").write_text("hello\n", encoding="utf-8")
        status_before = _git(tmp_path, "status", "--porcelain=v1")

        result, td = _run_supervisor_postrun(
            tmp_path,
            base_head=base_head,
            allowed_files=["docs/**"],
        )

        assert result.returncode == 0, result.stderr
        patch = (td / "implementation-diff.patch").read_text(encoding="utf-8")
        assert "new file mode" in patch
        assert "docs/new.md" in patch
        report = json.loads((td / "scope-violations.json").read_text(encoding="utf-8"))
        assert report["changed_files"] == ["docs/new.md"]
        assert report["violations"] == []
        assert _git(tmp_path, "status", "--porcelain=v1") == status_before

    def test_outside_allowed_and_forbidden_path_fails_closed(self, tmp_path):
        base_head = _init_git_repo(tmp_path)
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "oops.py").write_text("bad = True\n", encoding="utf-8")

        result, td = _run_supervisor_postrun(
            tmp_path,
            base_head=base_head,
            allowed_files=["docs/**"],
            forbidden_files=["app/**"],
        )

        assert result.returncode == 71
        report = json.loads((td / "scope-violations.json").read_text(encoding="utf-8"))
        kinds = {item["type"] for item in report["violations"]}
        assert kinds == {"outside-allowed-files", "forbidden-file"}

    def test_single_star_does_not_cross_directory_boundary(self, tmp_path):
        base_head = _init_git_repo(tmp_path)
        nested = tmp_path / "app" / "sub"
        nested.mkdir(parents=True)
        (nested / "x.py").write_text("x = 1\n", encoding="utf-8")

        result, _ = _run_supervisor_postrun(
            tmp_path,
            base_head=base_head,
            allowed_files=["app/*.py"],
        )

        assert result.returncode == 71

    def test_worker_commit_cannot_hide_changes_and_is_scope_violation(self, tmp_path):
        base_head = _init_git_repo(tmp_path)
        (tmp_path / "base.txt").write_text("changed by worker\n", encoding="utf-8")
        _git(tmp_path, "add", "base.txt")
        _git(tmp_path, "commit", "-q", "-m", "worker commit")

        result, td = _run_supervisor_postrun(
            tmp_path,
            base_head=base_head,
            allowed_files=["base.txt"],
        )

        assert result.returncode == 71
        patch = (td / "implementation-diff.patch").read_text(encoding="utf-8")
        assert "changed by worker" in patch
        report = json.loads((td / "scope-violations.json").read_text(encoding="utf-8"))
        assert any(item["type"] == "head-changed" for item in report["violations"])

    def test_required_check_failure_controls_final_exit(self, tmp_path):
        base_head = _init_git_repo(tmp_path)
        (tmp_path / "base.txt").write_text("changed\n", encoding="utf-8")

        result, td = _run_supervisor_postrun(
            tmp_path,
            base_head=base_head,
            allowed_files=["base.txt"],
            required_checks=["python3 -c 'import sys; sys.exit(9)'"],
        )

        assert result.returncode == 72
        checks = (td / "required-checks.log").read_text(encoding="utf-8")
        assert "FAIL exit=9" in checks

    def test_required_check_parent_mutation_fails_closed(self, tmp_path):
        """A required check runs after the first parent guard, so the
        supervisor must verify the parent checkout again after checks.

        Regression: without the second snapshot this absolute-path write
        survives while the runner still exits 0.
        """
        parent = tmp_path / "parent"
        parent.mkdir()
        base_head = _init_git_repo(parent)
        worker = tmp_path / "worker"
        _git(parent, "worktree", "add", "--detach", str(worker), "HEAD")
        td = tmp_path / "artifacts"
        td.mkdir()
        parent_file = parent / "base.txt"
        required_check = f"printf 'tampered by check\\n' > {shlex.quote(str(parent_file))}"

        script = "\n".join(
            [
                f"td={shlex.quote(str(td))}",
                'mkdir -p "$td"',
                *_parent_prerun_snapshot_script_lines(str(parent)),
                f"cd {shlex.quote(str(worker))}",
                f"BASE_HEAD={shlex.quote(base_head)}",
                "RC=0",
                *_supervisor_postrun_script_lines(
                    [],
                    [],
                    [required_check],
                    parent_root=str(parent),
                ),
                "exit $FINAL_RC",
            ]
        )
        result = subprocess.run(
            ["sh", "-c", script],
            cwd=worker,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 74, result.stderr
        checks = (td / "required-checks.log").read_text(encoding="utf-8")
        assert "PASS" in checks
        before = (td / "parent-tree-before.txt").read_text(encoding="utf-8").strip()
        after = (td / "parent-tree-after.txt").read_text(encoding="utf-8").strip()
        assert before != after
        assert parent_file.read_text(encoding="utf-8") == "tampered by check\n"

    def test_parent_checkout_mutation_from_worker_fails_closed(self, tmp_path):
        parent = tmp_path / "parent"
        parent.mkdir()
        base_head = _init_git_repo(parent)
        worker = tmp_path / "worker"
        _git(parent, "worktree", "add", "--detach", str(worker), "HEAD")
        td = tmp_path / "artifacts"
        td.mkdir()

        script = "\n".join(
            [
                f"td={shlex.quote(str(td))}",
                "mkdir -p \"$td\"",
                *_parent_prerun_snapshot_script_lines(str(parent)),
                f"cd {shlex.quote(str(worker))}",
                f"BASE_HEAD={shlex.quote(base_head)}",
                "RC=0",
                f"printf 'tampered\\n' > {shlex.quote(str(parent / 'base.txt'))}",
                *_supervisor_postrun_script_lines(
                    [],
                    [],
                    [],
                    parent_root=str(parent),
                ),
                "exit $FINAL_RC",
            ]
        )
        result = subprocess.run(
            ["sh", "-c", script],
            cwd=worker,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 74, result.stderr
        before = (td / "parent-tree-before.txt").read_text(encoding="utf-8").strip()
        after = (td / "parent-tree-after.txt").read_text(encoding="utf-8").strip()
        assert before != after

    def test_parent_index_only_mutation_fails_closed(self, tmp_path):
        parent = tmp_path / "parent"
        parent.mkdir()
        base_head = _init_git_repo(parent)
        (parent / "base.txt").write_text("preexisting parent edit\n", encoding="utf-8")
        worker = tmp_path / "worker"
        _git(parent, "worktree", "add", "--detach", str(worker), "HEAD")
        td = tmp_path / "artifacts"
        td.mkdir()

        script = "\n".join(
            [
                f"td={shlex.quote(str(td))}",
                'mkdir -p "$td"',
                *_parent_prerun_snapshot_script_lines(str(parent)),
                f"cd {shlex.quote(str(worker))}",
                f"BASE_HEAD={shlex.quote(base_head)}",
                "RC=0",
                f"git -C {shlex.quote(str(parent))} add base.txt",
                *_supervisor_postrun_script_lines(
                    [],
                    [],
                    [],
                    parent_root=str(parent),
                ),
                "exit $FINAL_RC",
            ]
        )
        result = subprocess.run(
            ["sh", "-c", script],
            cwd=worker,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 74, result.stderr
        before = (td / "parent-index-tree-before.txt").read_text(encoding="utf-8").strip()
        after = (td / "parent-index-tree-after.txt").read_text(encoding="utf-8").strip()
        assert before != after

    def test_worktree_reuse_requires_clean_git_toplevel(self):
        script = _build_opencode_script(
            TD,
            TASK_ID,
            None,
            project_root="/srv/proj",
            worktree_path="/srv/proj/.ai-bridge/worktrees/t1",
        )
        assert "git -C \"$wt\" rev-parse --show-toplevel" in script
        assert "Refusing non-worktree-root path" in script
        assert "Refusing dirty existing worktree" in script
