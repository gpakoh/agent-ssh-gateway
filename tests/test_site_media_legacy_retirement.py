"""Retired local codec image and actual shell rollback topology (fully isolated).

No Docker daemon, service stop, credential or production host is required.
Bash executes extracted production branch bodies with a fake docker inspector.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts/deploy-from-registry.sh"
WORKFLOW = ROOT / ".github/workflows/ci.yml"


def test_retired_codec_is_not_in_gateway_sources_images_or_ci() -> None:
    assert not list((ROOT / "app/site_media_worker").glob("*.py"))
    assert not (ROOT / "docker/Dockerfile.site-media-worker").exists()
    assert not (ROOT / "tests/test_site_media_worker_http.py").exists()

    workflow_text = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    assert "media-worker-qualify" not in workflow["jobs"]
    assert "Dockerfile.site-media-worker" not in workflow_text
    assert "CI_SMOKE_MEDIA_IMAGE_ID" not in workflow_text
    assert "gpakoh/site-media-worker" not in workflow_text

    # Keep the three Gateway-owned release artifacts and their LKG deploy.
    assert workflow_text.count("bash scripts/ci-docker-push-pinned.sh") == 3
    assert "build-and-push" in workflow["jobs"]
    assert workflow["jobs"]["deploy"]["needs"] == ["build-and-push"]
    assert "host-smoke" in workflow["jobs"]

    overlay = yaml.safe_load((ROOT / "docker/site-media.compose.yml").read_text())
    assert set(overlay["services"]) == {"mcp-oauth"}
    assert set(overlay["services"]["mcp-oauth"]["networks"]) == {"internal_net", "media_net"}
    assert overlay["networks"]["media_net"] == {
        "external": True, "name": "quart-media-internal",
    }


def _production_rollback_branches() -> str:
    """Select verbatim existing shell code, no copy of logic to fake test."""
    source = DEPLOY.read_text(encoding="utf-8")
    detection_start = source.index("SITE_MEDIA_FIRST_ATTACHMENT=false\n")
    detection_end = source.index("RUNNING_SSHD_ID=", detection_start)
    rollback_start = source.index(
        'if [ "$SITE_MEDIA_FIRST_ATTACHMENT" = "true" ]; then', detection_end,
    )
    rollback_end = source.index(
        'if ! deploy_services "$PREVIOUS_GATEWAY_IMAGE"', rollback_start,
    )
    return source[detection_start:detection_end] + source[rollback_start:rollback_end]


@pytest.mark.parametrize(
    ("existing_networks", "overlay_enabled", "expected_first", "expected_compose", "expected_enabled"),
    [
        ({"internal_net": {}}, "true", "true", "base-only", "false"),
        ({"internal_net": {}, "quart-media-internal": {}}, "true", "false", "base-and-media", "true"),
        ({"internal_net": {}}, "false", "false", "base-and-media", "false"),
    ],
)
def test_first_attach_rollback_in_unprivileged_bash_sandbox(
    tmp_path: Path, existing_networks: dict[str, dict],
    overlay_enabled: str, expected_first: str,
    expected_compose: str, expected_enabled: str,
) -> None:
    # This executes the exact inspect + topology restoration branches from
    # production code with a fake read-only Docker inspector and a fake
    # deploy_services; no calls to real Docker or network are possible.
    script = """
set -euo pipefail
docker() {
    [ "$1" = inspect ] && [ "$2" = mcp-oauth ] || return 99
    printf '%s\\n' "$MOCK_DOCKER_NETWORKS"
}
log() { printf 'LOG:%s\\n' "$*" >&2; }
BASE_COMPOSE=base-only
COMPOSE=base-and-media
PREVIOUS_GATEWAY_IMAGE=sha256:gateway-lkg
PREVIOUS_MCP_IMAGE=sha256:mcp-lkg
PREVIOUS_SSHD_IMAGE=sha256:sshd-lkg
""" + _production_rollback_branches() + """
printf 'FIRST=%s\\nCOMPOSE=%s\\nOVERLAY=%s\\n' \\
    "$SITE_MEDIA_FIRST_ATTACHMENT" "$COMPOSE" "$SITE_MEDIA_OVERLAY_ENABLED"
# In the real deploy script this is the guarded LKG restore invocation.
deploy_services() {
  printf 'RESTORE=%s,%s,%s COMPOSE=%s\\n' "$1" "$2" "$3" "$COMPOSE"
}
deploy_services "$PREVIOUS_GATEWAY_IMAGE" "$PREVIOUS_MCP_IMAGE" "$PREVIOUS_SSHD_IMAGE"
"""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "SITE_MEDIA_OVERLAY_ENABLED": overlay_enabled,
        "MOCK_DOCKER_NETWORKS": json.dumps(existing_networks),
    }
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=False, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert f"FIRST={expected_first}" in result.stdout
    assert f"COMPOSE={expected_compose}" in result.stdout
    assert f"OVERLAY={expected_enabled}" in result.stdout
    assert "RESTORE=sha256:gateway-lkg,sha256:mcp-lkg,sha256:sshd-lkg" in result.stdout
    assert f"COMPOSE={expected_compose}" in result.stdout.split("RESTORE=", 1)[1]
    if expected_first == "true":
        assert "Rolling back the first Media attachment" in result.stderr
    else:
        assert "Rolling back the first Media attachment" not in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_rollback_keeps_verified_lkg_failure_handling() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    assert 'if ! deploy_services "$PREVIOUS_GATEWAY_IMAGE" "$PREVIOUS_MCP_IMAGE" "$PREVIOUS_SSHD_IMAGE"; then' in text
    assert "Rollback deployment FAILED" in text
    assert "Rollback provenance is missing or inconsistent" in text
    assert 'if smoke "$PREVIOUS_DEPLOY_SHA"; then' in text
