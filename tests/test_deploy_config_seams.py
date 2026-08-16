"""Config-coherence tests for seams between docker-compose.yml and Dockerfiles.

None of these are Python-code bugs — app/known_hosts.py, the mcp-server auth
middleware, and TokenStore were each fully unit-tested in isolation and all
green. Every bug here was the *deployed configuration* never actually wiring
two things together that only work as a pair. No unit test can catch that
kind of gap; only something that reads the actual shipped compose/Dockerfile
content can.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker" / "docker-compose.yml"
ENV_EXAMPLE_PATH = ROOT / "docker" / ".env.example"
MCP_SERVER_DOCKERFILE = ROOT / "docker" / "Dockerfile.mcp-server"
GATEWAY_DOCKERFILE = ROOT / "docker" / "Dockerfile"
SSHD_DOCKERFILE = ROOT / "docker" / "sshd" / "Dockerfile"
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy-from-registry.sh"
CI_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
HOST_SMOKE_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "host-smoke.yml"
MAKEFILE_PATH = ROOT / "Makefile"


def _load_compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _load_workflow(path: Path) -> dict:
    # PyYAML (YAML 1.1) parses the bare key `on:` as the boolean True, not
    # the string "on" -- doesn't affect the `concurrency:` key these tests
    # check, but worth noting so a future test here doesn't get tripped
    # up trying to index result["on"].
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _env_dict(env_list: list[str]) -> dict[str, str]:
    """Compose environment entries are "KEY=value" strings, not a mapping."""
    result = {}
    for entry in env_list:
        key, _, value = entry.partition("=")
        result[key] = value
    return result


class TestAgentRuntimeIsolationWiring:
    def test_oauth_and_sshd_share_managed_agent_runtime_volume(self):
        compose = _load_compose()
        oauth = compose["services"]["mcp-oauth"]
        sshd = compose["services"]["sshd"]
        agent_sshd = compose["services"]["agent-sshd"]
        oauth_env = _env_dict(oauth["environment"])

        assert "MCP_AGENT_STATE_ROOT" in oauth_env
        assert "MCP_AGENT_WORKSPACE_ROOT" in oauth_env
        assert "MCP_AGENT_SOURCE_ROOT" in oauth_env
        assert "/var/lib/mcp-agent/state" in oauth_env["MCP_AGENT_STATE_ROOT"]
        assert "/var/lib/mcp-agent/workspaces" in oauth_env["MCP_AGENT_WORKSPACE_ROOT"]
        assert "/var/lib/mcp-agent/sources" in oauth_env["MCP_AGENT_SOURCE_ROOT"]
        mount = "agent_runtime:/var/lib/mcp-agent"
        assert mount in oauth["volumes"]
        assert mount in sshd["volumes"]
        assert mount in agent_sshd["volumes"]
        assert "agent_sources:/var/lib/mcp-agent/sources" in oauth["volumes"]
        assert "agent_sources:/var/lib/mcp-agent/sources:ro" in sshd["volumes"]
        assert "agent_sources:/var/lib/mcp-agent/sources:ro" in agent_sshd["volumes"]
        assert "agent_runtime" in compose["volumes"]
        assert "agent_sources" in compose["volumes"]

    def test_dedicated_agent_executor_has_no_authoritative_workspace_mount(self):
        compose = _load_compose()
        agent_sshd = compose["services"]["agent-sshd"]
        volumes = agent_sshd["volumes"]
        assert not any("WORKSPACE_HOST_PATH" in volume for volume in volumes)
        assert not any(":/workspace" in volume for volume in volumes)
        assert "agent_runtime:/var/lib/mcp-agent" in volumes
        assert "agent_sources:/var/lib/mcp-agent/sources:ro" in volumes

    def test_oauth_routes_agent_execution_to_dedicated_executor(self):
        compose = _load_compose()
        oauth = compose["services"]["mcp-oauth"]
        env = _env_dict(oauth["environment"])
        assert env["MCP_AGENT_EXECUTOR_SSH_HOST"] == "${MCP_AGENT_EXECUTOR_SSH_HOST:-agent-sshd}"
        assert env["MCP_AGENT_EXECUTOR_SSH_PORT"] == "${MCP_AGENT_EXECUTOR_SSH_PORT:-2222}"
        assert env["MCP_AGENT_EXECUTOR_SSH_USERNAME"] == (
            "${MCP_OAUTH_SSH_USERNAME:?set MCP_OAUTH_SSH_USERNAME}"
        )
        assert env["MCP_AGENT_EXECUTOR_SSH_KEY_PATH"] == "/app/ssh_key"
        assert "agent-sshd" in oauth["depends_on"]

    def test_sshd_image_installs_git_ssh_transport(self):
        text = SSHD_DOCKERFILE.read_text(encoding="utf-8")
        assert "openssh-client-default" in text

    def test_images_prepare_shared_agent_source_directory(self):
        assert "/var/lib/mcp-agent/sources" in SSHD_DOCKERFILE.read_text(encoding="utf-8")
        assert "/var/lib/mcp-agent/sources" in MCP_SERVER_DOCKERFILE.read_text(encoding="utf-8")

    def test_deploy_publishes_exact_ci_sha_source_only_after_smoke(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        assert 'checkout_sha=$(git rev-parse HEAD' in text
        assert 'if [ "$checkout_sha" != "$DEPLOY_TAG" ]' in text
        assert 'git bundle create "$bundle_tmp" HEAD' in text
        assert 'git bundle list-heads "$bundle_tmp" HEAD' in text
        publish_fn = text[text.index("publish_agent_source_bundle()") : text.index("run_migrations()")]
        assert "if ! docker exec mcp-oauth python3 -c 'import os,sys; os.replace" in publish_fn
        assert 'git bundle list-heads "$container_path" HEAD' in publish_fn
        assert "final verification mismatch" in publish_fn
        assert publish_fn.rstrip().endswith("}") and "return 0\n}" in publish_fn
        smoke_gate = text.rfind("elif smoke; then")
        publish = text.rfind("if publish_agent_source_bundle; then")
        record = text.rfind('write_state "$NEW_GATEWAY_IMAGE"')
        assert -1 < smoke_gate < publish < record



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


class TestDeployRunsAlembicMigrations:
    """M15: app/main.py's startup create_all() is a fresh-DB/resilience
    fallback, not a substitute for tracking migration history -- nothing
    ever invoked Alembic itself. The deploy script must run `alembic
    upgrade head` against the freshly-deployed container, gating the
    smoke test on it (a migration failure must not be smoke-tested as if
    nothing happened) and must never treat it as optional/backgrounded.
    """

    def test_deploy_script_runs_alembic_upgrade_head(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        assert "alembic upgrade head" in text
        # Runs inside the deployed container (has alembic + real
        # DATABASE_URL + network access to mcp-postgres already) rather
        # than requiring a separate host-side Python/alembic install.
        assert "docker exec web-ssh-gateway alembic upgrade head" in text

    def test_migration_failure_gates_smoke_not_just_logged(self):
        """A migration failure must skip smoke() entirely (fall straight
        to the rollback path), not run smoke and report false success.
        """
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        lines = text.splitlines()
        run_migrations_call = next(
            i for i, line in enumerate(lines) if "run_migrations" in line and "()" not in line and line.strip().startswith(("if", "!"))
        )
        # The next non-blank lines must gate smoke() behind run_migrations
        # succeeding -- i.e. `if ! run_migrations; then ... elif smoke`.
        window = "\n".join(lines[run_migrations_call : run_migrations_call + 5])
        assert "run_migrations" in window
        assert "smoke" in window
        assert window.index("run_migrations") < window.index("smoke")


class TestCiSmokeGatewayUsesHttpCheckNotHealthStatus:
    """Live-discovered regression: web-ssh-gateway's HEALTHCHECK (M17)
    requires /health's body to report status=="ok", which folds in SSH
    reachability -- unreachable in this step's deliberately standalone
    (no sshd peer) container, so status can only ever be "degraded" and
    docker inspect's Health.Status can never reach "healthy" there, no
    matter how long the loop waits. Confirmed live: the app boots and
    /health already answers 200 within ~30s, but `docker inspect
    --format '{{.State.Health.Status}}'` stayed "starting" for the full
    poll window regardless. mcp-server has a different flake: its 30s
    HEALTHCHECK cadence can race the standalone smoke's 60s poll window
    under runner load. Both standalone checks therefore use direct
    `docker exec ... urlopen(...)` HTTP-200 readiness probes; production
    deploy still enforces the stricter Docker health states separately.
    """

    def test_gateway_check_does_not_use_health_status(self):
        text = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
        lines = text.splitlines()
        gw_section_start = next(
            i for i, line in enumerate(lines) if "Waiting for web-ssh-gateway to answer" in line
        )
        gw_section_end = next(
            i
            for i, line in enumerate(lines[gw_section_start:], start=gw_section_start)
            if "web-ssh-gateway OK" in line
        )
        window = "\n".join(lines[gw_section_start:gw_section_end])
        assert "State.Health.Status" not in window
        assert "docker exec" in window and "urlopen" in window

    def test_mcp_server_check_uses_direct_http_readiness(self):
        """Probe app readiness directly instead of Docker's 30s health cadence."""
        text = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
        lines = text.splitlines()
        mcp_section_start = next(
            i for i, line in enumerate(lines) if "Waiting for mcp-server to answer /healthz" in line
        )
        mcp_section_end = next(
            i
            for i, line in enumerate(lines[mcp_section_start:], start=mcp_section_start)
            if line.strip() == 'echo "mcp-server OK"'
        )
        window = "\n".join(lines[mcp_section_start:mcp_section_end])
        assert "State.Health.Status" not in window
        assert "docker exec" in window and "urlopen" in window and "/healthz" in window


class TestProductionSmokeIsAuthenticatedBlackBox:
    """P1 BLOCKER audit finding: the only post-deploy check was
    wait_docker_health() (docker inspect's own HEALTHCHECK status) --
    process readiness, not proof the actual authenticated API/MCP
    surface works. mcp-server's own HEALTHCHECK in particular hits
    /healthz, deliberately exempt from bearer auth, so it never
    exercised the auth boundary at all.
    """

    def test_gateway_dockerfile_copies_the_smoke_script(self):
        text = GATEWAY_DOCKERFILE.read_text(encoding="utf-8")
        assert "scripts/gateway_black_box_smoke.py" in text

    def test_mcp_server_dockerfile_copies_scripts_dir(self):
        """mcp_black_box_smoke.py ships via the existing wholesale
        `COPY scripts/` -- confirm that copy still exists (a MAJOR
        finding elsewhere in this same audit already covers
        mcp_oauth_healthcheck.py depending on this same copy)."""
        text = MCP_SERVER_DOCKERFILE.read_text(encoding="utf-8")
        assert "COPY --chown=appuser:appuser scripts/ ./scripts/" in text

    def test_smoke_runs_gateway_black_box_check_via_docker_exec(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        assert "docker exec web-ssh-gateway python3 scripts/gateway_black_box_smoke.py" in text

    def test_smoke_runs_mcp_black_box_check_via_docker_exec(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        assert "docker exec mcp-server python3 scripts/mcp_black_box_smoke.py" in text

    def test_black_box_checks_only_run_after_containers_are_healthy(self):
        """No point authenticating against a service that's still
        booting -- must be gated behind the existing health checks, not
        run unconditionally/in parallel with them."""
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        lines = text.splitlines()
        gate_line = next(i for i, line in enumerate(lines) if line.strip() == "if $ok; then")
        gateway_check_line = next(
            i for i, line in enumerate(lines) if "gateway_black_box_smoke.py" in line
        )
        assert gate_line < gateway_check_line


class TestRollbackSchemaCompatibilityVisibility:
    """MAJOR audit finding: rollback reverts the application images but
    never the DB schema (alembic has no downgrade step here, and a real
    auto-downgrade risks data loss) -- the script used to print an
    unqualified "Rollback OK" even when the schema stayed on the newer
    (post-migration) revision, implying full reversion when only the
    application was reverted. A real compatibility gate would require
    running the previous app version against the new schema, which is
    out of scope for a shell script; what it can do is stop claiming
    full success when it didn't achieve it.
    """

    def test_captures_pre_deploy_revision_before_deploying(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        lines = text.splitlines()
        capture_line = next(
            i for i, line in enumerate(lines) if "PRE_DEPLOY_REVISION=" in line
        )
        deploy_line = next(
            i
            for i, line in enumerate(lines)
            if 'deploy_services "$NEW_GATEWAY_IMAGE" "$NEW_MCP_IMAGE"' in line
        )
        assert capture_line < deploy_line, (
            "must read the OLD container's revision before it's replaced by the new deploy"
        )

    def test_rollback_message_distinguishes_schema_not_reverted(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        assert "SCHEMA_ADVANCED" in text
        assert "Rollback PARTIAL" in text
        assert "was NOT reverted" in text

    def test_does_not_attempt_automatic_schema_downgrade(self):
        """A mechanical `alembic downgrade` here would be its own hazard
        (data loss on a migration that dropped/renamed a column) --
        confirm this stays a visibility fix, not an auto-downgrade."""
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        assert "alembic downgrade" not in text


class TestMcpOauthIsPartOfTheDeployPipeline:
    """P0 BLOCKER audit finding: mcp-oauth (the public ChatGPT-facing OAuth
    endpoint) reuses the mcp-server image but was never actually
    redeployed by this script -- deploy_services() only ever recreated
    web-ssh-gateway/mcp-server, so a CI-built image landed in the registry
    with real BUILD_SHA provenance while mcp-oauth kept running whatever
    had last been deployed to it by hand. This is the actual root cause
    the audit observed as "MCP runtime build_sha: unknown".
    """

    def test_deploy_services_recreates_mcp_oauth(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        deploy_fn = text.split("deploy_services() {", 1)[1].split("\n}\n", 1)[0]
        assert "up -d --no-deps --no-build mcp-oauth" in deploy_fn
        # Must reuse the same MCP_SERVER_IMAGE as mcp-server, not a
        # separate/undefined image var -- they share one image by design.
        mcp_oauth_line = next(
            line for line in deploy_fn.splitlines() if "up -d --no-deps --no-build mcp-oauth" in line
        )
        assert "MCP_SERVER_IMAGE=" in mcp_oauth_line

    def test_smoke_waits_for_mcp_oauth_health(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        assert 'wait_docker_health "mcp-oauth"' in text


class TestDeployVerifiesRunningProvenance:
    """P0 BLOCKER audit finding: nothing ever confirmed the container
    running after a deploy is actually the commit that triggered it --
    Gateway/mcp-server/mcp-oauth could all report "healthy" while quietly
    still running stale code (a stuck `up -d`, a shadowing local tag).
    """

    def test_verify_provenance_compares_all_three_containers(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        fn = text.split("verify_provenance() {", 1)[1].split("\n}\n", 1)[0]
        for name in ("web-ssh-gateway", "mcp-server", "mcp-oauth"):
            assert name in fn
        assert "printenv BUILD_SHA" in fn
        assert 'DEPLOY_TAG"' in fn

    def test_verify_provenance_is_skipped_for_the_floating_latest_tag(self):
        """DEPLOY_TAG defaults to "latest" for manual/local invocation --
        a floating tag has no single commit to compare running BUILD_SHA
        against, so the check must no-op rather than false-fail."""
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        fn = text.split("verify_provenance() {", 1)[1].split("\n}\n", 1)[0]
        assert '"$DEPLOY_TAG" = "latest"' in fn

    def test_smoke_calls_verify_provenance_after_black_box_checks(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        lines = text.splitlines()
        mcp_check_line = next(
            i for i, line in enumerate(lines) if "mcp_black_box_smoke.py" in line
        )
        verify_call_line = next(
            i
            for i, line in enumerate(lines)
            if line.strip() == "verify_provenance || ok=false"
        )
        assert mcp_check_line < verify_call_line


class TestMcpOauthServiceCoherence:
    """#26: mcp-oauth reuses the mcp-server image with a different command
    to run the OAuth proxy (examples/mcp_client_remote/server.py) instead
    of the bearer-only entrypoint -- FastMCP's native OAuth and a static
    bearer token can't coexist in one process (see mcp_sse_serve.py's
    _force_fastmcp_auth_unwired()), so this must stay a genuinely separate
    service, not a flag on mcp-server.
    """

    def test_uses_oauth_command_not_bearer_entrypoint(self):
        svc = _load_compose()["services"]["mcp-oauth"]
        assert svc["command"] == ["python", "examples/mcp_client_remote/server.py"]

    def test_auth_mode_is_oauth(self):
        env = _env_dict(_load_compose()["services"]["mcp-oauth"]["environment"])
        assert env.get("MCP_AUTH_MODE") == "oauth"

    def test_has_its_own_dedicated_volume_not_shared_with_mcp_server(self):
        """Sharing mcp_server_tokens would couple the OAuth client/token
        store to the unrelated bearer-only service's -- must be its own
        volume. Only considers actual named Docker volumes (declared in
        the top-level volumes: block) -- a shared read-only bind mount
        like ../projects.yaml is expected and fine.
        """
        compose = _load_compose()
        named_volumes = set(compose["volumes"])

        def named_volume_refs(service_name: str) -> set[str]:
            return {
                v.split(":")[0]
                for v in compose["services"][service_name]["volumes"]
                if ":" in v and v.split(":")[0] in named_volumes
            }

        in_common = named_volume_refs("mcp-server") & named_volume_refs("mcp-oauth")
        assert not in_common, (
            f"mcp-oauth must not share a named volume with mcp-server: {in_common}"
        )
        assert "mcp_oauth_data" in named_volumes

    def test_has_a_readable_ssh_key_bind_mount(self):
        service = _load_compose()["services"]["mcp-oauth"]
        env = _env_dict(service["environment"])
        assert env.get("GATEWAY_SSH_KEY_PATH") == "/app/ssh_key"
        bind_mounts = [v for v in service["volumes"] if isinstance(v, dict) and v.get("type") == "bind"]
        targets = {v["target"] for v in bind_mounts}
        assert "/app/ssh_key" in targets, (
            "GATEWAY_SSH_KEY_PATH=/app/ssh_key is set but nothing bind-mounts a key there"
        )

    def test_client_and_token_store_both_persisted_under_app_data(self):
        env = _env_dict(_load_compose()["services"]["mcp-oauth"]["environment"])
        assert env.get("MCP_TOKEN_STORE_FILE", "").startswith("/app/data/")
        assert env.get("MCP_CLIENT_STORE_FILE", "").startswith("/app/data/")

    def test_healthcheck_uses_dedicated_oauth_script_not_inherited_curl(self):
        """The mcp-server image's baked HEALTHCHECK (curl localhost:8087/
        healthz) checks a port and path this service never listens on --
        examples/mcp_client_remote/server.py has no REST /healthz at all
        (confirmed live: 404 at both the outer proxy and the internal
        FastMCP instance). Compose must override it with a check that
        actually exercises this service's own transport (verified live:
        a real MCP initialize + tools/list handshake against port 8788
        authenticated with MCP_HEALTHCHECK_BEARER_TOKEN).
        """
        svc = _load_compose()["services"]["mcp-oauth"]
        healthcheck = svc.get("healthcheck")
        assert healthcheck is not None, "mcp-oauth must override the inherited image HEALTHCHECK"
        test_cmd = healthcheck["test"]
        assert "mcp_oauth_healthcheck.py" in " ".join(test_cmd)
        assert "curl" not in " ".join(test_cmd)

    def test_healthcheck_bearer_token_env_var_is_wired(self):
        env = _env_dict(_load_compose()["services"]["mcp-oauth"]["environment"])
        assert "MCP_HEALTHCHECK_BEARER_TOKEN" in env


class TestMcpOauthWorkspaceMount:
    """projects.yaml (registry_root: /media/1TB/Python) and
    MCP_GATEWAY_PROJECT_ROOT/MCP_PROJECT_MAP_JSON were all correctly
    configured, but mcp-oauth had no bind mount at all for the actual
    project source trees -- WorkspaceRegistry could resolve config but
    never find a single project's files on disk, so project_list always
    returned count: 0 (confirmed live) regardless of registry content.
    Live-verified after adding the mount: project_list -> 38 projects,
    and the P0 search_text/read_file secret-path fixes still hold against
    the now-reachable real project files (search_text query="PASSWORD"
    against web-ssh-gateway: 48 matches, docker/.env absent from all of
    them; read_file on docker/.env: SECRET_PATH_DENIED).
    """

    def test_workspace_mount_exists(self):
        service = _load_compose()["services"]["mcp-oauth"]
        bind_mounts = [v for v in service["volumes"] if isinstance(v, str) and ":" in v]
        # String-form volumes look like "SRC:DST[:MODE]" -- match on the
        # env var reference itself rather than a resolved host path,
        # since docker-compose.yml never hardcodes the real path.
        assert any("MCP_OAUTH_PROJECT_ROOT" in v for v in bind_mounts), (
            "mcp-oauth must bind-mount the project workspace root "
            "(MCP_OAUTH_PROJECT_ROOT) -- without it WorkspaceRegistry "
            "can resolve project config but never find any project's "
            "actual files on disk"
        )

    def test_workspace_mount_is_read_only(self):
        """User decision (Aug 11 2026): full rights for the OAuth GPT --
        mcp-oauth's workspace mount is deliberately :rw so the external
        ChatGPT agent can write project files directly (workspace_file_*,
        apply_patch). The old :ro boundary lives on only for
        web-ssh-gateway itself, not for mcp-oauth."""
        service = _load_compose()["services"]["mcp-oauth"]
        mount = next(
            v
            for v in service["volumes"]
            if isinstance(v, str) and "MCP_OAUTH_PROJECT_ROOT" in v
        )
        assert mount.endswith(":rw")

    def test_source_and_target_use_the_same_path(self):
        """Source and target must be identical -- projects.yaml's
        registry_root and MCP_PROJECT_MAP_JSON's per-project paths are
        already host-absolute; a different container-side path would
        require translating every one of those configured paths too.
        The mount string is "${VAR:?msg}:${VAR:?msg}:rw" -- the `:?`
        inside each ${...} expression makes a naive split(":") on the
        whole string wrong, so match on the expression appearing twice
        instead.
        """
        service = _load_compose()["services"]["mcp-oauth"]
        mount = next(
            v
            for v in service["volumes"]
            if isinstance(v, str) and "MCP_OAUTH_PROJECT_ROOT" in v
        )
        var_expr = "${MCP_OAUTH_PROJECT_ROOT:?set MCP_OAUTH_PROJECT_ROOT}"
        assert mount == f"{var_expr}:{var_expr}:rw", mount


class TestDeploymentConcurrencySerialization:
    """MAJOR audit finding: ci.yml's deploy job ran scripts/
    deploy-from-registry.sh with no lock of its own -- two close-together
    pushes to master could run two deploys in parallel, both recreating
    the same containers and running `alembic upgrade head` concurrently.
    """

    def test_ci_workflow_has_concurrency_group(self):
        wf = _load_workflow(CI_WORKFLOW_PATH)
        concurrency = wf.get("concurrency")
        assert concurrency is not None, "ci.yml must serialize master deploys via concurrency:"
        assert "${{ github.workflow }}" in concurrency["group"]
        assert "${{ github.ref }}" in concurrency["group"]

    def test_ci_workflow_never_cancels_master_mid_deploy(self):
        """cancel-in-progress must be conditioned off for master specifically
        -- aborting a deploy job mid-flight (as opposed to a PR's test job)
        can leave containers/migrations in a half-applied state."""
        wf = _load_workflow(CI_WORKFLOW_PATH)
        cancel_expr = wf["concurrency"]["cancel-in-progress"]
        assert "refs/heads/master" in str(cancel_expr)

    def test_host_smoke_workflow_has_concurrency_group(self):
        wf = _load_workflow(HOST_SMOKE_WORKFLOW_PATH)
        concurrency = wf.get("concurrency")
        assert concurrency is not None
        assert concurrency.get("cancel-in-progress") is False


class TestE2eSkipsHonestlyWithoutBrowserToolchain:
    """P1 MAJOR audit finding: this workflow also runs on a self-hosted
    Gitea runner pool where the `ubuntu-latest` label maps to a
    Python-focused custom image with no Chrome/Chromium/chromedriver at
    all -- confirmed live: every runner in the pool (python311/node22/
    docker-e2e/security) lacks a browser toolchain. The old mechanism ran
    pytest anyway, let it collect zero items (exit 5), and rewrote that
    into `exit 0` -- a real pass and "nothing could run here" were
    indistinguishable in the job's own status (TEST-15: zero collected
    tests must not read as a passing check). Detecting the toolchain
    first and gating the actual test step behind `if:` means GitHub
    Actions marks it skipped (grey), not passed (green). build-and-push
    now depends on e2e (`needs: [test, e2e]`) -- on runners with a
    browser the e2e job really gates the artifact; on the no-browser
    pool it completes as a skipped step and does not stall deploy.
    """

    def test_browser_presence_is_detected_before_running_tests(self):
        wf = _load_workflow(CI_WORKFLOW_PATH)
        steps_text = json.dumps(wf["jobs"]["e2e"])
        assert "chromedriver" in steps_text
        assert "google-chrome" in steps_text
        assert "browser_check" in steps_text

    def test_e2e_test_step_is_gated_on_browser_availability(self):
        wf = _load_workflow(CI_WORKFLOW_PATH)
        steps = wf["jobs"]["e2e"]["steps"]
        e2e_step = next(s for s in steps if s.get("name") == "E2E tests")
        assert e2e_step.get("if") == "steps.browser_check.outputs.available == 'true'"

    def test_a_real_test_failure_still_fails_the_job(self):
        """No more exit-code rewriting at all -- once the step only runs
        with a confirmed browser present, any nonzero pytest exit
        (failures, errors) propagates as the step's own exit status."""
        wf = _load_workflow(CI_WORKFLOW_PATH)
        steps = wf["jobs"]["e2e"]["steps"]
        e2e_step = next(s for s in steps if s.get("name") == "E2E tests")
        assert e2e_step["run"].strip() == "uv run pytest -m e2e -q"

    def test_build_and_push_depends_on_e2e(self):
        """P1 MAJOR audit finding (CI-06): build/deploy did not depend on
        the e2e job -- unit/static could pass while the e2e gate was
        skipped or failed and the artifact would still advance. Now the
        artifact cannot build before the e2e job has resolved."""
        wf = _load_workflow(CI_WORKFLOW_PATH)
        needs = wf["jobs"]["build-and-push"].get("needs", [])
        assert "e2e" in needs
        assert "test" in needs


class TestE2eActuallyRunsSomewhere:
    """MAJOR audit finding: pytest -m "not host_smoke and not e2e" in
    ci.yml's test job deliberately excludes tests/test_webui_e2e.py's 4
    Selenium tests, and nothing anywhere ever ran them for real."""

    def test_e2e_job_exists_and_runs_the_e2e_marker(self):
        wf = _load_workflow(CI_WORKFLOW_PATH)
        e2e_job = wf["jobs"].get("e2e")
        assert e2e_job is not None, "ci.yml must have a job that runs -m e2e"
        steps_text = json.dumps(e2e_job)
        assert "pytest -m e2e" in steps_text

    def test_e2e_job_puts_chromedriver_on_path(self):
        """The test file does a plain shutil.which("chromedriver") before
        ever touching Selenium -- ubuntu-latest ships ChromeDriver but
        only exposes it via $CHROMEWEBDRIVER, not necessarily PATH."""
        wf = _load_workflow(CI_WORKFLOW_PATH)
        steps_text = json.dumps(wf["jobs"]["e2e"])
        assert "CHROMEWEBDRIVER" in steps_text


class TestHostSmokePathsMatchRealCoverage:
    """MAJOR audit finding: host-smoke.yml's paths: filter didn't match
    what `make host-smoke` (pytest -m host_smoke) actually exercises --
    verified each test file's real source coverage rather than guessing.
    """

    def test_paths_cover_opencode_runner_wrapper_source(self):
        wf = _load_workflow(HOST_SMOKE_WORKFLOW_PATH)
        # `on:` parses as the boolean True key under PyYAML's YAML-1.1
        # rules -- see _load_workflow()'s docstring note.
        paths = wf[True]["push"]["paths"]
        assert "scripts/opencode_runner_wrapper.py" in paths

    def test_paths_cover_compileall_uvx_fallback_source(self):
        wf = _load_workflow(HOST_SMOKE_WORKFLOW_PATH)
        # `on:` parses as the boolean True key under PyYAML's YAML-1.1
        # rules -- see _load_workflow()'s docstring note.
        paths = wf[True]["push"]["paths"]
        assert "examples/mcp_server/mcp_client_tools.py" in paths

    def test_paths_cover_mtls_related_sources(self):
        wf = _load_workflow(HOST_SMOKE_WORKFLOW_PATH)
        # `on:` parses as the boolean True key under PyYAML's YAML-1.1
        # rules -- see _load_workflow()'s docstring note.
        paths = wf[True]["push"]["paths"]
        assert "app/auth_middleware.py" in paths
        assert any("nginx" in p for p in paths)


class TestPrBuildsAndSmokeTestsDockerArtifact:
    """P1 BLOCKER audit finding: a PR's `test` job only ever exercised
    the source tree -- ruff/mypy/pytest against files on disk -- never
    the actual Docker artifact. A broken Dockerfile, missing system
    dependency, bad COPY, or crashing entrypoint would first surface
    after merge (in build-and-push on master) or worse, in prod.
    """

    def test_build_and_push_job_runs_on_pull_request(self):
        wf = _load_workflow(CI_WORKFLOW_PATH)
        job_if = wf["jobs"]["build-and-push"]["if"]
        assert "pull_request" in job_if

    def test_registry_login_and_push_steps_gated_to_real_push_only(self):
        """A PR run must build + smoke-test but never touch the registry
        or :latest for an unmerged commit."""
        wf = _load_workflow(CI_WORKFLOW_PATH)
        steps = wf["jobs"]["build-and-push"]["steps"]
        gated_names = {"Log in to the Gitea container registry", "Push web-ssh-gateway image", "Push mcp-server image"}
        for step in steps:
            if step.get("name") in gated_names:
                assert step.get("if") == "github.event_name == 'push'", (
                    f"{step['name']!r} must be gated to push events only"
                )

    def test_build_and_smoke_test_steps_run_unconditionally(self):
        """The actual new coverage (build + smoke-test) must NOT be
        gated behind push -- that's the whole point of this fix."""
        wf = _load_workflow(CI_WORKFLOW_PATH)
        steps = wf["jobs"]["build-and-push"]["steps"]
        build_and_smoke_names = {
            "Build web-ssh-gateway image",
            "Build mcp-server image",
        }
        found = 0
        for step in steps:
            if step.get("name") in build_and_smoke_names:
                assert "if" not in step, f"{step['name']!r} must not be gated to push-only"
                found += 1
        assert found == len(build_and_smoke_names)


class TestMakeCheckMirrorsCiExactly:
    """MAJOR audit finding (CI-04): `make check`'s pytest invocation and
    ci.yml's were two separately-maintained implementations of "run the
    tests" -- `make check` silently dropped the coverage floor, the
    flaky-test rerun handling, and the collection-count floor, so it
    could pass locally on a change that would fail in CI.

    Fixed at the root instead of just re-syncing the flags a second time:
    ci.yml's steps now call the Makefile targets directly (make lint,
    make mypy, make test-unit, make test-integration, make test-smoke,
    make test-count, make lockfile-check) --
    there is exactly one place these flags are written down, so the two
    can no longer drift apart silently the way they already had once.
    """

    def test_makefile_owns_the_coverage_floor_and_rerun_handling(self):
        makefile = MAKEFILE_PATH.read_text(encoding="utf-8")
        for flag in ("--cov-fail-under=69", "--reruns 2", "--only-rerun 'WebSocketDisconnect'"):
            assert flag in makefile, f"Makefile missing {flag!r}"

    def test_ci_test_step_calls_make_test_unit_not_a_duplicate_pytest_invocation(self):
        wf = _load_workflow(CI_WORKFLOW_PATH)
        steps = wf["jobs"]["test"]["steps"]
        step = next(s for s in steps if s.get("name") == "Tests + coverage")
        assert step["run"].strip().startswith("make test-unit")
        # The flags themselves must NOT be duplicated here -- if they are,
        # the two really have drifted apart again.
        for flag in ("--cov-fail-under=69", "--reruns 2"):
            assert flag not in step["run"]

    def test_ci_lint_mypy_lockfile_testcount_steps_call_make_targets(self):
        wf = _load_workflow(CI_WORKFLOW_PATH)
        steps = {s.get("name"): s.get("run", "").strip() for s in wf["jobs"]["test"]["steps"]}
        assert steps.get("Lockfile freshness") == "make lockfile-check"
        assert steps.get("Ruff") == "make lint"
        assert steps.get("Mypy") == "make mypy"
        assert steps.get("Integration tests") == "make test-integration"
        assert steps.get("Portable smoke tests") == "make test-smoke"
        assert steps.get("Verify test count") == "make test-count"

    def test_make_check_verifies_lockfile_and_test_count(self):
        makefile = MAKEFILE_PATH.read_text(encoding="utf-8")
        assert "check: lockfile-check lint mypy compileall test test-count" in makefile
        assert "uv lock --check" in makefile
        assert "3500" in makefile


class TestCanonicalCiEntrypoints:
    """P1 MAJOR audit finding (TEST-08/CI-04): no canonical top-level
    `ci`/`test-unit`/`test-integration`/`test-smoke` commands existed --
    only `test` (unit tests) and `check` (the full local gate), with no
    dedicated name for integration or smoke runs and nothing named `ci`
    at all.
    """

    def test_canonical_targets_exist(self):
        makefile = MAKEFILE_PATH.read_text(encoding="utf-8")
        for target in ("ci:", "test-unit:", "test-integration:", "test-smoke:"):
            assert target in makefile, f"Makefile missing canonical target {target!r}"

    def test_ci_target_runs_the_full_local_gate(self):
        makefile = MAKEFILE_PATH.read_text(encoding="utf-8")
        assert "ci: check test-integration test-smoke" in makefile

    def test_test_is_a_backward_compatible_alias_for_test_unit(self):
        makefile = MAKEFILE_PATH.read_text(encoding="utf-8")
        assert "test: test-unit" in makefile

    def test_test_smoke_is_portable_and_host_smoke_stays_separate(self):
        """Canonical CI has portable smoke; live host checks stay separate."""
        makefile = MAKEFILE_PATH.read_text(encoding="utf-8")
        assert "test-smoke:" in makefile
        assert "uv run pytest -m smoke -q" in makefile
        assert "host-smoke:" in makefile
        assert "uv run pytest -m host_smoke -v" in makefile
        assert "test-smoke: host-smoke" not in makefile



class TestSshdVersionedArtifact:
    """The executor image must be built, smoke-tested and pushed by CI."""

    def test_sshd_dockerfile_carries_build_provenance(self):
        text = SSHD_DOCKERFILE.read_text(encoding="utf-8")
        assert "ARG BUILD_SHA=unknown" in text
        assert "ARG BUILD_TIME=unknown" in text
        assert "ENV BUILD_SHA=${BUILD_SHA} BUILD_TIME=${BUILD_TIME}" in text

    def test_ci_builds_and_smoke_tests_sshd_executor_image(self):
        wf = _load_workflow(CI_WORKFLOW_PATH)
        steps = wf["jobs"]["build-and-push"]["steps"]
        build = next(s for s in steps if s.get("name") == "Build sshd executor image")
        assert "docker/sshd/Dockerfile" in build["run"]
        assert "ssh-gateway-sshd:${{ github.sha }}" in build["run"]
        smoke = next(
            s
            for s in steps
            if s.get("name") == "Smoke-test built images (before pushing, on a push; standalone, on a PR)"
        )
        assert "ssh-gateway-sshd:${{ github.sha }}" in smoke["run"]
        assert "sshd -t" in smoke["run"]

    def test_ci_pushes_sshd_only_on_push_events(self):
        wf = _load_workflow(CI_WORKFLOW_PATH)
        steps = wf["jobs"]["build-and-push"]["steps"]
        push = next(s for s in steps if s.get("name") == "Push sshd executor image")
        assert push.get("if") == "github.event_name == 'push'"
        assert "ssh-gateway-sshd:${{ github.sha }}" in push["run"]
        assert "ssh-gateway-sshd:latest" in push["run"]

    def test_sshd_dockerfile_installs_ripgrep(self):
        """The agent's grep tool shells out to `rg` on the SSH target, so
        the executor image must install ripgrep in its apk add line."""
        text = SSHD_DOCKERFILE.read_text(encoding="utf-8")
        apk_line = next(line for line in text.splitlines() if "apk add" in line)
        assert "ripgrep" in apk_line


class TestAgentExecutorDataRoot:
    """The named agent-data volume must inherit an executor-writable owner.

    Docker initializes a fresh named volume from the image path it covers.
    Pre-creating /var/lib/mcp-agent as mcpuser prevents the persistent task/
    workspace volume from becoming another root-owned write dead-end.
    """

    def test_sshd_image_precreates_agent_data_root_for_mcpuser(self):
        text = SSHD_DOCKERFILE.read_text(encoding="utf-8")
        assert "mkdir -p /var/lib/mcp-agent/state /var/lib/mcp-agent/workspaces" in text
        assert "chown -R mcpuser:mcpuser /var/lib/mcp-agent" in text


class TestRedisTrustBoundary:
    """P1 Redis trust boundary: Redis must move off the shared external
    internal_net onto a project-private internal network, require a password,
    never put that password into argv or application logs, and only the
    gateway may share the private network with it.
    """

    def test_redis_not_on_shared_external_internal_net(self):
        assert "internal_net" not in _load_compose()["services"]["redis"]["networks"]

    def test_redis_net_is_internal_and_project_private(self):
        redis_net = _load_compose()["networks"]["redis_net"]
        assert redis_net.get("internal") is True
        assert not redis_net.get("external", False)

    def test_only_redis_and_gateway_attach_to_redis_net(self):
        compose = _load_compose()
        members = {
            name
            for name, svc in compose["services"].items()
            if "redis_net" in (svc.get("networks") or {})
        }
        assert members == {"redis", "web-ssh-gateway"}

    def test_gateway_keeps_shared_network_and_gains_private_redis_net(self):
        networks = _load_compose()["services"]["web-ssh-gateway"]["networks"]
        assert "internal_net" in networks
        assert "redis_net" in networks

    def test_redis_container_env_requires_password(self):
        env = _env_dict(_load_compose()["services"]["redis"]["environment"])
        assert env.get("REDIS_PASSWORD") == "${REDIS_PASSWORD:?REDIS_PASSWORD is required}"

    def test_redis_command_sources_requirepass_from_container_env(self):
        command = _load_compose()["services"]["redis"]["command"]
        requirepass_value = command.split("--requirepass", 1)[1].split()[0].strip('"')
        assert requirepass_value == "$$REDIS_PASSWORD", (
            "redis must start with --requirepass from the (Compose-escaped) "
            "container env var, not a hardcoded literal"
        )

    def test_redis_command_has_no_literal_secret(self):
        command = _load_compose()["services"]["redis"]["command"]
        requirepass_value = command.split("--requirepass", 1)[1].split()[0].strip('"')
        assert requirepass_value.startswith("$$")

    def test_redis_healthcheck_auths_via_redisauth_env_not_argv(self):
        joined = " ".join(_load_compose()["services"]["redis"]["healthcheck"]["test"])
        assert "REDISCLI_AUTH" in joined
        assert "$$REDIS_PASSWORD" in joined
        assert "redis-cli -a" not in joined
        assert "redis-cli --pass" not in joined

    def test_gateway_redis_url_requires_password(self):
        env = _env_dict(_load_compose()["services"]["web-ssh-gateway"]["environment"])
        url = env["REDIS_URL"]
        assert "REDIS_PASSWORD" in url
        assert ":?" in url, "gateway REDIS_URL must require REDIS_PASSWORD, not default it"
        assert url.startswith("redis://:${REDIS_PASSWORD")
        assert url.endswith("@redis:6379/0")

    def test_env_example_documents_urlsafe_token_hex_redis_password(self):
        text = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        assert "REDIS_PASSWORD=" in text
        assert "token_hex" in text
        assert "URL-safe" in text or "url-safe" in text
        for line in text.splitlines():
            if line.startswith("REDIS_PASSWORD="):
                value = line.split("=", 1)[1].strip()
                assert value.startswith("change-me"), (
                    f"REDIS_PASSWORD in docker/.env.example looks like a real secret: {value!r}"
                )
                return
        raise AssertionError("REDIS_PASSWORD not found in docker/.env.example")

    def test_redis_queue_connect_log_does_not_expose_url(self):
        text = (ROOT / "app" / "redis_queue.py").read_text(encoding="utf-8")
        lines = text.splitlines()
        log_line = next(line for line in lines if "Connected to Redis" in line)
        assert "redis_url" not in log_line
        assert "redis://" not in log_line


class TestOpenCodeProductionAdmission:
    def test_sshd_has_bounded_headroom_for_fleet(self):
        services = _load_compose()["services"]
        resources = services["agent-sshd"]["deploy"]["resources"]
        assert resources["limits"]["memory"] == "16G"
        assert resources["reservations"]["memory"] == "128M"

    def test_oauth_enables_durable_fleet_admission(self):
        env = _load_compose()["services"]["mcp-oauth"]["environment"]
        values = {item.split("=", 1)[0]: item.split("=", 1)[1] for item in env}
        assert values["MCP_AGENT_FLEET_ENABLED"] == "${MCP_AGENT_FLEET_ENABLED:-true}"
        assert values["MCP_AGENT_FLEET_CAPACITY"] == "${MCP_AGENT_FLEET_CAPACITY:-64}"
        assert values["MCP_AGENT_FLEET_POOL"] == "${MCP_AGENT_FLEET_POOL:-ssh-gateway/agent-sshd}"

    def test_proxy_is_fail_closed_for_builder_and_executor(self):
        services = _load_compose()["services"]
        for service in ("agent-sshd", "mcp-oauth"):
            env = services[service]["environment"]
            assert "OPENCODE_PROXY_REQUIRED=${OPENCODE_PROXY_REQUIRED:-true}" in env

    def test_dynamic_startup_reservation_is_configured(self):
        env = _load_compose()["services"]["mcp-oauth"]["environment"]
        assert "OPENCODE_STARTUP_RESERVE_BYTES=${OPENCODE_STARTUP_RESERVE_BYTES:-805306368}" in env
        assert "OPENCODE_STARTUP_RESERVE_SECONDS=${OPENCODE_STARTUP_RESERVE_SECONDS:-60}" in env


class TestDeploySourceIsolation:
    def test_deploy_uses_exact_workflow_checkout(self):
        workflow = _load_workflow(CI_WORKFLOW_PATH)
        steps = workflow["jobs"]["deploy"]["steps"]
        assert any(step.get("uses") == "actions/checkout@v7" for step in steps)

    def test_deploy_does_not_mount_mutable_host_checkout(self):
        text = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
        assert "/media/1TB/Python/web_ssh/web-ssh-gateway:/deploy/web-ssh-gateway" not in text
        assert "/docker/.env:/deploy/web-ssh-gateway.env:ro" in text
        assert "/.state:/deploy/web-ssh-gateway-state" in text

    def test_runtime_inputs_are_linked_into_ephemeral_checkout(self):
        text = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
        assert "ln -s /deploy/web-ssh-gateway.env docker/.env" in text
        assert "ln -s /deploy/web-ssh-gateway-state .state" in text
        assert "bash scripts/deploy-from-registry.sh" in text

class TestAgentExecutorIsPartOfTheDeployPipeline:
    def test_deploy_services_recreates_agent_sshd_with_pinned_executor_image(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        deploy_fn = text.split("deploy_services() {", 1)[1].split("\n}\n", 1)[0]
        line = next(line for line in deploy_fn.splitlines() if "up -d --no-deps --no-build agent-sshd" in line)
        assert 'SSH_GATEWAY_SSHD_IMAGE="$sshd_image"' in line

    def test_smoke_requires_agent_sshd_health(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        assert 'wait_docker_health "ssh-gateway-agent-sshd" ssh-gateway-agent-sshd 120' in text

    def test_executor_memory_gate_applies_to_both_sshd_containers(self):
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        fn = text.split("wait_docker_health() {", 1)[1].split("\n}\n", 1)[0]
        assert '"ssh-gateway-sshd"' in fn
        assert '"ssh-gateway-agent-sshd"' in fn
        assert "17179869184" in fn
