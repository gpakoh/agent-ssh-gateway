"""Content contract for gitea_get_file / GiteaClient.get_file.

Goal: keep large base64 blobs out of MCP tool responses by default while
preserving an explicit, schema-visible way to fetch real file content.

Two layers are exercised:

  * GiteaClient.get_file -- the actual decode/truncate/metadata-drop logic
    (this is the single choke point the MCP tool and the fleet servers share).
  * the gitea_get_file MCP tool wrapper -- parameter propagation (the safe
    default bound, the include_content opt-out, the max_content_bytes bound)
    and the negative-input guard.

Before this change the client's 256 KiB MAX_FILE_SIZE cap was the only
guard, so a 256 KiB blob was still dumped into chat; and there was no way
to request metadata only.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

_FLEET_DIR = str(Path(__file__).resolve().parents[1] / "examples" / "mcp_client_remote")
if _FLEET_DIR not in sys.path:
    sys.path.insert(0, _FLEET_DIR)

_MCP_SERVER_DIR = str(Path(__file__).resolve().parents[1] / "examples" / "mcp_server")
if _MCP_SERVER_DIR not in sys.path:
    sys.path.insert(0, _MCP_SERVER_DIR)

from examples.mcp_client_remote.fleet.gitea_client import (  # noqa: E402
    DEFAULT_GET_FILE_MAX_CONTENT_BYTES,
    GiteaClient,
)
from examples.mcp_server import server as mcp_server_mod  # noqa: E402

SMALL_TEXT = b"def hello():\n    return 42\n"
FILE_PAYLOAD = {
    "path": "examples/main.py",
    "name": "main.py",
    "sha": "abc123def456",
    "download_url": "https://git.example.com/raw/gpakoh/repo/main.py",
    "html_url": "https://git.example.com/gpakoh/repo/src/branch/main.py",
    "last_commit_sha": "feedbeefcafe",
    "content": base64.b64encode(SMALL_TEXT).decode(),
    "encoding": "base64",
}


def _file_transport(content: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = dict(FILE_PAYLOAD)
        payload["content"] = content
        return httpx.Response(200, json=payload, request=request)

    return httpx.MockTransport(handler)


def _client_with(payload_content: str) -> GiteaClient:
    client = GiteaClient("fake-token")
    client._client = httpx.AsyncClient(
        base_url="https://gitea.example/api/v1",
        transport=_file_transport(payload_content),
    )
    return client


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode()


# ---------------------------------------------------------------------------
# Client-level contract (the decode / truncate / metadata-drop logic).
# ---------------------------------------------------------------------------


class TestGiteaClientGetFileContentContract:
    @pytest.mark.asyncio
    async def test_default_returns_small_content_unchanged(self):
        """Regression: default call must keep current behavior -- content
        present, no truncation -- for a file under any bound."""
        client = _client_with(_b64(SMALL_TEXT))
        result = await client.get_file("gpakoh", "repo", "examples/main.py")
        assert result["content"] == _b64(SMALL_TEXT)
        assert "truncated" not in result
        assert "content_omitted" not in result
        assert result["path"] == "examples/main.py" and result["sha"] == "abc123def456"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_include_content_false_is_metadata_only(self):
        """include_content=False must drop content and mark content_omitted,
        keeping everything a caller needs to fetch it later."""
        client = _client_with(_b64(SMALL_TEXT))
        result = await client.get_file("gpakoh", "repo", "examples/main.py", include_content=False)
        assert "content" not in result
        assert result["content_omitted"] is True
        assert result["path"] == "examples/main.py"
        assert result["name"] == "main.py"
        assert result["sha"] == "abc123def456"
        assert result["last_commit_sha"] == "feedbeefcafe"
        assert result["html_url"]
        assert result["download_url"]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_default_bound_truncates_large_content(self):
        """Safety default bound (16 KiB) must truncate a large blob and carry
        a size marker instead of dumping it into the response."""
        big = _b64(b"x" * (DEFAULT_GET_FILE_MAX_CONTENT_BYTES + 1))
        client = _client_with(big)
        result = await client.get_file(
            "gpakoh", "repo", "big.bin", max_content_bytes=DEFAULT_GET_FILE_MAX_CONTENT_BYTES
        )
        assert result["truncated"] is True
        assert result["content_bytes"] == DEFAULT_GET_FILE_MAX_CONTENT_BYTES + 1
        assert "x" * 64 not in result["content"]
        assert "truncated" in result["content"].lower() or "[truncated" in result["content"]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_custom_bound_respected(self):
        """A caller-provided smaller bound must be honored."""
        raw = b"y" * 4096
        client = _client_with(_b64(raw))
        result = await client.get_file("gpakoh", "repo", "big.bin", max_content_bytes=1024)
        assert result["truncated"] is True
        assert result["content_bytes"] == 4096
        assert "[truncated 4096 bytes > 1024 limit]" in result["content"]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_none_bound_falls_back_to_previous_cap_behavior(self):
        """max_content_bytes=None keeps the old MAX_FILE_SIZE safety cap, so
        callers that never pass a bound see no behavior change for large files."""
        raw = b"z" * (17 * 1024)  # > 16 KiB default but < 256 KiB old cap
        client = _client_with(_b64(raw))
        result = await client.get_file("gpakoh", "repo", "big.bin")  # bound None
        assert result["content"] == _b64(raw)  # under old 256 KiB cap -> unchanged
        assert "truncated" not in result
        await client.aclose()

    @pytest.mark.asyncio
    async def test_non_positive_bound_rejected(self):
        """A non-positive explicit bound must be rejected, not silently mishandled."""
        client = _client_with(_b64(SMALL_TEXT))
        with pytest.raises(ValueError, match="max_content_bytes"):
            await client.get_file("gpakoh", "repo", "main.py", max_content_bytes=0)
        await client.aclose()


# ---------------------------------------------------------------------------
# Tool-level contract (the gitea_get_file MCP wrapper's parameter surface).
# ---------------------------------------------------------------------------


class _FakeRemoteClient:
    def __init__(self, methods: dict[str, AsyncMock]) -> None:
        self._methods = methods
        for name, mock in methods.items():
            setattr(self, name, mock)

    async def __aenter__(self) -> _FakeRemoteClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class TestGiteaGetFileToolContract:
    def _patch_client(self, monkeypatch, get_file: AsyncMock) -> None:
        monkeypatch.setattr(
            mcp_server_mod,
            "GiteaClient",
            lambda token: _FakeRemoteClient({"get_file": get_file}),
        )
        monkeypatch.setenv("GITEA_TOKEN", "tok")

    @pytest.mark.asyncio
    async def test_default_passes_safe_bound(self, monkeypatch):
        """Default call must send the safe 16 KiB bound so a large blob is
        truncated server-side even when the caller never opts in."""
        get_file = AsyncMock(return_value={"path": "main.py", "content_omitted": False})
        self._patch_client(monkeypatch, get_file)
        result = await mcp_server_mod.gitea_get_file("gpakoh", "repo", "main.py")
        assert result["ok"] is True
        get_file.assert_awaited_once_with(
            "gpakoh",
            "repo",
            "main.py",
            branch=None,
            include_content=True,
            max_content_bytes=DEFAULT_GET_FILE_MAX_CONTENT_BYTES,
        )

    @pytest.mark.asyncio
    async def test_include_content_false_is_metadata_only(self, monkeypatch):
        get_file = AsyncMock(return_value={"path": "main.py", "content_omitted": True})
        self._patch_client(monkeypatch, get_file)
        result = await mcp_server_mod.gitea_get_file(
            "gpakoh", "repo", "main.py", include_content=False
        )
        assert result["ok"] is True
        get_file.assert_awaited_once_with(
            "gpakoh",
            "repo",
            "main.py",
            branch=None,
            include_content=False,
            max_content_bytes=DEFAULT_GET_FILE_MAX_CONTENT_BYTES,
        )

    @pytest.mark.asyncio
    async def test_zero_bound_falls_back_to_client_cap(self, monkeypatch):
        """max_content_bytes=0 must translate to None (the client's old
        256 KiB safety cap), preserving the previous full-content path."""
        get_file = AsyncMock(return_value={"path": "main.py"})
        self._patch_client(monkeypatch, get_file)
        result = await mcp_server_mod.gitea_get_file(
            "gpakoh", "repo", "main.py", max_content_bytes=0
        )
        assert result["ok"] is True
        get_file.assert_awaited_once_with(
            "gpakoh",
            "repo",
            "main.py",
            branch=None,
            include_content=True,
            max_content_bytes=None,
        )

    @pytest.mark.asyncio
    async def test_negative_bound_rejected_as_invalid_input(self, monkeypatch):
        get_file = AsyncMock(return_value={"path": "main.py"})
        self._patch_client(monkeypatch, get_file)
        result = await mcp_server_mod.gitea_get_file(
            "gpakoh", "repo", "main.py", max_content_bytes=-1
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"
        get_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_full_content_still_fetches(self, monkeypatch):
        """Explicitly raising the bound must still return full content."""
        payload = {"path": "main.py", "content": _b64(b"full"), "encoding": "base64"}
        get_file = AsyncMock(return_value=payload)
        self._patch_client(monkeypatch, get_file)
        result = await mcp_server_mod.gitea_get_file(
            "gpakoh", "repo", "main.py", include_content=True, max_content_bytes=0
        )
        assert result["ok"] is True
        assert result["result"]["content"]  # content present
        get_file.assert_awaited_once_with(
            "gpakoh",
            "repo",
            "main.py",
            branch=None,
            include_content=True,
            max_content_bytes=None,
        )
