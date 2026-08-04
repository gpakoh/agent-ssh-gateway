"""Regression tests: fleet Gitea/GitHub clients must never leak the resolved
base URL (internal host/IP for Gitea, in particular) through an error
message.

Context: httpx.HTTPStatusError's own message embeds the fully-resolved
absolute URL. FastMCP's tool-call error handler sends str(exception) straight
to the external MCP client verbatim on any failure
(mcp/server/lowlevel/server.py: `except Exception as e: return
self._make_error_result(str(e))`) — so an ordinary mistake (a typo'd repo
name, a 404) leaked GITEA_API_BASE's internal host:port, exactly what
GITEA_FORWARDED_HOST/PROTO exist to hide from *successful* responses. No
existing test exercised the client's HTTP error path at all — only
normalize_list_response (a pure function, no network) was covered.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "mcp_client_remote"))

from fleet.gitea_client import GiteaClient  # noqa: E402
from fleet.github_client import GitHubClient  # noqa: E402

INTERNAL_HOST = "http://192.168.1.103:3005"


def _not_found_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "repository not found"}, request=request)

    return httpx.MockTransport(handler)


class TestGiteaClientErrorSanitization:
    @pytest.mark.asyncio
    async def test_404_does_not_leak_internal_host(self, monkeypatch):
        monkeypatch.setenv("GITEA_API_BASE", INTERNAL_HOST + "/api/v1")
        client = GiteaClient("fake-token")
        client._client = httpx.AsyncClient(
            base_url=INTERNAL_HOST + "/api/v1",
            transport=_not_found_transport(),
        )

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await client.get_repo("owner", "does-not-exist")

        message = str(excinfo.value)
        assert "192.168" not in message
        assert INTERNAL_HOST not in message
        assert "404" in message
        assert "/repos/owner/does-not-exist" in message

        await client.aclose()


class TestGitHubClientErrorSanitization:
    @pytest.mark.asyncio
    async def test_404_does_not_leak_base_url(self, monkeypatch):
        client = GitHubClient("fake-token")
        client._client = httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=_not_found_transport(),
        )

        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await client.get_repo("owner", "does-not-exist")

        message = str(excinfo.value)
        assert "api.github.com" not in message
        assert "404" in message
        assert "/repos/owner/does-not-exist" in message

        await client.aclose()
