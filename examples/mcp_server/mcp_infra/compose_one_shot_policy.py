"""Operator-owned Compose one-shot policy; no caller-controlled Docker argv.

The allowlist is an operator-supplied JSON object with bounded contracts and
operations. Parsing, service verification and signatures never return secret
environment values, even when the Compose config includes interpolated env.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from typing import Any

_POLICY_KEY = secrets.token_bytes(32)
_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,63}\Z")
_DIGEST_RE = re.compile(r"[A-Za-z0-9_.:/-]{1,320}@sha256:[0-9a-f]{64}\Z")
_MAX_POLICY_BYTES = 16384
_ALLOWED_MODES = {"readiness", "migration", "catalog_schemas", "catalog_tables"}
_FORBIDDEN_ENTRYPOINTS = {"sh", "bash", "ash", "dash", "zsh", "env", "printenv", "sudo"}
_CATALOG_QUERIES = {
    "catalog_schemas": (
        "BEGIN TRANSACTION READ ONLY; "
        "SELECT nspname FROM pg_catalog.pg_namespace "
        "WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' "
        "ORDER BY nspname LIMIT 100; COMMIT;"
    ),
    "catalog_tables": (
        "BEGIN TRANSACTION READ ONLY; "
        "SELECT schemaname || '.' || tablename FROM pg_catalog.pg_tables "
        "WHERE schemaname !~ '^pg_' AND schemaname <> 'information_schema' "
        "ORDER BY schemaname, tablename LIMIT 100; COMMIT;"
    ),
}


class OneShotContractError(ValueError):
    """Invalid or unproven one-shot authority; caller never sees raw config."""


def _ident(value: Any, label: str, *, env: bool = False) -> str:
    pattern = _ENV_NAME_RE if env else _NAME_RE
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise OneShotContractError(f"{label} is invalid")
    return value


def _argv(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 24:
        raise OneShotContractError("operator command list is missing or oversized")
    if any(
        not isinstance(item, str)
        or not 1 <= len(item) <= 256
        or any(ord(c) < 32 or ord(c) == 127 for c in item)
        for item in value
    ):
        raise OneShotContractError("operator command argv has unsafe entries")
    basename = value[0].rsplit("/", 1)[-1]
    if basename in _FORBIDDEN_ENTRYPOINTS or basename.startswith("-"):
        raise OneShotContractError("shell or environment export commands are forbidden")
    if basename.startswith("python") and "-c" in value:
        raise OneShotContractError("inline interpreter source is forbidden")
    return tuple(value)


def load_contract(contract: str, mode: str, *, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Read a bounded operator-owned mapping; no fallback to arbitrary caller paths."""
    _ident(contract, "contract")
    if mode not in _ALLOWED_MODES:
        raise OneShotContractError("unsupported operation")
    env = os.environ if environ is None else environ
    raw = env.get("MCP_COMPOSE_ONE_SHOT_CONTRACTS", "")
    if not raw or len(raw.encode("utf-8")) > _MAX_POLICY_BYTES:
        raise OneShotContractError("one-shot operator allowlist is unavailable")
    try:
        catalog = json.loads(raw)
    except (ValueError, TypeError):
        raise OneShotContractError("one-shot operator allowlist is invalid") from None
    if not isinstance(catalog, dict) or catalog.get("version") != 1 or type(catalog.get("version")) is not int:
        raise OneShotContractError("one-shot operator allowlist version is unsupported")
    specs = catalog.get("contracts")
    if not isinstance(specs, dict) or len(specs) > 16:
        raise OneShotContractError("one-shot operator contract catalog is invalid")
    spec = specs.get(contract)
    if not isinstance(spec, dict):
        raise OneShotContractError("unknown one-shot contract")
    expected = {
        "project_id", "compose_project", "service", "image", "network",
        "network_name", "env_names", "volume_sources", "operations",
    }
    if set(spec) != expected:
        raise OneShotContractError("one-shot contract schema is invalid")
    clean: dict[str, Any] = {}
    for key in ("project_id", "compose_project", "service", "network", "network_name"):
        clean[key] = _ident(spec[key], key)
    image = spec["image"]
    if not isinstance(image, str) or _DIGEST_RE.fullmatch(image) is None:
        raise OneShotContractError("service image must use an immutable digest")
    clean["image"] = image
    env_names = spec["env_names"]
    if (
        not isinstance(env_names, list) or len(env_names) > 32
        or any(not isinstance(v, str) or _ENV_NAME_RE.fullmatch(v) is None for v in env_names)
        or len(env_names) != len(set(env_names))
    ):
        raise OneShotContractError("operator env-name allowlist is invalid")
    clean["env_names"] = tuple(sorted(env_names))
    volume_sources = spec["volume_sources"]
    if not isinstance(volume_sources, dict) or len(volume_sources) > 16:
        raise OneShotContractError("operator volume source allowlist is invalid")
    for target, source in volume_sources.items():
        if (
            not isinstance(target, str) or not target.startswith("/")
            or len(target) > 256
            or any(ch in target for ch in ("\x00", "\r", "\n", ","))
        ):
            raise OneShotContractError("operator volume target allowlist is invalid")
        _ident(source, "volume source")
    clean["volume_sources"] = dict(sorted(volume_sources.items()))
    operations = spec["operations"]
    if not isinstance(operations, dict) or not operations or not set(operations) <= _ALLOWED_MODES:
        raise OneShotContractError("operator operation allowlist is invalid")
    if mode not in operations:
        raise OneShotContractError("operation is not allowlisted for this service")
    if mode.startswith("catalog_"):
        if operations[mode] != "postgres_catalog":
            raise OneShotContractError("catalog operation must use fixed read-only SQL")
        clean["argv"] = (
            "psql", "-X", "--no-psqlrc", "-qAt", "-v", "ON_ERROR_STOP=1",
            "-c", _CATALOG_QUERIES[mode],
        )
    else:
        clean["argv"] = _argv(operations[mode])
    clean["mode"] = mode
    clean["contract"] = contract
    return clean


def attest_compose_config(plan: dict[str, Any], config: Any) -> str:
    """Verify complete service identity and bind raw config with an opaque HMAC.

    Raw environment values are never emitted or copied to action parameters;
    only a process-keyed HMAC is retained to detect changes between preflight
    and a short-lived confirmation. The process key is not persisted.
    """
    if not isinstance(config, dict) or config.get("name") != plan["compose_project"]:
        raise OneShotContractError("Compose project identity mismatch")
    services = config.get("services")
    if not isinstance(services, dict) or plan["service"] not in services:
        raise OneShotContractError("Compose service identity mismatch")
    service = services[plan["service"]]
    if not isinstance(service, dict) or service.get("image") != plan["image"]:
        raise OneShotContractError("Compose service immutable image mismatch")
    actual_networks = service.get("networks")
    if isinstance(actual_networks, dict):
        network_names = set(actual_networks)
    elif isinstance(actual_networks, list) and all(isinstance(n, str) for n in actual_networks):
        network_names = set(actual_networks)
    else:
        raise OneShotContractError("Compose network metadata is unavailable")
    if network_names != {plan["network"]}:
        raise OneShotContractError("Compose service network identity mismatch")
    network_catalog = config.get("networks")
    network = network_catalog.get(plan["network"]) if isinstance(network_catalog, dict) else None
    if (
        not isinstance(network, dict)
        or network.get("name") != plan["network_name"]
        or network.get("internal") is not True
    ):
        raise OneShotContractError("Compose physical network identity mismatch")
    actual_environment = service.get("environment", {})
    if not isinstance(actual_environment, dict):
        raise OneShotContractError("Compose environment metadata is invalid")
    if set(actual_environment) != set(plan["env_names"]):
        raise OneShotContractError("Compose inherited environment names are not allowlisted")
    if any(not isinstance(value, str) or not value for value in actual_environment.values()):
        # A declared name without an interpolated value is not proven
        # inherited. In particular, a missing DB credential must not
        # silently downgrade a one-shot to the image's default identity.
        raise OneShotContractError("Compose inherited environment values are unavailable")
    volumes = service.get("volumes", [])
    if not isinstance(volumes, list) or any(not isinstance(v, dict) for v in volumes):
        raise OneShotContractError("Compose volume metadata is invalid")
    source_map = plan["volume_sources"]
    if len(volumes) != len(source_map):
        raise OneShotContractError("Compose service volume sources are not allowlisted")
    for volume in volumes:
        target = volume.get("target")
        source = volume.get("source")
        if (
            not isinstance(target, str)
            or target not in source_map
            or volume.get("type") != "volume"
            or source != source_map[target]
        ):
            raise OneShotContractError("Compose service volume sources are not allowlisted")
    volume_catalog = config.get("volumes", {})
    if not isinstance(volume_catalog, dict):
        raise OneShotContractError("Compose named volume catalog is invalid")
    for source in source_map.values():
        meta = volume_catalog.get(source)
        if (
            not isinstance(meta, dict)
            or meta.get("name") != f"{plan['compose_project']}_{source}"
            or meta.get("external") is True
        ):
            raise OneShotContractError("Compose named volume authority mismatch")
    if (
        service.get("privileged") not in (None, False)
        or service.get("network_mode") not in (None, "")
        or service.get("pid") not in (None, "")
        or service.get("ipc") not in (None, "")
        or service.get("devices") not in (None, [])
        or service.get("cap_add") not in (None, [])
        or service.get("security_opt") not in (None, [])
        or service.get("ports") not in (None, [])
        or service.get("build") is not None
        or service.get("entrypoint") not in (None, [], "")
        or service.get("secrets") not in (None, [])
        or service.get("configs") not in (None, [])
        or service.get("env_file") not in (None, [])
        or service.get("volumes_from") not in (None, [])
        or service.get("tmpfs") not in (None, [])
        or service.get("userns_mode") not in (None, "")
        or service.get("links") not in (None, [])
        or service.get("extends") is not None
        or service.get("restart") not in (None, "", "no")
    ):
        raise OneShotContractError("Compose service includes unsafe runtime privileges or published ports")
    raw = json.dumps(
        {"plan": plan, "compose_config": config},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    if len(raw) > 128 * 1024:
        raise OneShotContractError("Compose configuration is oversized")
    return hmac.new(_POLICY_KEY, raw, hashlib.sha256).hexdigest()


def parse_catalog_stdout(output: str) -> list[str]:
    """Only return fixed-query catalog identifiers, never arbitrary SQL output."""
    if not isinstance(output, str) or len(output) > 24_000:
        raise OneShotContractError("catalog output is oversized")
    rows: list[str] = []
    for raw in output.splitlines():
        item = raw.strip()
        if item in {"", "BEGIN", "COMMIT"}:
            continue
        if len(rows) == 100:
            raise OneShotContractError("catalog result exceeded row limit")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}(?:\.[A-Za-z_][A-Za-z0-9_]{0,62})?", item):
            raise OneShotContractError("catalog response contains an untrusted identifier")
        rows.append(item)
    return rows
