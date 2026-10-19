"""Fail-closed, pure admission contract for Site Audit CRM ingress-only deployment.

This module validates *trusted, independently collected* control-plane evidence.
It neither gathers evidence nor authorizes a Docker mutation. In particular,
caller-provided booleans must never be treated as operator or firewall approval.
A future guarded adapter must construct this evidence from source leases,
Docker inspection, and an authenticated network-policy verifier, then repeat
those checks under an exclusive lease immediately before making a change.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_GIT_BLOB_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
_PROTECTED = frozenset(
    {
        "site-audit-db",
        "site-audit-egress-proxy",
        "site-audit-pdf",
        "site-audit-crm-db",
        "site-audit-crm-web",
        "site-audit-crm-daemon",
    }
)
_TARGETS = frozenset(
    {
        "site-audit-api",
        "site-audit-crawler-worker",
        "site-audit-browser-worker",
        "site-audit-security-worker",
        "site-audit-performance-worker",
    }
)
_INGRESS = "site-audit-crm-ingress"


def _is_hash(value: Any, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _check_snapshot(
    evidence: Any,
    names: frozenset[str],
    prefix: str,
    blockers: set[str],
    *,
    forbid_host_ports: bool = True,
) -> None:
    if not isinstance(evidence, Mapping) or set(evidence) != names:
        blockers.add(f"{prefix}_set_mismatch")
        return
    for name in names:
        value = evidence[name]
        if not isinstance(value, Mapping):
            blockers.add(f"{prefix}_identity_invalid")
            continue
        if not _is_hash(value.get("container_id"), re.compile(r"^[0-9a-f]{64}$")):
            blockers.add(f"{prefix}_identity_invalid")
        if not _is_hash(value.get("image_id"), _SHA256):
            blockers.add(f"{prefix}_image_invalid")
        if not _is_hash(value.get("compose_hash"), re.compile(r"^[0-9a-f]{64}$")):
            blockers.add(f"{prefix}_compose_hash_invalid")
        if not isinstance(value.get("started_at"), str) or not value["started_at"].endswith("Z"):
            blockers.add(f"{prefix}_started_at_invalid")
        if value.get("running") is not True or value.get("health") != "healthy":
            blockers.add(f"{prefix}_unhealthy")
        if value.get("project") != "site-audit-platform":
            blockers.add(f"{prefix}_project_mismatch")
        # Protected dependencies must not publish host ports. Audit targets
        # such as audit-api may publish a control facade; compare them for
        # stability without classifying that as a CRM ingress bypass.
        ports = value.get("host_ports")
        if ports not in ([], (), ["published"]):
            blockers.add(f"{prefix}_host_port_evidence_invalid")
        if forbid_host_ports and ports not in ([], ()):
            blockers.add(f"{prefix}_host_port_exposed")


def evaluate_ingress_admission(evidence: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Report preflight blockers without ever granting permission to mutate.

    Only evidence harvested and authenticated by a dedicated Gateway adapter
    is meaningful. The non-secret result is suitable for logging and testing.
    A blocker-free result means *ready for another guarded preflight*, not GO.
    """
    blockers: set[str] = set()
    if not isinstance(evidence, Mapping):
        return {
            "status": "blocked",
            "blockers": ["evidence_missing"],
            "authorizes_mutation": False,
        }
    if evidence.get("source_project") != "site-audit-platform":
        blockers.add("source_project_mismatch")
    if not _is_hash(evidence.get("source_sha"), _SOURCE_SHA):
        blockers.add("source_sha_invalid")
    if evidence.get("source_sha") != evidence.get("operator_checkout_sha"):
        blockers.add("operator_source_stale")
    if evidence.get("source_verified") is not True:
        blockers.add("source_provenance_unverified")
    for field in ("compose_blob_sha", "nginx_blob_sha", "preflight_blob_sha"):
        if not _is_hash(evidence.get(field), _GIT_BLOB_SHA):
            blockers.add(f"{field}_invalid")
    if evidence.get("operator_worktree_clean") is not True:
        blockers.add("operator_worktree_untrusted")

    image = evidence.get("image")
    if not _is_hash(image, _IMAGE):
        blockers.add("immutable_image_missing")
    if not _is_hash(evidence.get("image_id"), _SHA256):
        blockers.add("image_id_unverified")
    if evidence.get("image_local_verified") is not True:
        blockers.add("image_local_unverified")
    if evidence.get("image_operator_approved") is not True:
        blockers.add("image_not_approved")

    if evidence.get("compose_project") != "site-audit-platform":
        blockers.add("compose_project_mismatch")
    if evidence.get("compose_service") != "crm-ingress":
        blockers.add("wrong_service")
    if evidence.get("container_name") != _INGRESS:
        blockers.add("wrong_container")
    if evidence.get("upstream") != "crm-web:80":
        blockers.add("upstream_mismatch")
    if evidence.get("control_network") != "site-audit-control":
        blockers.add("control_network_unverified")
    if evidence.get("ingress_absent") is not True:
        blockers.add("ingress_already_present_or_unknown")
    if evidence.get("port_8094_available") is not True:
        blockers.add("host_port_unavailable_or_unknown")
    if evidence.get("ingress_image_matches_compose") is not True:
        blockers.add("resolved_image_differs_from_source")
    if evidence.get("ingress_config_mount_readonly") is not True:
        blockers.add("nginx_config_mount_unverified")
    if evidence.get("no_dependencies") is not True or evidence.get("no_build") is not True:
        blockers.add("unsafe_compose_scope")
    if evidence.get("pending_operation_absent") is not True:
        blockers.add("pending_operation_unverified")
    if evidence.get("exclusive_operation_lease") is not True:
        blockers.add("exclusive_lease_missing")
    if evidence.get("durable_receipt_ready") is not True:
        blockers.add("durable_receipt_missing")

    # Only a host-enforced ACL can protect an all-interface bind. An nginx
    # trust-list alone is not an authorization or a firewall.
    if evidence.get("listen_host") != "0.0.0.0" or evidence.get("listen_port") != 8094:
        blockers.add("reviewed_bind_mismatch")
    if evidence.get("trusted_tls_proxy_verified") is not True:
        blockers.add("trusted_proxy_unverified")
    if evidence.get("host_firewall_acl_verified") is not True:
        blockers.add("firewall_acl_unverified")
    if evidence.get("forwarded_headers_restricted") is not True:
        blockers.add("forwarded_header_boundary_unverified")
    if evidence.get("allowed_proxy_source_matches_firewall") is not True:
        blockers.add("proxy_firewall_mismatch")

    before = evidence.get("protected_before")
    current = evidence.get("protected_current")
    _check_snapshot(before, _PROTECTED, "protected", blockers)
    _check_snapshot(current, _PROTECTED, "protected_current", blockers)
    if before != current:
        blockers.add("protected_changed")
    targets_before = evidence.get("targets_before")
    targets_current = evidence.get("targets_current")
    _check_snapshot(targets_before, _TARGETS, "targets", blockers, forbid_host_ports=False)
    _check_snapshot(targets_current, _TARGETS, "targets_current", blockers, forbid_host_ports=False)
    if targets_before != targets_current:
        blockers.add("targets_changed")

    # A legacy runtime is not allowed to match by merely having a working
    # health check. Compare exact config hashes to an independently reviewed
    # compatibility manifest before permitting a guarded attempt.
    if evidence.get("protected_config_compatibility_verified") is not True:
        blockers.add("protected_compose_drift_unresolved")
    if evidence.get("runtime_network_compatibility_verified") is not True:
        blockers.add("runtime_network_drift_unresolved")

    return {
        "status": "prerequisites_met" if not blockers else "blocked",
        "blockers": sorted(blockers),
        "authorizes_mutation": False,
    }