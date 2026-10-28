"""Real MCP signatures, write-mode visibility and caller-bound adapters."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from app.gitea_workflow_dispatch import DispatchRefused
from examples.mcp_server.mcp_infra.adapters import coordination_adapter, remote
from examples.mcp_server.mcp_infra.adapters import workflow_dispatch as adapter
from examples.mcp_server.tool_modes import MCP_CLIENT_WRITE_ONLY_GITEA_TOOLS, TOOL_NAMES_BY_MODE
from examples.mcp_server.tool_scopes import get_required_scopes


class Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("GITEA_TOKEN", "private-service-credential")
    monkeypatch.setenv("MCP_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(coordination_adapter, "_owner", lambda: "gpakoh")
    registered = []

    def source(repo):
        registered.append(repo)
        if repo != "project":
            raise ValueError("not a registered source")
        return repo

    monkeypatch.setattr(coordination_adapter, "_registered", source)
    monkeypatch.setattr(remote, "_caller_fingerprint", lambda: "authenticated-caller")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: Client())
    return registered


def arguments(**changes):
    return {
        "owner": "gpakoh",
        "repo": "project",
        "workflow_path": ".github/workflows/probe.yml",
        "ref": "refs/heads/master",
        "expected_ref_sha": "a" * 40,
        "inputs": {"approved": True},
        "operation_key": "qualified-mcp-probe-v1",
        "reason": "Operator explicitly requests this harmless probe",
        **changes,
    }


def test_only_write_admin_mode_can_register_dispatch_tools(monkeypatch):
    registered = {}
    monkeypatch.setattr(
        adapter, "register_tool", lambda name: lambda fn: registered.setdefault(name, fn)
    )
    adapter.register_all()
    for name in ("gitea_dispatch_workflow", "gitea_workflow_dispatch_status"):
        assert name in registered
        assert name in MCP_CLIENT_WRITE_ONLY_GITEA_TOOLS
        assert name in TOOL_NAMES_BY_MODE["mcp_client_write"]
        assert name not in TOOL_NAMES_BY_MODE["mcp_client"]
        assert get_required_scopes(name) == ["mcp:repo", "mcp:admin"]


def test_actual_fastmcp_schema_preserves_strict_boolean_and_scalar_inputs():
    mcp = FastMCP("workflow-dispatch-contract")
    tool = mcp._tool_manager.add_tool(
        adapter.gitea_dispatch_workflow, name="gitea_dispatch_workflow"
    )
    model = tool.fn_metadata.arg_model
    assert model.model_validate(arguments(dry_run=True)).dry_run is True
    for invalid in ("true", 1, None):
        with pytest.raises(ValidationError):
            model.model_validate(arguments(dry_run=invalid))
    for invalid in (["x"], {"nested": "x"}):
        with pytest.raises(ValidationError):
            model.model_validate(arguments(inputs={"approved": invalid}))


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"owner": "other"}, {"repo": "unregistered"}])
async def test_owner_and_registration_denials_precede_dispatch(monkeypatch, tmp_path, changes):
    setup(monkeypatch, tmp_path)
    dispatch = AsyncMock()
    monkeypatch.setattr(adapter, "dispatch_workflow", dispatch)
    result = await adapter.gitea_dispatch_workflow(**arguments(**changes))
    assert result["ok"] is False and result["error"]["code"] == "AUTH_ERROR"
    dispatch.assert_not_awaited()
    assert "private-service-credential" not in repr(result)


@pytest.mark.asyncio
async def test_missing_authenticated_principal_precedes_dispatch(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    monkeypatch.setattr(remote, "_caller_fingerprint", lambda: None)
    dispatch = AsyncMock()
    monkeypatch.setattr(adapter, "dispatch_workflow", dispatch)
    result = await adapter.gitea_dispatch_workflow(**arguments())
    assert result["ok"] is False and result["error"]["code"] == "AUTH_ERROR"
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_binds_actor_journal_and_dry_run_without_exposing_token(
    monkeypatch, tmp_path
):
    setup(monkeypatch, tmp_path)
    dispatch = AsyncMock(return_value={"state": "preflight_passed", "dispatched": False})
    monkeypatch.setattr(adapter, "dispatch_workflow", dispatch)
    result = await adapter.gitea_dispatch_workflow(**arguments(dry_run=True))
    assert result["ok"] is True
    kwargs = dispatch.await_args.kwargs
    assert kwargs["dry_run"] is True and len(kwargs["actor"]) == 64
    assert kwargs["journal_dir"] == tmp_path / "workflow-dispatch"
    assert "private-service-credential" not in repr(result)


@pytest.mark.asyncio
async def test_status_adapter_only_calls_read_operation(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path)
    dispatch = AsyncMock()
    read = AsyncMock(return_value={"state": "not_found", "dispatched": False})
    monkeypatch.setattr(adapter, "dispatch_workflow", dispatch)
    monkeypatch.setattr(adapter, "dispatch_status", read)
    result = await adapter.gitea_workflow_dispatch_status(
        "gpakoh", "project", "qualified-mcp-probe-v1"
    )
    assert result["ok"] is True and result["result"]["state"] == "not_found"
    read.assert_awaited_once()
    dispatch.assert_not_awaited()


def test_required_audit_failure_never_exposes_private_error(monkeypatch):
    class Audit:
        def append_required(self, event):
            raise OSError("private-service-credential")

    monkeypatch.setattr(remote, "_get_gitea_audit_logger", lambda: Audit())
    with pytest.raises(DispatchRefused) as caught:
        adapter._audit({"payload_sha256": "a" * 64})
    assert caught.value.code == "AUDIT_UNAVAILABLE"
    assert "private-service-credential" not in str(caught.value)
