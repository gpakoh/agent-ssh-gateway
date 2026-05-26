"""Mock-based tests for BatchOperationsManager."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from app.batch_operations import BatchOperationsManager


@pytest.fixture
def batch_manager():
    ssh = AsyncMock()
    editor = AsyncMock()
    ctx = AsyncMock()
    ctx.get_context.return_value = MagicMock(path="/project")
    ctx.commit_changes.return_value = {"success": True, "hash": "abc123"}
    return BatchOperationsManager(ssh, editor, ctx)


@pytest.mark.asyncio
async def test_batch_read(batch_manager):
    batch_manager._file_editor.read_file.return_value = "file content"
    result = await batch_manager.execute_batch(
        "sid", "ctx", [{"type": "read", "path": "app.py"}]
    )
    assert result.overall_success is True
    assert len(result.operations) == 1
    assert result.operations[0].success is True


@pytest.mark.asyncio
async def test_batch_edit(batch_manager):
    batch_manager._file_editor.edit_file.return_value = {"success": True, "operations_applied": 2}
    result = await batch_manager.execute_batch(
        "sid", "ctx", [{"type": "edit", "path": "app.py", "operations": [{"type": "replace"}]}],
        auto_commit=True, commit_message="test",
    )
    assert result.overall_success is True
    assert result.git_commit == "abc123"


@pytest.mark.asyncio
async def test_batch_create_and_delete(batch_manager):
    batch_manager._ssh.execute.return_value = {"exit_code": 0, "stderr": ""}
    result = await batch_manager.execute_batch(
        "sid", "ctx", [
            {"type": "create", "path": "new.py", "content": "print(1)"},
            {"type": "delete", "path": "old.py"},
        ]
    )
    assert result.overall_success is True
    assert len(result.operations) == 2


@pytest.mark.asyncio
async def test_batch_execute_command(batch_manager):
    batch_manager._ssh.execute.return_value = {
        "exit_code": 0, "stdout": "output", "stderr": "", "duration": 1.0,
    }
    result = await batch_manager.execute_batch(
        "sid", "ctx", [{"type": "execute", "command": "ls -la"}]
    )
    assert result.operations[0].success is True
    assert "output" in result.operations[0].output


@pytest.mark.asyncio
async def test_batch_stop_on_error(batch_manager):
    batch_manager._file_editor.read_file.side_effect = Exception("disk full")
    result = await batch_manager.execute_batch(
        "sid", "ctx", [
            {"type": "read", "path": "a.py"},
            {"type": "read", "path": "b.py"},
        ]
    )
    assert result.overall_success is False
    assert len(result.operations) == 1
