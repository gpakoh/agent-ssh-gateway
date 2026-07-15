"""Tests for MCP workspace preview/verify tools and safe param wiring."""

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"
sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(EXAMPLES_DIR.parent))


@pytest.fixture(autouse=True)
def _set_auth_mode():
    with patch.dict(os.environ, {"MCP_AUTH_MODE": "oauth", "MCP_GATEWAY_TOOL_MODE": "chatgpt"}, clear=False):
        yield


# ── Tool registration ───────────────────────────────────────────

ALL_PREVIEW_TOOLS = [
    "workspace_preview_write",
    "workspace_preview_edit",
    "workspace_preview_patch",
    "workspace_verify",
]

ALL_WRITE_TOOLS = [
    "workspace_file_write",
    "workspace_file_edit",
    "workspace_apply_patch",
]


def _registered_tool_names(mode: str = "chatgpt") -> set[str]:
    with patch.dict(os.environ, {"MCP_GATEWAY_TOOL_MODE": mode}):
        import examples.mcp_server.server as srv
        importlib.reload(srv)
        names = {t.name for t in srv.mcp._tool_manager._tools.values()}
    return names


def test_preview_tools_registered_in_chatgpt():
    """All 4 preview/verify tools must appear in chatgpt mode registry."""
    names = _registered_tool_names("chatgpt")
    for name in ALL_PREVIEW_TOOLS:
        assert name in names, f"{name!r} not registered in chatgpt"


def test_write_tools_registered_in_standard():
    """Write tools must be registered in standard mode."""
    names = _registered_tool_names("standard")
    for name in ALL_WRITE_TOOLS:
        assert name in names, f"{name!r} not registered in standard"


def test_write_tools_not_registered_in_chatgpt():
    """Write tools must NOT be registered in chatgpt (read-only safe mode)."""
    names = _registered_tool_names("chatgpt")
    for name in ALL_WRITE_TOOLS:
        assert name not in names, f"{name!r} should not be registered in chatgpt"


# ── Scope enforcement ───────────────────────────────────────────

def test_preview_tools_require_mcp_project():
    """Each preview/verify tool must require mcp:project scope."""
    from examples.mcp_server.tool_scopes import TOOL_SCOPES

    for name in ALL_PREVIEW_TOOLS:
        assert name in TOOL_SCOPES, f"{name!r} not in TOOL_SCOPES"
        assert "mcp:project" in TOOL_SCOPES[name], (
            f"{name!r} missing mcp:project scope"
        )


def test_preview_tools_reject_readonly_scope():
    """Tools must not be callable with only mcp:read scope."""
    from examples.mcp_server.tool_scopes import has_required_scope

    for name in ALL_PREVIEW_TOOLS:
        assert not has_required_scope(["mcp:read"], name), (
            f"{name!r} should require mcp:project, not mcp:read"
        )


def test_write_tools_have_safe_scope():
    """Write tools must still require mcp:project scope."""
    from examples.mcp_server.tool_scopes import TOOL_SCOPES

    for name in ALL_WRITE_TOOLS:
        assert "mcp:project" in TOOL_SCOPES[name]


# ── tool_modes.py registration ──────────────────────────────────

def test_chatgpt_mode_includes_preview_tools():
    """chatgpt mode must include preview/verify tools."""
    import examples.mcp_server.tool_modes as tm
    importlib.reload(tm)
    chatgpt = tm.TOOL_NAMES_BY_MODE["chatgpt"]
    for name in ALL_PREVIEW_TOOLS:
        assert name in chatgpt, f"{name!r} missing from chatgpt mode"


def test_chatgpt_mode_excludes_write_tools():
    """chatgpt mode must NOT include write tools (read-only safe mode)."""
    import examples.mcp_server.tool_modes as tm
    importlib.reload(tm)
    for name in ALL_WRITE_TOOLS:
        assert not tm.should_register_tool(name, "chatgpt"), (
            f"{name!r} should not be available in chatgpt"
        )


def test_standard_mode_includes_all_workspace():
    """standard mode must include all 7 workspace tools."""
    import examples.mcp_server.tool_modes as tm
    importlib.reload(tm)
    for name in ALL_PREVIEW_TOOLS + ALL_WRITE_TOOLS:
        assert tm.should_register_tool(name, "standard"), f"{name!r} missing from standard"


def test_full_mode_includes_all_workspace():
    """full mode must include all 7 workspace tools."""
    import examples.mcp_server.tool_modes as tm
    importlib.reload(tm)
    for name in ALL_PREVIEW_TOOLS + ALL_WRITE_TOOLS:
        assert tm.should_register_tool(name, "full"), f"{name!r} missing from full"


def test_minimal_mode_excludes_all_workspace():
    """minimal mode must NOT include any workspace tools."""
    import examples.mcp_server.tool_modes as tm
    importlib.reload(tm)
    for name in ALL_PREVIEW_TOOLS + ALL_WRITE_TOOLS:
        assert not tm.should_register_tool(name, "minimal"), f"{name!r} should not be in minimal"


# ── safe=false (default) ────────────────────────────────────────

def test_write_no_receipt_by_default():
    """workspace_file_write without safe returns no receipt."""
    mock_result = {
        "project_id": "p",
        "path": "f.txt",
        "size": 3,
        "encoding": "utf-8",
    }
    with patch(
        "examples.mcp_server.server._get_workspace_registry"
    ) as mock_reg, patch(
        "app.workspace.edit.project_file_write",
        return_value=mock_result,
    ) as mock_fn:
        mock_reg.return_value = MagicMock()
        from examples.mcp_server.server import gateway_workspace_file_write

        result = gateway_workspace_file_write(
            project_id="p",
            relative_path="f.txt",
            content="abc",
        )
    mock_fn.assert_called_once()
    # safe=False by default → no receipt kwarg passed (default in core)
    call_kwargs = mock_fn.call_args
    assert call_kwargs[1].get("safe", False) is False or "safe" not in call_kwargs[1]
    assert "receipt" not in result["result"]


def test_edit_no_receipt_by_default():
    """workspace_file_edit without safe returns no receipt."""
    mock_result = {
        "project_id": "p",
        "path": "f.txt",
        "size": 3,
        "replaced": True,
    }
    with patch(
        "examples.mcp_server.server._get_workspace_registry"
    ) as mock_reg, patch(
        "app.workspace.edit.project_file_edit",
        return_value=mock_result,
    ) as mock_fn:
        mock_reg.return_value = MagicMock()
        from examples.mcp_server.server import gateway_workspace_file_edit

        result = gateway_workspace_file_edit(
            project_id="p",
            relative_path="f.txt",
            old_string="old",
            new_string="new",
        )
    mock_fn.assert_called_once()
    assert "receipt" not in result["result"]


def test_patch_no_receipt_by_default():
    """workspace_apply_patch without safe returns no receipt."""
    mock_result = {
        "project_id": "p",
        "path": "f.txt",
        "applied": True,
    }
    with patch(
        "examples.mcp_server.server._get_workspace_registry"
    ) as mock_reg, patch(
        "app.workspace.edit.project_apply_patch",
        return_value=mock_result,
    ) as mock_fn:
        mock_reg.return_value = MagicMock()
        from examples.mcp_server.server import gateway_workspace_apply_patch

        result = gateway_workspace_apply_patch(
            project_id="p",
            relative_path="f.txt",
            patch="--- a/f\n+++ b/f\n",
        )
    mock_fn.assert_called_once()
    assert "receipt" not in result["result"]


# ── safe=true with receipt ──────────────────────────────────────

def test_write_safe_true_passes_safe():
    """workspace_file_write with safe=True passes safe to core function."""
    mock_result = {
        "project_id": "p",
        "path": "f.txt",
        "size": 3,
        "encoding": "utf-8",
        "receipt": {"id": "rcpt_abc123", "operation": "write"},
    }
    with patch(
        "examples.mcp_server.server._get_workspace_registry"
    ) as mock_reg, patch(
        "app.workspace.edit.project_file_write",
        return_value=mock_result,
    ) as mock_fn:
        mock_reg.return_value = MagicMock()
        from examples.mcp_server.server import gateway_workspace_file_write

        result = gateway_workspace_file_write(
            project_id="p",
            relative_path="f.txt",
            content="abc",
            safe=True,
        )
    mock_fn.assert_called_once()
    call_kwargs = mock_fn.call_args
    assert call_kwargs[1].get("safe") is True
    assert "receipt" in result["result"]


def test_edit_safe_true_passes_safe():
    """workspace_file_edit with safe=True passes safe to core function."""
    mock_result = {
        "project_id": "p",
        "path": "f.txt",
        "replaced": True,
        "receipt": {"id": "rcpt_def456", "operation": "edit"},
    }
    with patch(
        "examples.mcp_server.server._get_workspace_registry"
    ) as mock_reg, patch(
        "app.workspace.edit.project_file_edit",
        return_value=mock_result,
    ) as mock_fn:
        mock_reg.return_value = MagicMock()
        from examples.mcp_server.server import gateway_workspace_file_edit

        result = gateway_workspace_file_edit(
            project_id="p",
            relative_path="f.txt",
            old_string="old",
            new_string="new",
            safe=True,
        )
    mock_fn.assert_called_once()
    call_kwargs = mock_fn.call_args
    assert call_kwargs[1].get("safe") is True
    assert "receipt" in result["result"]


def test_patch_safe_true_passes_safe():
    """workspace_apply_patch with safe=True passes safe to core function."""
    mock_result = {
        "project_id": "p",
        "path": "f.txt",
        "applied": True,
        "receipt": {"id": "rcpt_ghi789", "operation": "patch"},
    }
    with patch(
        "examples.mcp_server.server._get_workspace_registry"
    ) as mock_reg, patch(
        "app.workspace.edit.project_apply_patch",
        return_value=mock_result,
    ) as mock_fn:
        mock_reg.return_value = MagicMock()
        from examples.mcp_server.server import gateway_workspace_apply_patch

        result = gateway_workspace_apply_patch(
            project_id="p",
            relative_path="f.txt",
            patch="--- a/f\n+++ b/f\n",
            safe=True,
        )
    mock_fn.assert_called_once()
    call_kwargs = mock_fn.call_args
    assert call_kwargs[1].get("safe") is True
    assert "receipt" in result["result"]


# ── Preview tools do not mutate ─────────────────────────────────

def test_preview_write_no_disk_mutation():
    """workspace_preview_write calls preview function, not edit function."""
    mock_result = {
        "project_id": "p",
        "path": "f.txt",
        "changed": True,
        "diff": "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-old\n+new",
        "before_hash": "sha256:aaa",
        "after_hash": "sha256:bbb",
    }
    with patch(
        "examples.mcp_server.server._get_workspace_registry"
    ) as mock_reg, patch(
        "app.workspace.preview.project_file_preview_write",
        return_value=mock_result,
    ) as mock_preview, patch(
        "app.workspace.edit.project_file_write",
    ) as mock_edit:
        mock_reg.return_value = MagicMock()
        from examples.mcp_server.server import gateway_workspace_preview_write

        result = gateway_workspace_preview_write(
            project_id="p",
            relative_path="f.txt",
            content="new",
        )
    mock_preview.assert_called_once()
    mock_edit.assert_not_called()
    assert result["result"]["changed"] is True
    assert "diff" in result["result"]
    assert "before_hash" in result["result"]
    assert "after_hash" in result["result"]


def test_preview_edit_no_disk_mutation():
    """workspace_preview_edit calls preview function, not edit function."""
    mock_result = {
        "project_id": "p",
        "path": "f.txt",
        "changed": True,
        "replaced": True,
        "diff": "--- a/f.txt\n+++ b/f.txt",
    }
    with patch(
        "examples.mcp_server.server._get_workspace_registry"
    ) as mock_reg, patch(
        "app.workspace.preview.project_file_preview_edit",
        return_value=mock_result,
    ) as mock_preview, patch(
        "app.workspace.edit.project_file_edit",
    ) as mock_edit:
        mock_reg.return_value = MagicMock()
        from examples.mcp_server.server import gateway_workspace_preview_edit

        result = gateway_workspace_preview_edit(
            project_id="p",
            relative_path="f.txt",
            old_string="old",
            new_string="new",
        )
    mock_preview.assert_called_once()
    mock_edit.assert_not_called()
    assert result["result"]["changed"] is True
    assert result["result"]["replaced"] is True


def test_preview_patch_no_disk_mutation():
    """workspace_preview_patch calls preview function, not edit function."""
    mock_result = {
        "project_id": "p",
        "path": "f.txt",
        "changed": True,
        "applied": True,
        "diff": "--- a/f.txt\n+++ b/f.txt",
    }
    with patch(
        "examples.mcp_server.server._get_workspace_registry"
    ) as mock_reg, patch(
        "app.workspace.preview.project_file_preview_patch",
        return_value=mock_result,
    ) as mock_preview, patch(
        "app.workspace.edit.project_apply_patch",
    ) as mock_edit:
        mock_reg.return_value = MagicMock()
        from examples.mcp_server.server import gateway_workspace_preview_patch

        result = gateway_workspace_preview_patch(
            project_id="p",
            relative_path="f.txt",
            patch="--- a/f\n+++ b/f\n",
        )
    mock_preview.assert_called_once()
    mock_edit.assert_not_called()
    assert result["result"]["applied"] is True


# ── Verify returns matches/current_hash/file_exists ─────────────

def test_verify_match():
    """workspace_verify returns matches=True when hash matches."""
    mock_result = {
        "project_id": "p",
        "path": "f.txt",
        "matches": True,
        "current_hash": "sha256:abc123",
        "file_exists": True,
    }
    with patch(
        "examples.mcp_server.server._get_workspace_registry"
    ) as mock_reg, patch(
        "app.workspace.preview.project_file_verify",
        return_value=mock_result,
    ):
        mock_reg.return_value = MagicMock()
        from examples.mcp_server.server import gateway_workspace_verify

        result = gateway_workspace_verify(
            project_id="p",
            relative_path="f.txt",
            expected_hash="sha256:abc123",
        )
    assert result["result"]["matches"] is True
    assert result["result"]["current_hash"] == "sha256:abc123"
    assert result["result"]["file_exists"] is True


def test_verify_mismatch():
    """workspace_verify returns matches=False when hash differs."""
    mock_result = {
        "project_id": "p",
        "path": "f.txt",
        "matches": False,
        "current_hash": "sha256:xyz789",
        "file_exists": True,
    }
    with patch(
        "examples.mcp_server.server._get_workspace_registry"
    ) as mock_reg, patch(
        "app.workspace.preview.project_file_verify",
        return_value=mock_result,
    ):
        mock_reg.return_value = MagicMock()
        from examples.mcp_server.server import gateway_workspace_verify

        result = gateway_workspace_verify(
            project_id="p",
            relative_path="f.txt",
            expected_hash="sha256:abc123",
        )
    assert result["result"]["matches"] is False
    assert result["result"]["current_hash"] == "sha256:xyz789"


def test_verify_file_not_found():
    """workspace_verify returns file_exists=False for missing file."""
    mock_result = {
        "project_id": "p",
        "path": "missing.txt",
        "matches": False,
        "current_hash": None,
        "file_exists": False,
    }
    with patch(
        "examples.mcp_server.server._get_workspace_registry"
    ) as mock_reg, patch(
        "app.workspace.preview.project_file_verify",
        return_value=mock_result,
    ):
        mock_reg.return_value = MagicMock()
        from examples.mcp_server.server import gateway_workspace_verify

        result = gateway_workspace_verify(
            project_id="p",
            relative_path="missing.txt",
            expected_hash="sha256:abc",
        )
    assert result["result"]["matches"] is False
    assert result["result"]["current_hash"] is None
    assert result["result"]["file_exists"] is False


# ── Error handling ──────────────────────────────────────────────

def test_preview_write_error_returns_tool_error():
    """workspace_preview_write returns tool_error on exception."""
    with patch(
        "examples.mcp_server.server._get_workspace_registry"
    ) as mock_reg, patch(
        "app.workspace.preview.project_file_preview_write",
        side_effect=RuntimeError("permission denied"),
    ):
        mock_reg.return_value = MagicMock()
        from examples.mcp_server.server import gateway_workspace_preview_write

        result = gateway_workspace_preview_write(
            project_id="p",
            relative_path="f.txt",
            content="x",
        )
    assert result["error"]["code"] == "TOOL_EXECUTION_FAILED"
    assert "permission denied" in result["error"]["message"]


def test_verify_error_returns_tool_error():
    """workspace_verify returns tool_error on exception."""
    with patch(
        "examples.mcp_server.server._get_workspace_registry"
    ) as mock_reg, patch(
        "app.workspace.preview.project_file_verify",
        side_effect=ValueError("bad hash format"),
    ):
        mock_reg.return_value = MagicMock()
        from examples.mcp_server.server import gateway_workspace_verify

        result = gateway_workspace_verify(
            project_id="p",
            relative_path="f.txt",
            expected_hash="not-a-hash",
        )
    assert result["error"]["code"] == "TOOL_EXECUTION_FAILED"
    assert "bad hash format" in result["error"]["message"]


def test_write_safe_error_returns_tool_error():
    """workspace_file_write with safe=True returns tool_error on exception."""
    with patch(
        "examples.mcp_server.server._get_workspace_registry"
    ) as mock_reg, patch(
        "app.workspace.edit.project_file_write",
        side_effect=OSError("disk full"),
    ):
        mock_reg.return_value = MagicMock()
        from examples.mcp_server.server import gateway_workspace_file_write

        result = gateway_workspace_file_write(
            project_id="p",
            relative_path="f.txt",
            content="x",
            safe=True,
        )
    assert result["error"]["code"] == "TOOL_EXECUTION_FAILED"
    assert "disk full" in result["error"]["message"]
