"""Docker cache cleanup must remain visible to the informational pack scanner."""

import pytest

from app.command_policy import (
    DOCKER_ADMIN_ALLOWED_ACTIONS,
    OPS_ALLOWED_DOCKER_ACTIONS,
    Severity,
    SuggestionKind,
    _validate_docker_action,
    evaluate_default,
    scan_command,
)
from app.packs import PackRegistry
from app.packs import registry as pack_registry
from app.packs.docker import build_docker_pack


@pytest.fixture
def registry() -> PackRegistry:
    result = PackRegistry()
    result.register(build_docker_pack())
    return result


@pytest.mark.parametrize("command", [
    "docker buildx prune",
    "docker buildx prune -a -f",
    "docker buildx --builder shared prune -f",
    "docker buildx --builder=shared prune --all",
    "docker buildx --builder builder prune",
    "docker buildx --builder=builder prune",
    "docker --context ci buildx prune -f",
    "docker --context=ci buildx --builder shared prune -f",
    "docker --host ssh://ci-host buildx --debug prune",
    "docker --config '/tmp/ci config' --context ci buildx prune",
    "docker -c ci buildx -D prune",
    "docker -Hunix:///var/run/docker.sock buildx prune",
    "/usr/bin/docker buildx prune",
    "docker.exe buildx prune",
    "DOCKER BUILDX PRUNE",
])
def test_buildx_prune(registry: PackRegistry, command: str) -> None:
    matches = registry.evaluate_pack("docker", command)
    assert [m.pattern_name for m in matches] == ["buildx-prune"]
    assert matches[0].severity == Severity.MEDIUM
    assert matches[0].suggestions[0].kind == SuggestionKind.PREVIEW_FIRST
    assert matches[0].suggestions[0].command == "docker buildx du"


@pytest.mark.parametrize("command", [
    "docker buildx --builder prune inspect",
    "docker buildx --builder=prune inspect",
    "docker buildx --builder builder inspect",
    "docker --context builder buildx inspect",
    "docker buildx inspect prune",
    "docker buildx ls",
    "docker buildx du --builder shared",
    "docker buildx build --tag prune .",
    "docker buildx prune-extra",
    "docker builder prune-extra",
    "docker ps; echo builder prune",
    "docker ps; echo buildx prune",
    "docker-compose buildx prune",
])
def test_non_prune_is_not_cache_cleanup(registry: PackRegistry, command: str) -> None:
    matches = registry.evaluate_pack("docker", command)
    assert not {"builder-prune", "buildx-prune"} & {m.pattern_name for m in matches}


@pytest.mark.parametrize("command, name", [
    ("docker builder prune", "builder-prune"),
    ("docker builder prune -a -f", "builder-prune"),
    ("docker --context ci builder prune", "builder-prune"),
    ("docker --context buildx builder prune", "builder-prune"),
    ("docker --config /tmp/conf --host ssh://ci-host builder prune", "builder-prune"),
    ("docker system prune -a", "system-prune"),
    ("docker volume prune -f", "volume-prune"),
    ("docker network prune -f", "network-prune"),
    ("docker image prune -a", "image-prune"),
    ("docker container prune -f", "container-prune"),
    ("docker compose down --volumes", "down-volumes"),
])
def test_existing_cleanup_detection(registry: PackRegistry, command: str, name: str) -> None:
    assert name in {m.pattern_name for m in registry.evaluate(command)}


@pytest.mark.parametrize("subcommand, expected", [
    ("ps", set()),
    ("builder prune", {"builder-prune"}),
    ("buildx --builder shared prune", {"buildx-prune"}),
])
def test_long_global_option_sequence(
    registry: PackRegistry, subcommand: str, expected: set[str],
) -> None:
    command = "docker " + "-H=unix:///var/run/docker.sock " * 64 + subcommand
    assert {m.pattern_name for m in registry.evaluate_pack("docker", command)} == expected


@pytest.mark.parametrize("command, expected", [
    ("docker \\\n  builder prune", {"builder-prune"}),
    ("docker \\\n  buildx prune", {"buildx-prune"}),
    ("docker buildx \\\n  --builder shared prune", {"buildx-prune"}),
    ("docker --context ci \\\n  builder prune", {"builder-prune"}),
    ("docker --context \\\n  ci buildx prune", {"buildx-prune"}),
    ("docker --context=ci\\\n  buildx prune", {"buildx-prune"}),
    ("docker buildx --builder=shared\\\n  prune", {"buildx-prune"}),
    ("docker buildx --builder ci \\\n  prune", {"buildx-prune"}),
    ("docker builder \\\n  prune", {"builder-prune"}),
    ("docker\\\nbuildx prune", set()),
    ("docker buildx\\\nprune", set()),
    ("docker\n  builder prune", set()),
    ("docker buildx --builder shared\n  prune", set()),
    ("docker buildx --builder prune \\\n  inspect", set()),
    ("docker --context ci;\n  buildx prune", set()),
])
def test_shell_line_continuation(
    registry: PackRegistry, command: str, expected: set[str],
) -> None:
    findings = registry.evaluate_pack("docker", command)
    assert {m.pattern_name for m in findings} & {"builder-prune", "buildx-prune"} == expected


@pytest.mark.parametrize("command, expected", [
    ("docker buildx prune", {"buildx-prune"}),
    ("docker \\\n  buildx prune", {"buildx-prune"}),
    ("docker buildx --builder prune inspect", set()),
    ("docker --context ci;\n  buildx prune", set()),
    ("tool=docker; $tool buildx prune", {"buildx-prune"}),
    ("bash -c 'tool=docker; $tool buildx prune'", {"buildx-prune"}),
    ("bash -c 'docker \\\n  buildx prune'", {"buildx-prune"}),
    ("sh <<'EOF'\ndocker \\\n  builder prune\nEOF", {"builder-prune"}),
    ("echo $(docker buildx prune)", {"buildx-prune"}),
    ("docker builder prune; docker buildx prune", {"builder-prune", "buildx-prune"}),
])
def test_scanner_report(
    registry: PackRegistry, monkeypatch: pytest.MonkeyPatch, command: str, expected: set[str],
) -> None:
    # Exercise actual normalization and report construction, scoped to Docker.
    monkeypatch.setattr(pack_registry, "get_registry", lambda: registry)
    report = scan_command(command)
    names = [finding.pattern_name for finding in report.findings]
    assert set(names) == expected
    assert report.total == len(names) == len(expected)
    for finding in report.findings:
        assert finding.severity == "medium"
        assert finding.suggestions[0]["kind"] == "preview_first"


@pytest.mark.parametrize("command, allowed", [
    ("docker buildx prune", False),
    ("docker --context ci buildx --builder shared prune", False),
    ("docker buildx --builder prune inspect", True),
    ("docker buildx du", True),
    ("docker builder prune", False),
])
def test_default_profile_cache_cleanup(
    registry: PackRegistry, monkeypatch: pytest.MonkeyPatch, command: str, allowed: bool,
) -> None:
    monkeypatch.setattr(pack_registry, "get_registry", lambda: registry)
    actual, reason = evaluate_default(command, "docker")
    assert actual is allowed
    if not allowed:
        assert "Destructive pattern blocked" in reason


@pytest.mark.parametrize("actions", [OPS_ALLOWED_DOCKER_ACTIONS, DOCKER_ADMIN_ALLOWED_ACTIONS])
def test_buildx_does_not_expand_action_allowlists(actions: set[str]) -> None:
    allowed, reason = _validate_docker_action(["docker", "buildx", "prune"], actions)
    assert not allowed
    assert "not allowed" in reason
