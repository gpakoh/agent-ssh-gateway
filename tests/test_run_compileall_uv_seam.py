"""host_smoke integration coverage for the real `uv`/`uvx` behavior behind
run_compileall's read-only-mount fallback.

Regression context: the fallback used to run `uvx --from compileall
python -m compileall`, assuming compileall was uvx-installable like
ruff/mypy/pytest. It isn't — it's a stdlib module — so uvx always failed
with "compileall was not found in the package registry", replacing the
actual syntax error with a useless resolution error. Every unit test for
this mocked the SSH execution layer entirely (fake execute_project_script/
execute_argv methods returning whatever the test author wrote), so none of
them ever ran a real `uv`/`uvx` process and could not have caught what the
real command actually prints. These tests shell out to the real binaries.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

UV_BIN = shutil.which("uv")
UVX_BIN = shutil.which("uvx")

pytestmark = [
    pytest.mark.host_smoke,
    pytest.mark.skipif(UV_BIN is None, reason="uv binary not available on this host"),
]


@pytest.fixture
def broken_py(tmp_path):
    path = tmp_path / "broken.py"
    path.write_text("def f(\n    this is not valid python\n")
    return path


@pytest.fixture
def clean_py(tmp_path):
    path = tmp_path / "clean.py"
    path.write_text("x = 1\n")
    return path


class TestRealUvNoProjectFallbackWorks:
    """This is the fallback run_compileall actually uses now — prove it for
    real, not through a mock that just echoes back whatever exit_code/stdout
    the test decided to hand it."""

    def test_reports_the_real_syntax_error(self, tmp_path, broken_py):
        result = subprocess.run(
            [UV_BIN, "run", "--no-project", "python3", "-m", "compileall", "--", str(broken_py)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 1
        assert "SyntaxError" in result.stdout
        assert str(broken_py) in result.stdout or broken_py.name in result.stdout

    def test_clean_file_passes(self, tmp_path, clean_py):
        result = subprocess.run(
            [UV_BIN, "run", "--no-project", "python3", "-m", "compileall", "--", str(clean_py)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0
        assert "SyntaxError" not in result.stdout


class TestRealUvxFallbackIsGenuinelyBroken:
    """Documents *why* compileall can't use the uvx --from <tool> pattern
    that ruff/mypy/pytest's fallback uses — locks the reasoning to real `uv`
    behavior instead of a comment that can silently go stale.
    """

    @pytest.mark.skipif(UVX_BIN is None, reason="uvx binary not available on this host")
    def test_uvx_from_compileall_fails_to_resolve(self, tmp_path):
        # Exact invocation _build_uvx_argv would have produced for this tool.
        result = subprocess.run(
            [UVX_BIN, "--from", "compileall", "python", "-m", "compileall"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode != 0
        combined = (result.stdout + result.stderr).lower()
        # The exact wording is uv's own — assert on the underlying fact
        # (no such installable package), not a brittle exact phrase.
        assert "compileall" in combined
