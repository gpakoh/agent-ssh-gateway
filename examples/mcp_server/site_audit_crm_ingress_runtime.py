"""Read-only, fixed-identity Docker evidence for Site Audit CRM ingress.

No credentials, arbitrary container names, network addresses, host paths,
external policy assertions, or deployment authority are accepted from callers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from examples.mcp_server.site_audit_crm_ingress_admission import evaluate_ingress_admission

PROTECTED = (
    "site-audit-db",
    "site-audit-egress-proxy",
    "site-audit-pdf",
    "site-audit-crm-db",
    "site-audit-crm-web",
    "site-audit-crm-daemon",
)
TARGETS = (
    "site-audit-api",
    "site-audit-crawler-worker",
    "site-audit-browser-worker",
    "site-audit-security-worker",
    "site-audit-performance-worker",
)
INGRESS = "site-audit-crm-ingress"


class IngressReadinessError(RuntimeError):
    """Classified failure; raw Docker output must never be returned."""


def _metadata(payload: Any) -> Mapping[str, Any]:
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], Mapping):
        return payload[0]
    if isinstance(payload, Mapping):
        return payload
    raise IngressReadinessError("docker_metadata_invalid")


def _snapshot(payload: Any) -> dict[str, Any]:
    item = _metadata(payload)
    config = item.get("Config")
    state = item.get("State")
    host_config = item.get("HostConfig")
    if not isinstance(config, Mapping) or not isinstance(state, Mapping):
        raise IngressReadinessError("container_metadata_incomplete")
    if not isinstance(host_config, Mapping):
        raise IngressReadinessError("host_port_metadata_missing")
    labels = config.get("Labels")
    if not isinstance(labels, Mapping):
        raise IngressReadinessError("compose_labels_missing")
    if "PortBindings" not in host_config:
        raise IngressReadinessError("host_port_evidence_missing")
    bindings = host_config.get("PortBindings")
    if bindings is None:
        bindings = {}
    if not isinstance(bindings, Mapping):
        raise IngressReadinessError("port_bindings_invalid")
    network = item.get("NetworkSettings")
    if not isinstance(network, Mapping) or "Ports" not in network:
        raise IngressReadinessError("network_port_evidence_missing")
    effective_ports = network["Ports"]
    if not isinstance(effective_ports, Mapping):
        raise IngressReadinessError("network_port_evidence_invalid")
    publish_all = host_config.get("PublishAllPorts")
    if not isinstance(publish_all, bool):
        raise IngressReadinessError("publish_all_ports_evidence_missing")
    for declared in (bindings, effective_ports):
        for entry in declared.values():
            if entry is not None and not isinstance(entry, list):
                raise IngressReadinessError("port_binding_evidence_invalid")
    # "published" is deliberately non-specific: no host IP or port is returned.
    # The effective network bindings catch Docker's --publish-all case where
    # HostConfig.PortBindings can be empty despite a live published port.
    publishes = (
        publish_all
        or any(bool(v) for v in bindings.values())
        or any(bool(v) for v in effective_ports.values())
    )
    health = state.get("Health")
    return {
        "container_id": item.get("Id"),
        "image_id": item.get("Image"),
        "compose_hash": labels.get("com.docker.compose.config-hash"),
        "started_at": state.get("StartedAt"),
        "running": state.get("Running"),
        "health": health.get("Status") if isinstance(health, Mapping) else None,
        "project": labels.get("com.docker.compose.project"),
        "host_ports": ["published"] if publishes else [],
    }


async def _capture(client: Any, names: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    results = {}
    for name in names:
        try:
            results[name] = _snapshot(await client.inspect(name, max_lines=10))
        except IngressReadinessError:
            raise
        except (ValueError, RuntimeError, OSError) as exc:
            raise IngressReadinessError("fixed_container_inspection_failed") from exc
    return results


async def collect_live_readiness(client: Any) -> dict[str, Any]:
    """Observe identity twice; never return GO or authorize Docker mutation."""
    try:
        ingress_exists = await client.container_exists(INGRESS)
        before = await _capture(client, PROTECTED)
        targets_before = await _capture(client, TARGETS)
        after = await _capture(client, PROTECTED)
        targets_after = await _capture(client, TARGETS)
        ingress_exists_after = await client.container_exists(INGRESS)
    except IngressReadinessError:
        raise
    except (ValueError, RuntimeError, OSError) as exc:
        raise IngressReadinessError("fixed_container_inventory_failed") from exc

    evidence = {
        "source_project": "site-audit-platform",
        # A live Docker snapshot cannot authenticate a Git source/release.
        # These obligations remain with a separate operator-owned preflight.
        "source_sha": None,
        "operator_checkout_sha": None,
        "source_verified": False,
        "compose_blob_sha": None,
        "nginx_blob_sha": None,
        "preflight_blob_sha": None,
        "operator_worktree_clean": False,
        "image": None,
        "image_id": None,
        "image_local_verified": False,
        "image_operator_approved": False,
        "compose_project": "site-audit-platform",
        "compose_service": "crm-ingress",
        "container_name": INGRESS,
        "upstream": None,  # Nginx upstream cannot be proven from Docker inventory.
        "control_network": None,  # Docker network membership was not examined.
        "ingress_absent": ingress_exists is False and ingress_exists_after is False,
        "port_8094_available": False,  # Docker inventory cannot prove host bind.
        "ingress_image_matches_compose": False,
        "ingress_config_mount_readonly": False,
        "no_dependencies": False,
        "no_build": False,
        "pending_operation_absent": False,
        "exclusive_operation_lease": False,
        "durable_receipt_ready": False,
        "listen_host": None,  # Host binding is not proven by Docker inventory.
        "listen_port": 8094,
        "tls_proxy_private_source": None,
        "operator_approved_tls_proxy_source": None,
        "operator_approved_service_cidr": None,
        "ingress_private_target": None,
        "operator_approved_private_ingress_target": None,
        "trusted_tls_proxy_verified": False,
        "private_proxy_route_verified": False,
        "forwarded_headers_restricted": False,
        "lan_access_cidr": None,
        "operator_approved_lan_access_cidr": None,
        "lan_access_operator_approved": False,
        "wan_ipv4_port_forward_absent_verified": False,
        "wan_ipv6_nonexposure_verified": False,
        "protected_before": before,
        "protected_current": after,
        "targets_before": targets_before,
        "targets_current": targets_after,
        "protected_config_compatibility_verified": False,
        "runtime_network_compatibility_verified": False,
    }
    verdict = evaluate_ingress_admission(evidence)
    # No source proof, port lease or firewall verification can be supplied by
    # this read-only collector. Even a fully stable stack is never a GO.
    return {
        "status": "no_go",
        "authorizes_mutation": False,
        "blockers": verdict["blockers"],
        "protected_dependencies": len(PROTECTED),
        "audit_targets": len(TARGETS),
        "protected_unchanged": before == after,
        "audit_targets_unchanged": targets_before == targets_after,
        "ingress_absent": evidence["ingress_absent"],
        "evidence_scope": "fixed_live_docker_read_only",
        "source_validation": "not_performed",
        "firewall_policy_validation": "not_performed",
        "durable_lkg_validation": "not_performed",
    }