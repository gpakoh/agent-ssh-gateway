"""One-time attested LKG bootstrap, tested without a Docker daemon."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci-bootstrap-deploy-state.sh"
SHA = "b4c6c9c6d268f8db638f22d4c9256832dddcd875"
GW = "registry.test/gpakoh/agent-ssh-gateway@sha256:138039b5da85ce0cb30c5b593b413b654d0dadd9d1712bb3bd39aa405f5ed0de"
MCP = "registry.test/gpakoh/mcp-server@sha256:a9a13014490aac48fb3f6a401061d09cc039c7ae7b355f65c551aff5aebde0ad"
SSHD = "registry.test/gpakoh/ssh-gateway-sshd@sha256:0c96682a4b8903b31bc0245d3bcbafec5750ed38be4eed52dd4966c644bc4ed8"
ACCEPTED_SHA = "891f3482b339711616af3d92c42903001cc69dee"
ACCEPTED_GW = "registry.test/gpakoh/agent-ssh-gateway@sha256:8723a10a93546e746ae122d66ab6c97f56f8ba4e68ce05a5ec75b6f8f69981ac"
ACCEPTED_MCP = "registry.test/gpakoh/mcp-server@sha256:157b3873d91d6c3ae2eeab3bf99c0fcfa78b9a6c036e16b32ee28f682729f8e0"
ACCEPTED_SSHD = "registry.test/gpakoh/ssh-gateway-sshd@sha256:f616f78acc163eaa6745bae249dc5beba17e97237a7c67761db417b84a3386a4"


def run_bootstrap(
    tmp_path: Path, *, drift: bool = False, baseline: str = "historic",
    sha_drift: bool = False,
) -> subprocess.CompletedProcess[str]:
    docker = tmp_path / "docker"
    docker.write_text(
        """#!/bin/sh
set -eu
if [ "$1" = inspect ] && [ "$3" = '{{.Config.Image}}' ]; then
    if [ "$4" = mcp-oauth ] && [ "$IMAGE_DRIFT" = 1 ]; then
        echo wrong-image
    elif [ "$BASELINE" = accepted ]; then
        case "$4" in
            web-ssh-gateway) echo "$REGISTRY/gpakoh/agent-ssh-gateway@sha256:8723a10a93546e746ae122d66ab6c97f56f8ba4e68ce05a5ec75b6f8f69981ac" ;;
            mcp-server|mcp-oauth) echo "$REGISTRY/gpakoh/mcp-server@sha256:157b3873d91d6c3ae2eeab3bf99c0fcfa78b9a6c036e16b32ee28f682729f8e0" ;;
            ssh-gateway-sshd|ssh-gateway-agent-sshd) echo "$REGISTRY/gpakoh/ssh-gateway-sshd@sha256:f616f78acc163eaa6745bae249dc5beba17e97237a7c67761db417b84a3386a4" ;;
            *) exit 2 ;;
        esac
    elif [ "$BASELINE" = unknown ]; then
        echo "$REGISTRY/gpakoh/unattested@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    else
        case "$4" in
            web-ssh-gateway) echo "$REGISTRY/gpakoh/agent-ssh-gateway@sha256:138039b5da85ce0cb30c5b593b413b654d0dadd9d1712bb3bd39aa405f5ed0de" ;;
            mcp-server|mcp-oauth) echo "$REGISTRY/gpakoh/mcp-server@sha256:a9a13014490aac48fb3f6a401061d09cc039c7ae7b355f65c551aff5aebde0ad" ;;
            ssh-gateway-sshd|ssh-gateway-agent-sshd) echo "$REGISTRY/gpakoh/ssh-gateway-sshd@sha256:0c96682a4b8903b31bc0245d3bcbafec5750ed38be4eed52dd4966c644bc4ed8" ;;
            *) exit 2 ;;
        esac
    fi
elif [ "$1" = inspect ] && [ "$3" = '{{.State.Health.Status}}' ]; then
    echo healthy
elif [ "$1" = exec ] && [ "$3" = printenv ] && [ "$4" = BUILD_SHA ]; then
    if [ "$SHA_DRIFT" = 1 ]; then
        echo wrong-sha
    elif [ "$BASELINE" = accepted ]; then
        echo 891f3482b339711616af3d92c42903001cc69dee
    else
        echo b4c6c9c6d268f8db638f22d4c9256832dddcd875
    fi
else
    exit 2
fi
""",
        encoding="utf-8",
    )
    docker.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "REGISTRY": "registry.test",
        "DEPLOY_STATE_DIR": str(tmp_path / "state"),
        "IMAGE_DRIFT": "1" if drift else "0",
        "BASELINE": baseline,
        "SHA_DRIFT": "1" if sha_drift else "0",
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=ROOT, env=env, text=True,
        capture_output=True, check=False, timeout=15,
    )


def test_exact_release_bootstrap_writes_private_receipt_idempotently(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    state.chmod(0o755)
    first = run_bootstrap(tmp_path)
    assert first.returncode == 0, first.stderr
    receipt = state / "web-ssh-gateway-deploy.json"
    data = json.loads(receipt.read_text())
    assert data["deploy_sha"] == SHA
    assert data["gateway_image"] == GW
    assert data["mcp_server_image"] == MCP
    assert data["sshd_image"] == SSHD
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    before = receipt.read_bytes()
    second = run_bootstrap(tmp_path)
    assert second.returncode == 0, second.stderr
    assert receipt.read_bytes() == before


def test_image_drift_fails_before_touching_volume(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    state.chmod(0o755)
    failed = run_bootstrap(tmp_path, drift=True)
    assert failed.returncode != 0
    assert "attested image" in failed.stderr
    assert list(state.iterdir()) == []
    assert stat.S_IMODE(state.stat().st_mode) == 0o755


def test_untrusted_existing_receipt_is_never_repaired(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    receipt = state / "web-ssh-gateway-deploy.json"
    receipt.write_text("{}")
    receipt.chmod(0o666)
    failed = run_bootstrap(tmp_path)
    assert failed.returncode != 0
    assert "DEPLOY_STATE_UNTRUSTED" in failed.stderr
    assert receipt.read_text() == "{}"
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o666


def test_latest_attested_release_bootstrap_requires_exact_five_digests_and_sha(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    qualified = run_bootstrap(tmp_path, baseline="accepted")
    assert qualified.returncode == 0, qualified.stderr
    receipt = state / "web-ssh-gateway-deploy.json"
    data = json.loads(receipt.read_text())
    assert data["deploy_sha"] == ACCEPTED_SHA
    assert data["gateway_image"] == ACCEPTED_GW
    assert data["mcp_server_image"] == ACCEPTED_MCP
    assert data["sshd_image"] == ACCEPTED_SSHD
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600

    before = receipt.read_bytes()
    # Existing receipt remains authoritative even if the live generation
    # changes; no rebootstrap/reset through this entry point.
    repeated = run_bootstrap(tmp_path, baseline="unknown")
    assert repeated.returncode == 0, repeated.stderr
    assert receipt.read_bytes() == before


def test_attested_release_rejects_one_wrong_image_without_state_mutation(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    state.chmod(0o755)
    outcome = run_bootstrap(tmp_path, baseline="accepted", drift=True)
    assert outcome.returncode != 0
    assert "attested image" in outcome.stderr
    assert list(state.iterdir()) == []
    assert stat.S_IMODE(state.stat().st_mode) == 0o755


def test_attested_release_rejects_wrong_build_sha_without_state_mutation(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    state.chmod(0o755)
    outcome = run_bootstrap(tmp_path, baseline="accepted", sha_drift=True)
    assert outcome.returncode != 0
    assert "BUILD_SHA" in outcome.stderr
    assert list(state.iterdir()) == []
    assert stat.S_IMODE(state.stat().st_mode) == 0o755


def test_unknown_running_image_cannot_bootstrap_empty_volume(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    state.chmod(0o755)
    outcome = run_bootstrap(tmp_path, baseline="unknown")
    assert outcome.returncode != 0
    assert "No independently attested release matches" in outcome.stderr
    assert list(state.iterdir()) == []
    assert stat.S_IMODE(state.stat().st_mode) == 0o755

