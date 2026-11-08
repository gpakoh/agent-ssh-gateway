"""CI deployment must persist the opt-in SSH target mount across rollback.

Run only the Compose-selection preamble of the real deploy script in a bounded
subprocess with stubbed docker/stat; never pull images or access real keys.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "deploy-from-registry.sh"


def _preamble() -> str:
    source = DEPLOY.read_text(encoding="utf-8")
    start = source.index('BASE_COMPOSE="docker compose')
    end = source.index('GATEWAY_REPO="${WEB_SSH_GATEWAY_REPO:', start)
    return source[start:end]


def _run(tmp_path: Path, **settings: str) -> subprocess.CompletedProcess[str]:
    keys = tmp_path / "keys"
    keys.mkdir(exist_ok=True)
    files = {
        "MCP_SSH_TARGETS_CONFIG_SOURCE": tmp_path / "targets.json",
        "MCP_SSH_LXC103_KEY_SOURCE": keys / "lxc103",
        "MCP_SSH_LXC104_KEY_SOURCE": keys / "lxc104",
    }
    for file in files.values():
        file.write_text("fixture", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bin_dir / "python3").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bin_dir / "stat").write_text(
        "#!/bin/sh\n"
        "case \"$2\" in\n"
        "  %a) printf '%s\\n' \"${FIXTURE_CONFIG_MODE:-644}\" ;;\n"
        "  '%u %a') printf '%s %s\\n' \"${FIXTURE_KEY_UID:-1000}\" \"${FIXTURE_KEY_MODE:-600}\" ;;\n"
        "  *) exit 9 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    for f in bin_dir.iterdir():
        f.chmod(0o755)

    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "SSH_TARGETS_OVERLAY_ENABLED": "false",
        "SITE_MEDIA_OVERLAY_ENABLED": "false",
        **{key: str(value) for key, value in files.items()},
        **settings,
    }
    script = "set -euo pipefail\nROOT=/tmp\n" + _preamble() + '\nprintf "RESULT=%s\\n" "$COMPOSE"\n'
    return subprocess.run(["bash", "-c", script], text=True, capture_output=True, env=env, timeout=8)


def test_default_is_base_compose(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "RESULT=docker compose -p web-ssh-gateway -f docker/docker-compose.yml\n" in result.stdout
    assert "ssh-targets.example" not in result.stdout


def test_valid_opt_in_is_persistent_fixed_readonly_overlay(tmp_path: Path) -> None:
    result = _run(tmp_path, SSH_TARGETS_OVERLAY_ENABLED="true")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith(
        "docker/docker-compose.yml -f docker/compose.ssh-targets.example.yml"
    )
    compose = (ROOT / "docker" / "compose.ssh-targets.example.yml").read_text(encoding="utf-8")
    assert compose.count(":ro") == 3
    assert "MCP_SSH_TARGETS_FILE=/run/ssh-targets/targets.json" in compose
    assert "MCP_SSH_TARGET_KEY_DIR=/run/ssh-targets/keys" in compose


def test_overlay_composes_with_media_overlay(tmp_path: Path) -> None:
    result = _run(tmp_path, SSH_TARGETS_OVERLAY_ENABLED="true", SITE_MEDIA_OVERLAY_ENABLED="true")
    assert result.returncode == 0, result.stderr
    assert "-f docker/site-media.compose.yml -f docker/compose.ssh-targets.example.yml" in result.stdout


def test_disabled_does_not_require_operator_key_files(tmp_path: Path) -> None:
    result = _run(
        tmp_path, MCP_SSH_TARGETS_CONFIG_SOURCE="/missing-targets",
        MCP_SSH_LXC103_KEY_SOURCE="/missing-lxc103",
        MCP_SSH_LXC104_KEY_SOURCE="/missing-lxc104",
    )
    assert result.returncode == 0, result.stderr


def test_opt_in_rejects_missing_source_without_replacement(tmp_path: Path) -> None:
    result = _run(tmp_path, SSH_TARGETS_OVERLAY_ENABLED="true", MCP_SSH_LXC104_KEY_SOURCE="/no-key")
    assert result.returncode != 0
    assert "NO-GO" in result.stderr
    assert "RESULT=" not in result.stdout


def test_opt_in_rejects_relative_file(tmp_path: Path) -> None:
    result = _run(tmp_path, SSH_TARGETS_OVERLAY_ENABLED="true", MCP_SSH_LXC103_KEY_SOURCE="key")
    assert result.returncode != 0


def test_opt_in_rejects_symlink(tmp_path: Path) -> None:
    alias = tmp_path / "symlink"
    alias.symlink_to(tmp_path / "keys" / "lxc103")
    result = _run(tmp_path, SSH_TARGETS_OVERLAY_ENABLED="true", MCP_SSH_LXC103_KEY_SOURCE=str(alias))
    assert result.returncode != 0


def test_opt_in_rejects_exposed_key_permissions(tmp_path: Path) -> None:
    result = _run(tmp_path, SSH_TARGETS_OVERLAY_ENABLED="true", FIXTURE_KEY_MODE="640")
    assert result.returncode != 0
    assert "mode 0600" in result.stderr


def test_opt_in_rejects_wrong_key_uid(tmp_path: Path) -> None:
    result = _run(tmp_path, SSH_TARGETS_OVERLAY_ENABLED="true", FIXTURE_KEY_UID="20001")
    assert result.returncode != 0
    assert "uid 1000" in result.stderr


def test_opt_in_rejects_writable_config(tmp_path: Path) -> None:
    result = _run(tmp_path, SSH_TARGETS_OVERLAY_ENABLED="true", FIXTURE_CONFIG_MODE="666")
    assert result.returncode != 0
    assert "unsafe config permissions" in result.stderr


def test_opt_in_rejects_unapproved_custom_compose(tmp_path: Path) -> None:
    result = _run(tmp_path, SSH_TARGETS_OVERLAY_ENABLED="true", COMPOSE="docker compose -f other.yml")
    assert result.returncode != 0
    assert "custom COMPOSE override" in result.stderr


def test_opt_in_rejects_unknown_toggle(tmp_path: Path) -> None:
    result = _run(tmp_path, SSH_TARGETS_OVERLAY_ENABLED="maybe")
    assert result.returncode != 0
    assert "must be true or false" in result.stderr
