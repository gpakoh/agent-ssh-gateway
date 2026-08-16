from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_write_agent_task_publishes_source_before_remote_write(monkeypatch):
    import examples.mcp_server.mcp_infra.adapters.agent as adapter
    import examples.mcp_server.server as server_mod

    events: list[str] = []
    client = MagicMock()

    def execute_script(project, script):
        events.append("write-task")
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}

    def publish(project, base_ref):
        events.append("publish-source")
        return "/var/lib/mcp-agent/sources/test.bundle"

    client.execute_project_script.side_effect = execute_script
    monkeypatch.setattr(server_mod, "client", client)
    monkeypatch.setattr(adapter, "ensure_managed_source_bundle", publish)

    result = adapter.gateway_write_agent_task(
        project="nod",
        task_id="source-wiring-001",
        agent="opencode",
        task="Do the thing",
        base_ref="a" * 40,
    )

    assert result["ok"] is True
    assert events == ["publish-source", "write-task"]


def test_source_publication_failure_leaves_no_runnable_task(monkeypatch):
    import examples.mcp_server.mcp_infra.adapters.agent as adapter
    import examples.mcp_server.server as server_mod

    client = MagicMock()
    monkeypatch.setattr(server_mod, "client", client)

    def fail_publish(project, base_ref):
        raise RuntimeError("managed source unavailable")

    monkeypatch.setattr(adapter, "ensure_managed_source_bundle", fail_publish)

    with pytest.raises(RuntimeError, match="managed source unavailable"):
        adapter.gateway_write_agent_task(
            project="nod",
            task_id="source-wiring-002",
            agent="opencode",
            task="Do the thing",
            base_ref="b" * 40,
        )

    client.execute_project_script.assert_not_called()
