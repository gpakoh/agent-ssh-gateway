"""Verify that isolated required checks use their bootstrapped project environment.

Regression for #866: "uv sync --frozen --extra dev" succeeded but "pytest" was
not found by the immediately following sh -c required check.  The command
being checked must be the same executable recorded in provenance.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
from pathlib import Path

from examples.mcp_server.candidate_verifier import build_candidate_verifier_script


def _committed_repo(tmp_path: Path, *, dev_extra: bool) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Verifier Test"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True
    )
    if dev_extra:
        (repo / "pyproject.toml").write_text(
            '[project]\nname="venv-regression"\nversion="0.1.0"\n'
            '[project.optional-dependencies]\ndev=[]\n', encoding="utf-8"
        )
        (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    else:
        # A candidate-controlled directory must not become part of trusted
        # check PATH unless a project dependency bootstrap actually ran.
        probe = repo / ".venv" / "bin" / "mcp-venv-test-probe"
        probe.parent.mkdir(parents=True)
        probe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        probe.chmod(0o755)
    (repo / "file.txt").write_text("safe\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, sha


def _run_script(
    repo: Path, sha: str, checks: list[str], *, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    script = build_candidate_verifier_script(
        staging_root=repo, expected_sha=sha, required_checks=checks,
        run_id="a" * 32, verifier_image="verifier-test-image"
    ).replace("SOURCE=/candidate-src", f"SOURCE={shlex.quote(str(repo))}", 1)
    return subprocess.run(
        ["sh"], input=script, text=True, capture_output=True,
        check=False, timeout=35, env=env
    )


def _check_receipt(stdout: str) -> dict[str, object]:
    encoded = next(
        line.removeprefix("MCP_CHECK_RECEIPT_V1:")
        for line in stdout.splitlines()
        if line.startswith("MCP_CHECK_RECEIPT_V1:")
    )
    return json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))


def test_uv_synced_project_executable_runs_and_is_attested(tmp_path: Path) -> None:
    repo, sha = _committed_repo(tmp_path, dev_extra=True)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        '[ "$1" = "sync" ] && [ "$2" = "--frozen" ] && '
        '[ "$3" = "--extra" ] && [ "$4" = "dev" ] || exit 90\n'
        'mkdir -p .venv/bin || exit 91\n'
        "cat > .venv/bin/mcp-venv-test-probe <<'PROBE'\n"
        "#!/bin/sh\n"
        '[ "$1" = "--smoke" ] || exit 92\n'
        'echo project-venv-ok\n'
        "PROBE\n"
        'chmod +x .venv/bin/mcp-venv-test-probe\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    completed = _run_script(
        repo, sha, ["mcp-venv-test-probe --smoke"], env=env
    )
    assert completed.returncode == 0, (
        completed.stdout[-1500:] + completed.stderr[-1500:]
    )
    receipt = _check_receipt(completed.stdout)
    assert receipt["exit_code"] == 0
    assert receipt["stdout_tail"].strip() == "project-venv-ok"
    tool = receipt["primary_tool"]
    assert tool["kind"] == "external"
    assert tool["name"] == "mcp-venv-test-probe"
    assert tool["path"].endswith("/repo/.venv/bin/mcp-venv-test-probe")
    assert len(tool["sha256"]) == 64


def test_unbootstrapped_candidate_venv_not_added_to_check_path(
    tmp_path: Path,
) -> None:
    repo, sha = _committed_repo(tmp_path, dev_extra=False)
    completed = _run_script(
        repo, sha, ["! command -v mcp-venv-test-probe"], env=dict(os.environ)
    )
    assert completed.returncode == 0, (
        completed.stdout[-1500:] + completed.stderr[-1500:]
    )
    assert _check_receipt(completed.stdout)["exit_code"] == 0


def test_uv_bootstrap_without_real_venv_fails_before_checks(tmp_path: Path) -> None:
    repo, sha = _committed_repo(tmp_path, dev_extra=True)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "mkdir -p .venv\n"
        "ln -s .. .venv/bin\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    completed = _run_script(repo, sha, ["true"], env=env)
    assert completed.returncode == 82
    assert completed.stdout.strip() == "MCP_VERIFY_ENV_STAGE=venv_missing"
