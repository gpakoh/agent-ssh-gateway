"""Fail malformed mutating workspace requests before registry access (#884)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from examples.mcp_server.mcp_infra.adapters import workspace

MUTATIONS: tuple[tuple[str, dict[str, Any], str], ...] = (
    (
        "gateway_workspace_file_write",
        {"project_id": "p", "relative_path": "file.txt", "content": "new data"},
        "project_file_write",
    ),
    (
        "gateway_workspace_file_edit",
        {
            "project_id": "p",
            "relative_path": "file.txt",
            "old_string": "before",
            "new_string": "after",
        },
        "project_file_edit",
    ),
    (
        "gateway_workspace_apply_patch",
        {
            "project_id": "p",
            "relative_path": "file.txt",
            "patch": "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-old\n+new\n",
        },
        "project_apply_patch",
    ),
)


@pytest.mark.parametrize(("tool_name", "args", "_core_name"), MUTATIONS)
@pytest.mark.parametrize(
    "bad_path",
    ["", "../README.md", "nested/../../README.md", "/etc/passwd", "~/private", "bad\x00name", None, 123],
)
def test_unsafe_mutation_path_is_typed_before_registry(
    tool_name: str, args: dict[str, Any], _core_name: str, bad_path: object
) -> None:
    with patch.object(
        workspace, "_server_workspace_registry",
        side_effect=AssertionError("registry must not be accessed"),
    ) as registry:
        result = getattr(workspace, tool_name)(**{**args, "relative_path": bad_path})

    registry.assert_not_called()
    assert result["ok"] is False
    error = result["error"]
    assert error["code"] == "INVALID_INPUT"
    assert error["details"] == {"field": "relative_path"}
    assert error["retryable"] is False
    assert error["message"] == "relative_path must be a safe project-relative file path"
    if isinstance(bad_path, str) and bad_path:
        assert bad_path not in str(error)


@pytest.mark.parametrize(("tool_name", "args", "_core_name"), MUTATIONS)
@pytest.mark.parametrize("bad_size", [0, -1, True, 1.5, "100", None])
def test_mutation_max_bytes_is_typed_before_registry(
    tool_name: str, args: dict[str, Any], _core_name: str, bad_size: object
) -> None:
    with patch.object(
        workspace, "_server_workspace_registry",
        side_effect=AssertionError("registry must not be accessed"),
    ) as registry:
        result = getattr(workspace, tool_name)(**args, max_bytes=bad_size)

    registry.assert_not_called()
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["retryable"] is False
    assert result["error"]["details"] == {"field": "max_bytes", "min_value": 1}


@pytest.mark.parametrize("bad_old_string", ["", None, 42])
def test_mutation_edit_old_string_is_typed_before_registry(bad_old_string: object) -> None:
    with patch.object(
        workspace, "_server_workspace_registry",
        side_effect=AssertionError("registry must not be accessed"),
    ) as registry:
        result = workspace.gateway_workspace_file_edit(
            project_id="p", relative_path="file.txt",
            old_string=bad_old_string, new_string="after",
        )

    registry.assert_not_called()
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["details"] == {"field": "old_string"}


@pytest.mark.parametrize("bad_patch", ["", "  \n", None, 42])
def test_mutation_empty_patch_is_typed_before_registry(bad_patch: object) -> None:
    with patch.object(
        workspace, "_server_workspace_registry",
        side_effect=AssertionError("registry must not be accessed"),
    ) as registry:
        result = workspace.gateway_workspace_apply_patch(
            project_id="p", relative_path="file.txt", patch=bad_patch,
        )

    registry.assert_not_called()
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["details"] == {"field": "patch"}


@pytest.mark.parametrize(("tool_name", "args", "core_name"), MUTATIONS)
@pytest.mark.parametrize("path", ["file.txt", "./file.txt", "nested/file.txt", "file..txt"])
def test_safe_mutation_forwards_arguments_unchanged(
    tool_name: str, args: dict[str, Any], core_name: str, path: str
) -> None:
    registry_object = object()
    with (
        patch.object(workspace, "_server_workspace_registry", return_value=registry_object) as registry,
        patch(f"app.workspace.edit.{core_name}", return_value={"project_id": "p", "changed": True}) as core,
    ):
        result = getattr(workspace, tool_name)(**{**args, "relative_path": path}, max_bytes=2)

    registry.assert_called_once_with()
    core.assert_called_once_with(
        **{**args, "relative_path": path},
        max_bytes=2,
        registry=registry_object,
        safe=False,
        **({"create_parents": False} if core_name == "project_file_write" else {}),
    )
    assert result["ok"] is True
