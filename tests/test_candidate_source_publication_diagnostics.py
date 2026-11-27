"""Regression: structured source-publication diagnostics survive candidate errors."""

import pytest

from examples.mcp_server.agent_sources import (
    ManagedSourceBundleError,
    ManagedSourcePublicationStageError,
)
from examples.mcp_server.candidate_clone import _source_failure_details


@pytest.mark.parametrize(
    ("reason", "cause"),
    [
        ("timeout", "timeout"),
        ("storage_full", "unavailable"),
        ("commit_unavailable", "missing_ref"),
        ("authentication_failed", "auth"),
        ("permission_denied", "permission"),
        ("failed", "source_materialization_failed"),
    ],
)
def test_preserves_structured_reason(reason: str, cause: str) -> None:
    error = ManagedSourcePublicationStageError(
        phase="remote_fetch", reason=reason, elapsed_ms=65000,
    )
    error.__cause__ = RuntimeError("private URL and credentials must not escape")
    assert _source_failure_details(error) == {
        "source_cause": cause,
        "source_phase": "remote_fetch",
        "source_reason": reason,
        "source_elapsed_ms": 65000,
    }


@pytest.mark.parametrize("phase", [
    "input_validation", "source_root", "remote_resolution", "scratch_init",
    "remote_fetch", "commit_verification", "ref_update", "bundle_creation",
    "bundle_verification", "artifact_install", "digest_binding",
])
def test_publication_phase_enum(phase: str) -> None:
    error = ManagedSourcePublicationStageError(phase=phase, reason="failed", elapsed_ms=0)
    assert _source_failure_details(error)["source_phase"] == phase


@pytest.mark.parametrize("field,value", [
    ("phase", "private-url timeout"), ("phase", []),
    ("reason", "private-token authentication failed"), ("reason", {}),
    ("elapsed_ms", -1), ("elapsed_ms", 600001),
    ("elapsed_ms", True), ("elapsed_ms", "123"),
])
def test_invalid_structured_fields_fail_closed(field: str, value: object) -> None:
    error = ManagedSourcePublicationStageError(phase="remote_fetch", reason="timeout", elapsed_ms=1)
    setattr(error, field, value)
    assert _source_failure_details(error) == {"source_cause": "source_materialization_failed"}


@pytest.mark.parametrize("message,cause", [
    ("authentication failed for private-url", "auth"),
    ("permission denied: private-path", "permission"),
    ("unclassified private-token", "source_materialization_failed"),
])
def test_legacy_classifier_contract(message: str, cause: str) -> None:
    assert _source_failure_details(ManagedSourceBundleError(message)) == {"source_cause": cause}
