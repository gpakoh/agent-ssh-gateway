"""Gitea Actions artifact metadata is exact-PR/run bound and never a download proxy."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from examples.mcp_client_remote.fleet.gitea_client import GiteaClient
from examples.mcp_server.mcp_infra.adapters import remote

HEAD = "a" * 40
PR = {
    "number": 54,
    "state": "open",
    "merged": False,
    "head": {"sha": HEAD, "repo": {"full_name": "gpakoh/xloud"}},
}
RUN = {
    "id": 200,
    "head_sha": HEAD,
    "event": "pull_request",
    "status": "completed",
    "conclusion": "success",
    "run_attempt": 2,
    "repository": {"full_name": "gpakoh/xloud"},
}
ARTIFACT = {
    "id": 456,
    "name": "preview-site",
    "size_in_bytes": 1234,
    "expired": False,
    "expires_at": "2026-11-06T00:00:00Z",
    "workflow_run": {"id": 200, "head_sha": HEAD, "run_attempt": 2},
    "archive_download_url": "https://evil.example/leak?token=PRIVATE",
    "url": "https://internal.example/private",
    "untrusted_data": {"email": "secret@example.com"},
}


class Fake:
    def __init__(self, *, pr=None, run=None, artifacts=None):
        self.pr = PR if pr is None else pr
        self.run = RUN if run is None else run
        self.artifacts = [ARTIFACT] if artifacts is None else artifacts
        self.calls = []
        self.get_pull_request = AsyncMock(side_effect=self._pr)
        self.get_action_run = AsyncMock(side_effect=self._run)
        self.list_action_run_artifacts = AsyncMock(side_effect=self._artifacts)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def _pr(self, *args):
        self.calls.append("pr")
        return self.pr

    async def _run(self, *args):
        self.calls.append("run")
        return self.run

    async def _artifacts(self, *args, **kwargs):
        self.calls.append("artifacts")
        return {"total_count": len(self.artifacts), "artifacts": self.artifacts}


async def fetch(fake, monkeypatch, **kwargs):
    monkeypatch.setenv("GITEA_TOKEN", "PRIVATE")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: fake)
    return await remote.gitea_list_action_run_artifacts(
        "gpakoh", "xloud", 54, 200, HEAD, 2, **kwargs
    )


@pytest.mark.asyncio
async def test_exact_pr_head_run_attempt_and_private_urls_never_escaped(monkeypatch):
    fake = Fake()
    result = await fetch(fake, monkeypatch)
    assert result["ok"] is True
    assert fake.calls == ["pr", "run", "artifacts", "pr", "run"]
    assert result["result"]["total_count"] == 1
    assert result["result"]["artifacts"] == [{
        "id": 456,
        "name": "preview-site",
        "size_in_bytes": 1234,
        "expired": False,
        "expires_at": "2026-11-06T00:00:00Z",
        "run_attempt_attested": True,
    }]
    assert result["result"]["download_authorized"] is False
    output = json.dumps(result)
    for forbidden in ["evil.example", "internal.example", "PRIVATE", "secret@example.com"]:
        assert forbidden not in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,code",
    [
        ({"state": "closed"}, "PR_NOT_OPEN"),
        ({"head": {"sha": "b" * 40}}, "HEAD_MISMATCH"),
        ({"head": {"sha": HEAD, "repo": {"full_name": "attacker/fork"}}}, "POLICY_DENIED"),
    ],
)
async def test_wrong_pr_blocks_all_artifact_reads(monkeypatch, change, code):
    fake = Fake(pr={**PR, **change})
    result = await fetch(fake, monkeypatch)
    assert result["ok"] is False
    assert result["error"]["code"] == code
    assert fake.calls == ["pr"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"head_sha": "b" * 40},
        {"event": "push"},
        {"status": "in_progress"},
        {"conclusion": "failure"},
        {"run_attempt": 3},
        {"repository": {"full_name": "attacker/repo"}},
        {"id": 201},
    ],
)
async def test_wrong_run_fails_before_listing(monkeypatch, change):
    fake = Fake(run={**RUN, **change})
    result = await fetch(fake, monkeypatch)
    assert result["ok"] is False
    assert fake.calls == ["pr", "run"]


@pytest.mark.asyncio
async def test_foreign_artifact_or_duplicate_id_fails_closed(monkeypatch):
    for artifacts in [
        [{**ARTIFACT, "workflow_run": {"id": 201, "head_sha": HEAD, "run_attempt": 2}}],
        [ARTIFACT, ARTIFACT],
        [{**ARTIFACT, "name": "preview/../../secret"}],
        [{**ARTIFACT, "id": True}],
    ]:
        fake = Fake(artifacts=artifacts)
        result = await fetch(fake, monkeypatch)
        assert result["ok"] is False
        assert "evil.example" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expiry",
    [
        "https://evil.example/private",
        "PRIVATE-TOKEN=redacted",
        "2026-11-06T00:00:00Z\\nAuthorization: token PRIVATE",
        "2026-11-32T00:00:00Z",
        "2026-11-06T00:00:00",
        True,
    ],
)
async def test_untrusted_expiry_never_becomes_an_output_channel(monkeypatch, expiry):
    fake = Fake(artifacts=[{**ARTIFACT, "expires_at": expiry}])
    result = await fetch(fake, monkeypatch)
    assert result["ok"] is False
    dumped = json.dumps(result)
    assert "evil.example" not in dumped
    assert "PRIVATE-TOKEN" not in dumped


@pytest.mark.asyncio
async def test_missing_artifact_attempt_never_claims_attempt_proven(monkeypatch):
    fake = Fake(artifacts=[{**ARTIFACT, "workflow_run": {"id": 200, "head_sha": HEAD}}])
    result = await fetch(fake, monkeypatch)
    assert result["ok"] is True
    assert result["result"]["artifacts"][0]["run_attempt_attested"] is False
    assert result["result"]["download_authorized"] is False


@pytest.mark.asyncio
async def test_pr_changed_after_listing_refuses_to_return_stale_metadata(monkeypatch):
    fake = Fake()
    fake.get_pull_request = AsyncMock(side_effect=[PR, {**PR, "state": "closed"}])
    result = await fetch(fake, monkeypatch)
    assert result["ok"] is False
    assert fake.calls == ["run", "artifacts"]


@pytest.mark.asyncio
@pytest.mark.parametrize("head", [None, 123, True, "", "A" * 40, "a" * 39])
async def test_invalid_head_does_not_reach_private_gitea(monkeypatch, head):
    fake = Fake()
    monkeypatch.setenv("GITEA_TOKEN", "PRIVATE")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: lambda token: fake)
    result = await remote.gitea_list_action_run_artifacts(
        "gpakoh", "xloud", 54, 200, head, 2
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_run_attempt_drift_after_listing_fails_closed(monkeypatch):
    fake = Fake()
    fake.get_action_run = AsyncMock(side_effect=[RUN, {**RUN, "run_attempt": 3}])
    result = await fetch(fake, monkeypatch)
    assert result["ok"] is False
    assert result["error"]["code"] == "CI_EVIDENCE_INCOMPLETE"
    assert fake.calls == ["pr", "artifacts", "pr"]


@pytest.mark.asyncio
@pytest.mark.parametrize("max_results", [0, True, -1, 21, "5"])
async def test_invalid_limit_blocks_all_remote_reads(monkeypatch, max_results):
    fake = Fake()
    result = await fetch(fake, monkeypatch, max_results=max_results)
    assert result["ok"] is False
    assert fake.calls == []


@pytest.mark.asyncio
async def test_list_metadata_truncation_is_explicit(monkeypatch):
    artifacts = [{**ARTIFACT, "id": i, "name": f"preview-{i}"} for i in range(1, 4)]
    fake = Fake(artifacts=artifacts)
    result = await fetch(fake, monkeypatch, max_results=2)
    assert result["ok"] is True
    assert result["result"]["total_count"] == 3
    assert len(result["result"]["artifacts"]) == 2
    assert result["result"]["truncated"] is True


@pytest.mark.asyncio
async def test_streamed_metadata_enforces_response_cap_before_json_and_denies_redirect(monkeypatch):
    raw = {"total_count": 1, "artifacts": [ARTIFACT]}
    responses = [
        httpx.Response(200, json=raw),
        httpx.Response(302, headers={"Location": "https://evil.example/private"}),
        httpx.Response(200, content=b"x" * (256 * 1024 + 8192)),
    ]
    for response in responses:
        paths = []
        def handler(request, *, _response=response, _paths=paths):
            _paths.append((str(request.url), request.headers.get("Authorization")))
            return _response
        client = GiteaClient("PRIVATE")
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            base_url="https://git.example/api/v1",
            headers={"Authorization": "token PRIVATE"},
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        try:
            if response.status_code == 200 and response.headers.get("content-type", "").startswith("application/json"):
                obj = await client.list_action_run_artifacts("gpakoh", "xloud", 200)
                assert obj["total_count"] == 1
                assert "archive_download_url" not in str(obj)
            else:
                with pytest.raises(ValueError):
                    await client.list_action_run_artifacts("gpakoh", "xloud", 200)
        finally:
            await client.aclose()
        assert len(paths) == 1
        assert paths[0][0].startswith("https://git.example/api/v1/repos/gpakoh/xloud/actions/runs/200/artifacts")
        assert paths[0][1] == "token PRIVATE"
