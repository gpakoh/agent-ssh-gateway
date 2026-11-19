"""No side-effect one-shot Compose contract policy regressions (P1 #879)."""
from __future__ import annotations

import json
from copy import deepcopy

import pytest

from examples.mcp_server.mcp_infra.compose_one_shot_policy import (
    OneShotContractError,
    attest_compose_config,
    load_contract,
    parse_catalog_stdout,
)

IMAGE = "registry.example/org/postgres@sha256:" + "a" * 64


def _spec() -> dict:
    return {
        "project_id": "infra-quart",
        "compose_project": "infra-quart",
        "service": "postgres",
        "image": IMAGE,
        "network": "internal",
        "network_name": "infra-quart_internal",
        "env_names": ["PGUSER", "PGPASSWORD", "PGDATABASE"],
        "volume_sources": {"/var/lib/postgresql/data": "pgdata"},
        "operations": {
            "readiness": ["pg_isready", "--timeout=3"],
            "migration": ["alembic", "upgrade", "head"],
            "catalog_schemas": "postgres_catalog",
            "catalog_tables": "postgres_catalog",
        },
    }


def _env(spec=None) -> dict:
    return {
        "MCP_COMPOSE_ONE_SHOT_CONTRACTS": json.dumps(
            {"version": 1, "contracts": {"pg-ops": _spec() if spec is None else spec}}
        )
    }


def _config() -> dict:
    return {
        "name": "infra-quart",
        "networks": {"internal": {"name": "infra-quart_internal", "internal": True}},
        "volumes": {"pgdata": {"name": "infra-quart_pgdata"}},
        "services": {
            "postgres": {
                "image": IMAGE,
                "networks": {"internal": None},
                "environment": {
                    "PGUSER": "postgres",
                    "PGPASSWORD": "confidential-value",
                    "PGDATABASE": "app",
                },
                "volumes": [
                    {"type": "volume", "source": "pgdata", "target": "/var/lib/postgresql/data"}
                ],
                "ports": [],
                "privileged": False,
            }
        },
    }


def test_default_denied_no_operator_allowlist():
    with pytest.raises(OneShotContractError, match="unavailable"):
        load_contract("pg-ops", "readiness", environ={})


def test_readiness_contract_and_opaque_policy_hmac_never_expose_secret():
    plan = load_contract("pg-ops", "readiness", environ=_env())
    config = _config()
    fingerprint = attest_compose_config(plan, config)
    assert len(fingerprint) == 64
    assert "confidential-value" not in repr(plan)
    assert "confidential-value" not in fingerprint
    changed = deepcopy(config)
    changed["services"]["postgres"]["environment"]["PGPASSWORD"] = "another-value"
    assert attest_compose_config(plan, changed) != fingerprint


@pytest.mark.parametrize("mode", ["catalog_schemas", "catalog_tables"])
def test_catalog_modes_use_fixed_read_only_sql_not_caller_sql(mode):
    plan = load_contract("pg-ops", mode, environ=_env())
    argv = plan["argv"]
    assert argv[0] == "psql"
    assert "BEGIN TRANSACTION READ ONLY" in argv[-1]
    assert "LIMIT 100" in argv[-1]
    assert "COMMIT" in argv[-1]


@pytest.mark.parametrize(("mutator", "reason"), [
    (lambda s: s.update(image="postgres:latest"), "immutable digest"),
    (lambda s: s.update(service="../../other"), "service"),
    (lambda s: s.update(env_names=["PGPASSWORD", "PGPASSWORD"]), "env-name"),
    (lambda s: s.update(operations={"migration": ["sh", "-c", "echo"]}), "shell"),
    (lambda s: s.update(operations={"migration": ["python", "-c", "pass"]}), "inline"),
    (lambda s: s.update(operations={"migration": "unsafe"}), "command list"),
    (lambda s: s.update(operations={"catalog_tables": ["psql", "-c", "DROP TABLE"]}), "catalog"),
])
def test_untrusted_operator_contract_is_rejected_before_action(mutator, reason):
    spec = _spec()
    mutator(spec)
    mode = "migration" if "migration" in spec["operations"] else "catalog_tables"
    if "catalog_tables" in spec["operations"] and isinstance(spec["operations"]["catalog_tables"], list):
        mode = "catalog_tables"
    with pytest.raises(OneShotContractError, match=reason):
        load_contract("pg-ops", mode, environ=_env(spec))


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("name", "other", "project"),
    ("image", "different", "image"),
    ("network", "public", "network"),
    ("env", "OTHER", "environment"),
    ("volume", "/var/run/docker.sock", "volume"),
    ("privileged", True, "privileges"),
    ("network_mode", "host", "privileges"),
    ("ports", [{"target": 5432, "published": 5432}], "ports"),
])
def test_compose_identity_enforced_before_dangerous_confirmation(field, value, reason):
    plan = load_contract("pg-ops", "readiness", environ=_env())
    config = _config()
    if field == "name":
        config["name"] = value
    else:
        service = config["services"]["postgres"]
        if field == "image":
            service["image"] = value
        elif field == "network":
            service["networks"] = {value: None}
        elif field == "env":
            service["environment"][value] = "confidential-extra"
        elif field == "volume":
            service["volumes"][0]["target"] = value
        elif field == "privileged":
            service["privileged"] = True
        elif field == "network_mode":
            service["network_mode"] = value
        else:
            service["ports"] = value
    with pytest.raises(OneShotContractError, match=reason) as exc:
        attest_compose_config(plan, config)
    assert "confidential" not in str(exc.value)


@pytest.mark.parametrize("payload", [
    "public\nusers\n",
    "BEGIN\npublic\nCOMMIT\n",
    "public\napp.sessions\n",
])
def test_catalog_parsing_returns_only_identifier_rows(payload):
    assert parse_catalog_stdout(payload)


@pytest.mark.parametrize("payload", [
    "PGPASSWORD=confidential-value",
    "http://example.invalid",
    "public\nhello world",
    "public\n" * 102,
])
def test_catalog_rejects_untrusted_output_without_echoing_value(payload):
    with pytest.raises(OneShotContractError) as exc:
        parse_catalog_stdout(payload)
    assert "confidential" not in str(exc.value)


@pytest.mark.parametrize(("property_name", "property_value"), [
    ("entrypoint", ["/bin/sh", "-c", "printenv"]),
    ("build", {"context": "."}),
    ("secrets", [{"source": "db_secret", "target": "password"}]),
    ("configs", [{"source": "root-config", "target": "/etc/config"}]),
    ("env_file", ["/root/.env"]),
    ("volumes_from", ["other-service"]),
    ("tmpfs", ["/host"]),
    ("userns_mode", "host"),
    ("links", ["other-service"]),
    ("extends", {"service": "other-service"}),
    ("restart", "always"),
])
def test_compose_policy_rejects_privileged_or_hidden_inheritance(
    property_name, property_value,
):
    plan = load_contract("pg-ops", "readiness", environ=_env())
    config = _config()
    config["services"]["postgres"][property_name] = property_value
    with pytest.raises(OneShotContractError, match="unsafe runtime") as exc:
        attest_compose_config(plan, config)
    assert "confidential-value" not in str(exc.value)


@pytest.mark.parametrize(("target", "source"), [
    ("/var/lib/postgresql/data", "unreviewed"),
    ("/var/run/docker.sock", "pgdata"),
])
def test_compose_policy_rejects_unreviewed_volume_sources(target, source):
    plan = load_contract("pg-ops", "readiness", environ=_env())
    config = _config()
    config["services"]["postgres"]["volumes"][0].update(target=target, source=source)
    with pytest.raises(OneShotContractError, match="volume"):
        attest_compose_config(plan, config)


def test_compose_policy_rejects_external_volume_and_untrusted_physical_network():
    plan = load_contract("pg-ops", "readiness", environ=_env())
    for change in ("external", "physical_network", "non_internal"):
        config = _config()
        if change == "external":
            config["volumes"]["pgdata"]["external"] = True
        elif change == "physical_network":
            config["networks"]["internal"]["name"] = "production_public"
        else:
            config["networks"]["internal"]["internal"] = False
        with pytest.raises(OneShotContractError):
            attest_compose_config(plan, config)


@pytest.mark.parametrize("unproven", [None, "", 123])
def test_compose_does_not_accept_declared_but_unavailable_secret(unproven):
    plan = load_contract("pg-ops", "readiness", environ=_env())
    config = _config()
    config["services"]["postgres"]["environment"]["PGPASSWORD"] = unproven
    with pytest.raises(OneShotContractError, match="environment values are unavailable"):
        attest_compose_config(plan, config)
