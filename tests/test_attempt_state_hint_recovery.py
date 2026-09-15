from __future__ import annotations

import json

import pytest

PROJECT_KEY = "historical-project-0123456789ab"
TASK_ID = "historical-task-20260912"
ATTEMPT_ID = "a" * 32


def _agent_tasks_module():
    import examples.mcp_server.agent_tasks as agent_tasks

    return agent_tasks


AttemptStateError = _agent_tasks_module().AttemptStateError
read_agent_attempt_hint_by_state_key = _agent_tasks_module().read_agent_attempt_hint_by_state_key


def _write_attempt(root, bucket: str, *, attempt_id: str = ATTEMPT_ID, job_id=None):
    task_dir = root / PROJECT_KEY / bucket / TASK_ID
    task_dir.mkdir(parents=True)
    record = {
        "attempt_id": attempt_id,
        "fingerprint": "f" * 64,
        "job_id": job_id,
    }
    path = task_dir / "attempt-state.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_attempt_hint_reads_active_state_without_trusting_job_id(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_AGENT_STATE_ROOT", str(tmp_path))
    _write_attempt(tmp_path, "tasks", job_id="forged-worker-job-id")

    assert (
        read_agent_attempt_hint_by_state_key(project_key=PROJECT_KEY, task_id=TASK_ID)
        == ATTEMPT_ID
    )


def test_attempt_hint_reads_archived_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_AGENT_STATE_ROOT", str(tmp_path))
    _write_attempt(tmp_path, "archive")

    assert (
        read_agent_attempt_hint_by_state_key(project_key=PROJECT_KEY, task_id=TASK_ID)
        == ATTEMPT_ID
    )


def test_attempt_hint_fails_closed_when_active_and_archive_both_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_AGENT_STATE_ROOT", str(tmp_path))
    _write_attempt(tmp_path, "tasks")
    _write_attempt(tmp_path, "archive")

    with pytest.raises(AttemptStateError, match="both active and archive"):
        read_agent_attempt_hint_by_state_key(project_key=PROJECT_KEY, task_id=TASK_ID)


def test_attempt_hint_rejects_symlink_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_AGENT_STATE_ROOT", str(tmp_path))
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps({"attempt_id": ATTEMPT_ID, "fingerprint": "f" * 64, "job_id": None}),
        encoding="utf-8",
    )
    task_dir = tmp_path / PROJECT_KEY / "tasks" / TASK_ID
    task_dir.mkdir(parents=True)
    (task_dir / "attempt-state.json").symlink_to(outside)

    with pytest.raises(AttemptStateError, match="not safely readable"):
        read_agent_attempt_hint_by_state_key(project_key=PROJECT_KEY, task_id=TASK_ID)


def test_attempt_hint_rejects_symlink_ancestor(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_AGENT_STATE_ROOT", str(tmp_path))
    outside = tmp_path / "outside"
    _write_attempt(outside, "tasks")
    (tmp_path / PROJECT_KEY).symlink_to(outside / PROJECT_KEY, target_is_directory=True)

    with pytest.raises(AttemptStateError, match="not safely readable"):
        read_agent_attempt_hint_by_state_key(project_key=PROJECT_KEY, task_id=TASK_ID)


def test_attempt_hint_rejects_non_uuid_hex_attempt_id(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_AGENT_STATE_ROOT", str(tmp_path))
    _write_attempt(tmp_path, "tasks", attempt_id="worker-controlled-value")

    with pytest.raises(AttemptStateError, match="invalid attempt_id"):
        read_agent_attempt_hint_by_state_key(project_key=PROJECT_KEY, task_id=TASK_ID)


def test_attempt_hint_missing_state_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_AGENT_STATE_ROOT", str(tmp_path))

    assert read_agent_attempt_hint_by_state_key(project_key=PROJECT_KEY, task_id=TASK_ID) is None


def test_attempt_hint_ignores_noncanonical_project_state_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_AGENT_STATE_ROOT", str(tmp_path))

    assert read_agent_attempt_hint_by_state_key(project_key="../escape", task_id=TASK_ID) is None


class _ResolveClient:
    def __init__(self, answers: dict[str, str | None]):
        self.answers = answers
        self.calls: list[str] = []

    def resolve_submission_job(self, submission_key: str) -> str | None:
        self.calls.append(submission_key)
        return self.answers.get(submission_key)


def _resolver_with(monkeypatch, *, trusted_identity=None, attempt_hint=None, answers=None):
    import examples.mcp_server.mcp_infra.adapters.agent as agent_adapter

    client = _ResolveClient(answers or {})
    monkeypatch.setattr(
        agent_adapter,
        "read_task_attempt_identity_by_state_key",
        lambda **_kwargs: trusted_identity,
    )
    monkeypatch.setattr(
        agent_adapter,
        "_read_agent_attempt_hint_by_state_key",
        lambda **_kwargs: attempt_hint,
    )
    monkeypatch.setattr(agent_adapter, "_server_agent_client", lambda: client)
    return agent_adapter._trusted_fleet_job_resolver(), client


def test_fleet_resolver_uses_attempt_hint_only_via_exact_gateway_key(monkeypatch):
    durable = f"{PROJECT_KEY}:{TASK_ID}"
    exact_key = f"task:{durable}:attempt:{ATTEMPT_ID}"
    resolver, client = _resolver_with(
        monkeypatch,
        attempt_hint=ATTEMPT_ID,
        answers={exact_key: "job-authoritative"},
    )

    assert resolver(durable) == "job-authoritative"
    assert client.calls == [exact_key]


def test_fleet_resolver_does_not_legacy_fallback_after_hint_miss(monkeypatch):
    durable = f"{PROJECT_KEY}:{TASK_ID}"
    exact_key = f"task:{durable}:attempt:{ATTEMPT_ID}"
    resolver, client = _resolver_with(
        monkeypatch,
        attempt_hint=ATTEMPT_ID,
        answers={f"task:{durable}": "wrong-legacy-job"},
    )

    assert resolver(durable) is None
    assert client.calls == [exact_key]


def test_fleet_resolver_legacy_fallback_requires_no_attempt_evidence(monkeypatch):
    durable = f"{PROJECT_KEY}:{TASK_ID}"
    legacy_key = f"task:{durable}"
    resolver, client = _resolver_with(
        monkeypatch,
        attempt_hint=None,
        answers={legacy_key: "job-legacy"},
    )

    assert resolver(durable) == "job-legacy"
    assert client.calls == [legacy_key]


def test_fleet_resolver_candidate_binding_wins_before_worker_hint(monkeypatch):
    import examples.mcp_server.mcp_infra.adapters.agent as agent_adapter

    durable = f"{PROJECT_KEY}:{TASK_ID}"
    hint_called = False
    client = _ResolveClient({})
    monkeypatch.setattr(
        agent_adapter,
        "read_task_attempt_identity_by_state_key",
        lambda **_kwargs: (ATTEMPT_ID, "job-control-plane"),
    )

    def _hint(**_kwargs):
        nonlocal hint_called
        hint_called = True
        return "b" * 32

    monkeypatch.setattr(agent_adapter, "_read_agent_attempt_hint_by_state_key", _hint)
    monkeypatch.setattr(agent_adapter, "_server_agent_client", lambda: client)

    assert agent_adapter._trusted_fleet_job_resolver()(durable) == "job-control-plane"
    assert hint_called is False
    assert client.calls == []
