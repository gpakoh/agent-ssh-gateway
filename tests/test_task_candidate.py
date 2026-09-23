"""Verifier receipt binding tests for trusted task-candidate delivery.

Covers AO-015 layer2: materialization must persist the structured verifier
receipt with a deterministic canonical digest, and push validation must
revalidate that evidence (head binding, immutable required checks, digest)
before authorizing staging use.  Missing, legacy, naked or tampered evidence
fails closed; adapters expose only bounded summary fields.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from examples.mcp_server.agent_paths import task_dir
from examples.mcp_server.mcp_infra.adapters import remote
from examples.mcp_server.task_candidate import (
    RECEIPT_FILENAME,
    RECEIPT_VERSION,
    VERIFIER_RECEIPT_SCHEMA_VERSION,
    CandidateError,
    _canonical_verifier_receipt_sha256,
    _candidate_record_dir,
    bind_task_attempt_job,
    materialize_task_candidate,
    record_task_delivery_contract,
    resolve_task_attempt_identity,
    validate_task_candidate_for_push,
    verifier_receipt_is_trusted,
)

PROJECT = "receipt-binding-project"
TASK = "receipt-binding-task-001"
OWNER = "gpakoh"
REPO = "agent-ssh-gateway"
BRANCH = "fix/receipt-binding-test"
REQUIRED_CHECKS = ["pytest -q", "ruff check ."]
IMAGE = "ghcr.io/example/verifier:1"
SHA = "4e846348f293539593a194236b42414336d22576"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _init_repo(root: Path) -> str:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "base.txt")
    _git(root, "commit", "-q", "-m", "base")
    return _git(root, "rev-parse", "HEAD")


def _write_evidence(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    required_checks: list[str],
) -> tuple[str, Path]:
    state = root.parent / "state"
    candidate = root.parent / "candidate-store"
    monkeypatch.setenv("MCP_AGENT_STATE_ROOT", str(state))
    monkeypatch.setenv("MCP_TASK_CANDIDATE_ROOT", str(candidate))
    base = _git(root, "rev-parse", "HEAD")
    td = Path(task_dir(PROJECT, TASK))
    td.mkdir(parents=True)
    patch = (
        "diff --git a/base.txt b/base.txt\n"
        "index df967b9..257cc56 100644\n"
        "--- a/base.txt\n"
        "+++ b/base.txt\n"
        "@@ -1 +1 @@\n"
        "-base\n"
        "+candidate\n"
    )
    (td / "base-head.txt").write_text(base + "\n", encoding="ascii")
    (td / "implementation-diff.patch").write_text(patch, encoding="utf-8")
    record_task_delivery_contract(
        project=PROJECT,
        task_id=TASK,
        base_ref=base,
        allowed_files=["base.txt"],
        forbidden_files=[],
        required_checks=required_checks,
    )
    attempt_id, _ = resolve_task_attempt_identity(
        project=PROJECT,
        task_id=TASK,
        fingerprint="fingerprint-001",
    )
    bind_task_attempt_job(
        project=PROJECT,
        task_id=TASK,
        attempt_id=attempt_id,
        fingerprint="fingerprint-001",
        job_id="job-001",
    )
    return base, td


def _diff_sha(td: Path) -> str:
    return hashlib.sha256((td / "implementation-diff.patch").read_bytes()).hexdigest()


def _job_success(job_id: str) -> dict[str, object]:
    assert job_id == "job-001"
    return {"status": "completed", "exit_code": 0}


def _check_entry(index: int, command: str) -> dict[str, Any]:
    return {
        "check_index": index,
        "command": command,
        "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
        "cwd": ".",
        "duration_ms": 7,
        "exit_code": 0,
        "stdout_tail": "collected 2 items",
        "stderr_tail": "",
        "verifier_image": IMAGE,
        "primary_tool": {
            "kind": "builtin",
            "name": "sh",
            "shell_path": "/bin/sh",
            "shell_sha256": "",
            "shell_identity": "verifier-sh",
        },
        "before_status_sha256": "a" * 64,
        "before_status_bytes": 32,
        "before_status_entries": 1,
        "after_status_sha256": "a" * 64,
        "after_status_bytes": 32,
        "after_status_entries": 1,
        "mutation_changed": False,
    }


def _verifier_receipt(candidate_head: str, commands: list[str]) -> dict[str, Any]:
    return {
        "expected_sha": candidate_head,
        "verifier_image": IMAGE,
        "check_count": len(commands),
        "checks": [_check_entry(index, command) for index, command in enumerate(commands)],
    }


def _verify_ok(
    _repo: Path, expected_sha: str, checks: list[str]
) -> dict[str, Any]:
    return _verifier_receipt(expected_sha, checks)


def _materialize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    verify_candidate: Any = _verify_ok,
    required_checks: list[str] | None = None,
) -> tuple[Path, str, Path, dict[str, Any]]:
    checks = list(REQUIRED_CHECKS if required_checks is None else required_checks)
    root = tmp_path / "repo"
    _init_repo(root)
    _, td = _write_evidence(root, monkeypatch, required_checks=checks)
    receipt = materialize_task_candidate(
        project_root=root,
        project=PROJECT,
        task_id=TASK,
        destination_owner=OWNER,
        destination_repo=REPO,
        destination_branch=BRANCH,
        expected_diff_sha256=_diff_sha(td),
        job_result=_job_success,
        verify_candidate=verify_candidate,
    )
    return root, _git(root, "rev-parse", "HEAD"), td, receipt


def _receipt_path(receipt: dict[str, Any]) -> Path:
    return _candidate_record_dir(PROJECT, TASK, receipt["attempt_id"]) / RECEIPT_FILENAME


def _read_stored_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    return json.loads(_receipt_path(receipt).read_text(encoding="utf-8"))


def _write_stored_receipt(receipt: dict[str, Any], payload: dict[str, Any]) -> None:
    _receipt_path(receipt).write_text(json.dumps(payload), encoding="utf-8")


def _validate(root: Path, expected_sha: str) -> tuple[dict[str, Any], Path]:
    return validate_task_candidate_for_push(
        project_root=root,
        project=PROJECT,
        task_id=TASK,
        destination_owner=OWNER,
        destination_repo=REPO,
        destination_branch=BRANCH,
        expected_sha=expected_sha,
    )


# ── materialization stores evidence + digest ──────────────────────


def test_materialize_persists_verifier_evidence_and_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, base, _, receipt = _materialize(tmp_path, monkeypatch)

    assert receipt["version"] == RECEIPT_VERSION
    assert receipt["verifier_receipt_schema_version"] == VERIFIER_RECEIPT_SCHEMA_VERSION
    evidence = receipt["verifier_receipt"]
    assert evidence["expected_sha"] == receipt["candidate_head_sha"]
    assert evidence["verifier_image"] == IMAGE
    assert evidence["check_count"] == len(REQUIRED_CHECKS)
    assert [check["command"] for check in evidence["checks"]] == REQUIRED_CHECKS
    assert all(check["exit_code"] == 0 for check in evidence["checks"])
    assert re_fullmatch_sha256(receipt["verifier_receipt_sha256"])
    assert receipt["verifier_receipt_sha256"] == _canonical_verifier_receipt_sha256(
        evidence
    )

    stored = _read_stored_receipt(receipt)
    assert stored["verifier_receipt"] == evidence
    assert stored["verifier_receipt_sha256"] == receipt["verifier_receipt_sha256"]
    assert (
        stored["verifier_receipt_schema_version"] == VERIFIER_RECEIPT_SCHEMA_VERSION
    )

    validated, staging = _validate(root, receipt["candidate_head_sha"])
    assert validated["candidate_head_sha"] == receipt["candidate_head_sha"]
    assert base == receipt["base_head"] or len(receipt["base_head"]) == 40
    assert staging.is_dir()


def re_fullmatch_sha256(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def test_verifier_receipt_digest_is_deterministic_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _, receipt = _materialize(tmp_path, monkeypatch)
    evidence = receipt["verifier_receipt"]

    shuffled = dict(reversed(list(evidence.items())))
    assert _canonical_verifier_receipt_sha256(shuffled) == _canonical_verifier_receipt_sha256(
        evidence
    )

    unicode_evidence = {"expected_sha": "é", "checks": []}
    expected_bytes = json.dumps(
        unicode_evidence,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    assert _canonical_verifier_receipt_sha256(unicode_evidence) == hashlib.sha256(
        expected_bytes
    ).hexdigest()

    # Re-materializing with identical evidence must reproduce the digest.
    again = _verifier_receipt(receipt["candidate_head_sha"], REQUIRED_CHECKS)
    assert _canonical_verifier_receipt_sha256(again) == receipt["verifier_receipt_sha256"]


# ── materialization fails closed without trustworthy evidence ─────


def test_materialize_requires_dict_verifier_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_receipt(_repo: Path, _sha: str, _checks: list[str]) -> None:
        return None

    with pytest.raises(CandidateError, match="verifier receipt is required"):
        _materialize(tmp_path, monkeypatch, verify_candidate=no_receipt)

    def non_dict_receipt(_repo: Path, _sha: str, _checks: list[str]) -> str:
        return "checks_verified=true"

    with pytest.raises(CandidateError, match="verifier receipt is required"):
        _materialize(tmp_path, monkeypatch, verify_candidate=non_dict_receipt)


def test_materialize_rejects_head_mismatch_in_verifier_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def wrong_head(_repo: Path, _sha: str, _checks: list[str]) -> dict[str, Any]:
        return _verifier_receipt("0" * 40, _checks)

    with pytest.raises(CandidateError, match="expected_sha does not match candidate head"):
        _materialize(tmp_path, monkeypatch, verify_candidate=wrong_head)


def test_materialize_rejects_nonzero_check_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(_repo: Path, sha: str, checks: list[str]) -> dict[str, Any]:
        receipt = _verifier_receipt(sha, checks)
        receipt["checks"][1]["exit_code"] = 1
        return receipt

    with pytest.raises(CandidateError, match="failed required check"):
        _materialize(tmp_path, monkeypatch, verify_candidate=failing)


def test_materialize_rejects_check_count_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def short(_repo: Path, sha: str, _checks: list[str]) -> dict[str, Any]:
        receipt = _verifier_receipt(sha, REQUIRED_CHECKS[:1])
        return receipt

    with pytest.raises(CandidateError, match="check count does not match required checks"):
        _materialize(tmp_path, monkeypatch, verify_candidate=short)


def test_materialize_rejects_out_of_order_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reordered(_repo: Path, sha: str, _checks: list[str]) -> dict[str, Any]:
        receipt = _verifier_receipt(sha, list(reversed(REQUIRED_CHECKS)))
        receipt["checks"] = list(reversed(receipt["checks"]))
        for index, check in enumerate(receipt["checks"]):
            check["check_index"] = index
            check["command_sha256"] = hashlib.sha256(
                check["command"].encode("utf-8")
            ).hexdigest()
        return receipt

    with pytest.raises(CandidateError, match="command does not match required checks"):
        _materialize(tmp_path, monkeypatch, verify_candidate=reordered)


def test_materialize_rejects_check_command_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def swapped_command(_repo: Path, sha: str, _checks: list[str]) -> dict[str, Any]:
        receipt = _verifier_receipt(sha, REQUIRED_CHECKS)
        forged = "true"
        receipt["checks"][0]["command"] = forged
        receipt["checks"][0]["command_sha256"] = hashlib.sha256(
            forged.encode("utf-8")
        ).hexdigest()
        return receipt

    with pytest.raises(CandidateError, match="command does not match required checks"):
        _materialize(tmp_path, monkeypatch, verify_candidate=swapped_command)


# ── push revalidation fails closed on tampered/legacy evidence ────


def test_push_revalidates_and_returns_staging_when_evidence_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, receipt = _materialize(tmp_path, monkeypatch)
    validated, staging = _validate(root, receipt["candidate_head_sha"])
    assert validated["verifier_receipt_sha256"] == receipt["verifier_receipt_sha256"]
    assert staging.is_dir()
    assert verifier_receipt_is_trusted(validated) is True


def test_push_rejects_tampered_verifier_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, receipt = _materialize(tmp_path, monkeypatch)
    stored = _read_stored_receipt(receipt)
    stored["verifier_receipt_sha256"] = "b" * 64
    _write_stored_receipt(receipt, stored)

    with pytest.raises(CandidateError, match="digest does not match"):
        _validate(root, receipt["candidate_head_sha"])


def test_push_rejects_tampered_verifier_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, receipt = _materialize(tmp_path, monkeypatch)
    stored = _read_stored_receipt(receipt)
    stored["verifier_receipt"]["checks"][0]["exit_code"] = 1
    _write_stored_receipt(receipt, stored)

    with pytest.raises(CandidateError, match="digest does not match"):
        _validate(root, receipt["candidate_head_sha"])


def test_push_rejects_evidence_with_recomputed_digest_but_wrong_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, receipt = _materialize(tmp_path, monkeypatch)
    stored = _read_stored_receipt(receipt)
    forged = "true"
    stored["verifier_receipt"]["checks"][0]["command"] = forged
    stored["verifier_receipt"]["checks"][0]["command_sha256"] = hashlib.sha256(
        forged.encode("utf-8")
    ).hexdigest()
    stored["verifier_receipt_sha256"] = _canonical_verifier_receipt_sha256(
        stored["verifier_receipt"]
    )
    _write_stored_receipt(receipt, stored)

    with pytest.raises(CandidateError, match="command does not match required checks"):
        _validate(root, receipt["candidate_head_sha"])


def test_push_rejects_tampered_candidate_head_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, receipt = _materialize(tmp_path, monkeypatch)
    stored = _read_stored_receipt(receipt)
    forged_head = "0" * 40
    stored["verifier_receipt"]["expected_sha"] = forged_head
    stored["verifier_receipt_sha256"] = _canonical_verifier_receipt_sha256(
        stored["verifier_receipt"]
    )
    _write_stored_receipt(receipt, stored)

    with pytest.raises(CandidateError, match="expected_sha does not match candidate head"):
        _validate(root, receipt["candidate_head_sha"])


def test_push_rejects_check_count_mismatch_in_stored_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, receipt = _materialize(tmp_path, monkeypatch)
    stored = _read_stored_receipt(receipt)
    stored["verifier_receipt"]["checks"] = stored["verifier_receipt"]["checks"][:1]
    stored["verifier_receipt"]["check_count"] = 1
    stored["verifier_receipt_sha256"] = _canonical_verifier_receipt_sha256(
        stored["verifier_receipt"]
    )
    _write_stored_receipt(receipt, stored)

    with pytest.raises(CandidateError, match="check count does not match required checks"):
        _validate(root, receipt["candidate_head_sha"])


def test_push_rejects_reordered_stored_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, receipt = _materialize(tmp_path, monkeypatch)
    stored = _read_stored_receipt(receipt)
    checks = stored["verifier_receipt"]["checks"]
    first, second = checks[0], checks[1]
    first["command"], second["command"] = second["command"], first["command"]
    first["command_sha256"] = hashlib.sha256(first["command"].encode("utf-8")).hexdigest()
    second["command_sha256"] = hashlib.sha256(second["command"].encode("utf-8")).hexdigest()
    stored["verifier_receipt_sha256"] = _canonical_verifier_receipt_sha256(
        stored["verifier_receipt"]
    )
    _write_stored_receipt(receipt, stored)

    with pytest.raises(CandidateError, match="command does not match required checks"):
        _validate(root, receipt["candidate_head_sha"])


def test_push_rejects_legacy_receipt_without_verifier_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, receipt = _materialize(tmp_path, monkeypatch)
    stored = _read_stored_receipt(receipt)
    stored.pop("verifier_receipt", None)
    stored.pop("verifier_receipt_sha256", None)
    stored.pop("verifier_receipt_schema_version", None)
    _write_stored_receipt(receipt, stored)

    with pytest.raises(CandidateError, match="missing trusted verifier evidence"):
        _validate(root, receipt["candidate_head_sha"])


def test_push_rejects_naked_checks_verified_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, receipt = _materialize(tmp_path, monkeypatch)
    stored = _read_stored_receipt(receipt)
    stored.pop("verifier_receipt", None)
    stored.pop("verifier_receipt_sha256", None)
    stored.pop("verifier_receipt_schema_version", None)
    stored["checks_verified"] = True
    _write_stored_receipt(receipt, stored)

    with pytest.raises(CandidateError, match="missing trusted verifier evidence"):
        _validate(root, receipt["candidate_head_sha"])


def test_verifier_receipt_is_trusted_ignores_naked_bool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _, receipt = _materialize(tmp_path, monkeypatch)
    naked = {
        "candidate_head_sha": receipt["candidate_head_sha"],
        "checks_verified": True,
    }
    assert verifier_receipt_is_trusted(naked) is False
    assert verifier_receipt_is_trusted(receipt) is True

    stripped = dict(receipt)
    stripped["checks_verified"] = True
    stripped.pop("verifier_receipt", None)
    assert verifier_receipt_is_trusted(stripped) is False


# ── adapter: bounded summary and derived checks_verified ──────────


def _full_stored_receipt() -> dict[str, Any]:
    evidence = _verifier_receipt(SHA, REQUIRED_CHECKS)
    evidence["checks"][0]["stdout_tail"] = "SECRET-RAW-OUTPUT-TAIL"
    return {
        "version": RECEIPT_VERSION,
        "project": PROJECT,
        "task_id": TASK,
        "attempt_id": "attempt-001",
        "candidate_head_sha": SHA,
        "base_head": "0" * 40,
        "implementation_diff_sha256": "a" * 64,
        "created_at": "2026-09-23T00:00:00+00:00",
        "verifier_receipt_schema_version": VERIFIER_RECEIPT_SCHEMA_VERSION,
        "verifier_receipt_sha256": _canonical_verifier_receipt_sha256(evidence),
        "verifier_receipt": evidence,
    }


class _Registry:
    def __init__(self, root: Path) -> None:
        self.root = root

    def project_info(self, project: str) -> dict[str, Any]:
        return {"root": str(self.root), "type": "repository"}


class _FakeGiteaClient:
    def __init__(self, token: str) -> None:
        assert token == "managed-token"

    async def __aenter__(self) -> _FakeGiteaClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get_user(self) -> dict[str, Any]:
        return {"login": OWNER}

    async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        return {"permissions": {"push": True}, "default_branch": "master"}

    async def get_branch(self, owner: str, repo: str, branch: str) -> dict[str, Any]:
        return {
            "name": branch,
            "protected": False,
            "commit": {"id": SHA},
        }


async def test_materialize_adapter_exposes_only_bounded_verifier_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class AgentClient:
        def job_result(self, job_id: str, redact_output: bool = True) -> dict[str, Any]:
            return {"status": "completed", "exit_code": 0}

    full = _full_stored_receipt()

    def fake_materialize(**_kwargs: Any) -> dict[str, Any]:
        return full

    monkeypatch.setattr(remote, "_server_workspace_registry", lambda: _Registry(tmp_path))
    monkeypatch.setattr(remote, "_server_agent_client", lambda: AgentClient())
    monkeypatch.setattr(remote, "materialize_task_candidate", fake_materialize)

    result = await remote.gitea_materialize_task_candidate(
        project=PROJECT,
        task_id=TASK,
        owner=OWNER,
        repo=REPO,
        destination_branch=BRANCH,
        expected_diff_sha256="a" * 64,
    )

    assert result["ok"] is True
    payload = result["result"]
    assert payload["verifier_receipt_sha256"] == full["verifier_receipt_sha256"]
    assert (
        payload["verifier_receipt_schema_version"] == VERIFIER_RECEIPT_SCHEMA_VERSION
    )
    assert payload["check_count"] == len(REQUIRED_CHECKS)
    assert payload["verifier_image"] == IMAGE
    assert "verifier_receipt" not in payload
    assert "stdout_tail" not in json.dumps(payload)
    assert "SECRET-RAW-OUTPUT-TAIL" not in json.dumps(payload)


async def test_push_adapter_derives_checks_verified_only_from_revalidated_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = tmp_path / "trusted-candidate-staging"
    staging.mkdir()
    full = _full_stored_receipt()
    monkeypatch.setenv("GITEA_TOKEN", "managed-token")
    monkeypatch.setenv("GITEA_GIT_BASE", "https://git.example.test")
    monkeypatch.setattr(remote, "_server_workspace_registry", lambda: _Registry(tmp_path))
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: _FakeGiteaClient)
    monkeypatch.setattr(
        remote,
        "validate_task_candidate_for_push",
        lambda **_kwargs: (full, staging),
    )
    monkeypatch.setattr(remote, "push_trusted_staging_sha", lambda **_kwargs: None)

    result = await remote.gitea_push_local_ref(
        project=PROJECT,
        task_id=TASK,
        owner=OWNER,
        repo=REPO,
        destination_branch=BRANCH,
        expected_sha=SHA,
    )

    assert result["ok"] is True
    assert result["result"]["verified"] is True
    assert result["result"]["checks_verified"] is True
    assert "SECRET-RAW-OUTPUT-TAIL" not in json.dumps(result)


async def test_push_adapter_cannot_claim_checks_verified_from_naked_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = tmp_path / "trusted-candidate-staging"
    staging.mkdir()
    naked = {
        "candidate_head_sha": SHA,
        "checks_verified": True,
    }
    monkeypatch.setenv("GITEA_TOKEN", "managed-token")
    monkeypatch.setenv("GITEA_GIT_BASE", "https://git.example.test")
    monkeypatch.setattr(remote, "_server_workspace_registry", lambda: _Registry(tmp_path))
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: _FakeGiteaClient)
    monkeypatch.setattr(
        remote,
        "validate_task_candidate_for_push",
        lambda **_kwargs: (naked, staging),
    )
    monkeypatch.setattr(remote, "push_trusted_staging_sha", lambda **_kwargs: None)

    result = await remote.gitea_push_local_ref(
        project=PROJECT,
        task_id=TASK,
        owner=OWNER,
        repo=REPO,
        destination_branch=BRANCH,
        expected_sha=SHA,
    )

    assert result["ok"] is True
    assert result["result"]["checks_verified"] is False


async def test_push_adapter_derives_nothing_when_validation_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def deny(**_kwargs: Any) -> tuple[dict[str, Any], Path]:
        raise CandidateError(
            "candidate receipt is missing trusted verifier evidence",
            code="POLICY_DENIED",
        )

    def must_not_push(**_kwargs: Any) -> None:
        raise AssertionError("push must not run without revalidated receipt evidence")

    monkeypatch.setenv("GITEA_TOKEN", "managed-token")
    monkeypatch.setattr(remote, "_server_workspace_registry", lambda: _Registry(tmp_path))
    monkeypatch.setattr(remote, "validate_task_candidate_for_push", deny)
    monkeypatch.setattr(remote, "push_trusted_staging_sha", must_not_push)

    async def inline_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    monkeypatch.setattr(remote.asyncio, "to_thread", inline_to_thread)
    result = await remote.gitea_push_local_ref(
        project=PROJECT,
        task_id=TASK,
        owner=OWNER,
        repo=REPO,
        destination_branch=BRANCH,
        expected_sha=SHA,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "POLICY_DENIED"
    assert "verifier evidence" in result["error"]["message"]
