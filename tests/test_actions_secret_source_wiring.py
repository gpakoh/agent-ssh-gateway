from pathlib import Path

CI_WORKFLOW = Path(".github/workflows/ci.yml")
COMPOSE = Path("docker/docker-compose.yml")


def test_deploy_provisions_actions_secret_source_volume_without_exposing_host_path():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    compose = COMPOSE.read_text(encoding="utf-8")

    assert "MCP_ACTIONS_SECRET_SOURCE_VOLUME: mcp-actions-secret-sources" in workflow
    assert "- name: Provision approved Actions secret source volume" in workflow
    assert "source_file=/deploy/secrets/registry.token" in workflow
    assert 'docker volume create "$source_volume"' in workflow
    assert '-v "$source_volume:/secret-sources"' in workflow
    assert "chown 1000:1000" in workflow
    assert "chmod 400" in workflow
    assert "mv -f \"$tmp\" /secret-sources/registry_token" in workflow

    assert "actions_secret_sources:/run/mcp-secret-sources:ro" in compose
    assert "name: ${MCP_ACTIONS_SECRET_SOURCE_VOLUME:-mcp-actions-secret-sources}" in compose
    assert "/media/1TB/Python/quart-platform/infra-quart/secrets/registry.token" not in compose
    assert "/deploy/secrets/registry.token" not in compose


def test_secret_source_volume_is_not_mounted_into_non_admin_mcp_server():
    compose = COMPOSE.read_text(encoding="utf-8")
    mcp_server, mcp_oauth_and_after = compose.split("  mcp-oauth:", maxsplit=1)

    assert "/run/mcp-secret-sources" not in mcp_server
    assert "actions_secret_sources:/run/mcp-secret-sources:ro" in mcp_oauth_and_after
