"""Workflow dispatch: strict preflight, at-most-once POST, honest correlation."""

from __future__ import annotations

import asyncio
import base64
import json
import multiprocessing
import os
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app.gitea_workflow_dispatch import (
    DispatchRefused,
    digest,
    dispatch_status,
    dispatch_workflow,
    operation_lock,
    workflow_inputs,
)
from examples.mcp_client_remote.fleet.gitea_client import GiteaClient

HEAD = "a" * 40
KEY = "qualified-workflow-676-v1"
ACTOR = "b" * 64


@pytest.fixture(autouse=True)
def _hermetic_transport_environment(monkeypatch):
    # These tests use MockTransport only; ambient developer proxies must not
    # introduce optional SOCKS dependencies while constructing the client.
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


PATH = ".github/workflows/dispatch-probe.yml"
RAW = b"""name: Safe dispatch probe
on:
  workflow_dispatch:
    inputs:
      approved:
        type: boolean
        required: true
      label:
        type: string
        required: true
      level:
        type: choice
        options: [low, high]
        default: low
"""


def run(run_id=101, **changes):
    return {
        "id": run_id,
        "event": "workflow_dispatch",
        "head_sha": HEAD,
        "head_branch": "master",
        "run_attempt": 1,
        "actor": {"login": "operator"},
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": "gpakoh/project"},
        **changes,
    }


class Client:
    def __init__(self):
        self.version = "1.27.0+test"
        self.head = HEAD
        self.raw = RAW
        self.encoded = None
        self.posts = 0
        self.applied = False
        self.return_id = 101
        self.before = [run(100)]
        self.after = [run()]
        self.actual = run()
        self.permission = True
        self.workflow_path = PATH
        self.workflow_state = "active"
        self.failure = None
        self.before_post = lambda: None

    async def get_user(self):
        return {"login": "operator"}

    async def get_repo(self, owner, repo):
        return {
            "full_name": f"{owner}/{repo}",
            "archived": False,
            "permissions": {"push": self.permission, "admin": self.permission},
            "html_url": f"https://git.example/{owner}/{repo}",
        }

    async def get_version(self):
        return {"version": self.version}

    async def get_branch(self, owner, repo, branch):
        assert branch == "master"
        return {"commit": {"id": self.head}}

    async def list_workflows(self, owner, repo):
        return {
            "workflows": [
                {
                    "id": "dispatch-probe.yml",
                    "path": self.workflow_path,
                    "state": self.workflow_state,
                }
            ]
        }

    async def get_file(self, owner, repo, *, path, branch, include_content, max_content_bytes):
        assert branch == HEAD and path == PATH and include_content is True
        return {
            "path": path,
            "type": "file",
            "encoding": "base64",
            "resolved_commit_sha": HEAD,
            "content": base64.b64encode(self.raw).decode(),
        }

    async def list_workflow_runs(self, owner, repo, workflow_id):
        assert workflow_id == "dispatch-probe.yml"
        return {"workflow_runs": self.after if self.applied else self.before}

    async def get_action_run(self, owner, repo, run_id):
        return self.actual

    async def dispatch_workflow(self, owner, repo, workflow_id, ref, inputs):
        self.before_post()
        self.posts += 1
        self.applied = True
        self.encoded = inputs
        if self.failure:
            raise self.failure
        return {"run_id": self.return_id}


def request(root, **changes):
    return {
        "owner": "gpakoh",
        "repo": "project",
        "workflow": PATH,
        "ref": "refs/heads/master",
        "expected_ref_sha": HEAD,
        "inputs": {"approved": True, "label": "not-a-secret-value"},
        "operation_key": KEY,
        "reason": "Operator explicitly requests this harmless workflow",
        "actor": ACTOR,
        "journal_dir": root,
        "audit": lambda event: None,
        **changes,
    }


async def status(client, root, **changes):
    return await dispatch_status(
        client,
        owner="gpakoh",
        repo="project",
        operation_key=KEY,
        actor=ACTOR,
        journal_dir=root,
        **changes,
    )


@pytest.mark.asyncio
async def test_dispatch_binds_only_direct_returned_id_then_reads_without_repeating(tmp_path):
    client = Client()
    result = await dispatch_workflow(client, **request(tmp_path))
    assert result["state"] == "bound" and result["verified"] is True
    assert result["run_id"] == 101 and result["conclusion"] == "success"
    assert result["html_url"] == "https://git.example/gpakoh/project/actions/runs/101"
    assert client.encoded == {"approved": "true", "label": "not-a-secret-value", "level": "low"}
    assert await dispatch_workflow(client, **request(tmp_path)) == result
    assert await status(client, tmp_path) == result
    assert client.posts == 1
    assert "not-a-secret-value" not in next(tmp_path.glob("*.json")).read_text()


@pytest.mark.asyncio
async def test_receipt_and_required_audit_precede_post(tmp_path):
    client = Client()
    audit = []

    def before():
        receipt = json.loads(next(tmp_path.glob("*.json")).read_text())
        assert receipt["phase"] == "dispatched" and receipt["run_id"] is None
        assert receipt["payload_sha256"] == audit[0]["payload_sha256"]

    client.before_post = before
    await dispatch_workflow(client, **request(tmp_path, audit=audit.append))
    assert client.posts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [httpx.ReadTimeout("private-response"), RuntimeError("private-response")]
)
async def test_lost_response_never_claims_single_matching_run_as_ours(tmp_path, failure):
    client = Client()
    client.failure = failure
    result = await dispatch_workflow(client, **request(tmp_path))
    assert result["state"] == "outcome_unknown"
    assert result["candidate_run_ids"] == [101] and result["verified"] is False
    assert result["run_id"] is None
    assert "private-response" not in repr(result)
    await dispatch_workflow(client, **request(tmp_path))
    assert client.posts == 1


@pytest.mark.asyncio
async def test_no_content_response_remains_unbound_even_with_multiple_candidates(tmp_path):
    client = Client()
    client.return_id = None
    client.after = [run(), run(102)]
    result = await dispatch_workflow(client, **request(tmp_path))
    assert result["state"] == "accepted_unbound"
    assert result["candidate_run_ids"] == [101, 102] and result["run_id"] is None
    assert (await status(client, tmp_path))["verified"] is False
    assert client.posts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"head_sha": "c" * 40},
        {"event": "push"},
        {"actor": {"login": "someone"}},
        {"repository": {"full_name": "gpakoh/other"}},
        {"run_attempt": True},
        {"head_branch": "other"},
        {"id": True},
    ],
)
async def test_direct_id_with_wrong_run_identity_is_not_verified(tmp_path, change):
    client = Client()
    client.actual.update(change)
    result = await dispatch_workflow(client, **request(tmp_path))
    assert result["state"] == "run_identity_mismatch" and result["verified"] is False
    assert client.posts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"inputs": {"approved": False, "label": "other"}},
        {"expected_ref_sha": "c" * 40},
        {"repo": "other"},
        {"reason": "A changed operator explanation of the request"},
    ],
)
async def test_changed_payload_cannot_reuse_operation_key(tmp_path, change):
    client = Client()
    await dispatch_workflow(client, **request(tmp_path))
    with pytest.raises(DispatchRefused, match="different request"):
        await dispatch_workflow(client, **request(tmp_path, **change))
    assert client.posts == 1


@pytest.mark.asyncio
async def test_other_caller_cannot_reuse_or_read_operation(tmp_path):
    client = Client()
    await dispatch_workflow(client, **request(tmp_path))
    with pytest.raises(DispatchRefused, match="another caller"):
        await dispatch_workflow(client, **request(tmp_path, actor="c" * 64))
    with pytest.raises(DispatchRefused, match="caller"):
        await dispatch_status(
            client,
            owner="gpakoh",
            repo="project",
            operation_key=KEY,
            actor="c" * 64,
            journal_dir=tmp_path,
        )
    assert client.posts == 1


@pytest.mark.asyncio
async def test_dry_run_checks_inputs_without_reserving_or_posting(tmp_path):
    client = Client()
    result = await dispatch_workflow(client, **request(tmp_path, dry_run=True))
    assert result["state"] == "preflight_passed" and result["dispatched"] is False
    assert not list(tmp_path.glob("*.json")) and client.posts == 0
    assert (await status(client, tmp_path))["state"] == "not_found"
    await dispatch_workflow(client, **request(tmp_path))
    assert client.posts == 1


@pytest.mark.parametrize(
    "inputs",
    [
        {"approved": "true", "label": "text"},
        {"approved": 1, "label": "text"},
        {"approved": True, "label": False},
        {"approved": True},
        {"approved": True, "label": "text", "extra": "no"},
        {"approved": True, "label": "text", "level": "unknown"},
        {"approved": True, "label": "x" * 2049},
        {"approved": True, "label": "x\n"},
    ],
)
def test_input_schema_refuses_coercion_unknown_or_unbounded_values(inputs):
    with pytest.raises(DispatchRefused):
        workflow_inputs(RAW, inputs)


@pytest.mark.parametrize(
    "raw",
    [
        b"on: push",
        b"on: {workflow_dispatch: {inputs: {token: {type: string}}}}",
        b"on: {workflow_dispatch: {inputs: {target: {type: environment}}}}",
        b"on: {workflow_dispatch: null}\non: push",
        b"!!python/object:foo {}",
        b"x" * 65537,
    ],
)
def test_nonmanual_unsafe_or_duplicate_workflow_is_refused(raw):
    with pytest.raises(DispatchRefused):
        workflow_inputs(raw, {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"ref": "master"},
        {"ref": "refs/heads/../x"},
        {"workflow": "https://evil/x.yml"},
        {"workflow": ".github/workflows/../x.yml"},
        {"owner": "gpakoh/other"},
        {"expected_ref_sha": "bad"},
        {"operation_key": "short"},
        {"actor": ""},
    ],
)
async def test_invalid_target_cannot_cross_post_boundary(tmp_path, change):
    client = Client()
    with pytest.raises(DispatchRefused):
        await dispatch_workflow(client, **request(tmp_path, **change))
    assert client.posts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attribute,value", [("permission", False), ("head", "c" * 40), ("version", "1.23.9")]
)
async def test_remote_preflight_refuses_permission_drift_or_unsupported_version(
    tmp_path, attribute, value
):
    client = Client()
    setattr(client, attribute, value)
    with pytest.raises(DispatchRefused):
        await dispatch_workflow(client, **request(tmp_path))
    assert client.posts == 0 and not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
async def test_audit_failure_refuses_post(tmp_path):
    client = Client()

    def audit(event):
        raise OSError("audit write failed")

    with pytest.raises(OSError):
        await dispatch_workflow(client, **request(tmp_path, audit=audit))
    assert client.posts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("http_status,expected", [(403, "rejected"), (500, "outcome_unknown")])
async def test_http_rejection_vs_uncertain_server_error(tmp_path, http_status, expected):
    client = Client()
    response = httpx.Response(
        http_status, request=httpx.Request("POST", "https://git.example/dispatch")
    )
    client.failure = httpx.HTTPStatusError(
        "private detail", request=response.request, response=response
    )
    assert (await dispatch_workflow(client, **request(tmp_path)))["state"] == expected
    await dispatch_workflow(client, **request(tmp_path))
    assert client.posts == 1


def _hold_lock(root, ready, release):
    with operation_lock(Path(root), KEY):
        ready.set()
        release.wait(5)


@pytest.mark.asyncio
async def test_another_process_cannot_dispatch_same_operation(tmp_path):
    ctx = multiprocessing.get_context("fork")
    ready, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_hold_lock, args=(str(tmp_path), ready, release))
    process.start()
    try:
        assert ready.wait(3)
        client = Client()
        with pytest.raises(DispatchRefused, match="already active"):
            await dispatch_workflow(client, **request(tmp_path))
        assert client.posts == 0
    finally:
        release.set()
        process.join(3)
        if process.is_alive():
            process.kill()
            process.join()


def _crash_after_receipt(root):
    client = Client()
    client.before_post = lambda: os._exit(17)
    asyncio.run(dispatch_workflow(client, **request(Path(root))))


@pytest.mark.asyncio
async def test_process_death_before_response_never_reposts(tmp_path):
    process = multiprocessing.get_context("fork").Process(
        target=_crash_after_receipt, args=(str(tmp_path),)
    )
    process.start()
    process.join(4)
    assert process.exitcode == 17
    client = Client()
    result = await dispatch_workflow(client, **request(tmp_path))
    assert result["state"] == "outcome_unknown" and client.posts == 0


@pytest.mark.asyncio
async def test_corrupt_or_symlinked_receipt_refuses_dispatch(tmp_path):
    path = tmp_path / (digest(KEY) + ".json")
    path.write_text("invalid")
    path.chmod(0o600)
    client = Client()
    with pytest.raises(DispatchRefused):
        await dispatch_workflow(client, **request(tmp_path))
    path.unlink()
    target = tmp_path / "other"
    target.write_text("private")
    path.symlink_to(target)
    with pytest.raises(OSError):
        await dispatch_workflow(client, **request(tmp_path))
    assert client.posts == 0 and target.read_text() == "private"


@pytest.mark.asyncio
@pytest.mark.parametrize("code,body", [(200, {"workflow_run_id": 101}), (204, None)])
async def test_native_client_posts_exact_endpoint_once_and_requests_run_details(code, body):
    requests = []

    def handler(req):
        requests.append(req)
        return httpx.Response(code, json=body) if body else httpx.Response(code)

    client = GiteaClient("private-token")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://git.example/api/v1/", transport=httpx.MockTransport(handler)
    )
    try:
        result = await client.dispatch_workflow(
            "gpakoh", "project", "probe.yml", "refs/heads/master", {"approved": "true"}
        )
    finally:
        await client.aclose()
    assert result["run_id"] == (101 if code == 200 else None)
    assert len(requests) == 1
    req = requests[0]
    assert req.url.path == "/api/v1/repos/gpakoh/project/actions/workflows/probe.yml/dispatches"
    assert req.url.params["return_run_details"] == "true"
    assert json.loads(req.content) == {"ref": "refs/heads/master", "inputs": {"approved": "true"}}


@pytest.mark.asyncio
async def test_dispatch_cannot_use_generic_lifecycle_post_or_unsafe_filename(monkeypatch):
    client = GiteaClient("private-token")
    post = AsyncMock()
    monkeypatch.setattr(client._client, "post", post)
    try:
        with pytest.raises(ValueError):
            await client._post_action(
                "/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches",
                owner="gpakoh",
                repo="project",
                workflow_id="probe.yml",
            )
        for name in ("../probe.yml", "x/y.yml", "https://evil/probe.yml"):
            with pytest.raises(ValueError):
                await client.dispatch_workflow("gpakoh", "project", name, "refs/heads/master", {})
    finally:
        await client.aclose()
    post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attribute,value",
    [
        ("workflow_path", ".gitea/workflows/dispatch-probe.yml"),
        ("workflow_state", "disabled_manually"),
    ],
)
async def test_disabled_or_other_directory_workflow_cannot_be_dispatched(
    tmp_path, attribute, value
):
    client = Client()
    setattr(client, attribute, value)
    with pytest.raises(DispatchRefused, match="reviewed repository path"):
        await dispatch_workflow(client, **request(tmp_path))
    assert client.posts == 0


@pytest.mark.asyncio
async def test_ref_drift_during_preflight_refuses_before_post(tmp_path):
    client = Client()
    client.get_branch = AsyncMock(
        side_effect=[{"commit": {"id": HEAD}}, {"commit": {"id": "c" * 40}}]
    )
    with pytest.raises(DispatchRefused, match="changed during preflight"):
        await dispatch_workflow(client, **request(tmp_path))
    assert client.posts == 0 and not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
async def test_receipt_failure_after_post_does_not_make_key_reusable(tmp_path, monkeypatch):
    from app import gitea_workflow_dispatch as module

    original = module.save_receipt
    calls = 0

    def fail_second(path, receipt):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("private-storage-error")
        original(path, receipt)

    client = Client()
    monkeypatch.setattr(module, "save_receipt", fail_second)
    result = await dispatch_workflow(client, **request(tmp_path))
    assert result["state"] == "outcome_unknown" and result["receipt_persist_failed"]
    assert "private-storage-error" not in repr(result)
    assert (await dispatch_workflow(client, **request(tmp_path)))["state"] == "outcome_unknown"
    assert client.posts == 1


@pytest.mark.asyncio
async def test_cancelled_worker_retains_pending_receipt_without_repeating(tmp_path):
    client = Client()
    client.failure = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await dispatch_workflow(client, **request(tmp_path))
    client.failure = None
    result = await dispatch_workflow(client, **request(tmp_path))
    assert result["state"] == "outcome_unknown" and client.posts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"workflow_run_id": True}, {"workflow_run_id": "101"}, {}, [101]])
async def test_unusable_success_response_is_unknown_not_a_retry_signal(body):
    from examples.mcp_client_remote.fleet.gitea_client import GiteaMutationOutcomeUnknown

    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=body)

    client = GiteaClient("private-token")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://git.example/api/v1/", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(GiteaMutationOutcomeUnknown):
            await client.dispatch_workflow(
                "gpakoh", "project", "probe.yml", "refs/heads/master", {}
            )
    finally:
        await client.aclose()
    assert len(calls) == 1
