from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(EXAMPLES_DIR.parent))

import examples.mcp_server.server as srv  # noqa: E402
from app.workspace.policy import WorkspacePolicyError  # noqa: E402
from examples.mcp_client_remote.fleet.docker_client import RunResult  # noqa: E402

PROJECT_ID = "site-audit-platform"
RESOLVED_ROOT = "/allowed/site-audit-platform"


def _registered_project() -> SimpleNamespace:
    registry = SimpleNamespace()
    registry.project_info = MagicMock(return_value={"root": RESOLVED_ROOT})
    return registry


def _unknown_project() -> SimpleNamespace:
    registry = SimpleNamespace()
    registry.project_info = MagicMock(side_effect=WorkspacePolicyError("unknown project"))
    return registry


def _docker_client() -> MagicMock:
    client = MagicMock()
    client._validate_project_dir = MagicMock()
    client.last_truncated = False
    client.last_redacted = False
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "kwargs", "field"),
    [
        ("docker_compose_ps", {"project_dir": 42}, "project_dir"),
        ("docker_compose_ps", {"project_dir": "  "}, "project_dir"),
        ("docker_compose_ps", {"limit": 0}, "limit"),
        ("docker_compose_ps", {"limit": 501}, "limit"),
        ("docker_compose_ps", {"limit": True}, "limit"),
        ("docker_compose_ps", {"limit": "50"}, "limit"),
        ("docker_compose_services", {"project_dir": 42}, "project_dir"),
        ("docker_compose_services", {"project_dir": "  "}, "project_dir"),
        ("docker_compose_logs", {"project_dir": 42}, "project_dir"),
        ("docker_compose_logs", {"project_dir": "  "}, "project_dir"),
        ("docker_compose_logs", {"services": "web"}, "services"),
        ("docker_compose_logs", {"services": ["bad/name"]}, "services"),
        ("docker_compose_logs", {"tail": 0}, "tail"),
        ("docker_compose_logs", {"tail": 1001}, "tail"),
        ("docker_compose_logs", {"tail": True}, "tail"),
        ("docker_compose_logs", {"tail": "100"}, "tail"),
        ("docker_compose_logs", {"follow": True}, "follow"),
        ("docker_compose_logs", {"follow": 1}, "follow"),
        ("docker_compose_logs", {"timestamps": "yes"}, "timestamps"),
    ],
)
async def test_compose_read_rejects_bad_schema_before_docker_and_registry(
    tool_name: str, kwargs: dict[str, object], field: str
) -> None:
    with (
        patch.object(srv, "DockerClient", side_effect=AssertionError("premature Docker client")),
        patch.object(
            srv, "_get_workspace_registry", side_effect=AssertionError("premature registry lookup")
        ),
    ):
        result = await getattr(srv, tool_name)(**kwargs)
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["details"]["field"] == field
    assert result["error"]["message"] == "Invalid Docker Compose read argument"
    assert "bad/name" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "kwargs", "client_method", "expected_kwargs"),
    [
        ("docker_compose_ps", {"limit": 1}, "compose_ps", {"limit": 1}),
        ("docker_compose_ps", {"limit": 500}, "compose_ps", {"limit": 500}),
        (
            "docker_compose_logs",
            {"tail": 1, "follow": False, "timestamps": True},
            "compose_logs",
            {"tail": 1, "follow": False, "timestamps": True},
        ),
        (
            "docker_compose_logs",
            {"tail": 1000, "follow": False, "timestamps": False},
            "compose_logs",
            {"tail": 1000, "follow": False, "timestamps": False},
        ),
    ],
)
async def test_compose_read_accepts_valid_numeric_boundaries(
    tool_name: str,
    kwargs: dict[str, object],
    client_method: str,
    expected_kwargs: dict[str, object],
) -> None:
    client = _docker_client()
    response: object = [] if client_method == "compose_ps" else {"lines": [], "count": 0}
    method = AsyncMock(return_value=response)
    setattr(client, client_method, method)
    with patch.object(srv, "DockerClient", return_value=client):
        result = await getattr(srv, tool_name)(**kwargs)
    assert result["ok"] is True
    expected_call = {"project_dir": None, **expected_kwargs}
    if client_method == "compose_logs":
        expected_call["services"] = None
    method.assert_awaited_once_with(**expected_call)


@pytest.mark.asyncio
async def test_compose_services_resolves_registered_project_id() -> None:
    registry = _registered_project()
    client = _docker_client()
    client.compose_services = AsyncMock(return_value={"services": ["audit-api"], "count": 1})

    with (
        patch.object(srv, "_get_workspace_registry", return_value=registry),
        patch.object(srv, "DockerClient", return_value=client),
    ):
        result = await srv.docker_compose_services(project_dir=PROJECT_ID)

    assert result["ok"] is True
    registry.project_info.assert_called_once_with(PROJECT_ID)
    client._validate_project_dir.assert_called_once_with(RESOLVED_ROOT)
    client.compose_services.assert_awaited_once_with(project_dir=RESOLVED_ROOT)


@pytest.mark.asyncio
async def test_compose_ps_resolves_registered_project_id() -> None:
    registry = _registered_project()
    client = _docker_client()
    client.compose_ps = AsyncMock(return_value=[])

    with (
        patch.object(srv, "_get_workspace_registry", return_value=registry),
        patch.object(srv, "DockerClient", return_value=client),
    ):
        result = await srv.docker_compose_ps(project_dir=PROJECT_ID, limit=5)

    assert result["ok"] is True
    client.compose_ps.assert_awaited_once_with(project_dir=RESOLVED_ROOT, limit=5)


@pytest.mark.asyncio
async def test_compose_logs_resolves_registered_project_id() -> None:
    registry = _registered_project()
    client = _docker_client()
    client.compose_logs = AsyncMock(return_value={"lines": [], "count": 0})

    with (
        patch.object(srv, "_get_workspace_registry", return_value=registry),
        patch.object(srv, "DockerClient", return_value=client),
    ):
        result = await srv.docker_compose_logs(
            project_dir=PROJECT_ID,
            services=["audit-api"],
            tail=20,
            follow=False,
            timestamps=True,
        )

    assert result["ok"] is True
    client.compose_logs.assert_awaited_once_with(
        project_dir=RESOLVED_ROOT,
        services=["audit-api"],
        tail=20,
        follow=False,
        timestamps=True,
    )


@pytest.mark.asyncio
async def test_unknown_registry_value_preserves_legacy_path_semantics() -> None:
    registry = _unknown_project()
    client = _docker_client()
    client.compose_services = AsyncMock(return_value={"services": [], "count": 0})
    legacy_path = "/allowed/legacy-compose-root"

    with (
        patch.object(srv, "_get_workspace_registry", return_value=registry),
        patch.object(srv, "DockerClient", return_value=client),
    ):
        result = await srv.docker_compose_services(project_dir=legacy_path)

    assert result["ok"] is True
    client._validate_project_dir.assert_not_called()
    client.compose_services.assert_awaited_once_with(project_dir=legacy_path)


@pytest.mark.asyncio
async def test_registered_project_rejection_does_not_leak_resolved_root() -> None:
    registry = _registered_project()
    client = _docker_client()
    client._validate_project_dir.side_effect = ValueError(
        f"Project directory {RESOLVED_ROOT} is outside allowed roots"
    )

    with (
        patch.object(srv, "_get_workspace_registry", return_value=registry),
        patch.object(srv, "DockerClient", return_value=client),
    ):
        result = await srv.docker_compose_services(project_dir=PROJECT_ID)

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert PROJECT_ID in result["error"]["message"]
    assert RESOLVED_ROOT not in result["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("impl_name", "client_method", "kwargs", "return_value"),
    [
        (
            "_docker_compose_up_impl",
            "compose_up",
            {"services": ["audit-api"], "detach": True, "build": False, "timeout": 60},
            "started",
        ),
        (
            "_docker_compose_restart_impl",
            "compose_restart",
            {"services": ["audit-api"], "timeout": 30},
            "restarted",
        ),
        (
            "_docker_compose_build_impl",
            "compose_build",
            {"services": ["audit-api"], "no_cache": False, "timeout": 120},
            "built",
        ),
        (
            "_docker_compose_down_impl",
            "compose_down",
            {"remove_orphans": False, "timeout": 30, "volumes": False},
            RunResult("down", "", 0),
        ),
    ],
)
async def test_confirmed_compose_impls_resolve_registered_project_id(
    impl_name: str,
    client_method: str,
    kwargs: dict[str, object],
    return_value: object,
) -> None:
    registry = _registered_project()
    client = _docker_client()
    method = AsyncMock(return_value=return_value)
    setattr(client, client_method, method)

    with (
        patch.object(srv, "_get_workspace_registry", return_value=registry),
        patch.object(srv, "DockerClient", return_value=client),
    ):
        result = await getattr(srv, impl_name)(project_dir=PROJECT_ID, **kwargs)

    assert result == return_value
    method.assert_awaited_once_with(project_dir=RESOLVED_ROOT, **kwargs)


@pytest.mark.asyncio
async def test_compose_confirmation_keeps_logical_project_id() -> None:
    srv._confirm_store._actions.clear()
    registry = _registered_project()
    client = _docker_client()
    try:
        with (
            patch.object(srv, "_get_workspace_registry", return_value=registry),
            patch.object(srv, "DockerClient", return_value=client),
        ):
            action = await srv.docker_compose_up(
                project_dir=PROJECT_ID,
                services=["audit-api"],
                detach=True,
                build=False,
                timeout=60,
            )
        assert action["ok"] is True
        result = action["result"]
        pending = srv._confirm_store._actions[result["action_id"]]
        assert pending.kwargs["project_dir"] == PROJECT_ID
        assert pending.kwargs["_resolved_project_dir"] == RESOLVED_ROOT
        assert PROJECT_ID in result["summary"]
        assert RESOLVED_ROOT not in result["summary"]
    finally:
        srv._confirm_store._actions.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "client_method", "docker_error", "expected_code", "reason"),
    [
        (
            tool, method,
            "docker exited 1: no configuration file provided: not found",
            "INVALID_INPUT", "config_not_found",
        )
        for tool, method in (
            ("docker_compose_services", "compose_services"),
            ("docker_compose_ps", "compose_ps"),
            ("docker_compose_logs", "compose_logs"),
        )
    ] + [
        (
            tool, method,
            "docker exited 1: open /secret/tenant/compose.yaml: permission denied",
            "DOCKER_COMMAND_FAILED", "config_unreadable",
        )
        for tool, method in (
            ("docker_compose_services", "compose_services"),
            ("docker_compose_ps", "compose_ps"),
            ("docker_compose_logs", "compose_logs"),
        )
    ],
)
async def test_compose_read_reports_safe_actionable_config_diagnostic(
    tool_name: str,
    client_method: str,
    docker_error: str,
    expected_code: str,
    reason: str,
) -> None:
    client = _docker_client()
    method = AsyncMock(side_effect=RuntimeError(docker_error))
    setattr(client, client_method, method)

    with (
        patch.object(srv, "_get_workspace_registry", return_value=_unknown_project()),
        patch.object(srv, "DockerClient", return_value=client),
    ):
        result = await getattr(srv, tool_name)(
            project_dir="/allowed/compose-app"
        )

    assert result["ok"] is False
    assert result["error"]["code"] == expected_code
    assert result["error"]["details"]["field"] == "project_dir"
    assert result["error"]["details"]["reason"] == reason
    assert "/secret/tenant" not in str(result)
    assert result["error"]["retryable"] is False
    method.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "client_method"),
    [
        ("docker_compose_services", "compose_services"),
        ("docker_compose_ps", "compose_ps"),
        ("docker_compose_logs", "compose_logs"),
    ],
)
async def test_compose_read_cli_timeout_does_not_claim_helper_termination(
    tool_name: str, client_method: str,
) -> None:
    from examples.mcp_client_remote.fleet.docker_client import DockerClient

    process = MagicMock()
    process.communicate = AsyncMock(side_effect=TimeoutError())
    process.wait = AsyncMock(return_value=-9)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
        with pytest.raises(RuntimeError) as caught:
            await DockerClient()._run(
                ["docker", "run", "secret-argv-marker"], timeout=60.0,
            )
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()
    client = _docker_client()
    method = AsyncMock(side_effect=caught.value)
    setattr(client, client_method, method)
    with (
        patch.object(srv, "_get_workspace_registry", return_value=_unknown_project()),
        patch.object(srv, "DockerClient", return_value=client),
    ):
        result = await getattr(srv, tool_name)(project_dir="/allowed/compose-app")

    assert result["ok"] is False
    assert result["error"]["code"] == "TIMEOUT"
    assert result["error"]["retryable"] is False
    details = result["error"]["details"]
    assert details["phase"] == "docker_cli"
    assert details["timeout_s"] == 60.0
    assert details["helper_outcome"] == "unknown"
    assert "docker_ps" in result["error"]["hint"]
    assert "secret-argv-marker" not in str(result)
    method.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "client_method"),
    [
        ("docker_compose_services", "compose_services"),
        ("docker_compose_ps", "compose_ps"),
        ("docker_compose_logs", "compose_logs"),
    ],
)
async def test_compose_read_unrelated_docker_failures_remain_command_failed(
    tool_name: str, client_method: str
) -> None:
    client = _docker_client()
    setattr(client, client_method, AsyncMock(side_effect=RuntimeError(
        "docker exited 1: connection refused"
    )))
    with (
        patch.object(srv, "_get_workspace_registry", return_value=_unknown_project()),
        patch.object(srv, "DockerClient", return_value=client),
    ):
        result = await getattr(srv, tool_name)(project_dir="/allowed/compose-app")
    assert result["ok"] is False
    assert result["error"]["code"] == "DOCKER_COMMAND_FAILED"
    assert result["error"]["message"] == "docker exited 1: connection refused"
