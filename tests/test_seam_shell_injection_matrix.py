"""Seam test: every shell-command-string-building function found this
session, run against the same single-quote-breakout attack, proven by
actually executing the constructed string through a real shell.

Context: scaffolding.py's `mkdir -p '{module_dir}'`, context_editing.py's
`git ls-files/show '{path}'`, webhook_manager.py's `cd {target_path}`, and
agent_tools.py's `cat {task_dir}/{task_id}/task.json` all interpolated a
caller-controlled string into a shell command — some with no escaping at
all, some inconsistently (a sibling function in the same file used
shlex.quote() correctly while the one next to it didn't). Each was found,
fixed, and regression-tested independently in a different audit round;
this file exists so the next shell-command-building function added
anywhere in this codebase gets checked against the same attack
automatically.

Each probe returns the constructed shell command string (to be executed
for real and checked for the injected marker file) or None if the
function rejected the malicious input outright before ever building a
command — both are safe outcomes. A probe that returns a string which,
when executed, creates the marker file is a real command-injection bug.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Protocol
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER_DIR = REPO_ROOT / "examples" / "mcp_server"


class ShellInjectionProbe(Protocol):
    def __call__(self, marker: Path) -> str | None: ...


def _payload(marker: Path) -> str:
    """A value that triggers command execution under EITHER unsafe pattern
    found this session:

    - Interpolated with no quoting at all (webhook_manager's old
      `f"cd {target_path} && ..."`): the leading `$(...)` is an unquoted
      command substitution and runs immediately.
    - Interpolated inside a hand-written single-quote wrapper
      (scaffolding's old `f"mkdir -p '{module_dir}'"`): the embedded `'`
      closes that wrapper early, and `; touch ...` runs as a new command.

    Either shape leaves *marker* on disk; a value processed through
    shlex.quote() defuses both, since the whole payload — `$(...)`, the
    stray `'`, and the `;` — ends up inside one properly escaped token.
    """
    return f"$(touch {marker}_a)'; touch {marker}_b; echo '"


def _marker_hit(marker: Path) -> bool:
    return Path(f"{marker}_a").exists() or Path(f"{marker}_b").exists()


# ── app/services/scaffolding.py ─────────────────────────────────────────


def probe_scaffold_python_class(marker: Path) -> str | None:
    import asyncio

    from app.services.scaffolding import scaffold_python_class

    manager = AsyncMock()
    manager.execute = AsyncMock(return_value={"stdout": "", "stderr": "", "exit_code": 0})
    file_editor = AsyncMock()
    file_editor.write_file = AsyncMock(return_value={"success": True})

    asyncio.run(
        scaffold_python_class(
            manager,
            file_editor,
            session_id="s1",
            module_path=_payload(marker),
            class_name="Foo",
            methods=[],
            include_test=False,
        )
    )
    return manager.execute.call_args_list[0].args[1]


# ── app/services/context_editing.py ─────────────────────────────────────


def probe_edit_file_with_context_git_diff(marker: Path) -> str | None:
    import asyncio

    from app.git_manager import GitStatus
    from app.services.context_editing import edit_file_with_context

    ctx = MagicMock()
    ctx.session_id = "s1"
    ctx.path = "/tmp/proj"
    ctx.context_id = "c1"
    ctx.auto_commit = False
    ctx.auto_validate = False
    ctx.git_info = MagicMock()
    ctx.git_info.status = GitStatus.CLEAN

    context_manager = AsyncMock()
    context_manager.create_backup = AsyncMock()
    context_manager.record_edit = AsyncMock()
    context_manager.add_file_to_context = AsyncMock()

    file_editor = AsyncMock()
    file_editor.edit_file = AsyncMock(
        return_value={"success": True, "operations_applied": 1, "changed": True}
    )

    manager = AsyncMock()
    manager.execute = AsyncMock(return_value={"stdout": "NOT_TRACKED", "stderr": "", "exit_code": 0})

    asyncio.run(
        edit_file_with_context(
            ctx,
            context_manager,
            file_editor,
            manager,
            path=_payload(marker),
            operations=[{"type": "replace", "old": "a", "new": "b"}],
        )
    )
    if not manager.execute.call_args_list:
        return None
    return manager.execute.call_args_list[0].args[1]


# ── app/webhook_manager.py ──────────────────────────────────────────────


def probe_webhook_execute_deploy(marker: Path) -> str | None:
    import asyncio
    import inspect

    from app.webhook_manager import WebhookManager

    job_manager = AsyncMock()
    job_manager.create_job = AsyncMock(return_value="job-1")
    wm = WebhookManager(ssh_manager=AsyncMock(), job_manager=job_manager)
    kwargs = dict(
        name="deploy",
        webhook_type="generic",
        target_path=_payload(marker),
        deploy_command="true",
        context_id="ctx1",
    )
    if "secret" in inspect.signature(wm.add_webhook).parameters:
        kwargs["secret"] = "s"  # required by older pre-removal signatures
    config = wm.add_webhook(**kwargs)
    asyncio.run(wm.execute_deploy(session_id="s1", webhook_id=config.id))
    return job_manager.create_job.call_args.kwargs["command"]


# ── examples/mcp_server/agent_tools.py ──────────────────────────────────


def probe_agent_tools_read_task_json(marker: Path) -> str | None:
    sys.path.insert(0, str(MCP_SERVER_DIR))
    from agent_tools import _read_task_json

    calls: list[tuple[str, str]] = []

    def fake_run_cmd(project: str, command: str) -> dict:
        calls.append((project, command))
        return {"stdout": "{}", "stderr": "", "exit_code": 0}

    try:
        _read_task_json(fake_run_cmd, project="p", task_id=_payload(marker))
    except Exception:
        pass
    return calls[0][1] if calls else None


# ── examples/mcp_server/agent_tasks.py ──────────────────────────────────


def probe_agent_tasks_read_agent_task_file(marker: Path) -> str | None:
    sys.path.insert(0, str(MCP_SERVER_DIR))
    from agent_tasks import read_agent_task_file

    calls: list[tuple[str, str]] = []

    def fake_run_cmd(project: str, command: str) -> dict:
        calls.append((project, command))
        return {"stdout": "", "stderr": "", "exit_code": 0}

    try:
        read_agent_task_file(
            fake_run_cmd,
            project="p",
            task_id="a12345678901",
            filename=_payload(marker),
        )
    except Exception:
        pass
    return calls[0][1] if calls else None


PROBES: list[tuple[str, ShellInjectionProbe]] = [
    ("scaffolding.scaffold_python_class(module_path)", probe_scaffold_python_class),
    ("context_editing.edit_file_with_context(path)", probe_edit_file_with_context_git_diff),
    ("webhook_manager.execute_deploy(target_path)", probe_webhook_execute_deploy),
    ("agent_tools._read_task_json(task_id)", probe_agent_tools_read_task_json),
    ("agent_tasks.read_agent_task_file(filename)", probe_agent_tasks_read_agent_task_file),
]


def test_baseline_confirms_attack_vector_is_real(tmp_path):
    marker = tmp_path / "pwned"

    # Shape 1: hand-written single-quote wrapper (scaffolding's old
    # f"mkdir -p '{module_dir}'") — the embedded `'` breaks out.
    quoted = f"echo '{_payload(marker)}'"
    subprocess.run(["sh", "-c", quoted], check=False)
    assert _marker_hit(marker), "quote-breakout baseline must demonstrate the injection with no protection"

    marker2 = tmp_path / "pwned2"
    # Shape 2: no quoting at all (webhook_manager's old
    # f"cd {target_path} && ...") — the unquoted $(...) runs immediately.
    unquoted = f"echo {_payload(marker2)}"
    subprocess.run(["sh", "-c", unquoted], check=False)
    assert _marker_hit(marker2), "unquoted baseline must demonstrate the injection with no protection"


@pytest.mark.parametrize("name,probe", PROBES, ids=[n for n, _ in PROBES])
def test_no_shell_injection_in_any_seam(name, probe):
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "pwned"
        command = probe(marker)
        if command is None:
            return  # rejected outright before building a command — safe
        subprocess.run(["sh", "-c", command], check=False, cwd=tmp)
        assert not _marker_hit(marker), (
            f"{name} let a malicious value break out of shell quoting: {command!r}"
        )
