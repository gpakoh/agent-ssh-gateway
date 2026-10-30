"""Gateway media client overlay cannot deploy codecs or leak operator tokens."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/site_media_preflight.py"
spec = importlib.util.spec_from_file_location("site_media_preflight", SCRIPT)
assert spec and spec.loader
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)
TOKEN = "s" * 42
URL = "http://media-processing:8091"


@pytest.fixture
def composed():
    overlay = yaml.safe_load((SCRIPT.parent.parent / "docker/site-media.compose.yml").read_text())
    mcp = overlay["services"]["mcp-oauth"]
    return {
        "services": {
            "mcp-oauth": {
                **mcp,
                "environment": {
                    "SITE_MEDIA_WORKER_TOKEN": TOKEN,
                    "MEDIA_API_BASE_URL": URL,
                },
            }
        },
        "networks": overlay["networks"],
    }


def test_operator_secret_and_approved_url():
    assert preflight.validate_environment({"SITE_MEDIA_WORKER_TOKEN": TOKEN}) == TOKEN
    for token in ("short", "", "x" * 33 + "\n", "x" * 513):
        with pytest.raises(preflight.MediaPreflightError):
            preflight.validate_environment({"SITE_MEDIA_WORKER_TOKEN": token})
    for url in ("http://evil:8091", "https://media-processing:8091",
                "http://media-processing:8091/evil", "http://127.0.0.1:8091"):
        with pytest.raises(preflight.MediaPreflightError):
            preflight.validate_environment({
                "SITE_MEDIA_WORKER_TOKEN": TOKEN, "MEDIA_API_BASE_URL": url,
            })


def test_networks_and_no_gateway_owned_codec_worker(composed):
    preflight.validate_compose(composed, TOKEN)
    assert set(composed["services"]) == {"mcp-oauth"}
    assert set(composed["services"]["mcp-oauth"]["networks"]) == {"internal_net", "media_net"}
    assert composed["networks"]["media_net"] == {
        "external": True, "name": "quart-media-internal",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("networks", {"media_net": {}}),
        ("networks", {"internal_net": {}}),
        ("networks", {"internal_net": {}, "external": {}}),
        ("environment", {"MEDIA_API_BASE_URL": URL}),
        ("environment", {"SITE_MEDIA_WORKER_TOKEN": "different", "MEDIA_API_BASE_URL": URL}),
        ("environment", {"SITE_MEDIA_WORKER_TOKEN": TOKEN, "MEDIA_API_BASE_URL": "http://evil:8091"}),
    ],
)
def test_fail_closed_on_mcp_drift(composed, field, value):
    composed["services"]["mcp-oauth"][field] = value
    with pytest.raises(preflight.MediaPreflightError):
        preflight.validate_compose(composed, TOKEN)


def test_fail_closed_if_external_network_not_exact(composed):
    for network in (
        {"external": False, "name": "quart-media-internal"},
        {"external": True, "name": "public_net"},
    ):
        composed["networks"]["media_net"] = network
        with pytest.raises(preflight.MediaPreflightError):
            preflight.validate_compose(composed, TOKEN)


def test_fail_closed_if_gateway_starts_codec_service(composed):
    composed["services"]["site-media-worker"] = {"image": "unsafe"}
    with pytest.raises(preflight.MediaPreflightError, match="must not run"):
        preflight.validate_compose(composed, TOKEN)


def test_readonly_compose_inspection_redacts_secrets(composed, capsys):
    env = {"SITE_MEDIA_WORKER_TOKEN": TOKEN}
    mock = SimpleNamespace(returncode=0, stdout=json.dumps(composed), stderr="")
    with patch.object(preflight.subprocess, "run", return_value=mock) as call:
        data = preflight.inspect_compose(env)
    preflight.validate_compose(data, TOKEN)
    assert call.call_args.kwargs["capture_output"] is True
    assert call.call_args.kwargs["env"] is env
    assert call.call_args.args[0][-3:] == ["config", "--format", "json"]
    broken = SimpleNamespace(returncode=1, stdout="", stderr=TOKEN)
    with patch.object(preflight.os, "environ", env), patch.object(
        preflight.subprocess, "run", return_value=broken
    ):
        assert preflight.main() == 1
    assert TOKEN not in capsys.readouterr().err
