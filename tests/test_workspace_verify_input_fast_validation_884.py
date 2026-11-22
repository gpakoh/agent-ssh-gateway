"""Fail unsafe workspace_verify paths before registry access (#884).

Keep existing arbitrary expected-hash mismatch compatibility unchanged.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from examples.mcp_server.mcp_infra.adapters import workspace


@pytest.mark.parametrize(
    "bad_path",
    ["", "../README.md", "nested/../../README.md", "/etc/passwd",
     "~/private", "bad\x00name", None, 123],
)
def test_unsafe_verify_path_rejected_before_registry(bad_path: object) -> None:
    with patch.object(
        workspace, "_server_workspace_registry",
        side_effect=AssertionError("registry must not be accessed"),
    ) as registry:
        result = workspace.gateway_workspace_verify(
            project_id="p", relative_path=bad_path,
            expected_hash="sha256:short-but-accepted",
        )

    registry.assert_not_called()
    assert result["ok"] is False
    error = result["error"]
    assert error["code"] == "INVALID_INPUT"
    assert error["retryable"] is False
    assert error["details"] == {"field": "relative_path"}
    assert error["message"] == "relative_path must be a safe project-relative file path"
    if isinstance(bad_path, str) and bad_path:
        assert bad_path not in str(error)


@pytest.mark.parametrize("path", ["file.txt", "./file.txt", "nested/file.txt", "file..txt"])
@pytest.mark.parametrize("hash_value", ["sha256:abc", "sha256:wrong", "not-a-hash", "sha256:" + "0" * 64])
def test_safe_verify_forwards_arbitrary_expected_hash_unchanged(
    path: str, hash_value: str
) -> None:
    registry_object = object()
    expected = {
        "project_id": "p", "path": path,
        "matches": False, "verified": False, "current_hash": "sha256:real",
        "expected_hash": hash_value, "file_exists": True,
    }
    with (
        patch.object(workspace, "_server_workspace_registry", return_value=registry_object) as registry,
        patch("app.workspace.preview.project_file_verify", return_value=expected) as core,
    ):
        result = workspace.gateway_workspace_verify(
            project_id="p", relative_path=path, expected_hash=hash_value,
        )

    registry.assert_called_once_with()
    core.assert_called_once_with(
        project_id="p", relative_path=path,
        expected_hash=hash_value, registry=registry_object,
    )
    assert result["ok"] is True
    assert result["result"] == expected
