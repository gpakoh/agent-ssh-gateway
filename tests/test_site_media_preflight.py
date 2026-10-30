"""Fail-closed site media overlay preflight: immutable image, secret, networks."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "site_media_preflight.py"
spec = importlib.util.spec_from_file_location("site_media_preflight", SCRIPT)
assert spec and spec.loader
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)

TOKEN = "s" * 42
IMAGE = "192.168.1.103:3005/gpakoh/site-media-worker@sha256:" + "a" * 64


@pytest.fixture
def composed():
    # Minimal real-world Docker Compose config JSON shape. Tests against
    # the tracked overlay guard regressions in the source YAML.
    overlay = yaml.safe_load((SCRIPT.parent.parent / "docker/site-media.compose.yml").read_text())
    worker = copy.deepcopy(overlay["services"]["site-media-worker"])
    worker["image"] = IMAGE
    worker["environment"] = {"SITE_MEDIA_WORKER_TOKEN": TOKEN}
    mcp = copy.deepcopy(overlay["services"]["mcp-oauth"])
    mcp["environment"] = {"SITE_MEDIA_WORKER_TOKEN": TOKEN}
    return {
        "services": {"mcp-oauth": mcp, "site-media-worker": worker},
        "networks": overlay["networks"],
    }


def test_approved_digest_and_private_secret_do_not_leak():
    assert preflight.validate_environment(
        {"SITE_MEDIA_WORKER_TOKEN": TOKEN, "SITE_MEDIA_WORKER_IMAGE": IMAGE}
    ) == (IMAGE, TOKEN)
    for rejected in (
        IMAGE.replace("@sha256:", ":latest-sha256-"),
        "docker.io/library/site-media-worker@sha256:" + "a" * 64,
        "192.168.1.103:3005/gpakoh/site-media-worker:latest",
        "",
    ):
        with pytest.raises(preflight.MediaPreflightError, match="immutable|pinned|approved"):
            preflight.validate_environment(
                {"SITE_MEDIA_WORKER_TOKEN": TOKEN, "SITE_MEDIA_WORKER_IMAGE": rejected}
            )
    for rejected in ("short", "x" * 33 + "\n", "", "x" * 513):
        with pytest.raises(preflight.MediaPreflightError, match="operator-owned"):
            preflight.validate_environment(
                {"SITE_MEDIA_WORKER_TOKEN": rejected, "SITE_MEDIA_WORKER_IMAGE": IMAGE}
            )


def test_overlay_keeps_gateway_and_worker_networks_isolated(composed):
    preflight.validate_compose(composed, IMAGE, TOKEN)
    mcp = composed["services"]["mcp-oauth"]
    worker = composed["services"]["site-media-worker"]
    assert set(mcp["networks"]) == {"internal_net", "media_net"}
    assert set(worker["networks"]) == {"media_net"}
    assert "build" not in worker


@pytest.mark.parametrize(
    "service,field,value",
    [
        ("mcp-oauth", "networks", {"media_net": {}}),
        ("site-media-worker", "networks", {"media_net": {}, "internal_net": {}}),
        ("site-media-worker", "image", "site-media-worker:latest"),
        ("site-media-worker", "build", {"context": ".."}),
        ("site-media-worker", "ports", ["8091:8091"]),
        ("site-media-worker", "volumes", ["../:/repo"]),
        ("site-media-worker", "read_only", False),
        ("site-media-worker", "privileged", True),
        ("site-media-worker", "cap_drop", []),
        ("site-media-worker", "security_opt", []),
        ("site-media-worker", "user", "0"),
        ("site-media-worker", "cpus", 8),
        ("site-media-worker", "mem_limit", "4g"),
        ("site-media-worker", "pids_limit", 1024),
        ("site-media-worker", "profiles", []),
        ("mcp-oauth", "environment", {"SITE_MEDIA_WORKER_TOKEN": "different-secret"}),
        ("site-media-worker", "environment", {"SITE_MEDIA_WORKER_TOKEN": "different-secret"}),
    ],
)
def test_fail_closed_if_deployment_contract_drifted(composed, service, field, value):
    composed["services"][service][field] = value
    with pytest.raises(preflight.MediaPreflightError):
        preflight.validate_compose(composed, IMAGE, TOKEN)


def test_fail_closed_if_media_net_is_externally_reachable(composed):
    composed["networks"]["media_net"]["internal"] = False
    with pytest.raises(preflight.MediaPreflightError, match="internal"):
        preflight.validate_compose(composed, IMAGE, TOKEN)


def test_compose_command_is_read_only_and_never_prints_config(composed):
    fake = SimpleNamespace(returncode=0, stdout=json.dumps(composed), stderr="")
    env = {"SITE_MEDIA_WORKER_IMAGE": IMAGE, "SITE_MEDIA_WORKER_TOKEN": TOKEN}
    with patch.object(preflight.subprocess, "run", return_value=fake) as call:
        config = preflight.inspect_compose(env)
    preflight.validate_compose(config, IMAGE, TOKEN)
    command = call.call_args.args[0]
    assert command[:2] == ["docker", "compose"]
    assert command[-3:] == ["config", "--format", "json"]
    assert not any(word in command for word in ["up", "pull", "build", "exec", "run"])
    assert call.call_args.kwargs["capture_output"] is True
    assert call.call_args.kwargs["env"] is env


def test_failed_compose_never_prints_sensitive_stderr(composed, capsys):
    env = {"SITE_MEDIA_WORKER_IMAGE": IMAGE, "SITE_MEDIA_WORKER_TOKEN": TOKEN}
    fake = SimpleNamespace(returncode=1, stdout="", stderr=f"fatal {TOKEN}")
    with patch.object(preflight.os, "environ", env), patch.object(
        preflight.subprocess, "run", return_value=fake
    ):
        assert preflight.main() == 1
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert "NO-GO" in output.err
