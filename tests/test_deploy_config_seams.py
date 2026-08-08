"""Config-coherence tests for seams between docker-compose.yml and Dockerfiles.

None of these are Python-code bugs — app/known_hosts.py, the mcp-server auth
middleware, and TokenStore were each fully unit-tested in isolation and all
green. Every bug here was the *deployed configuration* never actually wiring
two things together that only work as a pair. No unit test can catch that
kind of gap; only something that reads the actual shipped compose/Dockerfile
content can.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker" / "docker-compose.yml"
MCP_SERVER_DOCKERFILE = ROOT / "docker" / "Dockerfile.mcp-server"
GATEWAY_DOCKERFILE = ROOT / "docker" / "Dockerfile"


def _load_compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _env_dict(env_list: list[str]) -> dict[str, str]:
    """Compose environment entries are "KEY=value" strings, not a mapping."""
    result = {}
    for entry in env_list:
        key, _, value = entry.partition("=")
        result[key] = value
    return result


class TestStrictHostKeyCheckingNeedsAStore:
    """Regression: SSH_STRICT_HOST_KEY_CHECKING=true with no KNOWN_HOSTS_STORE
    configured silently falls back to NullHostKeyStore + paramiko.RejectPolicy
    — every SSH connection to any host is rejected, with no way to ever trust
    a host (the known-hosts API writes into a store that discards everything).
    Both app/known_hosts.py's store classes and the policy selection logic
    were already fully unit-tested and correct; the gap was purely that
    docker-compose.yml turned strict checking on without ever wiring a store.
    """

    def test_web_ssh_gateway_has_a_known_hosts_store_if_strict_checking_is_on(self):
        env = _env_dict(_load_compose()["services"]["web-ssh-gateway"]["environment"])
        strict = env.get("SSH_STRICT_HOST_KEY_CHECKING", "").strip().lower()
        if strict == "true":
            store = env.get("KNOWN_HOSTS_STORE", "").strip()
            assert store in ("file", "postgres"), (
                "SSH_STRICT_HOST_KEY_CHECKING=true with no usable KNOWN_HOSTS_STORE "
                "means every SSH connection is permanently rejected — there is no "
                "way to ever trust a host. Set KNOWN_HOSTS_STORE=file or =postgres."
            )

    def test_file_store_points_at_a_mounted_writable_path(self):
        """KNOWN_HOSTS_FILE must live under a path that's actually mounted
        (not the container's ephemeral, read_only rootfs), or every store()
        call fails to persist and the gateway is back to "can never trust a
        host" — just with a less obvious error.
        """
        service = _load_compose()["services"]["web-ssh-gateway"]
        env = _env_dict(service["environment"])
        if env.get("KNOWN_HOSTS_STORE", "").strip() != "file":
            return
        known_hosts_file = env.get("KNOWN_HOSTS_FILE", "").strip()
        assert known_hosts_file, "KNOWN_HOSTS_STORE=file requires KNOWN_HOSTS_FILE"
        mount_targets = [v.split(":")[1] for v in service.get("volumes", []) if ":" in v]
        assert any(known_hosts_file.startswith(target) for target in mount_targets), (
            f"KNOWN_HOSTS_FILE={known_hosts_file!r} isn't under any mounted volume "
            f"({mount_targets!r}) — on a read_only rootfs, every write silently fails."
        )


class TestMcpServerHealthcheckDoesNotHitAuthedEndpoint:
    """Regression: the Docker HEALTHCHECK probed /mcp (bearer-protected),
    which always answered 401 without a token — harmless (curl without -f
    doesn't care about status), but spammed the container's own logs with a
    misleading "unauthorized" line every 30s, and never actually verified a
    real 200/failure signal.
    """

    def test_healthcheck_targets_the_unauthenticated_healthz_path(self):
        text = MCP_SERVER_DOCKERFILE.read_text(encoding="utf-8")
        healthcheck_line = next(
            (line for line in text.splitlines() if "curl" in line and "8087" in line),
            None,
        )
        assert healthcheck_line is not None, "expected a curl-based HEALTHCHECK CMD"
        assert "/healthz" in healthcheck_line
        assert "/mcp " not in healthcheck_line and not healthcheck_line.rstrip().endswith("/mcp")


class TestMcpServerTokenStoreVolumeIsWritableByItsOwnUser:
    """Regression: a volume mounted at a path the image never chowned to the
    non-root runtime user gets created root:root-owned by Docker — the
    container's own `appuser` can't write to it, so TokenStore's real writes
    fail with Permission denied even though the read-only-rootfs error is
    gone. /app/data is chowned to appuser in the Dockerfile specifically so
    volumes mounted there inherit the right ownership on first use.
    """

    def test_token_store_volume_mounted_under_the_chowned_app_data_path(self):
        service = _load_compose()["services"]["mcp-server"]
        env = _env_dict(service["environment"])
        token_store_file = env.get("MCP_TOKEN_STORE_FILE", "").strip()
        assert token_store_file, "mcp-server should set MCP_TOKEN_STORE_FILE explicitly"

        mount_targets = [v.split(":")[1] for v in service.get("volumes", []) if ":" in v]
        assert any(token_store_file.startswith(target) for target in mount_targets), (
            f"MCP_TOKEN_STORE_FILE={token_store_file!r} isn't under any mounted "
            f"volume ({mount_targets!r})"
        )

        dockerfile_text = MCP_SERVER_DOCKERFILE.read_text(encoding="utf-8")
        # A chown line like "chown -R appuser:appuser /app /tmp" covers
        # /app/data recursively without literally containing that substring
        # — collect the chowned *paths* themselves, not raw line text.
        chowned_paths: list[str] = []
        for line in dockerfile_text.splitlines():
            if "chown" not in line or "appuser" not in line:
                continue
            for token in line.split():
                if token.startswith("/"):
                    chowned_paths.append(token.rstrip("&"))

        mount_target_covered = any(
            token_store_file.startswith(target)
            and any(target == p or target.startswith(p.rstrip("/") + "/") for p in chowned_paths)
            for target in mount_targets
        )
        assert mount_target_covered, (
            f"mounted volume target(s) {mount_targets!r} for MCP_TOKEN_STORE_FILE "
            f"aren't chowned to appuser anywhere in {MCP_SERVER_DOCKERFILE.name} "
            f"(chowned paths found: {chowned_paths!r}) — a fresh named volume there "
            "is created root-owned and unwritable by the container's own non-root user."
        )


class TestGatewayHealthcheckReadsBodyStatusNotJustHttpStatus:
    """Regression (M17): /health always answers HTTP 200 (its body carries a
    status/ready field for callers to interpret) -- app/routers/system.py
    intentionally never returns 503 on degraded, since that endpoint's HTTP
    contract is relied on elsewhere (many tests, and any external caller
    that only checks "did /health respond") as an always-200 liveness probe.
    A bare `urlopen('.../health')` HEALTHCHECK therefore only proves the
    process accepted a TCP connection, not that Redis/Postgres/SSH are
    actually working -- both docker/Dockerfile's own HEALTHCHECK and
    docker-compose.yml's override (which replaces it entirely, so both need
    the same fix) must parse the JSON body and fail when status != "ok".
    """

    def test_dockerfile_healthcheck_parses_body_status(self):
        text = GATEWAY_DOCKERFILE.read_text(encoding="utf-8")
        healthcheck_line = next(
            (line for line in text.splitlines() if "8085/health" in line),
            None,
        )
        assert healthcheck_line is not None, "expected a HEALTHCHECK CMD probing :8085/health"
        assert "json.load" in healthcheck_line
        assert "status" in healthcheck_line
        assert "== 'ok'" in healthcheck_line or '== "ok"' in healthcheck_line

    def test_compose_healthcheck_override_parses_body_status(self):
        service = _load_compose()["services"]["web-ssh-gateway"]
        test_cmd = service["healthcheck"]["test"]
        joined = " ".join(test_cmd)
        assert "8085/health" in joined
        assert "json.load" in joined
        assert "status" in joined
        assert "== 'ok'" in joined or '== "ok"' in joined
