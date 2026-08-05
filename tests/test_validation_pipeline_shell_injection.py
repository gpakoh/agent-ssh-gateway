"""Regression tests for ValidationPipeline shell-injection bugs.

validation_pipeline.py built commands like "cd {path} && ...",
"test -f {check_path}/{file} ...", and "test -d {project_root}/{subdir} ..."
via naive f-string interpolation with ZERO shell escaping around path.
path is attacker-controllable via POST /api/validate -> ContextManager
.validate_context() -> ctx.path (a plain, unconstrained string from
ContextCreateRequest.path; master-key gated, same threat model as other
fixes this session).

Each probe drives the real ValidationPipeline method (a fake
self._ssh.execute captures the command) and executes it for real via
`sh -c`, checking for a marker file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.validation_pipeline import ValidationPipeline


def _payload(marker: Path) -> str:
    return f"$(touch {marker}_a)'; touch {marker}_b; echo '"


def _marker_hit(marker: Path) -> bool:
    return Path(f"{marker}_a").exists() or Path(f"{marker}_b").exists()


def _make_ssh(stdout: str = "", exit_code: int = 0) -> AsyncMock:
    ssh = AsyncMock()
    ssh.execute = AsyncMock(return_value={"stdout": stdout, "stderr": "", "exit_code": exit_code})
    return ssh


class TestValidationPipelineShellInjection:
    @pytest.mark.asyncio
    async def test_quick_check_path_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = _make_ssh(exit_code=0)
        vp = ValidationPipeline(ssh)

        await vp.quick_check("s1", _payload(marker))

        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"quick_check()'s command let path break out of shell quoting: {command!r}"
        )

    @pytest.mark.asyncio
    async def test_run_mypy_path_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = _make_ssh()
        vp = ValidationPipeline(ssh)

        await vp._run_mypy("s1", _payload(marker))

        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"_run_mypy()'s command let path break out of shell quoting: {command!r}"
        )

    @pytest.mark.asyncio
    async def test_run_pytest_path_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = _make_ssh()
        vp = ValidationPipeline(ssh)

        await vp._run_pytest("s1", _payload(marker))

        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"_run_pytest()'s command let test_path break out of shell quoting: {command!r}"
        )

    @pytest.mark.asyncio
    async def test_detect_project_path_is_escaped(self, tmp_path):
        marker = tmp_path / "pwned"
        ssh = _make_ssh(stdout="NOT_FOUND")
        vp = ValidationPipeline(ssh)

        await vp._detect_project("s1", _payload(marker))

        command = ssh.execute.call_args_list[0].args[1]
        subprocess.run(["sh", "-c", command], check=False)
        assert not _marker_hit(marker), (
            f"_detect_project()'s existence check let path break out of shell quoting: {command!r}"
        )
