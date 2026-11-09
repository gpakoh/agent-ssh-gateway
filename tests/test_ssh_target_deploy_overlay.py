"""CI must preserve opt-in SSH mounts without reading host keys.

Run only the Compose-selection preamble with a stubbed Docker host probe.
The CI job never mounts the private operator file tree.
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
    (bin_dir / "docker").write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  inspect) printf 'sha256:%064d\\n' 0 ;;\n"
        "  run)\n"
        "    [ \"${FIXTURE_HOST_PROBE_FAIL:-0}\" != 1 ] || exit 1\n"
        "    [ \"${FIXTURE_KEY_UID:-1000}\" = 1000 ] || exit 1\n"
        "    [ \"${FIXTURE_KEY_MODE:-600}\" = 600 ] || exit 1\n"
        "    [ \"${FIXTURE_CONFIG_MODE:-644}\" != 666 ] || exit 1\n"
        "    ;;\n"
        "  compose) exit 0 ;;\n"
        "  *) exit 8 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    # Exercise the Docker host-probe shim deterministically without sleeping
    # or requiring the verifier container to expose its own timeout binary.
    (bin_dir / "timeout").write_text(
        "#!/bin/sh\n"
        "[ \"$1\" = '--signal=TERM' ] || exit 8\n"
        "[ \"$2\" = '--kill-after=5s' ] || exit 8\n"
        "[ \"$3\" = '120s' ] || exit 8\n"
        "shift 3\n"
        "exec \"$@\"\n",
        encoding="utf-8",
    )
    # The media-overlay preflight is an independent, already-tested entry
    # point. Stub only that executable in this bounded Compose preamble test.
    (bin_dir / "python3").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
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
    # CI runner has no direct view of host credentials. The host-only
    # no-network Docker probe must refuse a missing/untrusted input.
    result = _run(tmp_path, SSH_TARGETS_OVERLAY_ENABLED="true", FIXTURE_HOST_PROBE_FAIL="1")
    assert result.returncode != 0
    assert "host operator input missing or unsafe" in result.stderr
    assert "RESULT=" not in result.stdout


def test_host_only_sources_are_not_stat_checked_in_ci_job(tmp_path: Path) -> None:
    # Paths are deliberately invisible in the CI job container, while the
    # Docker daemon owns the exact operator files on the host.
    root = "/srv/operator-ssh-private"
    result = _run(
        tmp_path,
        SSH_TARGETS_OVERLAY_ENABLED="true",
        MCP_SSH_TARGETS_CONFIG_SOURCE=f"{root}/targets.json",
        MCP_SSH_LXC103_KEY_SOURCE=f"{root}/keys/lxc103",
        MCP_SSH_LXC104_KEY_SOURCE=f"{root}/keys/lxc104",
    )
    assert result.returncode == 0, result.stderr
    assert "ssh-targets.example" in result.stdout


def test_host_preflight_uses_pinned_networkless_readonly_verifier() -> None:
    source = _preamble()
    assert 'docker inspect -f \'{{.Image}}\' mcp-oauth' in source
    assert "--pull never --network none --read-only --cap-drop ALL" in source
    assert "--security-opt no-new-privileges --user 1000:1000" in source
    assert "--mount \"type=bind,source=$operator_dir,target=/operator-ssh-targets,readonly\"" in source
    assert "(root / path).lstat()" in source
    assert "stat.S_ISREG" in source
    assert 'check("keys/lxc103", uid=1000, exact_mode=0o600)' in source
    assert 'check("keys/lxc104", uid=1000, exact_mode=0o600)' in source
    assert "if ! $COMPOSE config -q" in source


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
    assert "host operator input missing or unsafe" in result.stderr


def test_opt_in_rejects_wrong_key_uid(tmp_path: Path) -> None:
    result = _run(tmp_path, SSH_TARGETS_OVERLAY_ENABLED="true", FIXTURE_KEY_UID="20001")
    assert result.returncode != 0
    assert "host operator input missing or unsafe" in result.stderr


def test_opt_in_rejects_writable_config(tmp_path: Path) -> None:
    result = _run(tmp_path, SSH_TARGETS_OVERLAY_ENABLED="true", FIXTURE_CONFIG_MODE="666")
    assert result.returncode != 0
    assert "host operator input missing or unsafe" in result.stderr


def test_opt_in_rejects_unapproved_custom_compose(tmp_path: Path) -> None:
    result = _run(tmp_path, SSH_TARGETS_OVERLAY_ENABLED="true", COMPOSE="docker compose -f other.yml")
    assert result.returncode != 0
    assert "custom COMPOSE override" in result.stderr


def test_opt_in_rejects_unknown_toggle(tmp_path: Path) -> None:
    result = _run(tmp_path, SSH_TARGETS_OVERLAY_ENABLED="maybe")
    assert result.returncode != 0
    assert "must be true or false" in result.stderr
