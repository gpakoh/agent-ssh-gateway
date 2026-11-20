"""Pin Gitea CI run actor and repository provenance across merge gate reads."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from examples.mcp_server.mcp_infra.adapters import remote

HEAD = "a" * 40
VALID_RUN = {
    "id": 765,
    "run_attempt": 2,
    "event": "pull_request",
    "head_sha": HEAD,
    "status": "completed",
    "conclusion": "success",
    "actor": {"login": "ci-author"},
    "trigger_actor": {"login": "ci-rerun-operator"},
    "repository": {"name": "repo", "full_name": "owner/repo"},
}


def test_provenance_receipt_accepts_different_trigger_actor_and_correct_repo():
    assert remote._ci_run_provenance_receipt(
        VALID_RUN, "owner", "repo"
    ) == ("owner/repo", "ci-author", "ci-rerun-operator")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("actor",), None),
        (("actor",), "ci-author"),
        (("actor", "login"), ""),
        (("actor", "login"), "ci-author\ncredential"),
        (("actor", "login"), " ci-author"),
        (("actor", "login"), 3),
        (("trigger_actor",), None),
        (("trigger_actor", "login"), ""),
        (("trigger_actor", "login"), "rerun\r"),
        (("repository",), None),
        (("repository", "full_name"), "another/repo"),
        (("repository", "full_name"), "owner/repo-evil"),
        (("repository", "name"), "different"),
    ],
)
def test_provenance_receipt_rejects_untrusted_or_changed_identity(path, value):
    run = deepcopy(VALID_RUN)
    if len(path) == 1:
        run[path[0]] = value
    else:
        run[path[0]][path[1]] = value
    assert remote._ci_run_provenance_receipt(run, "owner", "repo") is None


@pytest.mark.asyncio
async def test_job_attestation_refuses_actor_drift_after_jobs():
    run = deepcopy(VALID_RUN)
    client = AsyncMock()
    client.list_action_run_jobs.return_value = {
        "total_count": 1,
        "jobs": [{
            "id": 1,
            "run_id": 765,
            "run_attempt": 2,
            "head_sha": HEAD,
            "name": "Change scope",
            "status": "completed",
            "conclusion": "success",
        }],
    }
    client.get_action_run.return_value = {
        **run,
        "trigger_actor": {"login": "different-rerun-operator"},
    }
    receipt = remote._ci_run_attempt_receipt(run)
    assert receipt is not None
    assert await remote._gitea_ci_job_attestation(
        client, "owner", "repo", receipt,
        ("CI / Change scope (pull_request)",),
        run_provenance=("owner/repo", "ci-author", "ci-rerun-operator"),
    ) == "CI_EVIDENCE_INCOMPLETE"


@pytest.mark.asyncio
async def test_job_attestation_accepts_exact_actor_and_trigger_after_jobs():
    run = deepcopy(VALID_RUN)
    client = AsyncMock()
    client.list_action_run_jobs.return_value = {
        "total_count": 1,
        "jobs": [{
            "id": 1,
            "run_id": 765,
            "run_attempt": 2,
            "head_sha": HEAD,
            "name": "Change scope",
            "status": "completed",
            "conclusion": "success",
        }],
    }
    client.get_action_run.return_value = run
    receipt = remote._ci_run_attempt_receipt(run)
    assert receipt is not None
    assert await remote._gitea_ci_job_attestation(
        client, "owner", "repo", receipt,
        ("CI / Change scope (pull_request)",),
        run_provenance=("owner/repo", "ci-author", "ci-rerun-operator"),
    ) == "CI_GREEN"
