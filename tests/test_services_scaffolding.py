"""Tests for app.services.scaffolding — command construction safety."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.services.scaffolding import scaffold_python_class


@pytest.mark.asyncio
async def test_module_path_with_single_quote_does_not_break_out_of_quoting():
    """Regression: mkdir -p '{module_dir}' single-quoted module_dir directly
    into the shell command with no escaping. ScaffoldRequest.module_path has
    no pattern restriction (unlike class_name) — a module_path containing a
    single quote broke out of the quoting and injected arbitrary shell
    commands into the mkdir -p invocation.

    Proven empirically: build the exact command string the service would
    hand to the SSH manager, then actually run it through a real shell in a
    scratch directory and confirm the injected command's side effect
    (creating a marker file) never happened.
    """
    manager = AsyncMock()
    manager.execute = AsyncMock(return_value={"stdout": "", "stderr": "", "exit_code": 0})
    file_editor = AsyncMock()
    file_editor.write_file = AsyncMock(return_value={"success": True})

    with tempfile.TemporaryDirectory() as tmpdir:
        marker = Path(tmpdir) / "pwned"
        malicious_path = f"foo'; touch {marker}; echo '"

        await scaffold_python_class(
            manager,
            file_editor,
            session_id="s1",
            module_path=malicious_path,
            class_name="Foo",
            methods=[],
            include_test=False,
        )

        mkdir_command = manager.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", mkdir_command], cwd=tmpdir, check=False)

        assert not marker.exists(), (
            f"module_path broke out of shell quoting and ran an injected command: "
            f"{mkdir_command!r}"
        )


@pytest.mark.asyncio
async def test_normal_module_path_still_works():
    manager = AsyncMock()
    manager.execute = AsyncMock(return_value={"stdout": "", "stderr": "", "exit_code": 0})
    file_editor = AsyncMock()
    file_editor.write_file = AsyncMock(return_value={"success": True})

    result = await scaffold_python_class(
        manager,
        file_editor,
        session_id="s1",
        module_path="app/services",
        class_name="Foo",
        methods=["bar"],
        include_test=True,
    )

    mkdir_command = manager.execute.call_args_list[0].args[1]
    assert "app/services" in mkdir_command
    assert "app/services/foo.py" in result.files_created
    assert "app/services/test_foo.py" in result.files_created
