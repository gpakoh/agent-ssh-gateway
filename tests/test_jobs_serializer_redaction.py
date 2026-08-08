"""Regression tests for the centralized redacted job serializer (audit P0-2).

COMMAND_OUTPUT_REDACTION_ENABLED was only honored by /result: GET /api/jobs,
bulk execute, dead-letter and the SSE "Started: <command>" status event
returned raw command/stdout/stderr. Every surface now funnels through
serialize_job().
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import state as _state
from app.auth_middleware import AuthIdentity, token_fingerprint
from app.config import settings
from app.job_manager import JobManager, JobRecord
from app.job_serializer import serialize_job
from app.routers import jobs as jobs_router

SECRET_CMD = "export API_TOKEN=abc123 && cd /media/1TB/Python && pytest"
SECRET_OUT = "TOKEN=abc123\npassword=secret123\n"
REDACTED_MARK = "[REDACTED]"
OWNER_TOKEN = "agent-token-owner"


def _identity(token: str, role: str | None = None) -> AuthIdentity:
    return AuthIdentity(token_type="agent", token=token, name="agent", role=role)


class TestSerializeJob:
    def _job(self) -> JobRecord:
        job = JobRecord(job_id="j-1", session_id="s-1", command=SECRET_CMD)
        job.status = "completed"
        job.stdout = SECRET_OUT
        job.stderr = "error: password=secret123"
        job.exit_code = 0
        job.error_message = "boom API_TOKEN=abc123"
        return job

    def test_redact_true_redacts_command_stdout_stderr_error(self):
        out = serialize_job(self._job(), redact=True)
        assert SECRET_CMD.split("=")[1].split(" ")[0] not in out["command"]
        assert REDACTED_MARK in out["command"]
        assert "abc123" not in out["stdout"]
        assert "secret123" not in out["stderr"]
        assert "abc123" not in out["error_message"]

    def test_redact_false_keeps_raw(self):
        job = self._job()
        out = serialize_job(job, redact=False)
        assert out["command"] == SECRET_CMD
        assert out["stdout"] == SECRET_OUT
        assert out["stderr"] == job.stderr

    def test_include_output_false_drops_stdout_stderr(self):
        out = serialize_job(self._job(), redact=False, include_output=False)
        assert "stdout" not in out
        assert "stderr" not in out
        assert out["status"] == "completed"
        assert out["exit_code"] == 0

    def test_redis_dict_shape_normalized(self):
        redis_job = {
            "id": "r-1",
            "session_id": "s-1",
            "command": "echo API_TOKEN=zzz9",
            "status": "failed",
            "completed_at": 1.0,
            "stdout": "",
            "stderr": "API_TOKEN=zzz9",
            "exit_code": 1,
            "error": "API_TOKEN=zzz9",
        }
        out = serialize_job(redis_job, redact=True)
        assert out["job_id"] == "r-1"
        assert out["error_message"] == "API_TOKEN=" + REDACTED_MARK
        assert "zzz9" not in json.dumps(out)

    def test_extra_keys_preserved(self):
        out = serialize_job(
            {"job_id": "j-1", "status": "running", "wait_timed_out": True, "progress": {"p": 1}},
            redact=True,
        )
        assert out["wait_timed_out"] is True
        assert out["progress"] == {"p": 1}


class TestSerializeJobPathRedaction:
    """M8: an absolute host path a caller already knew (its own project
    root, embedded by the caller into `command` before submission) must not
    be echoed back verbatim through job_result() -- redact_secrets() alone
    is secret-pattern-only, with no path awareness.
    """

    PROJECT_ROOT = "/media/1TB/Python/web_ssh/web-ssh-gateway/workspace/demo-project"

    def _job(self) -> JobRecord:
        job = JobRecord(
            job_id="j-path-1",
            session_id="s-1",
            command=f"sh {self.PROJECT_ROOT}/.ai-bridge/tmp/mcp_script_abc123.sh",
            redact_path_prefix=self.PROJECT_ROOT,
        )
        job.status = "completed"
        job.stdout = f"bash: {self.PROJECT_ROOT}/.ai-bridge/tmp/mcp_script_abc123.sh: line 3: uv: not found\n"
        job.stderr = f"cd {self.PROJECT_ROOT}/subdir failed"
        job.exit_code = 127
        job.error_message = f"script {self.PROJECT_ROOT}/foo.sh not found"
        return job

    def test_redact_true_strips_project_root(self):
        out = serialize_job(self._job(), redact=True)
        assert self.PROJECT_ROOT not in out["command"]
        assert self.PROJECT_ROOT not in out["stdout"]
        assert self.PROJECT_ROOT not in out["stderr"]
        assert self.PROJECT_ROOT not in out["error_message"]
        # Same marker convention as mcp_client_tools.py's own
        # _redact_project_root() -- "." for the bare root, "./" for a
        # nested path -- so the two layers stay idempotent together.
        assert out["command"] == "sh ./.ai-bridge/tmp/mcp_script_abc123.sh"

    def test_redact_false_keeps_raw_path(self):
        out = serialize_job(self._job(), redact=False)
        assert self.PROJECT_ROOT in out["command"]
        assert self.PROJECT_ROOT in out["stdout"]

    def test_redact_path_prefix_itself_never_leaks_into_output(self):
        """redact_path_prefix is internal-only plumbing, not a field callers
        should ever see echoed back."""
        out = serialize_job(self._job(), redact=True)
        assert "redact_path_prefix" not in out
        out_raw = serialize_job(self._job(), redact=False)
        assert "redact_path_prefix" not in out_raw

    def test_no_prefix_set_is_a_no_op(self):
        job = JobRecord(job_id="j-2", session_id="s-1", command=f"echo {self.PROJECT_ROOT}")
        out = serialize_job(job, redact=True)
        assert self.PROJECT_ROOT in out["command"]

    def test_redis_dict_shape_project_root_redacted(self):
        """Same fix for jobs mirrored to Redis (job survives a restart) --
        save_terminal_job() persists redact_path_prefix under the same
        dict-shape key job_serializer already normalizes from."""
        redis_job = {
            "id": "r-path-1",
            "session_id": "s-1",
            "command": f"sh {self.PROJECT_ROOT}/script.sh",
            "status": "completed",
            "completed_at": 1.0,
            "stdout": f"{self.PROJECT_ROOT}/script.sh: ok",
            "stderr": "",
            "exit_code": 0,
            "error": None,
            "redact_path_prefix": self.PROJECT_ROOT,
        }
        out = serialize_job(redis_job, redact=True)
        assert self.PROJECT_ROOT not in out["command"]
        assert self.PROJECT_ROOT not in out["stdout"]
        assert "redact_path_prefix" not in out


class TestJobManagerStartedMessage:
    @pytest.mark.asyncio
    async def test_started_message_redacts_command(self, monkeypatch):
        """SSE status event 'Started: <command>' must not carry the raw
        command when output redaction is enabled."""
        monkeypatch.setattr(settings, "command_output_redaction_enabled", True)
        monkeypatch.setattr(settings, "command_policy_mode", "disabled")

        mock_ssh = AsyncMock()

        async def _stream(*args, **kwargs):
            yield "exit", "0"

        mock_ssh.execute_stream = _stream
        jm = JobManager(ssh_manager=mock_ssh, max_jobs=10)
        job_id = await jm.create_job("s1", SECRET_CMD, owner_id="user:admin")

        queue: asyncio.Queue = asyncio.Queue()
        job = await jm.get_job(job_id)
        job.add_listener(queue)
        await jm.wait_for_all_jobs()

        events: list[dict] = []
        while not queue.empty():
            events.append(await queue.get())

        started = [e for e in events if e.get("type") == "status" and "Started:" in (e.get("message") or "")]
        assert started, "expected a 'Started:' status event"
        assert "abc123" not in started[0]["message"]
        assert REDACTED_MARK in started[0]["message"]


class TestJobsApiRedaction:
    """API-level: /api/jobs list, /result and dead-letter redact.

    Router coroutines are called directly (bypassing FastAPI DI) with a
    hand-built AuthIdentity, matching test_jobs_ownership conventions.
    """

    def _owned_job(self) -> JobRecord:
        job = JobRecord(job_id="job-r1", session_id="s-1", command=SECRET_CMD)
        job.owner_id = token_fingerprint(OWNER_TOKEN)
        job.stdout = SECRET_OUT
        job.stderr = "error: password=secret123"
        job.exit_code = 0
        job.status = "completed"
        return job

    @pytest.mark.asyncio
    async def test_jobs_list_has_no_output_and_redacted_command(self, monkeypatch):
        """Regression: GET /api/jobs used JobResultResponse(**job.to_dict())
        — raw command/stdout/stderr. List must not carry output at all and
        command must be redacted under COMMAND_OUTPUT_REDACTION_ENABLED."""
        job = self._owned_job()
        monkeypatch.setattr(_state, "job_manager", AsyncMock())
        _state.job_manager.list_jobs = AsyncMock(return_value=[job])
        monkeypatch.setattr(settings, "command_output_redaction_enabled", True)

        resp = await jobs_router.jobs_list(None, None, _identity(OWNER_TOKEN))
        listed = resp.jobs[0]
        assert listed.stdout == ""
        assert listed.stderr == ""
        assert "abc123" not in listed.command
        assert REDACTED_MARK in listed.command

    @pytest.mark.asyncio
    async def test_jobs_result_redacts_command(self, monkeypatch):
        """Regression: job_result(redact_output=true) kept the raw command
        field, exposing internal filesystem paths. command must now pass
        through redaction like stdout/stderr."""
        job = self._owned_job()
        monkeypatch.setattr(_state, "job_manager", AsyncMock())
        _state.job_manager.get_job = AsyncMock(return_value=job)

        resp = await jobs_router.jobs_result("job-r1", True, _identity(OWNER_TOKEN))
        assert "abc123" not in resp.command
        assert REDACTED_MARK in resp.command
        assert "abc123" not in resp.stdout
        assert "secret123" not in resp.stderr

    @pytest.mark.asyncio
    async def test_jobs_result_redact_false_keeps_raw(self, monkeypatch):
        job = self._owned_job()
        monkeypatch.setattr(_state, "job_manager", AsyncMock())
        _state.job_manager.get_job = AsyncMock(return_value=job)

        resp = await jobs_router.jobs_result("job-r1", False, _identity(OWNER_TOKEN))
        assert resp.command == SECRET_CMD
        assert resp.stdout == SECRET_OUT

    @pytest.mark.asyncio
    async def test_dead_letter_redacts_redis_payload(self, monkeypatch):
        """Regression: jobs_dead_letter returned the Redis job verbatim —
        command/stdout/stderr/error raw. Must pass through the serializer."""
        redis_job = {
            "id": "dl-1",
            "session_id": "s-1",
            "command": SECRET_CMD,
            "status": "failed",
            "owner_id": token_fingerprint(OWNER_TOKEN),
            "completed_at": 1.0,
            "stdout": SECRET_OUT,
            "stderr": "API_TOKEN=abc123",
            "exit_code": 1,
            "error": "API_TOKEN=abc123",
        }
        queue = MagicMock()
        queue.get_dead_letter_jobs = AsyncMock(return_value=[redis_job])
        monkeypatch.setattr(_state, "redis_queue", queue)
        monkeypatch.setattr(settings, "command_output_redaction_enabled", True)

        resp = await jobs_router.jobs_dead_letter(100, _identity(OWNER_TOKEN))
        job = resp["jobs"][0]
        assert "abc123" not in json.dumps(job)
        assert REDACTED_MARK in json.dumps(job)
