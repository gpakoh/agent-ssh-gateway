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
import os
import shlex
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        assert "/tmp/opencode-proxy-leases" in script
        assert "memory.current" in script
        assert "memory.max" in script
        assert "all live proxies are already leased" in script

    def test_no_proxy_lines_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)
        script = _build_opencode_script(TD, TASK_ID, None, project_root="/srv/proj")
        assert "PROXYFETCH_EOF" not in script
        assert "proxy/report" not in script
        assert "RATE_LIMITED=1" not in script

    def test_proxy_is_required_by_default(self, monkeypatch):
        monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)
        monkeypatch.delenv("OPENCODE_PROXY_REQUIRED", raising=False)
        monkeypatch.setenv("OPENCODE_ADMISSION_WAIT_SECONDS", "0")
        script = _build_opencode_script(TD, TASK_ID, None, project_root="/srv/proj")
        assert "PROXY_BLOCKED=1" in script
        assert "Proxy provider is not configured" in script
        assert 'echo "Status: blocked"' in script

    def test_direct_fallback_requires_explicit_opt_out(self, monkeypatch):
        monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)
        monkeypatch.setenv("OPENCODE_PROXY_REQUIRED", "false")
        script = _build_opencode_script(TD, TASK_ID, None, project_root="/srv/proj")
        assert "PROXY_BLOCKED=1" not in script

    def test_opencode_output_captured_for_detection(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_PROXY_PROVIDER_URL", PROVIDER)
        script = _build_opencode_script(TD, TASK_ID, None, project_root="/srv/proj")
        assert '> "$td/opencode-output.log" 2>&1' in script
        assert 'cat "$td/opencode-output.log"' in script

    def test_rate_limited_status_wins_over_failed(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_PROXY_PROVIDER_URL", PROVIDER)
        script = _build_opencode_script(TD, TASK_ID, None, project_root="/srv/proj")
        assert 'echo "Status: rate-limited" > "$td/agent-status.md"' in script

    def test_worker_status_preserved_before_canonical_final_status(self, monkeypatch):
        """A worker may write a detailed step log to agent-status.md. The
        runner must preserve that text before replacing the public status with
        the canonical one-line terminal state, and surface it in agent-report."""
        monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)
        script = _build_opencode_script(TD, TASK_ID, None, project_root="/srv/proj")

        snapshot = 'cp "$td/agent-status.md" "$td/worker-status.md"'
        canonical = 'echo "Status: needs-review" > "$td/agent-status.md"'
        assert snapshot in script
        assert script.index(snapshot) < script.index(canonical)
        assert '## Worker status snapshot' in script
        assert 'cat "$td/worker-status.md" >> "$td/agent-report.md"' in script


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

    def test_managed_clone_never_uses_source_git_worktree_metadata(self):
        base_ref = "a" * 40
        bundle = f"/var/lib/mcp-agent/sources/project/{base_ref}.bundle"
        script = _build_opencode_script(
            "/var/lib/mcp-agent/state/task",
            TASK_ID,
            None,
            project_root="/srv/proj",
            worktree_path="/var/lib/mcp-agent/workspaces/task",
            managed_clone=True,
            base_ref=base_ref,
            managed_source_path=bundle,
        )
        assert 'git clone --no-hardlinks --no-checkout "$MANAGED_SOURCE_BUNDLE" "$wt"' in script
        assert f"TASK_BASE_REF='{base_ref}'" in script
        assert 'TASK_BASE_COMMIT="$TASK_BASE_REF"' in script
        assert bundle in script
        assert 'git -C "$wt" checkout --detach "$TASK_BASE_COMMIT"' in script
        assert "git worktree add" not in script
        assert "workspace with baseline drift" in script


class TestIsolatedWorktreeGuard:
    def test_parent_checkout_is_forbidden(self):
        error = _isolated_worktree_error("/srv/proj", ".")
        assert error is not None
        assert "outside the authoritative source checkout" in error

    def test_nested_workspace_inside_source_is_forbidden(self):
        error = _isolated_worktree_error(
            "/srv/proj", "/srv/proj/.ai-bridge/worktrees/task-1"
        )
        assert error is not None
        assert "outside the authoritative source checkout" in error

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


def _make_source_bundle(source: Path, destination: Path) -> Path:
    _git(source, "update-ref", "refs/heads/source", "HEAD")
    _git(source, "bundle", "create", str(destination), "refs/heads/source")
    return destination


def test_explicit_base_ref_checks_out_pinned_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_PROXY_REQUIRED", "false")
    monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)
    source = tmp_path / "source-pinned"
    source.mkdir()
    pinned_head = _init_git_repo(source)
    (source / "base.txt").write_text("newer\n", encoding="utf-8")
    _git(source, "add", "base.txt")
    _git(source, "commit", "-q", "-m", "newer")
    current_head = _git(source, "rev-parse", "HEAD")
    assert current_head != pinned_head

    artifacts = tmp_path / "pinned-artifacts" / TASK_ID
    artifacts.mkdir(parents=True)
    (artifacts / "current-plan.md").write_text("# noop\n", encoding="utf-8")
    workspace = tmp_path / "pinned-workspaces" / TASK_ID
    fake_bin = tmp_path / "pinned-bin"
    fake_bin.mkdir()
    fake = fake_bin / "opencode"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    script = _build_opencode_script(
        str(artifacts),
        TASK_ID,
        None,
        project_root=str(source),
        worktree_path=str(workspace),
        base_ref=pinned_head,
    )
    result = subprocess.run(
        ["sh", "-c", script],
        cwd=source,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert _git(workspace, "rev-parse", "HEAD") == pinned_head
    assert _git(source, "rev-parse", "HEAD") == current_head


def test_existing_clean_workspace_rejects_base_ref_drift(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_PROXY_REQUIRED", "false")
    monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)
    source = tmp_path / "source-drift"
    source.mkdir()
    pinned_head = _init_git_repo(source)
    (source / "base.txt").write_text("newer\n", encoding="utf-8")
    _git(source, "add", "base.txt")
    _git(source, "commit", "-q", "-m", "newer")
    current_head = _git(source, "rev-parse", "HEAD")
    workspace = tmp_path / "drift-workspaces" / TASK_ID
    workspace.parent.mkdir(parents=True)
    _git(source, "worktree", "add", "--detach", str(workspace), current_head)

    artifacts = tmp_path / "drift-artifacts" / TASK_ID
    artifacts.mkdir(parents=True)
    (artifacts / "current-plan.md").write_text("# noop\n", encoding="utf-8")
    fake_bin = tmp_path / "drift-bin"
    fake_bin.mkdir()
    marker = tmp_path / "drift-opencode-ran"
    fake = fake_bin / "opencode"
    fake.write_text(f"#!/bin/sh\nprintf ran > {shlex.quote(str(marker))}\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    script = _build_opencode_script(
        str(artifacts),
        TASK_ID,
        None,
        project_root=str(source),
        worktree_path=str(workspace),
        base_ref=pinned_head,
    )
    result = subprocess.run(
        ["sh", "-c", script],
        cwd=source,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode != 0
    assert not marker.exists()
    assert "baseline drift" in (artifacts / "agent-status.md").read_text(encoding="utf-8")
    assert _git(workspace, "rev-parse", "HEAD") == current_head


def test_managed_clone_executes_without_creating_source_worktree_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_PROXY_REQUIRED", "false")
    monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)
    source = tmp_path / "source"
    source.mkdir()
    source_head = _init_git_repo(source)
    source_bundle = _make_source_bundle(source, tmp_path / "source.bundle")
    artifacts = tmp_path / "agent-state" / TASK_ID
    artifacts.mkdir(parents=True)
    (artifacts / "current-plan.md").write_text("# Do nothing\n", encoding="utf-8")
    workspace = tmp_path / "managed-workspaces" / TASK_ID

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_opencode = fake_bin / "opencode"
    fake_opencode.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_opencode.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    source_status_before = _git(source, "status", "--porcelain=v1", "--untracked-files=all")
    source_refs_before = _git(source, "show-ref")
    source_index_before = (source / ".git" / "index").read_bytes()
    objects_dir = source / ".git" / "objects"
    source_objects_before = {
        path.relative_to(objects_dir): path.read_bytes()
        for path in objects_dir.rglob("*")
        if path.is_file()
    }
    assert not (source / ".git" / "worktrees").exists()

    script = _build_opencode_script(
        str(artifacts),
        TASK_ID,
        None,
        project_root=str(source),
        worktree_path=str(workspace),
        managed_clone=True,
        base_ref=source_head,
        managed_source_path=str(source_bundle),
    )
    result = subprocess.run(
        ["sh", "-c", script],
        cwd=source,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert workspace.is_dir()
    assert (workspace / ".git").is_dir(), "managed workspace must be an independent clone"
    assert _git(workspace, "remote") == "", "managed workspace must not retain source remotes"
    assert _git(workspace, "rev-parse", "HEAD") == source_head
    assert _git(source, "status", "--porcelain=v1", "--untracked-files=all") == source_status_before
    assert _git(source, "show-ref") == source_refs_before
    assert (source / ".git" / "index").read_bytes() == source_index_before
    source_objects_after = {
        path.relative_to(objects_dir): path.read_bytes()
        for path in objects_dir.rglob("*")
        if path.is_file()
    }
    assert source_objects_after == source_objects_before
    assert not (source / ".git" / "worktrees").exists()
    assert (artifacts / "agent-status.md").read_text(encoding="utf-8").strip() == "Status: needs-review"


def test_managed_clone_rejects_existing_workspace_with_remote(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_PROXY_REQUIRED", "false")
    monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)
    source = tmp_path / "source-existing-remote"
    source.mkdir()
    source_head = _init_git_repo(source)
    source_bundle = _make_source_bundle(source, tmp_path / "source-existing-remote.bundle")
    artifacts = tmp_path / "agent-state-existing-remote" / TASK_ID
    artifacts.mkdir(parents=True)
    (artifacts / "current-plan.md").write_text("# Do nothing\n", encoding="utf-8")
    workspace = tmp_path / "managed-existing-remote" / TASK_ID
    workspace.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "--no-hardlinks", str(source), str(workspace))
    _git(workspace, "checkout", "--detach", source_head)
    assert _git(workspace, "remote") == "origin"

    fake_bin = tmp_path / "bin-existing-remote"
    fake_bin.mkdir()
    marker = tmp_path / "opencode-ran-existing-remote"
    fake_opencode = fake_bin / "opencode"
    fake_opencode.write_text(
        f"#!/bin/sh\nprintf ran > {shlex.quote(str(marker))}\nexit 0\n",
        encoding="utf-8",
    )
    fake_opencode.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    script = _build_opencode_script(
        str(artifacts),
        TASK_ID,
        None,
        project_root=str(source),
        worktree_path=str(workspace),
        managed_clone=True,
        base_ref=source_head,
        managed_source_path=str(source_bundle),
    )
    result = subprocess.run(
        ["sh", "-c", script],
        cwd=source,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode != 0
    assert not marker.exists()
    status = (artifacts / "agent-status.md").read_text(encoding="utf-8")
    assert "Refusing managed clone with source remote metadata" in status


def test_managed_clone_rejects_symlink_workspace(tmp_path, monkeypatch):
    source = tmp_path / "source-symlink"
    source.mkdir()
    source_head = _init_git_repo(source)
    source_bundle = _make_source_bundle(source, tmp_path / "source-symlink.bundle")
    artifacts = tmp_path / "agent-state-symlink" / TASK_ID
    artifacts.mkdir(parents=True)
    (artifacts / "current-plan.md").write_text("# Do nothing\n", encoding="utf-8")
    workspace = tmp_path / "managed-symlink" / TASK_ID
    workspace.parent.mkdir(parents=True)
    workspace.symlink_to(source, target_is_directory=True)

    fake_bin = tmp_path / "bin-symlink"
    fake_bin.mkdir()
    marker = tmp_path / "opencode-ran"
    fake_opencode = fake_bin / "opencode"
    fake_opencode.write_text(
        f"#!/bin/sh\nprintf ran > {shlex.quote(str(marker))}\nexit 0\n",
        encoding="utf-8",
    )
    fake_opencode.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    status_before = _git(source, "status", "--porcelain=v1", "--untracked-files=all")
    refs_before = _git(source, "show-ref")
    script = _build_opencode_script(
        str(artifacts),
        TASK_ID,
        None,
        project_root=str(source),
        worktree_path=str(workspace),
        managed_clone=True,
        base_ref=source_head,
        managed_source_path=str(source_bundle),
    )
    result = subprocess.run(
        ["sh", "-c", script],
        cwd=source,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode != 0
    assert "Refusing symlink workspace" in (artifacts / "agent-status.md").read_text(
        encoding="utf-8"
    )
    assert not marker.exists(), "OpenCode must never run after workspace path rejection"
    assert _git(source, "status", "--porcelain=v1", "--untracked-files=all") == status_before
    assert _git(source, "show-ref") == refs_before


def test_managed_clone_requires_registry_root_at_git_toplevel(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_PROXY_REQUIRED", "false")
    monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)
    repo = tmp_path / "monorepo"
    repo.mkdir()
    repo_head = _init_git_repo(repo)
    source_bundle = _make_source_bundle(repo, tmp_path / "monorepo.bundle")
    nested = repo / "service"
    nested.mkdir()
    artifacts = tmp_path / "agent-state-nested" / TASK_ID
    artifacts.mkdir(parents=True)
    (artifacts / "current-plan.md").write_text("# Do nothing\n", encoding="utf-8")
    workspace = tmp_path / "managed-nested" / TASK_ID

    fake_bin = tmp_path / "bin-nested"
    fake_bin.mkdir()
    fake_opencode = fake_bin / "opencode"
    fake_opencode.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_opencode.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    script = _build_opencode_script(
        str(artifacts),
        TASK_ID,
        None,
        project_root=str(nested),
        worktree_path=str(workspace),
        managed_clone=True,
        base_ref=repo_head,
        managed_source_path=str(source_bundle),
    )
    result = subprocess.run(
        ["sh", "-c", script],
        cwd=nested,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert str(nested) not in script
    assert workspace.is_dir()
    assert _git(workspace, "rev-parse", "HEAD") == repo_head
    assert _git(workspace, "remote") == ""


def _run_proxy_preflight_script(tmp_path: Path, monkeypatch, *, provider_body: str | None):
    source = tmp_path / "proxy-source"
    source.mkdir()
    _init_git_repo(source)
    artifacts = tmp_path / "proxy-artifacts"
    artifacts.mkdir()
    (artifacts / "current-plan.md").write_text("# noop\n", encoding="utf-8")
    fake_bin = tmp_path / "proxy-bin"
    fake_bin.mkdir()
    marker = tmp_path / "opencode-ran"
    fake = fake_bin / "opencode"
    fake.write_text(
        f"#!/bin/sh\nprintf ran > {shlex.quote(str(marker))}\nexit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.delenv("OPENCODE_PROXY_REQUIRED", raising=False)
    monkeypatch.setenv("OPENCODE_ADMISSION_WAIT_SECONDS", "0")
    monkeypatch.setenv("OPENCODE_STARTUP_RESERVE_BYTES", "0")
    if provider_body is None:
        monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)
    else:
        provider = tmp_path / "provider.txt"
        provider.write_text(provider_body, encoding="utf-8")
        monkeypatch.setenv("OPENCODE_PROXY_PROVIDER_URL", provider.as_uri())
    script = _build_opencode_script(
        str(artifacts), TASK_ID, None, project_root=str(source)
    )
    result = subprocess.run(
        ["sh", "-c", script], cwd=source, text=True, capture_output=True, check=False, timeout=15
    )
    return result, artifacts, marker


def test_required_proxy_missing_blocks_before_opencode(tmp_path, monkeypatch):
    result, artifacts, marker = _run_proxy_preflight_script(
        tmp_path, monkeypatch, provider_body=None
    )
    assert result.returncode == 76
    assert not marker.exists()
    assert (artifacts / "agent-status.md").read_text().strip() == "Status: blocked"
    assert "not configured" in (artifacts / "proxy-status.log").read_text()


def test_malformed_proxy_blocks_before_opencode(tmp_path, monkeypatch):
    result, artifacts, marker = _run_proxy_preflight_script(
        tmp_path, monkeypatch, provider_body="not-a-proxy\n"
    )
    assert result.returncode == 76
    assert not marker.exists()
    assert (artifacts / "agent-status.md").read_text().strip() == "Status: blocked"
    assert "rc=3" in (artifacts / "proxy-status.log").read_text()


def test_valid_proxy_allows_opencode_and_is_not_logged(tmp_path, monkeypatch):
    proxy = "http://127.0.0.1:19999"
    result, artifacts, marker = _run_proxy_preflight_script(
        tmp_path, monkeypatch, provider_body=proxy + "\n"
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert marker.exists()
    worker_status = (artifacts / "worker-status.md").read_text()
    assert "Using exclusive live proxy from configured provider" in worker_status
    assert proxy not in worker_status


class _ProxyPoolHandler(BaseHTTPRequestHandler):
    proxies = ["http://127.0.0.1:19001", "http://127.0.0.1:19002"]
    reports: list[dict[str, object]] = []
    get_count = 0
    fail_get_after_first = False

    def do_GET(self):
        type(self).get_count += 1
        if self.fail_get_after_first and self.get_count > 1:
            self.send_response(503)
            self.end_headers()
            return
        body = json.dumps({"proxies": self.proxies}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            self.reports.append(json.loads(raw))
        except ValueError:
            self.reports.append({"invalid": raw.decode("utf-8", "replace")})
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def test_parallel_runners_receive_distinct_proxy_leases(tmp_path, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyPoolHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source = tmp_path / "parallel-source"
        source.mkdir()
        _init_git_repo(source)
        fake_bin = tmp_path / "parallel-bin"
        fake_bin.mkdir()
        capture = tmp_path / "proxy-capture.txt"
        fake = fake_bin / "opencode"
        fake.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$HTTP_PROXY" >> "$PROXY_CAPTURE"\n'
            "sleep 1\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")
        monkeypatch.setenv("PROXY_CAPTURE", str(capture))
        monkeypatch.setenv(
            "OPENCODE_PROXY_PROVIDER_URL",
            f"http://127.0.0.1:{server.server_port}/proxy?format=provider",
        )
        monkeypatch.setenv("OPENCODE_PROXY_REQUIRED", "true")
        monkeypatch.setenv("OPENCODE_STARTUP_RESERVE_BYTES", "0")
        monkeypatch.setenv("OPENCODE_ADMISSION_WAIT_SECONDS", "5")
        monkeypatch.setenv("OPENCODE_ADMISSION_POLL_SECONDS", "1")

        processes = []
        for i in range(2):
            artifacts = tmp_path / f"parallel-artifacts-{i}"
            artifacts.mkdir()
            (artifacts / "current-plan.md").write_text("# noop\n", encoding="utf-8")
            script = _build_opencode_script(
                str(artifacts), f"{TASK_ID}-{i}", None, project_root=str(source)
            )
            processes.append(
                subprocess.Popen(
                    ["sh", "-c", script],
                    cwd=source,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=os.environ.copy(),
                )
            )

        results = [proc.communicate(timeout=20) + (proc.returncode,) for proc in processes]
        assert all(item[2] == 0 for item in results), results
        proxies = capture.read_text(encoding="utf-8").splitlines()
        assert len(proxies) == 2
        assert len(set(proxies)) == 2
        assert set(proxies) == set(_ProxyPoolHandler.proxies)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_startup_stall_retries_with_different_proxy(tmp_path, monkeypatch):
    _ProxyPoolHandler.reports = []
    _ProxyPoolHandler.get_count = 0
    _ProxyPoolHandler.fail_get_after_first = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyPoolHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source = tmp_path / "startup-retry-source"
        source.mkdir()
        _init_git_repo(source)
        artifacts = tmp_path / "startup-retry-artifacts"
        artifacts.mkdir()
        (artifacts / "current-plan.md").write_text("# noop\n", encoding="utf-8")

        fake_bin = tmp_path / "startup-retry-bin"
        fake_bin.mkdir()
        capture = tmp_path / "startup-retry-proxies.txt"
        fake = fake_bin / "opencode"
        fake.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$HTTP_PROXY" >> "$PROXY_CAPTURE"\n'
            'case "$HTTP_PROXY" in\n'
            '  *:19001) printf "\\033[0m\\n> build · big-pickle\\n\\033[0m"; sleep 10 ;;\n'
            '  *) printf "\\033[0m\\n> build · big-pickle\\n\\033[0m\\nstartup-ok\\n"; exit 0 ;;\n'
            "esac\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")
        monkeypatch.setenv("PROXY_CAPTURE", str(capture))
        monkeypatch.setenv(
            "OPENCODE_PROXY_PROVIDER_URL",
            f"http://127.0.0.1:{server.server_port}/proxy?format=provider",
        )
        monkeypatch.setenv("OPENCODE_PROXY_REQUIRED", "true")
        monkeypatch.setenv("OPENCODE_STARTUP_RESERVE_BYTES", "0")
        monkeypatch.setenv("OPENCODE_ADMISSION_WAIT_SECONDS", "1")
        monkeypatch.setenv("OPENCODE_ADMISSION_POLL_SECONDS", "1")
        monkeypatch.setenv("OPENCODE_STARTUP_RESPONSE_TIMEOUT_SECONDS", "1")
        monkeypatch.setenv("OPENCODE_STARTUP_KILL_GRACE_SECONDS", "1")

        script = _build_opencode_script(
            str(artifacts), TASK_ID, None, project_root=str(source)
        )
        result = subprocess.run(
            ["sh", "-c", script],
            cwd=source,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert _ProxyPoolHandler.get_count == 1
        assert capture.read_text(encoding="utf-8").splitlines() == _ProxyPoolHandler.proxies
        assert any(
            report.get("proxy") == _ProxyPoolHandler.proxies[0]
            for report in _ProxyPoolHandler.reports
        )
        worker_status = (artifacts / "worker-status.md").read_text(encoding="utf-8")
        assert "OpenCode startup stalled; rotating proxy" in worker_status
        assert "startup-ok" in (artifacts / "opencode-output.log").read_text(encoding="utf-8")
        report = (artifacts / "agent-report.md").read_text(encoding="utf-8")
        assert "Failure reason: none" in report
    finally:
        _ProxyPoolHandler.fail_get_after_first = False
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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

    def test_required_check_ignored_worker_venv_cannot_change_result(self, tmp_path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "tests@example.invalid")
        _git(root, "config", "user.name", "Agent Runner Tests")
        (root / "base.txt").write_text("base\n", encoding="utf-8")
        (root / ".gitignore").write_text("task-artifacts/\n.venv/\n", encoding="utf-8")
        _git(root, "add", "base.txt", ".gitignore")
        _git(root, "commit", "-q", "-m", "base")
        base_head = _git(root, "rev-parse", "HEAD")

        (root / "base.txt").write_text("changed by worker\n", encoding="utf-8")
        venv = root / ".venv"
        venv.mkdir()
        (venv / "sitecustomize.py").write_text(
            "raise SystemExit('worker venv active')\n", encoding="utf-8"
        )
        assert ".venv" not in _git(root, "status", "--porcelain=v1", "--untracked-files=all")

        python_home = subprocess.run(
            ["python3", "-c", "import sys; print(sys.base_prefix)"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        monkeypatch.setenv("PYTHONPATH", str(tmp_path / "pythonpath-extra"))
        monkeypatch.setenv("PYTHONHOME", python_home)
        monkeypatch.setenv("VIRTUAL_ENV", str(venv))

        check = (
            "python3 -c \"import os; "
            "assert open('base.txt').read() == 'changed by worker\\n'; "
            "assert not os.path.isdir('.venv'); "
            "assert 'PYTHONPATH' not in os.environ; "
            "assert 'PYTHONHOME' not in os.environ; "
            "assert 'VIRTUAL_ENV' not in os.environ; "
            "print('ok')\""
        )
        result, td = _run_supervisor_postrun(
            root,
            base_head=base_head,
            allowed_files=["base.txt"],
            required_checks=[check],
        )

        assert result.returncode == 0, result.stderr
        patch = (td / "implementation-diff.patch").read_text(encoding="utf-8")
        assert "base.txt" in patch
        assert ".venv" not in patch
        checks = (td / "required-checks.log").read_text(encoding="utf-8")
        assert "PASS" in checks
        assert "ok" in checks

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

    def test_workspace_reuse_requires_clean_git_toplevel(self):
        script = _build_opencode_script(
            TD,
            TASK_ID,
            None,
            project_root="/srv/proj",
            worktree_path="/srv/agent-workspaces/t1",
        )
        assert "git -C \"$wt\" rev-parse --show-toplevel" in script
        assert "Refusing non-worktree-root path" in script
        assert "Refusing dirty existing workspace" in script

    def test_managed_clone_runs_without_authoritative_source_mount(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCODE_PROXY_REQUIRED", "false")
        monkeypatch.delenv("OPENCODE_PROXY_PROVIDER_URL", raising=False)
        source = tmp_path / "source-only-for-bundle-construction"
        source.mkdir()
        source_head = _init_git_repo(source)
        source_bundle = _make_source_bundle(source, tmp_path / "immutable-source.bundle")
        artifacts = tmp_path / "agent-state" / TASK_ID
        artifacts.mkdir(parents=True)
        (artifacts / "current-plan.md").write_text("# Do nothing\n", encoding="utf-8")
        workspace = tmp_path / "managed-workspaces" / TASK_ID

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_opencode = fake_bin / "opencode"
        fake_opencode.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_opencode.chmod(0o755)
        monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

        script = _build_opencode_script(
            str(artifacts),
            TASK_ID,
            None,
            project_root=None,
            worktree_path=str(workspace),
            managed_clone=True,
            base_ref=source_head,
            managed_source_path=str(source_bundle),
        )

        assert str(source) not in script
        result = subprocess.run(
            ["sh", "-c", script],
            cwd=tmp_path,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert _git(workspace, "rev-parse", "HEAD") == source_head
        assert _git(workspace, "remote") == ""
        assert (artifacts / "agent-status.md").read_text(encoding="utf-8").strip() == "Status: needs-review"
