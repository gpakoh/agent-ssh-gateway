"""Mock-based tests for FileEditor and path validation."""
import pytest
from unittest.mock import AsyncMock
from app.file_editor import FileEditor, ExecutionError
from app.security import validate_path


@pytest.fixture
def mock_ssh():
    return AsyncMock()


@pytest.fixture
def editor(mock_ssh):
    return FileEditor(ssh_manager=mock_ssh)


@pytest.mark.asyncio
async def test_read_file_success(editor, mock_ssh):
    mock_ssh.execute.side_effect = [
        {"exit_code": 0, "stdout": "FILE", "stderr": ""},
        {"exit_code": 0, "stdout": "hello world\n", "stderr": ""},
    ]
    content = await editor.read_file("sid-123", "/tmp/test.txt")
    assert content == "hello world\n"
    assert mock_ssh.execute.call_count == 2


@pytest.mark.asyncio
async def test_read_file_directory_error(editor, mock_ssh):
    mock_ssh.execute.return_value = {"exit_code": 0, "stdout": "DIR", "stderr": ""}
    with pytest.raises(ExecutionError, match="directory"):
        await editor.read_file("sid-123", "/tmp")


@pytest.mark.asyncio
async def test_write_file_success(editor, mock_ssh):
    mock_ssh.execute.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
    await editor.write_file("sid-123", "/tmp/new.txt", "content")
    assert mock_ssh.execute.call_count >= 2


@pytest.mark.asyncio
async def test_edit_file_replace(editor, mock_ssh):
    mock_ssh.execute.side_effect = [
        {"exit_code": 0, "stdout": "FILE", "stderr": ""},
        {"exit_code": 0, "stdout": "old text here", "stderr": ""},
        {"exit_code": 0, "stdout": "", "stderr": ""},
        {"exit_code": 0, "stdout": "", "stderr": ""},
    ]
    result = await editor.edit_file("sid-123", "/tmp/file.txt", [
        {"type": "replace", "old": "old text", "new": "new text"},
    ])
    assert result["success"] is True
    assert result["operations_applied"] == 1
    assert result["changed"] is True


@pytest.mark.asyncio
async def test_edit_file_create(editor, mock_ssh):
    mock_ssh.execute.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
    result = await editor.edit_file("sid-123", "/tmp/new.txt", [
        {"type": "create", "text": "created content"},
    ])
    assert result["success"] is True
    assert result["changed"] is True


@pytest.mark.asyncio
async def test_validate_path_blocks_traversal():
    with pytest.raises(ValueError, match="traversal"):
        validate_path("../../../etc/passwd")


def test_validate_path_forbidden():
    with pytest.raises(ValueError):
        validate_path("/etc/passwd")


def test_validate_path_allowed():
    result = validate_path("/home/user/test.txt")
    assert result == "/home/user/test.txt"


@pytest.mark.asyncio
async def test_diff_files_success(editor, mock_ssh):
    mock_ssh.execute.return_value = {"exit_code": 0, "stdout": "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new", "stderr": ""}
    result = await editor.diff_files("sid-123", "/tmp/a.txt", "/tmp/b.txt")
    assert "old" in result
    assert "new" in result


@pytest.mark.asyncio
async def test_apply_patch_success(editor, mock_ssh):
    mock_ssh.execute.return_value = {"exit_code": 0, "stdout": "patching succeeded", "stderr": ""}
    result = await editor.apply_patch("sid-123", "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new")
    assert result["success"] is True


@pytest.mark.asyncio
async def test_read_file_execution_error(editor, mock_ssh):
    mock_ssh.execute.side_effect = [
        {"exit_code": 0, "stdout": "FILE", "stderr": ""},
        {"exit_code": 1, "stdout": "", "stderr": "Permission denied"},
    ]
    with pytest.raises(ExecutionError, match="Permission denied"):
        await editor.read_file("sid-123", "/var/log/alternatives.log")


@pytest.mark.asyncio
async def test_write_file_mkdir_failure(editor, mock_ssh):
    mock_ssh.execute.return_value = {"exit_code": 1, "stdout": "", "stderr": "cannot create directory"}
    with pytest.raises(ExecutionError, match="cannot create directory"):
        await editor.write_file("sid-123", "/readonly/new.txt", "content")


@pytest.mark.asyncio
async def test_edit_file_operation_error(editor, mock_ssh):
    mock_ssh.execute.side_effect = [
        {"exit_code": 0, "stdout": "FILE", "stderr": ""},
        {"exit_code": 0, "stdout": "some content", "stderr": ""},
    ]
    with pytest.raises(ExecutionError):
        await editor.edit_file("sid-123", "/tmp/f.txt", [
            {"type": "insert_after", "after": "not found"},
        ])
