"""Cross-layer contract checks for standalone artifact provenance."""

from __future__ import annotations

import httpx
import pytest

from examples.mcp_client_remote.fleet.gitea_artifacts_provenance import (
    ArtifactLease,
    _verify_exact_artifact,
)
from examples.mcp_client_remote.fleet.gitea_artifacts_spool import (
    ArtifactLease as SpoolLease,
)
from examples.mcp_client_remote.fleet.gitea_artifacts_transport import (
    ArtifactTransportError,
)
from examples.mcp_client_remote.fleet.gitea_client import GiteaClient


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [2, None, 3])
async def test_real_gitea_metadata_matches_provenance_contract(attempt):
    head = "a" * 40
    seen = []

    def respond(request):
        path = request.url.path
        seen.append(path)
        if path.endswith("/pulls/54"):
            payload = {
                "number": 54, "state": "open", "merged": False,
                "head": {"sha": head, "repo": {"full_name": "gpakoh/xloud"}},
            }
        elif path.endswith("/actions/runs/200"):
            payload = {
                "id": 200, "head_sha": head, "run_attempt": 2,
                "event": "pull_request", "status": "completed",
                "conclusion": "success",
                "repository": {"full_name": "gpakoh/xloud"},
            }
        elif path.endswith("/actions/runs/200/artifacts"):
            payload = {
                "total_count": 1,
                "artifacts": [{
                    "id": 456, "name": "preview-bundle",
                    "size_in_bytes": 1024, "expired": False,
                    "expires_at": "2099-01-01T00:00:00Z",
                    "workflow_run": {
                        "id": 200, "head_sha": head, "run_attempt": attempt,
                    },
                }],
            }
        else:
            raise AssertionError("unexpected Gitea request")
        return httpx.Response(200, json=payload)

    client = GiteaClient("test-only")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        base_url="https://gitea.example/api/v1",
    )
    try:
        lease = ArtifactLease("gpakoh", "xloud", 54, 200, head, 2, 456)
        assert SpoolLease is ArtifactLease
        if attempt == 2:
            proof = await _verify_exact_artifact(client, lease)
            assert proof["artifact_id"] == 456
            assert proof["size_bytes"] == 1024
        else:
            with pytest.raises(ArtifactTransportError, match="attempt"):
                await _verify_exact_artifact(client, lease)
        assert len(seen) == 3
    finally:
        await client.aclose()
