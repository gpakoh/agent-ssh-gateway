"""Fast preview argument validation: no registry or filesystem work (#884)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from examples.mcp_server.mcp_infra.adapters import workspace

PREVIEWS: tuple[tuple[str, dict[str, Any], str], ...] = (
    (
        "gateway_workspace_preview_write",
        {"project_id": "p", "relative_path": "file.txt", "content": "data"},
        "project_file_preview_write",
    ),
    (
        "gateway_workspace_preview_edit",
        {
            "project_id": "p",
            "relative_path": "file.txt",
            "old_string": "before",
            "new_string": "after",
        },
        "project_file_preview_edit",
    ),
    (
        "gateway_workspace_preview_patch",
        {"project_id": "p", "relative_path": "file.txt", "patch": "valid patch"},
        "project_file_preview_patch",
    ),
)


@pytest.mark.parametrize(("tool_name", "args", "_core_name"), PREVIEWS)
@pytest.mark.parametrize("bad_size", [0, -1, True, 1.5, "100", None])
def test_invalid_max_bytes_is_typed_before_registry(
    tool_name: str, args: dict[str, Any], _core_name: str, bad_size: object
) -> None:
    with patch.object(
        workspace,
        "_server_workspace_registry",
        side_effect=AssertionError("registry must not be accessed"),
    ) as registry:
        result = getattr(workspace, tool_name)(**args, max_bytes=bad_size)

    registry.assert_not_called()
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"] == {"field": "max_bytes", "min_value": 1}


@pytest.mark.parametrize("bad_old_string", ["", None, 42])
def test_invalid_old_string_is_typed_before_registry(bad_old_string: object) -> None:
    with patch.object(
        workspace,
        "_server_workspace_registry",
        side_effect=AssertionError("registry must not be accessed"),
    ) as registry:
        result = workspace.gateway_workspace_preview_edit(
            project_id="p",
            relative_path="file.txt",
            old_string=bad_old_string,
            new_string="after",
        )

    registry.assert_not_called()
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"] == {"field": "old_string"}


@pytest.mark.parametrize(("tool_name", "args", "core_name"), PREVIEWS)
def test_valid_preview_still_calls_core_with_original_limit(
    tool_name: str, args: dict[str, Any], core_name: str
) -> None:
    registry_object = object()
    fake_result = {"project_id": "p", "changed": False}
    with (
        patch.object(
            workspace, "_server_workspace_registry", return_value=registry_object
        ) as registry,
        patch(
            f"app.workspace.preview.{core_name}", return_value=fake_result
        ) as core,
    ):
        result = getattr(workspace, tool_name)(**args, max_bytes=2)

    registry.assert_called_once_with()
    core.assert_called_once_with(
        **args, max_bytes=2, registry=registry_object
    )
    assert result["ok"] is True
    assert result["result"] == fake_result


@pytest.mark.parametrize(("tool_name", "args", "_core_name"), PREVIEWS)
@pytest.mark.parametrize(
    "bad_path",
    ["", "../README.md", "nested/../../README.md", "/etc/passwd", "~/private", "bad\x00name", None, 123],
)
def test_unsafe_preview_path_rejected_before_registry(
    tool_name: str, args: dict[str, Any], _core_name: str, bad_path: object
) -> None:
    with patch.object(
        workspace,
        "_server_workspace_registry",
        side_effect=AssertionError("registry must not be accessed"),
    ) as registry:
        result = getattr(workspace, tool_name)(**{**args, "relative_path": bad_path})

    registry.assert_not_called()
    assert result["ok"] is False
    error = result["error"]
    assert error["code"] == "INVALID_INPUT"
    assert error["retryable"] is False
    assert error["details"] == {"field": "relative_path"}
    assert error["message"] == "relative_path must be a safe project-relative file path"
    if isinstance(bad_path, str) and bad_path:
        assert bad_path not in error["message"]


@pytest.mark.parametrize("bad_patch", ["", "  \n", None, 42])
def test_empty_preview_patch_rejected_before_registry(bad_patch: object) -> None:
    with patch.object(
        workspace,
        "_server_workspace_registry",
        side_effect=AssertionError("registry must not be accessed"),
    ) as registry:
        result = workspace.gateway_workspace_preview_patch(
            project_id="p", relative_path="file.txt", patch=bad_patch
        )

    registry.assert_not_called()
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"] == {"field": "patch"}


@pytest.mark.parametrize(("tool_name", "args", "core_name"), PREVIEWS)
@pytest.mark.parametrize("relative_path", ["./file.txt", "nested/file.txt", "file..txt"])
def test_safe_preview_path_forwards_unchanged(
    tool_name: str, args: dict[str, Any], core_name: str, relative_path: str
) -> None:
    registry_object = object()
    with (
        patch.object(
            workspace, "_server_workspace_registry", return_value=registry_object
        ),
        patch(f"app.workspace.preview.{core_name}", return_value={"changed": False}) as core,
    ):
        result = getattr(workspace, tool_name)(**{**args, "relative_path": relative_path})

    assert result["ok"] is True
    core.assert_called_once_with(
        **{**args, "relative_path": relative_path},
        max_bytes=1_000_000,
        registry=registry_object,
    )
