"""Exact live Gitea Actions provenance for private artifact leases.

This module validates repository, pull request, run, attempt, artifact
metadata and expiry. No archive bytes, temporary files or MCP responses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .gitea_artifacts_transport import MAX_ZIP_BYTES, ArtifactTransportError

_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")


@dataclass(frozen=True)
class ArtifactLease:
    owner: str
    repo: str
    pull_number: int
    run_id: int
    head_sha: str
    run_attempt: int
    artifact_id: int
    expected_sha256: str | None = None

    def validate(self) -> None:
        if any(
            not isinstance(s, str) or not _SEGMENT_RE.fullmatch(s) or ".." in s
            for s in (self.owner, self.repo)
        ):
            raise ArtifactTransportError("Invalid repository identity")
        for value in (self.pull_number, self.run_id, self.run_attempt, self.artifact_id):
            if type(value) is not int or value < 1:
                raise ArtifactTransportError("Invalid PR/run/artifact lease")
        if not isinstance(self.head_sha, str) or not _SHA_RE.fullmatch(self.head_sha):
            raise ArtifactTransportError("Invalid expected PR head")
        if self.expected_sha256 is not None and (
            not isinstance(self.expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.expected_sha256) is None
        ):
            raise ArtifactTransportError("Invalid expected artifact checksum")


async def _verify_exact_artifact(client: Any, lease: ArtifactLease) -> dict[str, Any]:
    """All five identity axes are exact; a missing attempt is never sufficient."""
    pr = await client.get_pull_request(lease.owner, lease.repo, lease.pull_number)
    if not isinstance(pr, dict) or type(pr.get("number")) is not int or (
        pr["number"] != lease.pull_number or pr.get("state") != "open"
        or pr.get("merged") is not False
    ):
        raise ArtifactTransportError("Artifact PR is not an open same-repository PR")
    head = pr.get("head")
    if (
        not isinstance(head, dict)
        or head.get("sha") != lease.head_sha
        or not isinstance(head.get("repo"), dict)
        or head["repo"].get("full_name") != f"{lease.owner}/{lease.repo}"
    ):
        raise ArtifactTransportError("Artifact PR HEAD or repository drifted")
    run = await client.get_action_run(lease.owner, lease.repo, lease.run_id)
    if (
        not isinstance(run, dict)
        or type(run.get("id")) is not int or run["id"] != lease.run_id
        or run.get("head_sha") != lease.head_sha
        or type(run.get("run_attempt")) is not int
        or run["run_attempt"] != lease.run_attempt
        or run.get("event") != "pull_request"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or not isinstance(run.get("repository"), dict)
        or run["repository"].get("full_name") != f"{lease.owner}/{lease.repo}"
    ):
        raise ArtifactTransportError("Exact artifact run evidence changed or is incomplete")
    page = await client.list_action_run_artifacts(lease.owner, lease.repo, lease.run_id)
    if not isinstance(page, dict):
        raise ArtifactTransportError("Artifact inventory is not trustworthy")
    items = page.get("artifacts")
    count = page.get("total_count")
    if (
        type(count) is not int or not 0 <= count <= 64
        or not isinstance(items, list) or len(items) != count
    ):
        raise ArtifactTransportError("Artifact inventory is incomplete")
    seen: set[int] = set()
    matched: dict[str, Any] | None = None
    for item in items:
        if not isinstance(item, dict):
            raise ArtifactTransportError("Artifact inventory item is invalid")
        artifact_id = item.get("id")
        provenance = item.get("workflow_run")
        if (
            type(artifact_id) is not int or artifact_id < 1 or artifact_id in seen
            or not isinstance(provenance, dict)
            or type(provenance.get("id")) is not int
            or provenance["id"] != lease.run_id
            or provenance.get("head_sha") != lease.head_sha
            or type(provenance.get("run_attempt")) is not int
            or provenance["run_attempt"] != lease.run_attempt
        ):
            raise ArtifactTransportError("Artifact provenance is not attempt-attested")
        seen.add(artifact_id)
        if artifact_id == lease.artifact_id:
            matched = item
    if matched is None:
        raise ArtifactTransportError("Exact artifact ID no longer exists")
    size = matched.get("size_in_bytes")
    if type(size) is not int or not 0 < size <= MAX_ZIP_BYTES:
        raise ArtifactTransportError("Artifact exceeds its bounded size")
    if matched.get("expired") is not False:
        raise ArtifactTransportError("Artifact is expired or its expiry is unknown")
    expiry = matched.get("expires_at")
    if not isinstance(expiry, str) or len(expiry) > 64:
        raise ArtifactTransportError("Artifact expiry is not attested")
    try:
        stamp = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError:
        raise ArtifactTransportError("Artifact expiry is not attested") from None
    if stamp.utcoffset() is None or stamp <= datetime.now(UTC):
        raise ArtifactTransportError("Artifact has expired")
    # Never return provider URLs, actor, or other untrusted raw metadata.
    return {"artifact_id": lease.artifact_id, "size_bytes": size, "expiry": expiry}
