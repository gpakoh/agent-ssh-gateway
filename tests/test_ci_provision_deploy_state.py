"""Release-gated preparation of the dedicated SSH Gateway LKG volume."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci-provision-deploy-state-volume.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE = "e3dbf1f89af5361c7c0281004db41461007ad5a2"


def run_helper(tmp_path: Path, release: str = RELEASE) -> subprocess.CompletedProcess[str]:
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = image ]; then\n"
        "  echo registry.test/gpakoh/mcp-server@sha256:" + "a" * 64 + "\n"
        "elif [ \"$1\" = run ]; then\n"
        "  printf '%s\\n' \"$@\" > \"$DOCKER_LOG\"\n"
        "else\n"
        "  exit 7\n"
        "fi\n",
        encoding="utf-8",
    )
    docker.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "REGISTRY": "registry.test",
        "RELEASE_SHA": release,
        "DEPLOY_STATE_DIR": "/deploy/web-ssh-gateway-state",
        "DOCKER_LOG": str(tmp_path / "docker-args"),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=ROOT, env=env,
        capture_output=True, text=True, check=False, timeout=15,
    )


def test_ci_prepares_root_volume_before_trusted_receipt_bootstrap() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    before = workflow.index("bash scripts/ci-provision-deploy-state-volume.sh")
    bootstrap = workflow.index("bash scripts/ci-bootstrap-deploy-state.sh")
    deploy = workflow.index("- name: Deploy, smoke-test, roll back on failure")
    assert before < bootstrap < deploy


def test_only_named_volume_and_digest_pinned_helper_are_used(tmp_path: Path) -> None:
    outcome = run_helper(tmp_path)
    assert outcome.returncode == 0, outcome.stderr
    args = (tmp_path / "docker-args").read_text(encoding="utf-8")
    assert "--user\n0:0\n" in args
    assert "-v\nssh-gateway-deploy-state:/trusted-state\n" in args
    assert "--entrypoint\n/bin/sh\n" in args
    assert "registry.test/gpakoh/mcp-server@sha256:" + "a" * 64 in args
    assert "--privileged" not in args
    assert 'if [ -e "$receipt" ] || [ -L "$receipt" ]' in args
    assert 'if [ -n "$(ls -A "$root")" ]' in args
    assert 'chown "$uid:$gid" "$root"' in args
    assert "chmod 0700" in args


def test_invalid_release_is_rejected_without_docker_mutation(tmp_path: Path) -> None:
    outcome = run_helper(tmp_path, release="latest")
    assert outcome.returncode != 0
    assert "pinned Git commit" in outcome.stderr
    assert not (tmp_path / "docker-args").exists()
