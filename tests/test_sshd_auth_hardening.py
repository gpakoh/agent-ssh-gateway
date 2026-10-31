"""P1-11 SSH executor must be key-only in isolated CI and deployed containers."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker/sshd/Dockerfile"
COMPOSE = ROOT / "docker/docker-compose.yml"
WORKFLOW = ROOT / ".github/workflows/ci.yml"
SMOKE = ROOT / "scripts/ci-sshd-auth-smoke.sh"


def test_executor_image_has_no_password_auth_or_fixed_password() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert 'echo "PasswordAuthentication no"' in dockerfile
    assert 'echo "KbdInteractiveAuthentication no"' in dockerfile
    assert 'echo "PermitEmptyPasswords no"' in dockerfile
    assert 'echo "AuthenticationMethods publickey"' in dockerfile
    assert 'echo "PubkeyAuthentication yes"' in dockerfile
    assert "chpasswd" not in dockerfile
    assert "mcpuser:NP:" in dockerfile
    assert "PasswordAuthentication yes" not in dockerfile


def test_both_executor_services_use_same_pinned_image() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]
    expected = "$" + "{SSH_GATEWAY_SSHD_IMAGE:-web-ssh-gateway-sshd:latest}"
    for service in ("sshd", "agent-sshd"):
        assert services[service]["image"] == expected
        assert services[service]["healthcheck"]["test"] == ["CMD", "sshd", "-t"]


def test_isolated_smoke_checks_effective_config_and_real_authentication() -> None:
    script = SMOKE.read_text(encoding="utf-8")
    assert "--network none" in script
    assert "--rm" in script
    assert "sha256:" in script
    assert "sshd -T -C user=mcpuser,host=localhost,addr=127.0.0.1" in script
    assert "grep -Eq" in script and "/etc/shadow" in script
    assert 'chmod 600 /tmp/smoke_key' in script
    assert "PreferredAuthentications=publickey" in script
    assert "PreferredAuthentications=password" in script
    assert "pubkeyauthentication yes" in script
    assert "passwordauthentication no" in script
    assert "kbdinteractiveauthentication no" in script
    assert "Authentications that can continue: publickey" in script
    assert "--publish" not in script and " -p 127.0.0.1:" not in script


def test_premerge_ci_checks_exact_built_image_before_push() -> None:
    ci = WORKFLOW.read_text(encoding="utf-8")
    smoke = 'bash scripts/ci-sshd-auth-smoke.sh "$CI_SMOKE_SSHD_IMAGE_ID"'
    assert smoke in ci
    assert ci.index(smoke) < ci.index("- name: Push web-ssh-gateway image")
    # Live validation is a separate script; the workflow only orchestrates it.
    assert 'bash scripts/ci-sshd-live-auth-check.sh "${{ github.sha }}"' in ci
    verify = "- name: Verify effective key-only SSH auth on both live executors"
    assert ci.index(verify) < ci.index("- name: Make host-smoke (full suite)")


def test_postdeploy_verifier_has_single_scoped_responsibility() -> None:
    script = (ROOT / "scripts/ci-sshd-live-auth-check.sh").read_text(encoding="utf-8")
    assert "verify_live_executor ssh-gateway-sshd" in script
    assert "verify_live_executor ssh-gateway-agent-sshd" in script
    assert 'running_sha="$(docker exec "$name" printenv BUILD_SHA)"' in script
    assert "sshd -T -C user=mcpuser,host=localhost,addr=127.0.0.1" in script
    assert "authenticationmethods publickey" in script
    assert 'grep -Eq "^mcpuser:NP:" /etc/shadow' in script
    assert "docker run" not in script


@pytest.mark.parametrize(
    ("drift", "expected_exit", "expected_error"),
    [
        ("none", 0, ""),
        ("missing-method", 1, "authenticationmethods publickey"),
        ("password-enabled", 1, "passwordauthentication no"),
        ("missing-sentinel", 1, "missing the non-crypt mcpuser password sentinel"),
        ("wrong-sha", 1, "runs unexpected build SHA"),
    ],
)
def test_postdeploy_sshd_gate_rejects_effective_runtime_drift(
    tmp_path: Path, drift: str, expected_exit: int, expected_error: str
) -> None:
    """Run the real live-auth gate using only a fake local Docker executable.

    This is a negative/positive predicate regression; no host Docker daemon,
    live container, credentials, SSH keys, or network endpoint is touched.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'test "$1" = exec\n'
        'case "$3" in\n'
        '  printenv)\n'
        '    test "$4" = BUILD_SHA\n'
        '    if [ "$SSH_SMOKE_DRIFT" = wrong-sha ] && [ "$2" = ssh-gateway-agent-sshd ]; then\n'
        '      printf "%s\\n" "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"\n'
        '    else\n'
        '      printf "%s\\n" "$SSH_SMOKE_SHA"\n'
        '    fi\n'
        '    ;;\n'
        '  sshd)\n'
        '    test "$4" = -T\n'
        '    printf "%s\\n" "kbdinteractiveauthentication no" "permitemptypasswords no" "pubkeyauthentication yes"\n'
        '    if [ "$SSH_SMOKE_DRIFT" = password-enabled ] && [ "$2" = ssh-gateway-agent-sshd ]; then\n'
        '      printf "%s\\n" "passwordauthentication yes"\n'
        '    else\n'
        '      printf "%s\\n" "passwordauthentication no"\n'
        '    fi\n'
        '    if [ "$SSH_SMOKE_DRIFT" != missing-method ] || [ "$2" != ssh-gateway-agent-sshd ]; then\n'
        '      printf "%s\\n" "authenticationmethods publickey"\n'
        '    fi\n'
        '    ;;\n'
        '  /bin/sh)\n'
        '    test "$4" = -c\n'
        '    test "$5" = \'grep -Eq "^mcpuser:NP:" /etc/shadow\'\n'
        '    if [ "$SSH_SMOKE_DRIFT" = missing-sentinel ] && [ "$2" = ssh-gateway-agent-sshd ]; then\n'
        '      exit 1\n'
        '    fi\n'
        '    ;;\n'
        '  *) exit 98 ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)
    deployed_sha = "a" * 40
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["SSH_SMOKE_SHA"] = deployed_sha
    env["SSH_SMOKE_DRIFT"] = drift
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/ci-sshd-live-auth-check.sh"), deployed_sha],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == expected_exit, result.stdout + result.stderr
    if expected_exit:
        assert expected_error in result.stderr
        assert "ssh-gateway-agent-sshd: pinned image" not in result.stdout
    else:
        assert result.stderr == ""
        assert "ssh-gateway-sshd: pinned image" in result.stdout
        assert "ssh-gateway-agent-sshd: pinned image" in result.stdout
