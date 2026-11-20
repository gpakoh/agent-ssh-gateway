"""A single Gitea PR read is not proof of a concurrent merge (#890).

When exact-head CI evidence is incomplete, a merged PR must be independently
confirmed in a second read. Neither a transient response nor a drifting merge
commit may turn an incomplete CI gate into a reported concurrent merge.
"""
from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import pytest

from examples.mcp_server.mcp_infra.adapters import remote

HEAD = "a" * 40
BASE = "b" * 40
MERGE = "c" * 40


def merged_receipt() -> dict:
    return {
        "number": 25,
        "state": "closed",
        "merged": True,
        "merge_commit_sha": MERGE,
        "head": {"sha": HEAD, "ref": "feat/one"},
        "base": {"sha": BASE, "ref": "master"},
    }


async def check(client: AsyncMock):
    return await remote._concurrent_merge_after_incomplete_ci(
        client, "owner", "repo", 25, HEAD, "master"
    )


@pytest.mark.asyncio
async def test_two_stable_independent_observations_report_concurrent_merge():
    client = AsyncMock()
    client.get_pull_request.side_effect = [
        merged_receipt(), deepcopy(merged_receipt())
    ]
    result = await check(client)
    assert result["ok"] is False
    assert result["error"]["code"] == "PR_NOT_OPEN"
    assert result["error"]["details"]["mutation_started"] is False
    assert result["error"]["details"]["observed_merge_commit_sha"] == MERGE
    assert client.get_pull_request.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field, changed",
    [
        ("number", 26),
        ("state", "open"),
        ("merged", False),
        ("merge_commit_sha", "d" * 40),
        ("head", {"sha": "e" * 40, "ref": "feat/one"}),
        ("base", {"sha": BASE, "ref": "develop"}),
        ("base", {"sha": "invalid", "ref": "master"}),
    ],
)
async def test_changed_second_observation_does_not_report_merged(field, changed):
    first = merged_receipt()
    second = deepcopy(first)
    second[field] = changed
    client = AsyncMock()
    client.get_pull_request.side_effect = [first, second]
    assert await check(client) is None
    assert client.get_pull_request.await_count == 2


@pytest.mark.asyncio
async def test_second_observation_transport_error_does_not_report_merged():
    client = AsyncMock()
    client.get_pull_request.side_effect = [
        merged_receipt(),
        httpx.ReadTimeout("provider response dropped"),
    ]
    assert await check(client) is None
    assert client.get_pull_request.await_count == 2


@pytest.mark.asyncio
async def test_untrusted_first_observation_does_not_trigger_extra_reads():
    client = AsyncMock()
    first = merged_receipt()
    first["head"] = {"sha": "d" * 40}
    client.get_pull_request.side_effect = [first]
    assert await check(client) is None
    assert client.get_pull_request.await_count == 1
