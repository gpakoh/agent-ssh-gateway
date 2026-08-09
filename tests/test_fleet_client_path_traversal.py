"""Regression: gitea_client.py/github_client.py built request paths via
"/repos/{owner}/{repo}/...".format(owner=owner, repo=repo) with zero
validation on owner/repo/path. httpx normalizes ".." segments in the
resulting path against the *full* URL (base_url's own path included), so
an owner like "foo/../../admin" reached {api_base}/admin/... instead of
{api_base}/repos/foo/.../admin/... — a real escape past ALLOWED_ENDPOINTS,
using the fleet adapter's own GITEA_TOKEN/GITHUB_TOKEN credential.

Proven empirically: build the request via httpx directly (matching what
the client does internally) and confirm the resolved URL for a malicious
owner lands outside /repos/ entirely.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "mcp_client_remote"))

from fleet.gitea_client import GiteaClient  # noqa: E402
from fleet.github_client import GitHubClient  # noqa: E402


def test_httpx_normalizes_dotdot_past_repos_prefix():
    """The underlying mechanism: proves this isn't just a theoretical risk."""
    client = httpx.AsyncClient(base_url="https://host.example/api/v1")
    req = client.build_request("GET", "/repos/foo/../../admin/x/branches")
    assert str(req.url) == "https://host.example/api/v1/admin/x/branches"


def _recording_transport(seen: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True}, request=request)

    return httpx.MockTransport(handler)


class TestGiteaClientPathTraversal:
    @pytest.mark.asyncio
    async def test_owner_traversal_rejected(self):
        seen: dict = {}
        client = GiteaClient("fake-token")
        client._client = httpx.AsyncClient(
            base_url="https://gitea.example/api/v1",
            transport=_recording_transport(seen),
        )
        with pytest.raises(ValueError, match="Invalid owner"):
            await client.get_repo("foo/../../admin", "x")
        assert "url" not in seen, "request must never be sent for a malicious owner"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_repo_traversal_rejected(self):
        seen: dict = {}
        client = GiteaClient("fake-token")
        client._client = httpx.AsyncClient(
            base_url="https://gitea.example/api/v1",
            transport=_recording_transport(seen),
        )
        with pytest.raises(ValueError, match="Invalid repo"):
            await client.get_repo("owner", "../../admin")
        assert "url" not in seen
        await client.aclose()

    @pytest.mark.asyncio
    async def test_get_file_path_traversal_rejected(self):
        seen: dict = {}
        client = GiteaClient("fake-token")
        client._client = httpx.AsyncClient(
            base_url="https://gitea.example/api/v1",
            transport=_recording_transport(seen),
        )
        with pytest.raises(ValueError, match="Invalid path"):
            await client.get_file("owner", "repo", "../../../etc/passwd")
        assert "url" not in seen
        await client.aclose()

    @pytest.mark.asyncio
    async def test_normal_call_still_works(self):
        seen: dict = {}
        client = GiteaClient("fake-token")
        client._client = httpx.AsyncClient(
            base_url="https://gitea.example/api/v1",
            transport=_recording_transport(seen),
        )
        await client.get_repo("owner", "repo")
        assert seen["url"] == "https://gitea.example/api/v1/repos/owner/repo"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_get_file_normal_nested_path_still_works(self):
        seen: dict = {}
        client = GiteaClient("fake-token")
        client._client = httpx.AsyncClient(
            base_url="https://gitea.example/api/v1",
            transport=_recording_transport(seen),
        )
        await client.get_file("owner", "repo", "src/nested/main.py")
        assert seen["url"] == (
            "https://gitea.example/api/v1/repos/owner/repo/contents/src/nested/main.py"
        )
        await client.aclose()

    @pytest.mark.asyncio
    async def test_get_file_empty_path_lists_repo_root(self):
        """P2 audit finding: validate_repo_path() rejects an empty path
        (by design -- distinguishing it from a malicious/invalid one), so
        there was previously no way to request a repo's top-level listing
        through get_file(). path="" (the new default) must route to the
        path-less contents endpoint instead of being rejected."""
        seen: dict = {}
        client = GiteaClient("fake-token")
        client._client = httpx.AsyncClient(
            base_url="https://gitea.example/api/v1",
            transport=_recording_transport(seen),
        )
        await client.get_file("owner", "repo")
        assert seen["url"] == "https://gitea.example/api/v1/repos/owner/repo/contents"
        await client.aclose()


class TestGitHubClientPathTraversal:
    @pytest.mark.asyncio
    async def test_owner_traversal_rejected(self):
        seen: dict = {}
        client = GitHubClient("fake-token")
        client._client = httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=_recording_transport(seen),
        )
        with pytest.raises(ValueError, match="Invalid owner"):
            await client.get_repo("foo/../../user", "x")
        assert "url" not in seen
        await client.aclose()

    @pytest.mark.asyncio
    async def test_get_file_path_traversal_rejected(self):
        seen: dict = {}
        client = GitHubClient("fake-token")
        client._client = httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=_recording_transport(seen),
        )
        with pytest.raises(ValueError, match="Invalid path"):
            await client.get_file("owner", "repo", "../../../secrets")
        assert "url" not in seen
        await client.aclose()

    @pytest.mark.asyncio
    async def test_normal_call_still_works(self):
        seen: dict = {}
        client = GitHubClient("fake-token")
        client._client = httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=_recording_transport(seen),
        )
        await client.get_repo("owner", "repo")
        assert seen["url"] == "https://api.github.com/repos/owner/repo"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_get_file_empty_path_lists_repo_root(self):
        """P2 audit finding: see the Gitea counterpart above."""
        seen: dict = {}
        client = GitHubClient("fake-token")
        client._client = httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=_recording_transport(seen),
        )
        await client.get_file("owner", "repo")
        assert seen["url"] == "https://api.github.com/repos/owner/repo/contents"
        await client.aclose()
