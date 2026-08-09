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
MCP_SERVER_DOCKERFILE = ROOT / "docker" / "Dockerfile.mcp-server"
GATEWAY_DOCKERFILE = ROOT / "docker" / "Dockerfile"
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
    poll window regardless. mcp-server's own HEALTHCHECK has no such
    coupling and is unaffected -- only the gateway's check needed to
    change, to a direct `docker exec ... urlopen(...)` HTTP-200 check
    matching this step's own original stated intent ("confirm it boots
    and answers its own health path with HTTP 200").
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

    def test_mcp_server_check_still_uses_health_status(self):
        """Unaffected -- mcp-server's own HEALTHCHECK has no SSH/Redis
        coupling and reports healthy fine standalone."""
        text = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
        lines = text.splitlines()
        mcp_section_start = next(
            i for i, line in enumerate(lines) if "Waiting for mcp-server to report healthy" in line
        )
        mcp_section_end = next(
            i
            for i, line in enumerate(lines[mcp_section_start:], start=mcp_section_start)
            if line.strip() == 'echo "mcp-server OK"'
        )
        window = "\n".join(lines[mcp_section_start:mcp_section_end])
        assert "State.Health.Status" in window


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
    """MAJOR audit finding: `make check`'s pytest invocation and ci.yml's
    were two separately-maintained implementations of "run the tests" --
    `make check` silently dropped the coverage floor, the flaky-test
    rerun handling, and the collection-count floor, so it could pass
    locally on a change that would fail in CI.
    """

    def test_make_test_matches_ci_coverage_floor(self):
        makefile = MAKEFILE_PATH.read_text(encoding="utf-8")
        ci = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
        assert "--cov-fail-under=69" in makefile
        assert "--cov-fail-under=69" in ci

    def test_make_test_matches_ci_rerun_handling(self):
        makefile = MAKEFILE_PATH.read_text(encoding="utf-8")
        ci = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
        for flag in ("--reruns 2", "--only-rerun 'WebSocketDisconnect'"):
            assert flag in makefile, f"Makefile missing {flag!r}"
            assert flag in ci, f"ci.yml missing {flag!r}"

    def test_make_check_verifies_lockfile_and_test_count(self):
        makefile = MAKEFILE_PATH.read_text(encoding="utf-8")
        assert "check: lockfile-check lint mypy compileall test test-count" in makefile
        assert "uv lock --check" in makefile
        assert "3500" in makefile
