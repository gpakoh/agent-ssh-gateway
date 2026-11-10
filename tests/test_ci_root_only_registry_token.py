"""Root-only registry token reader is bounded to trusted master-push CI.

Tests deliberately substitute a fake Docker CLI: no host registry token is
accessed, copied, printed, or modified by this test suite.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

SCRIPT = Path("scripts/ci-registry-token-stream.sh")
HOST_TOKEN = (
    "/media/1TB/Python/quart-platform/infra-quart/secrets/registry.token"
)


def _fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "docker"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['FAKE_DOCKER_CALLS'], 'a') as f:\n"
        "    f.write(json.dumps(args) + '\\n')\n"
        "if args == ['image', 'inspect', '--format', '{{.Id}}',"
        " 'sha256:dfd0f8d9c05f49a0b99f6812ae76809eaa79fb1c36963ca79078d2ce827caab8']:\n"
        "    print('sha256:dfd0f8d9c05f49a0b99f6812ae76809eaa79fb1c36963ca79078d2ce827caab8')\n"
        "elif args and args[0] == 'run':\n"
        "    sys.stdout.write('synthetic-test-credential\\n')\n"
        "else:\n"
        "    sys.exit(2)\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    calls = tmp_path / "docker-calls.jsonl"
    monkeypatch.setenv("FAKE_DOCKER_CALLS", str(calls))
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return calls


@pytest.mark.parametrize(
    "event,ref",
    [
        ("pull_request", "master"),
        ("push", "feature/unsafe"),
        ("", "master"),
    ],
)
def test_registry_token_stream_never_opens_secret_for_non_master_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str, ref: str
):
    calls = _fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_REF_NAME", ref)
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, timeout=10,
        check=False,
    )
    assert result.returncode == 64
    assert result.stdout == ""
    assert not calls.exists()
    assert "synthetic-test-credential" not in result.stderr


def test_registry_token_stream_uses_single_pinned_offline_root_only_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls = _fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF_NAME", "master")
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "synthetic-test-credential\n"
    assert "synthetic-test-credential" not in result.stderr
    args = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(args) == 2
    assert args[0] == [
        "image", "inspect", "--format", "{{.Id}}",
        "sha256:dfd0f8d9c05f49a0b99f6812ae76809eaa79fb1c36963ca79078d2ce827caab8",
    ]
    runner = args[1]
    assert runner[:3] == ["run", "--rm", "--pull=never"]
    for flag in ("--read-only", "--rm"):
        assert flag in runner
    assert runner[runner.index("--user") + 1] == "0:0"
    assert runner[runner.index("--network") + 1] == "none"
    assert runner[runner.index("--cap-drop") + 1] == "ALL"
    assert runner[runner.index("--security-opt") + 1] == "no-new-privileges"
    assert runner[runner.index("--entrypoint") + 1] == "/bin/cat"
    mount = runner[runner.index("--mount") + 1]
    assert mount == (
        f"type=bind,source={HOST_TOKEN},"
        "target=/run/registry.token,readonly"
    )
    assert runner[-2:] == ["sha256:dfd0f8d9c05f49a0b99f6812ae76809eaa79fb1c36963ca79078d2ce827caab8", "/run/registry.token"]
    assert "synthetic-test-credential" not in json.dumps(args)
    assert "chmod" not in SCRIPT.read_text(encoding="utf-8")


def test_workflow_has_no_direct_registry_host_file_mount_into_pr_build():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert f"{HOST_TOKEN}:/deploy/secrets/registry.token:ro" not in workflow
    assert "image: sha256:dfd0f8d9c05f49a0b99f6812ae76809eaa79fb1c36963ca79078d2ce827caab8" in workflow
    assert "if: github.event_name == 'push'" in workflow
    assert workflow.count("bash scripts/ci-registry-token-stream.sh") >= 2


def test_registry_secret_wiring_is_typed_yaml_and_scoped_to_master_push():
    workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text())
    jobs = workflow["jobs"]
    build = jobs["build-and-push"]
    deploy = jobs["deploy"]
    assert build["container"]["image"] == (
        "sha256:dfd0f8d9c05f49a0b99f6812ae76809eaa79fb1c36963ca79078d2ce827caab8"
    )
    assert not build["container"].get("volumes")
    # The image smoke step uses `set -euo pipefail`; act must invoke Bash,
    # not the pinned container image's default /bin/sh (dash).
    assert build["defaults"]["run"]["shell"] == "bash"
    smoke_steps = [
        step for step in build["steps"]
        if "set -euo pipefail" in step.get("run", "")
    ]
    assert smoke_steps, "build-and-push must retain the Bash-only smoke step"
    assert all(
        "/media/1TB/Python/quart-platform/infra-quart/secrets/registry.token"
        not in mount
        for mount in deploy["container"]["volumes"]
    )
    for job in (build, deploy):
        login_steps = [
            step for step in job["steps"]
            if step.get("name") == "Log in to the Gitea container registry"
        ]
        assert len(login_steps) == 1
        assert "ci-registry-token-stream.sh" in login_steps[0]["run"]
        assert "ci-docker-login-retry.sh" in login_steps[0]["run"]
        assert "set -euo pipefail" in login_steps[0]["run"]
    assert login_steps[0]["name"] == "Log in to the Gitea container registry"
    build_login = next(
        x for x in build["steps"]
        if x.get("name") == "Log in to the Gitea container registry"
    )
    assert build_login["if"] == "github.event_name == 'push'"
    # Previous successful deploy CI #24305 ran the login under POSIX sh.
    # The new secret-streamer pipefail must run under Bash on master push.
    deploy_login = next(
        step for step in deploy["steps"]
        if step.get("name") == "Log in to the Gitea container registry"
    )
    assert deploy_login["shell"] == "bash"
