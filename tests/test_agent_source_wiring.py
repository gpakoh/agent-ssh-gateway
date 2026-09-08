from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_write_agent_task_publishes_source_before_remote_write(monkeypatch, tmp_path):
    import examples.mcp_server.mcp_infra.adapters.agent as adapter
    import examples.mcp_server.server as server_mod
    from examples.mcp_server.agent_sources import ManagedSourcePublication

    events: list[str] = []
    client = MagicMock()

    def execute_script(project, script):
        events.append("write-task")
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}

    def publish(project, base_ref):
        events.append("publish-source")
        # Publication now carries the control-plane-proven digest that the
        # task metadata channel forwards to the worker script.
        return ManagedSourcePublication(
            path="/var/lib/mcp-agent/sources/test.bundle",
            sha256="a" * 64,
        )

    client.execute_project_script.side_effect = execute_script
    monkeypatch.setattr(server_mod, "client", client)
    monkeypatch.setattr(adapter, "ensure_managed_source_bundle", publish)

    monkeypatch.setenv("MCP_TASK_CANDIDATE_ROOT", str(tmp_path / "candidates"))

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


def test_dirty_review_source_is_published_and_bound_before_task_write(monkeypatch, tmp_path):
    import examples.mcp_server.mcp_infra.adapters.agent as adapter
    from examples.mcp_server.agent_sources import ManagedDirtyReviewPublication

    base_ref = "1" * 40
    snapshot_ref = "2" * 40
    tree_sha = "3" * 40
    digest = "4" * 64
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        adapter,
        "managed_workspace_path",
        lambda project, task_id: f"/managed/{project}/{task_id}",
    )
    monkeypatch.setattr(
        adapter,
        "ensure_dirty_worktree_review_bundle",
        lambda project, ref: ManagedDirtyReviewPublication(
            path="/managed/source.bundle",
            sha256=digest,
            base_ref=base_ref,
            snapshot_ref=snapshot_ref,
            tree_sha=tree_sha,
        ),
    )
    committed_publish = MagicMock()
    monkeypatch.setattr(adapter, "ensure_managed_source_bundle", committed_publish)

    def fake_write(run_cmd, **kwargs):
        captured.update(kwargs)
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(adapter, "_write_agent_task", fake_write)
    monkeypatch.setenv("MCP_TASK_CANDIDATE_ROOT", str(tmp_path / "candidate-store"))

    result = adapter.gateway_write_agent_task(
        project="nod",
        task_id="dirty-source-wiring-001",
        agent="opencode",
        task="Review current dirty worktree",
        base_ref=base_ref,
        source_mode="dirty_worktree_snapshot",
        workflow_phase="verification",
    )

    assert result["ok"] is True
    committed_publish.assert_not_called()
    assert captured["base_ref"] == base_ref
    assert captured["source_mode"] == "dirty_worktree_snapshot"
    assert captured["source_ref"] == snapshot_ref
    assert captured["source_tree_sha"] == tree_sha
    assert captured["managed_source_sha256"] == digest


def test_dirty_review_publication_failure_never_writes_task(monkeypatch):
    import examples.mcp_server.mcp_infra.adapters.agent as adapter

    monkeypatch.setattr(
        adapter,
        "managed_workspace_path",
        lambda project, task_id: f"/managed/{project}/{task_id}",
    )
    monkeypatch.setattr(
        adapter,
        "ensure_dirty_worktree_review_bundle",
        MagicMock(side_effect=RuntimeError("untracked files rejected")),
    )
    write_task = MagicMock()
    monkeypatch.setattr(adapter, "_write_agent_task", write_task)

    with pytest.raises(RuntimeError, match="untracked files rejected"):
        adapter.gateway_write_agent_task(
            project="nod",
            task_id="dirty-source-wiring-002",
            agent="opencode",
            task="Review current dirty worktree",
            base_ref="5" * 40,
            source_mode="dirty_worktree_snapshot",
        )

    write_task.assert_not_called()


def test_dirty_review_source_requires_managed_workspace(monkeypatch):
    import examples.mcp_server.mcp_infra.adapters.agent as adapter

    monkeypatch.setattr(adapter, "managed_workspace_path", lambda project, task_id: None)
    dirty_publish = MagicMock()
    monkeypatch.setattr(adapter, "ensure_dirty_worktree_review_bundle", dirty_publish)

    result = adapter.gateway_write_agent_task(
        project="nod",
        task_id="dirty-source-wiring-003",
        agent="opencode",
        task="Review current dirty worktree",
        base_ref="6" * 40,
        source_mode="dirty_worktree_snapshot",
    )

    assert result["ok"] is False
    assert "MCP_AGENT_WORKSPACE_ROOT" in result["error"]["message"]
    dirty_publish.assert_not_called()
