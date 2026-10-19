"""Adversarial admission tests: an eligibility check never authorizes mutation."""

from __future__ import annotations

from copy import deepcopy

import pytest

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


def _snapshot(names: tuple[str, ...]) -> dict[str, dict[str, object]]:
    return {
        name: {
            "container_id": format(i + 1, "064x"),
            "image_id": "sha256:" + format(i + 1, "064x"),
            "compose_hash": format(i + 1, "064x"),
            "started_at": "2026-10-18T12:00:00.000000Z",
            "running": True,
            "health": "healthy",
            "project": "site-audit-platform",
            "host_ports": [],
        }
        for i, name in enumerate(names)
    }


def _evidence() -> dict[str, object]:
    source_sha = "a" * 40
    return {
        "source_project": "site-audit-platform",
        "source_sha": source_sha,
        "operator_checkout_sha": source_sha,
        "source_verified": True,
        "compose_blob_sha": "b" * 40,
        "nginx_blob_sha": "c" * 40,
        "preflight_blob_sha": "d" * 40,
        "operator_worktree_clean": True,
        "image": "nginx@sha256:" + "1" * 64,
        "image_id": "sha256:" + "2" * 64,
        "image_local_verified": True,
        "image_operator_approved": True,
        "compose_project": "site-audit-platform",
        "compose_service": "crm-ingress",
        "container_name": "site-audit-crm-ingress",
        "upstream": "crm-web:80",
        "control_network": "site-audit-control",
        "ingress_absent": True,
        "port_8094_available": True,
        "ingress_image_matches_compose": True,
        "ingress_config_mount_readonly": True,
        "no_dependencies": True,
        "no_build": True,
        "pending_operation_absent": True,
        "exclusive_operation_lease": True,
        "durable_receipt_ready": True,
        "listen_host": "0.0.0.0",
        "listen_port": 8094,
        "tls_proxy_private_source": "172.20.30.1",
        "operator_approved_tls_proxy_source": "172.20.30.1",
        "operator_approved_service_cidr": "172.20.30.0/24",
        "ingress_private_target": "172.20.30.3",
        "operator_approved_private_ingress_target": "172.20.30.3",
        "trusted_tls_proxy_verified": True,
        "private_proxy_route_verified": True,
        "forwarded_headers_restricted": True,
        "lan_access_cidr": "172.20.40.0/24",
        "operator_approved_lan_access_cidr": "172.20.40.0/24",
        "lan_access_operator_approved": True,
        "wan_ipv4_port_forward_absent_verified": True,
        "wan_ipv6_nonexposure_verified": True,
        "protected_before": _snapshot(PROTECTED),
        "protected_current": _snapshot(PROTECTED),
        "targets_before": _snapshot(TARGETS),
        "targets_current": _snapshot(TARGETS),
        "protected_config_compatibility_verified": True,
        "runtime_network_compatibility_verified": True,
    }


def test_missing_evidence_is_blocked() -> None:
    assert evaluate_ingress_admission(None) == {
        "status": "blocked",
        "blockers": ["evidence_missing"],
        "authorizes_mutation": False,
    }


def test_even_verified_snapshot_cannot_authorize_mutation() -> None:
    result = evaluate_ingress_admission(_evidence())
    assert result == {
        "status": "prerequisites_met",
        "blockers": [],
        "authorizes_mutation": False,
    }


@pytest.mark.parametrize(
    ("field", "bad", "reason"),
    [
        ("source_sha", "a" * 39, "source_sha_invalid"),
        ("operator_checkout_sha", "f" * 40, "operator_source_stale"),
        ("source_verified", False, "source_provenance_unverified"),
        ("nginx_blob_sha", "", "nginx_blob_sha_invalid"),
        ("image", "nginx:stable-alpine", "immutable_image_missing"),
        ("image", "nginx@sha256:" + "X" * 64, "immutable_image_missing"),
        ("image_operator_approved", False, "image_not_approved"),
        ("image_local_verified", None, "image_local_unverified"),
        ("ingress_absent", False, "ingress_already_present_or_unknown"),
        ("port_8094_available", False, "host_port_unavailable_or_unknown"),
        ("listen_host", "127.0.0.1", "reviewed_bind_mismatch"),
        ("trusted_tls_proxy_verified", False, "trusted_proxy_unverified"),
        ("tls_proxy_private_source", "172.20.30.9", "reviewed_tls_proxy_source_mismatch"),
        ("operator_approved_tls_proxy_source", "172.20.30.9", "reviewed_tls_proxy_source_mismatch"),
        ("operator_approved_service_cidr", "172.20.31.0/24", "reviewed_tls_proxy_source_mismatch"),
        ("ingress_private_target", "172.20.30.9", "reviewed_private_ingress_target_mismatch"),
        ("operator_approved_private_ingress_target", "172.20.30.9", "reviewed_private_ingress_target_mismatch"),
        ("private_proxy_route_verified", False, "private_proxy_route_unverified"),
        ("forwarded_headers_restricted", False, "forwarded_header_boundary_unverified"),
        ("lan_access_cidr", "0.0.0.0/0", "lan_access_scope_mismatch"),
        ("operator_approved_lan_access_cidr", "172.20.30.0/24", "lan_access_scope_mismatch"),
        ("lan_access_operator_approved", False, "lan_access_not_approved"),
        ("wan_ipv4_port_forward_absent_verified", None, "wan_ipv4_exposure_unverified"),
        ("wan_ipv6_nonexposure_verified", None, "wan_ipv6_exposure_unverified"),
        ("control_network", "unknown", "control_network_unverified"),
        ("no_dependencies", False, "unsafe_compose_scope"),
        ("no_build", False, "unsafe_compose_scope"),
        ("pending_operation_absent", False, "pending_operation_unverified"),
        ("exclusive_operation_lease", False, "exclusive_lease_missing"),
        ("durable_receipt_ready", None, "durable_receipt_missing"),
        ("protected_config_compatibility_verified", False, "protected_compose_drift_unresolved"),
        ("runtime_network_compatibility_verified", False, "runtime_network_drift_unresolved"),
    ],
)
def test_unverified_requirement_blocks(
    field: str, bad: object, reason: str
) -> None:
    evidence = _evidence()
    evidence[field] = bad
    result = evaluate_ingress_admission(evidence)
    assert result["status"] == "blocked"
    assert reason in result["blockers"]
    assert result["authorizes_mutation"] is False


def test_existing_protected_crm_changes_are_not_ignored() -> None:
    evidence = _evidence()
    assert isinstance(evidence["protected_current"], dict)
    evidence["protected_current"]["site-audit-crm-web"]["container_id"] = "f" * 64
    result = evaluate_ingress_admission(evidence)
    assert "protected_changed" in result["blockers"]
    assert result["authorizes_mutation"] is False


def test_missing_crm_dependency_is_blocked() -> None:
    evidence = _evidence()
    assert isinstance(evidence["protected_current"], dict)
    del evidence["protected_current"]["site-audit-crm-db"]
    result = evaluate_ingress_admission(evidence)
    assert "protected_set_mismatch" in result["blockers"] or "protected_current_set_mismatch" in result["blockers"]
    assert result["status"] == "blocked"


def test_exposed_crm_dependency_is_blocked() -> None:
    evidence = _evidence()
    assert isinstance(evidence["protected_before"], dict)
    evidence["protected_before"]["site-audit-crm-web"]["host_ports"] = ["0.0.0.0:8094:80"]
    result = evaluate_ingress_admission(evidence)
    assert "protected_host_port_exposed" in result["blockers"]


def test_unhealthy_or_foreign_dependency_is_blocked() -> None:
    evidence = _evidence()
    assert isinstance(evidence["protected_current"], dict)
    evidence["protected_current"]["site-audit-crm-web"]["health"] = "unhealthy"
    evidence["protected_current"]["site-audit-crm-db"]["project"] = "foreign"
    result = evaluate_ingress_admission(evidence)
    assert "protected_current_unhealthy" in result["blockers"]
    assert "protected_current_project_mismatch" in result["blockers"]


def test_audit_target_replacement_is_detected() -> None:
    evidence = _evidence()
    assert isinstance(evidence["targets_current"], dict)
    evidence["targets_current"]["site-audit-api"]["started_at"] = "2026-10-18T13:00:00Z"
    result = evaluate_ingress_admission(evidence)
    assert "targets_changed" in result["blockers"]


def test_bad_shape_cannot_raise_or_leak_secret_values() -> None:
    evidence = deepcopy(_evidence())
    evidence["protected_current"] = {"malicious": "SECRET-CANARY"}
    result = evaluate_ingress_admission(evidence)
    assert result["status"] == "blocked"
    assert "SECRET-CANARY" not in repr(result)
    assert result["authorizes_mutation"] is False

def test_audit_api_published_control_port_is_not_crm_ingress_bypass() -> None:
    evidence = _evidence()
    assert isinstance(evidence["targets_before"], dict)
    assert isinstance(evidence["targets_current"], dict)
    evidence["targets_before"]["site-audit-api"]["host_ports"] = ["published"]
    evidence["targets_current"]["site-audit-api"]["host_ports"] = ["published"]
    result = evaluate_ingress_admission(evidence)
    assert "targets_host_port_exposed" not in result["blockers"]
    assert "targets_current_host_port_exposed" not in result["blockers"]
    assert result["status"] == "prerequisites_met"
    assert result["authorizes_mutation"] is False

def test_old_firewall_only_proof_is_insufficient_for_private_lan_policy() -> None:
    evidence = _evidence()
    evidence["wan_ipv4_port_forward_absent_verified"] = False
    evidence["wan_ipv6_nonexposure_verified"] = False
    evidence["host_firewall_acl_verified"] = True  # legacy flag cannot override WAN checks
    evidence["allowed_proxy_source_matches_firewall"] = True
    result = evaluate_ingress_admission(evidence)
    assert "wan_ipv4_exposure_unverified" in result["blockers"]
    assert "wan_ipv6_exposure_unverified" in result["blockers"]
    assert result["authorizes_mutation"] is False


def test_entire_10_10_service_subnet_cannot_impersonate_proxy() -> None:
    evidence = _evidence()
    evidence["tls_proxy_private_source"] = "172.20.30.0/24"
    result = evaluate_ingress_admission(evidence)
    assert "reviewed_tls_proxy_source_mismatch" in result["blockers"]
    assert result["authorizes_mutation"] is False

def test_private_sources_and_approved_scopes_must_match() -> None:
    evidence = _evidence()
    evidence["lan_access_cidr"] = "172.20.30.0/24"
    evidence["operator_approved_lan_access_cidr"] = "172.20.30.0/24"
    result = evaluate_ingress_admission(evidence)
    assert "lan_access_scope_mismatch" in result["blockers"]
    assert result["authorizes_mutation"] is False


def test_unroutable_or_public_proxy_source_is_never_approved() -> None:
    evidence = _evidence()
    evidence["tls_proxy_private_source"] = "203.0.113.4"
    evidence["operator_approved_tls_proxy_source"] = "203.0.113.4"
    result = evaluate_ingress_admission(evidence)
    assert "reviewed_tls_proxy_source_mismatch" in result["blockers"]
    assert result["authorizes_mutation"] is False
