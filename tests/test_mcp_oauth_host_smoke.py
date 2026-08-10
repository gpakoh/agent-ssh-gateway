"""Host-smoke test for the live mcp-oauth container: full black-box
DCR/OAuth flow -> real tools/call git_status (audit #6 follow-up).

Runs on the host-smoke runner (same host as the prod docker stack):
scripts/mcp_oauth_black_box_smoke.py (checked out from the repo) talks
to 127.0.0.1:8788 -- the mcp-oauth public proxy -- and drives the whole
MCP->Gateway->SSH->harmless command->exact result chain against the
REAL deployed containers: OAuth token via /register -> /authorize ->
/oauth/consent -> /token, then git_status (scope mcp:project, the only
safe-profile tool that really executes an SSH command:
/api/ssh/execute-argv -> sshd -> git status --short).

The consent password (MCP_AUTHORIZE_PASSWORD) lives only inside the
container, so it is fetched via `docker exec mcp-oauth printenv`; the
checkout's own python3 runs the script against the published proxy.

Skipped (not failed) when docker or the mcp-oauth container is absent,
so a plain CI checkout or a host without the stack stays green.

Usage:
  python -m pytest tests/test_mcp_oauth_host_smoke.py -v
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.host_smoke

CONTAINER = os.environ.get("MCP_SMOKE_CONTAINER", "mcp-oauth")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mcp_oauth_black_box_smoke.py"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _docker_exec(*args: str) -> str:
    return subprocess.run(
        ["docker", "exec", CONTAINER, *args],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def test_mcp_oauth_black_box_smoke_full_flow():
    """Full OAuth flow + git_status must succeed against the live stack."""
    if not _docker_available():
        pytest.skip("docker CLI not available on this runner")
    password = _docker_exec("printenv", "MCP_AUTHORIZE_PASSWORD")
    if not password:
        pytest.skip(f"container {CONTAINER} not running on this host")

    env = dict(os.environ)
    env["MCP_AUTHORIZE_PASSWORD"] = password
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )
    assert result.returncode == 0, (
        f"black-box smoke failed ({result.returncode})\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "OK" in result.stdout, f"unexpected stdout: {result.stdout}"
