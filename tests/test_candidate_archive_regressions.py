"""Regressions recovered from the 2026-10-17 candidate-saves archives."""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest

from examples.mcp_server import candidate_clone
from examples.mcp_server.mcp_infra.adapters import remote


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize("metadata", ["missing", "malformed"])
@pytest.mark.parametrize("branch_state", ["other", "same", "detached"])
def test_legacy_branch_observation_fences_unreadable_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metadata: str, branch_state: str
) -> None:
    """Retain the archived other-branch case and its fail-closed complements."""
    source = tmp_path / "source"
    source.mkdir()
    legacy = tmp_path / ".mcp-candidate-clones" / "candidate-legacy-manual"
    legacy.mkdir(parents=True)
    _git(legacy, "init", "-q")
    _git(legacy, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "--allow-empty", "-q", "-m", "fixture")
    requested = "candidate/legacy-isolation"
    observed = requested if branch_state == "same" else "candidate/unrelated"
    _git(legacy, "branch", "-M", observed)
    if branch_state == "detached":
        _git(legacy, "checkout", "--detach", "-q", "HEAD")
    path = legacy / ".git" / "mcp-candidate-clone.json"
    if metadata == "malformed":
        path.write_text("{invalid", encoding="utf-8")
    old_bytes = path.read_bytes() if path.exists() else None
    old_head = _git(legacy, "rev-parse", "HEAD")

    def resolve(root: Path) -> tuple[str, str, str]:
        assert root in {source, legacy}
        return "fixture-user", "https://trusted.invalid/owner/requested.git", "fixture-token"

    monkeypatch.setattr(candidate_clone, "_resolve_trusted_remote", resolve)
    kwargs = dict(source_project="source-project", branch=requested,
                  exclude_project_id="candidate-new", source_root=source)
    if branch_state == "other":
        assert candidate_clone._find_lineage_claimant(tmp_path, **kwargs) is None
    else:
        with pytest.raises(candidate_clone.CandidateCloneError) as caught:
            candidate_clone._find_lineage_claimant(tmp_path, **kwargs)
        assert caught.value.code == "CANDIDATE_LINEAGE_SCAN_FAILED"
        assert caught.value.details is not None
        assert caught.value.details["candidate_project_id"] == legacy.name
        assert caught.value.details["observed_branch"] == ("HEAD" if branch_state == "detached" else requested)
    assert _git(legacy, "rev-parse", "HEAD") == old_head
    assert (path.read_bytes() if path.exists() else None) == old_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize("observations", [
    ("timeout", "timeout", "timeout"),
    ("timeout", "absent", "absent"),
    ("absent", "timeout", "absent"),
    ("timeout", "exact"),
    ("absent", "timeout", "exact"),
])
async def test_verified_publication_reconciliation_retains_unreadable_observation(
    observations: tuple[str, ...],
) -> None:
    """Archived unreadable readback regression, against the current helper."""
    expected = "a" * 40
    calls: list[tuple[str, str, str]] = []

    class Client:
        async def get_branch(self, owner: str, repo: str, branch: str) -> dict:
            calls.append((owner, repo, branch))
            value = observations[len(calls) - 1]
            request = httpx.Request("GET", "https://trusted.invalid/branch")
            if value == "timeout":
                raise httpx.TransportError("fixture-private-diagnostic")
            if value == "absent":
                response = httpx.Response(404, request=request)
                raise httpx.HTTPStatusError("missing", request=request, response=response)
            return {"name": branch, "commit": {"id": expected}}

    state, branch, details = await remote._reconcile_verified_push_postcondition(
        Client(), "owner", "repo", "feature/archive-regression",
        expected_head_sha=expected, previous_branch=None,
    )
    assert calls == [("owner", "repo", "feature/archive-regression")] * len(observations)
    assert details["reconciliation_attempts"] == len(observations)
    assert "fixture-private-diagnostic" not in repr(details)
    if observations[-1] == "exact":
        assert state == "success"
        assert branch == {"name": "feature/archive-regression", "commit": {"id": expected}}
    else:
        assert state == "unknown"
        assert branch is None
        assert details["last_read_error_class"] == "TransportError"
        assert details["bounded_reads_match_pre_push_state"] is False
        assert details["observed"] == (
            {} if all(value == "timeout" for value in observations)
            else {"present": False, "head_sha": None}
        )


def test_site_audit_durable_state_mount_is_admin_only() -> None:
    """Retain the archive's admin-only isolation invariant for today's volume."""
    import yaml

    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "docker" / "docker-compose.yml")
        .read_text(encoding="utf-8")
    )
    volume = "site_audit_deploy_state"
    name = "ssh-gateway-site-audit-deploy-state"
    target = "/var/lib/site-audit-deploy-state"
    assert compose["volumes"][volume]["name"] == name
    assert {
        key for key, spec in compose["volumes"].items()
        if isinstance(spec, dict) and spec.get("name") == name
    } == {volume}

    mounts = []
    for service, spec in compose["services"].items():
        for mount in spec.get("volumes", []):
            if isinstance(mount, str):
                source, _, destination = mount.partition(":")
                if source == volume:
                    mounts.append((service, destination))
            elif mount.get("type") == "volume" and mount.get("source") == volume:
                destination = mount["target"]
                if mount.get("read_only", False):
                    destination += ":ro"
                mounts.append((service, destination))
        if service != "mcp-oauth":
            assert not any(
                entry.split(":", 1)[0] == "mcp-oauth"
                for entry in spec.get("volumes_from", [])
            ), service
    assert mounts in [
        [("mcp-oauth", target)],
        [("mcp-oauth", target + ":rw")],
    ]
