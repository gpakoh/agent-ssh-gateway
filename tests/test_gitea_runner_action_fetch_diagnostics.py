"""Bounded classification of Gitea runner-side GitHub action fetch failures.

Diagnostics are read-only and never authorize job reruns or Docker cleanup.
"""
from __future__ import annotations

import pytest

from examples.mcp_server.mcp_infra.adapters import remote

TIMESTAMP = "2026-11-21T16:55:37.4361984Z"


def _log(action: str, *, cleanup: bool = False) -> dict[str, object]:
    lines = [
        "2026-11-21T16:55:28.5616144Z runner-python311(version:v1.0.8) received task 112706 of job coverage, be triggered by event: pull_request",
        f"{TIMESTAMP} git clone 'https://github.com/actions/{action}' # ref=v3",
        f'2026-11-21T16:56:53.3261019Z ##[error]Get "https://github.com/actions/{action}/info/refs?service=git-upload-pack": read tcp 172.19.0.35:33390->140.82.121.3:443: read: connection reset by peer',
        "2026-11-21T16:56:53.3264026Z Cleaning up container for job Coverage 3.12",
    ]
    if cleanup:
        lines.append(
            "2026-11-21T16:57:53.326Z failed to remove container: Delete "
            '"http://%2Fvar%2Frun%2Fdocker.sock/...": context deadline exceeded'
        )
    lines.append("2026-11-21T16:56:56.8935424Z Job failed")
    return {
        "logs": "\n".join(lines),
        "truncated": False,
        "line_truncated": False,
    }


@pytest.mark.parametrize("action", ["upload-artifact", "download-artifact"])
@pytest.mark.parametrize("cleanup", [False, True])
def test_runner_artifact_fetch_failure_has_non_authorizing_diagnostic(
    action: str, cleanup: bool
) -> None:
    diagnostic = remote._diagnose_action_runner_fetch_failure(_log(action, cleanup=cleanup))
    assert diagnostic is not None
    assert diagnostic["failure_class"] == "external_action_source_fetch"
    assert diagnostic["action"] == f"actions/{action}@v3"
    assert diagnostic["network_error_class"] == "connection_reset"
    assert diagnostic["cleanup_timeout_observed"] is cleanup
    assert diagnostic["rerun_authorized"] is False
    assert diagnostic["evidence_scope"] == "bounded_redacted_log_signature"


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("truncated", True),
        ("line_truncated", True),
        ("logs", None),
    ],
)
def test_partial_or_invalid_log_does_not_claim_external_fetch(
    change: str, value: object
) -> None:
    payload = _log("download-artifact")
    payload[change] = value
    assert remote._diagnose_action_runner_fetch_failure(payload) is None


@pytest.mark.parametrize(
    "logs",
    [
        # The failure is from the test suite, not GitHub action checkout.
        "2026-11-21T16:55:37Z pytest failed\n2026-11-21T16:56:56Z Job failed",
        # A malicious test may echo superficially convincing text.
        'echo git clone https://github.com/actions/upload-artifact # ref=v3\n'
        'echo ##[error]Get "https://github.com/actions/upload-artifact/info/refs?service=git-upload-pack": connection reset by peer',
        # Fetch action and error action are inconsistent.
        _log("download-artifact")["logs"].replace(
            "##[error]Get \"https://github.com/actions/download-artifact",
            "##[error]Get \"https://github.com/actions/upload-artifact",
        ),
        # Job outcome not known, or still executing.
        _log("upload-artifact")["logs"].replace("Job failed", "Job running"),
        # A generic network timeout outside checkout is not the supported signal.
        '2026-11-21T16:56:53Z ##[error]GET https://api.github.com failed: i/o timeout\n'
        '2026-11-21T16:56:56Z Job failed',
    ],
)
def test_untrusted_or_unrelated_log_does_not_classify(logs: str) -> None:
    assert remote._diagnose_action_runner_fetch_failure(
        {"logs": logs, "truncated": False, "line_truncated": False}
    ) is None


def test_timeout_classification_is_bounded_and_redacted() -> None:
    payload = _log("upload-artifact")
    payload["logs"] = str(payload["logs"]).replace("connection reset by peer", "i/o timeout")
    diagnostic = remote._diagnose_action_runner_fetch_failure(payload)
    assert diagnostic is not None
    assert diagnostic["network_error_class"] == "network_timeout"
    assert "172.19." not in str(diagnostic)
    assert "http" not in str(diagnostic).lower()


def test_large_log_cannot_force_unbounded_signature_scans() -> None:
    payload = _log("upload-artifact")
    payload["logs"] = "x" * 90000 + str(payload["logs"])
    assert remote._diagnose_action_runner_fetch_failure(payload) is None


@pytest.mark.asyncio
async def test_gitea_log_tool_reports_runner_failure_without_rerun_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGiteaClient:
        def __init__(self, _token: str) -> None:
            pass

        async def __aenter__(self) -> FakeGiteaClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def get_action_job_logs(
            self, owner: str, repo: str, job_id: int, max_bytes: int
        ) -> dict[str, object]:
            assert (owner, repo, job_id, max_bytes) == ("owner", "repo", 170283, 4096)
            return {"job_id": job_id, **_log("download-artifact", cleanup=True)}

    monkeypatch.setenv("GITEA_TOKEN", "redacted-fake-token")
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: FakeGiteaClient)

    result = await remote.gitea_get_action_job_logs(
        "owner", "repo", 170283, max_bytes=4096, tail_lines=80
    )
    assert result["ok"] is True
    diagnostic = result["result"]["runner_failure_diagnostic"]
    assert diagnostic["failure_class"] == "external_action_source_fetch"
    assert diagnostic["network_error_class"] == "connection_reset"
    assert diagnostic["cleanup_timeout_observed"] is True
    assert diagnostic["rerun_authorized"] is False
    assert "redacted-fake-token" not in str(result)

    short_tail = await remote.gitea_get_action_job_logs(
        "owner", "repo", 170283, max_bytes=4096, tail_lines=2
    )
    assert short_tail["ok"] is True
    assert short_tail["result"]["line_truncated"] is True
    assert "runner_failure_diagnostic" not in short_tail["result"]