from __future__ import annotations

import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import examples.mcp_server.task_candidate as task_candidate_module
from examples.mcp_server.agent_paths import task_dir
from examples.mcp_server.agent_sources import (
    ManagedSourceBundleError,
    ManagedSourcePublication,
)
from examples.mcp_server.task_candidate import (
    CONTRACT_FILENAME,
    RECEIPT_FILENAME,
    RECEIPT_VERSION,
    CandidateError,
    _candidate_record_dir,
    _changed_candidate_paths,
    _enforce_candidate_scope,
    _staging_repo,
    bind_task_attempt_job,
    materialize_task_candidate,
    record_task_delivery_contract,
    resolve_task_attempt_identity,
    validate_task_candidate_for_push,
)

PROJECT = "candidate-test-project"
TASK = "trusted-delivery-task-001"
OWNER = "gpakoh"
REPO = "agent-ssh-gateway"
BRANCH = "fix/trusted-candidate-test"


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
    checks_rc: int = 0,
    fingerprint: str = "fingerprint-001",
    base: str | None = None,
) -> tuple[str, Path]:
    state = root.parent / "state"
    candidate = root.parent / "candidate-store"
    monkeypatch.setenv("MCP_AGENT_STATE_ROOT", str(state))
    monkeypatch.setenv("MCP_TASK_CANDIDATE_ROOT", str(candidate))
    base = base if base is not None else _git(root, "rev-parse", "HEAD")
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
    (td / "scope-violations.json").write_text(
        json.dumps(
            {
                "base_head": base,
                "post_head": base,
                "changed_files": ["base.txt"],
                "allowed_files": ["base.txt"],
                "forbidden_files": [],
                "violations": [],
            }
        ),
        encoding="utf-8",
    )
    final_rc = 0 if checks_rc == 0 else 72
    (td / "supervisor-verdict.json").write_text(
        json.dumps(
            {
                "version": 1,
                "base_head": base,
                "post_head": base,
                "evidence_rc": 0,
                "scope_rc": 0,
                "checks_rc": checks_rc,
                "parent_rc": 0,
                "final_rc": final_rc,
                "scope_ran": 1,
                "checks_ran": 1,
            }
        ),
        encoding="utf-8",
    )
    record_task_delivery_contract(
        project=PROJECT,
        task_id=TASK,
        base_ref=base,
        allowed_files=["base.txt"],
        forbidden_files=[],
        required_checks=[],
    )
    trusted_attempt, trusted_job = resolve_task_attempt_identity(
        project=PROJECT,
        task_id=TASK,
        fingerprint=fingerprint,
    )
    assert trusted_job is None
    bind_task_attempt_job(
        project=PROJECT,
        task_id=TASK,
        attempt_id=trusted_attempt,
        fingerprint=fingerprint,
        job_id="job-001",
    )
    (td / "attempt-state.json").write_text(
        json.dumps(
            {
                "attempt_id": trusted_attempt,
                "fingerprint": fingerprint,
                "job_id": "job-001",
            }
        ),
        encoding="utf-8",
    )
    return base, td


def _diff_sha(td: Path) -> str:
    return hashlib.sha256((td / "implementation-diff.patch").read_bytes()).hexdigest()


def _job_success(job_id: str) -> dict[str, object]:
    assert job_id == "job-001"
    return {"status": "completed", "exit_code": 0}


def _verify_success(repo: Path, expected_sha: str, checks: list[str]) -> None:
    assert _git(repo, "rev-parse", "HEAD") == expected_sha
    assert checks == []


def _materialize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "repo"
    _init_repo(root)
    base, td = _write_evidence(root, monkeypatch)
    receipt = materialize_task_candidate(
        project_root=root,
        project=PROJECT,
        task_id=TASK,
        destination_owner=OWNER,
        destination_repo=REPO,
        destination_branch=BRANCH,
        expected_diff_sha256=_diff_sha(td),
        job_result=_job_success,
        verify_candidate=_verify_success,
    )
    return root, base, td, receipt


def test_materialize_exposes_only_readable_source_to_isolated_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _, td = _write_evidence(root, monkeypatch)
    observed: dict[str, int] = {}

    def verifier(repo: Path, _sha: str, _checks: list[str]) -> None:
        observed["parent_mode"] = repo.parent.stat().st_mode & 0o777
        observed["repo_mode"] = repo.stat().st_mode & 0o777

    materialize_task_candidate(
        project_root=root,
        project=PROJECT,
        task_id=TASK,
        destination_owner=OWNER,
        destination_repo=REPO,
        destination_branch=BRANCH,
        expected_diff_sha256=_diff_sha(td),
        job_result=_job_success,
        verify_candidate=verifier,
    )
    assert observed["parent_mode"] & 0o005 == 0o005
    assert observed["repo_mode"] & 0o005 == 0o005


def test_materialize_records_minimum_receipt_and_persistent_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, base, _, receipt = _materialize(tmp_path, monkeypatch)

    assert receipt["version"] == RECEIPT_VERSION
    assert receipt["project"] == PROJECT
    assert receipt["task_id"] == TASK
    assert isinstance(receipt["attempt_id"], str) and receipt["attempt_id"]
    assert receipt["fingerprint"] == "fingerprint-001"
    assert receipt["base_head"] == base
    assert len(receipt["implementation_diff_sha256"]) == 64
    assert len(receipt["delivery_contract_sha256"]) == 64
    assert len(receipt["candidate_head_sha"]) == 40
    assert receipt["destination"] == {"owner": OWNER, "repo": REPO, "branch": BRANCH}
    assert receipt["created_at"]
    _, staging = validate_task_candidate_for_push(
        project_root=root,
        project=PROJECT,
        task_id=TASK,
        destination_owner=OWNER,
        destination_repo=REPO,
        destination_branch=BRANCH,
        expected_sha=receipt["candidate_head_sha"],
    )
    assert _git(staging, "show", "HEAD:base.txt") == "candidate"


def test_mutable_supervisor_verdict_cannot_block_trusted_reverification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _, td = _write_evidence(root, monkeypatch, checks_rc=127)
    receipt = materialize_task_candidate(
        project_root=root,
        project=PROJECT,
        task_id=TASK,
        destination_owner=OWNER,
        destination_repo=REPO,
        destination_branch=BRANCH,
        expected_diff_sha256=_diff_sha(td),
        job_result=_job_success,
        verify_candidate=_verify_success,
    )
    assert receipt["job_id"] == "job-001"


def test_forged_candidate_sha_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, _ = _materialize(tmp_path, monkeypatch)
    with pytest.raises(CandidateError, match="expected_sha"):
        validate_task_candidate_for_push(
            project_root=root,
            project=PROJECT,
            task_id=TASK,
            destination_owner=OWNER,
            destination_repo=REPO,
            destination_branch=BRANCH,
            expected_sha="1" * 40,
        )


def test_arbitrary_reachable_canonical_object_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, receipt = _materialize(tmp_path, monkeypatch)
    arbitrary = _git(root, "rev-parse", "HEAD")
    assert arbitrary != receipt["candidate_head_sha"]
    with pytest.raises(CandidateError, match="expected_sha"):
        validate_task_candidate_for_push(
            project_root=root,
            project=PROJECT,
            task_id=TASK,
            destination_owner=OWNER,
            destination_repo=REPO,
            destination_branch=BRANCH,
            expected_sha=arbitrary,
        )


def test_arbitrary_managed_workspace_object_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, _ = _materialize(tmp_path, monkeypatch)
    managed = tmp_path / "managed-agent-workspace"
    arbitrary = _init_repo(managed)
    with pytest.raises(CandidateError, match="expected_sha"):
        validate_task_candidate_for_push(
            project_root=root,
            project=PROJECT,
            task_id=TASK,
            destination_owner=OWNER,
            destination_repo=REPO,
            destination_branch=BRANCH,
            expected_sha=arbitrary,
        )


def test_changed_diff_after_receipt_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, td, receipt = _materialize(tmp_path, monkeypatch)
    with (td / "implementation-diff.patch").open("ab") as handle:
        handle.write(b"\n# changed after receipt\n")
    with pytest.raises(CandidateError, match="diff changed"):
        validate_task_candidate_for_push(
            project_root=root,
            project=PROJECT,
            task_id=TASK,
            destination_owner=OWNER,
            destination_repo=REPO,
            destination_branch=BRANCH,
            expected_sha=receipt["candidate_head_sha"],
        )


def test_wrong_project_or_task_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, receipt = _materialize(tmp_path, monkeypatch)
    with pytest.raises(CandidateError):
        validate_task_candidate_for_push(
            project_root=root,
            project="other-project",
            task_id=TASK,
            destination_owner=OWNER,
            destination_repo=REPO,
            destination_branch=BRANCH,
            expected_sha=receipt["candidate_head_sha"],
        )
    with pytest.raises(CandidateError):
        validate_task_candidate_for_push(
            project_root=root,
            project=PROJECT,
            task_id="other-task-123",
            destination_owner=OWNER,
            destination_repo=REPO,
            destination_branch=BRANCH,
            expected_sha=receipt["candidate_head_sha"],
        )


def test_changed_mutable_attempt_state_is_ignored_after_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, td, receipt = _materialize(tmp_path, monkeypatch)
    attempt = json.loads((td / "attempt-state.json").read_text())
    attempt["attempt_id"] = "attempt-forged"
    attempt["fingerprint"] = "fingerprint-forged"
    attempt["job_id"] = "job-forged"
    (td / "attempt-state.json").write_text(json.dumps(attempt))
    checked, _ = validate_task_candidate_for_push(
        project_root=root,
        project=PROJECT,
        task_id=TASK,
        destination_owner=OWNER,
        destination_repo=REPO,
        destination_branch=BRANCH,
        expected_sha=receipt["candidate_head_sha"],
    )
    assert checked["attempt_id"] == receipt["attempt_id"]
    assert checked["fingerprint"] == "fingerprint-001"
    assert checked["job_id"] == "job-001"


def test_exact_recorded_candidate_is_permitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, receipt = _materialize(tmp_path, monkeypatch)
    candidate_root = tmp_path / "candidate-store"
    checked, staging = validate_task_candidate_for_push(
        project_root=root,
        project=PROJECT,
        task_id=TASK,
        destination_owner=OWNER,
        destination_repo=REPO,
        destination_branch=BRANCH,
        expected_sha=receipt["candidate_head_sha"],
    )
    assert checked == receipt
    assert staging.is_dir()
    assert candidate_root in staging.parents
    assert _git(staging, "rev-parse", "HEAD") == receipt["candidate_head_sha"]


def test_auth1_commit_sha_is_not_special_cased(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _, _, _ = _materialize(tmp_path, monkeypatch)
    auth1_sha = "16c4989fc8f5cc72713461fb2e80e1b65da2b741"
    with pytest.raises(CandidateError, match="expected_sha"):
        validate_task_candidate_for_push(project_root=root, project=PROJECT, task_id=TASK, destination_owner=OWNER, destination_repo=REPO, destination_branch=BRANCH, expected_sha=auth1_sha)


def test_candidate_root_rejects_symlinked_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _, td = _write_evidence(root, monkeypatch)
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    alias = tmp_path / "candidate-parent-alias"
    alias.symlink_to(real_parent, target_is_directory=True)
    monkeypatch.setenv("MCP_TASK_CANDIDATE_ROOT", str(alias / "store"))

    with pytest.raises(CandidateError, match="symlink"):
        materialize_task_candidate(
            project_root=root,
            project=PROJECT,
            task_id=TASK,
            destination_owner=OWNER,
            destination_repo=REPO,
            destination_branch=BRANCH,
            expected_diff_sha256=_diff_sha(td),
            job_result=_job_success,
            verify_candidate=_verify_success,
        )


def test_concurrent_materialize_is_serialized_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _, td = _write_evidence(root, monkeypatch)

    def run_once() -> dict[str, object]:
        return materialize_task_candidate(
            project_root=root,
            project=PROJECT,
            task_id=TASK,
            destination_owner=OWNER,
            destination_repo=REPO,
            destination_branch=BRANCH,
            expected_diff_sha256=_diff_sha(td),
            job_result=_job_success,
            verify_candidate=_verify_success,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: run_once(), range(2)))

    assert first == second
    assert first["candidate_head_sha"] == second["candidate_head_sha"]
    checked, staging = validate_task_candidate_for_push(
        project_root=root,
        project=PROJECT,
        task_id=TASK,
        destination_owner=OWNER,
        destination_repo=REPO,
        destination_branch=BRANCH,
        expected_sha=str(first["candidate_head_sha"]),
    )
    assert checked == first
    assert _git(staging, "rev-parse", "HEAD") == first["candidate_head_sha"]


def test_mutable_attempt_state_cannot_change_trusted_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _, td, receipt = _materialize(tmp_path, monkeypatch)
    forged = {"attempt_id": "attempt-forged", "fingerprint": "forged", "job_id": "evil-job"}
    (td / "attempt-state.json").write_text(json.dumps(forged), encoding="utf-8")
    checked, _ = validate_task_candidate_for_push(project_root=root, project=PROJECT, task_id=TASK, destination_owner=OWNER, destination_repo=REPO, destination_branch=BRANCH, expected_sha=receipt["candidate_head_sha"])
    assert checked["attempt_id"] == receipt["attempt_id"]
    assert checked["job_id"] == "job-001"



def test_changed_diff_after_review_before_materialize_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _, td = _write_evidence(root, monkeypatch)
    approved = _diff_sha(td)
    with (td / "implementation-diff.patch").open("ab") as handle:
        handle.write(b"\n# changed after architect review\n")

    with pytest.raises(CandidateError, match="changed since architect approval"):
        materialize_task_candidate(
            project_root=root,
            project=PROJECT,
            task_id=TASK,
            destination_owner=OWNER,
            destination_repo=REPO,
            destination_branch=BRANCH,
            expected_diff_sha256=approved,
            job_result=_job_success,
            verify_candidate=_verify_success,
        )


def test_mutable_scope_json_cannot_authorize_out_of_scope_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _, td = _write_evidence(root, monkeypatch)
    (root / "other.txt").write_text("other\n", encoding="utf-8")
    patch = "diff --git a/other.txt b/other.txt\nnew file mode 100644\nindex 0000000..27fa349\n--- /dev/null\n+++ b/other.txt\n@@ -0,0 +1 @@\n+other\n"
    (td / "implementation-diff.patch").write_text(patch, encoding="utf-8")
    (td / "scope-violations.json").write_text(json.dumps({"violations": []}), encoding="utf-8")
    with pytest.raises(CandidateError, match="outside immutable allowed scope"):
        materialize_task_candidate(project_root=root, project=PROJECT, task_id=TASK, destination_owner=OWNER, destination_repo=REPO, destination_branch=BRANCH, expected_diff_sha256=_diff_sha(td), job_result=_job_success, verify_candidate=_verify_success)


def test_candidate_scope_rename_cannot_hide_forbidden_source(tmp_path: Path) -> None:
    root = tmp_path / "rename-repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "forbidden.txt").write_text("secret\n", encoding="utf-8")
    _git(root, "add", "forbidden.txt")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")

    _git(root, "mv", "forbidden.txt", "allowed.txt")
    _git(root, "commit", "-q", "-m", "rename")
    candidate = _git(root, "rev-parse", "HEAD")

    changed = set(_changed_candidate_paths(root, base, candidate))
    assert changed == {"forbidden.txt", "allowed.txt"}
    with pytest.raises(CandidateError, match="outside immutable allowed scope|forbidden file"):
        _enforce_candidate_scope(
            root,
            base_head=base,
            candidate_head=candidate,
            allowed_files=["allowed.txt"],
            forbidden_files=["forbidden.txt"],
        )


def test_terminal_failed_job_can_be_salvaged_by_trusted_reverification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _, td = _write_evidence(root, monkeypatch)

    receipt = materialize_task_candidate(
        project_root=root,
        project=PROJECT,
        task_id=TASK,
        destination_owner=OWNER,
        destination_repo=REPO,
        destination_branch=BRANCH,
        expected_diff_sha256=_diff_sha(td),
        job_result=lambda _job: {"status": "failed", "exit_code": 79},
        verify_candidate=_verify_success,
    )

    assert receipt["job_terminal_status"] == "failed"
    assert receipt["job_exit_code"] == 79


def test_nonterminal_trusted_job_is_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _, td = _write_evidence(root, monkeypatch)
    with pytest.raises(CandidateError, match="terminal") as excinfo:
        materialize_task_candidate(project_root=root, project=PROJECT, task_id=TASK, destination_owner=OWNER, destination_repo=REPO, destination_branch=BRANCH, expected_diff_sha256=_diff_sha(td), job_result=lambda _job: {"status": "running", "exit_code": None}, verify_candidate=_verify_success)
    assert excinfo.value.code == "CANDIDATE_JOB_NOT_SUCCESSFUL"
    assert excinfo.value.retryable is True
    assert excinfo.value.details == {"job_id": "job-001", "job_status": "running"}


def test_ambiguous_gateway_job_can_use_final_runner_verdict_for_terminality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _, td = _write_evidence(root, monkeypatch)
    (td / "agent-status.md").write_text("Status: needs-review\n", encoding="utf-8")
    (td / "agent-heartbeat.json").write_text(
        json.dumps(
            {
                "version": 1,
                "state": "finished",
                "phase": "final",
                "updated_at": "2026-09-05T17:00:00Z",
                "updated_epoch": 1_778_000_000,
                "runner_pid": 123,
                "exit_code": 0,
            }
        ),
        encoding="utf-8",
    )

    receipt = materialize_task_candidate(
        project_root=root,
        project=PROJECT,
        task_id=TASK,
        destination_owner=OWNER,
        destination_repo=REPO,
        destination_branch=BRANCH,
        expected_diff_sha256=_diff_sha(td),
        job_result=lambda _job: {"status": "ambiguous", "exit_code": None},
        verify_candidate=_verify_success,
    )

    assert receipt["job_terminal_status"] == "needs-review"
    assert receipt["job_exit_code"] == 0
    assert receipt["terminal_evidence"] == {
        "source": "runner_heartbeat",
        "status": "needs-review",
        "exit_code": 0,
        "heartbeat_state": "finished",
        "heartbeat_phase": "final",
    }


def test_running_gateway_job_overrides_forged_final_runner_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _, td = _write_evidence(root, monkeypatch)
    (td / "agent-status.md").write_text("Status: needs-review\n", encoding="utf-8")
    (td / "agent-heartbeat.json").write_text(
        json.dumps({"state": "finished", "phase": "final", "exit_code": 0}),
        encoding="utf-8",
    )

    with pytest.raises(CandidateError) as excinfo:
        materialize_task_candidate(
            project_root=root,
            project=PROJECT,
            task_id=TASK,
            destination_owner=OWNER,
            destination_repo=REPO,
            destination_branch=BRANCH,
            expected_diff_sha256=_diff_sha(td),
            job_result=lambda _job: {"status": "running", "exit_code": None},
            verify_candidate=_verify_success,
        )

    assert excinfo.value.code == "CANDIDATE_JOB_NOT_SUCCESSFUL"
    assert excinfo.value.details == {"job_id": "job-001", "job_status": "running"}


def test_materialized_candidate_is_readable_by_distinct_verifier_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _, td = _write_evidence(root, monkeypatch)
    observed: dict[str, int] = {}

    def verify(repo: Path, _sha: str, _checks: list[str]) -> None:
        observed["parent"] = repo.parent.stat().st_mode & 0o777
        observed["repo"] = repo.stat().st_mode & 0o777
        observed["git_head"] = (repo / ".git" / "HEAD").stat().st_mode & 0o777
        observed["source"] = (repo / "base.txt").stat().st_mode & 0o777

    materialize_task_candidate(
        project_root=root,
        project=PROJECT,
        task_id=TASK,
        destination_owner=OWNER,
        destination_repo=REPO,
        destination_branch=BRANCH,
        expected_diff_sha256=_diff_sha(td),
        job_result=lambda _job_id: {"status": "completed", "exit_code": 0},
        verify_candidate=verify,
    )
    assert observed["parent"] & 0o005 == 0o005
    assert observed["repo"] & 0o005 == 0o005
    assert observed["git_head"] & 0o004 == 0o004
    assert observed["source"] & 0o004 == 0o004


def test_changed_trusted_contract_after_receipt_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, receipt = _materialize(tmp_path, monkeypatch)
    candidate_root = tmp_path / "candidate-store"
    contract_path = next(candidate_root.glob(f"project-*/task-*/{CONTRACT_FILENAME}"))
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["allowed_files"] = ["base.txt", "other.txt"]
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    with pytest.raises(CandidateError, match="delivery contract changed"):
        validate_task_candidate_for_push(
            project_root=root,
            project=PROJECT,
            task_id=TASK,
            destination_owner=OWNER,
            destination_repo=REPO,
            destination_branch=BRANCH,
            expected_sha=receipt["candidate_head_sha"],
        )


def _attempt_id(td: Path) -> str:
    return json.loads((td / "attempt-state.json").read_text(encoding="utf-8"))["attempt_id"]


def test_receipt_less_staging_is_recovered_by_reverification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, receipt = _materialize(tmp_path, monkeypatch)
    staging = _staging_repo(_candidate_record_dir(PROJECT, TASK, receipt["attempt_id"]))
    assert staging.is_dir()
    receipt_path = _candidate_record_dir(PROJECT, TASK, receipt["attempt_id"]) / RECEIPT_FILENAME
    receipt_path.unlink()
    td = Path(task_dir(PROJECT, TASK))

    verifier_calls = {"n": 0}

    def verifier(repo: Path, expected_sha: str, checks: list[str]) -> None:
        verifier_calls["n"] += 1
        _verify_success(repo, expected_sha, checks)

    recovered = materialize_task_candidate(
        project_root=root,
        project=PROJECT,
        task_id=TASK,
        destination_owner=OWNER,
        destination_repo=REPO,
        destination_branch=BRANCH,
        expected_diff_sha256=_diff_sha(td),
        job_result=_job_success,
        verify_candidate=verifier,
    )
    assert verifier_calls["n"] == 1
    assert recovered["version"] == RECEIPT_VERSION
    assert recovered["candidate_head_sha"] == receipt["candidate_head_sha"]
    assert staging.is_dir()
    checked, _ = validate_task_candidate_for_push(
        project_root=root,
        project=PROJECT,
        task_id=TASK,
        destination_owner=OWNER,
        destination_repo=REPO,
        destination_branch=BRANCH,
        expected_sha=recovered["candidate_head_sha"],
    )
    assert checked == recovered


def test_candidate_commit_date_is_pinned_to_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    base = _init_repo(root)
    _, td = _write_evidence(root, monkeypatch)
    expected_date = _git(root, "show", "-s", "--format=%cI", base)

    # Patch the globals dictionary of the exact callable this test invokes.
    # Other tests may deliberately remove/reimport examples.mcp_server modules,
    # so resolving task_candidate through sys.modules/package attributes here
    # can return a different module object from materialize_task_candidate's
    # captured globals and make this hook silently ineffective.
    materialize_globals = materialize_task_candidate.__globals__
    original_run_git = materialize_globals["_run_git"]
    commit_dates: list[tuple[str | None, str | None]] = []

    def recording_run_git(
        cwd: Path, args: list[str], *, env: dict[str, str] | None = None
    ) -> str:
        if args and args[0] == "commit":
            assert env is not None
            commit_dates.append((env.get("GIT_AUTHOR_DATE"), env.get("GIT_COMMITTER_DATE")))
        return original_run_git(cwd, args, env=env)

    monkeypatch.setitem(materialize_globals, "_run_git", recording_run_git)
    materialize_task_candidate(
        project_root=root,
        project=PROJECT,
        task_id=TASK,
        destination_owner=OWNER,
        destination_repo=REPO,
        destination_branch=BRANCH,
        expected_diff_sha256=_diff_sha(td),
        job_result=_job_success,
        verify_candidate=_verify_success,
    )
    assert commit_dates == [(expected_date, expected_date)]


def test_symlink_staging_is_denied_and_external_target_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _, td = _write_evidence(root, monkeypatch)
    record_dir = _candidate_record_dir(PROJECT, TASK, _attempt_id(td))
    staging = _staging_repo(record_dir)
    external = tmp_path / "external-target"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.symlink_to(external, target_is_directory=True)

    with pytest.raises(CandidateError, match="orphan candidate staging"):
        materialize_task_candidate(
            project_root=root,
            project=PROJECT,
            task_id=TASK,
            destination_owner=OWNER,
            destination_repo=REPO,
            destination_branch=BRANCH,
            expected_diff_sha256=_diff_sha(td),
            job_result=_job_success,
            verify_candidate=_verify_success,
        )
    assert staging.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert (external / "base.txt").exists() is False


def test_symlink_staging_parent_is_denied_and_external_target_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _, td = _write_evidence(root, monkeypatch)
    record_dir = _candidate_record_dir(PROJECT, TASK, _attempt_id(td))
    staging = _staging_repo(record_dir)
    external = tmp_path / "external-parent"
    external_repo = external / "repo"
    external_repo.mkdir(parents=True)
    sentinel = external_repo / "sentinel.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    staging.parent.parent.mkdir(parents=True, exist_ok=True)
    staging.parent.symlink_to(external, target_is_directory=True)

    with pytest.raises(CandidateError, match="safely removed"):
        materialize_task_candidate(
            project_root=root,
            project=PROJECT,
            task_id=TASK,
            destination_owner=OWNER,
            destination_repo=REPO,
            destination_branch=BRANCH,
            expected_diff_sha256=_diff_sha(td),
            job_result=_job_success,
            verify_candidate=_verify_success,
        )
    assert staging.parent.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert external_repo.is_dir()


def test_verifier_failure_leaves_no_materialize_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _, td = _write_evidence(root, monkeypatch)
    candidate_root = tmp_path / "candidate-store"

    def failing(_repo: Path, _sha: str, _checks: list[str]) -> None:
        raise RuntimeError("isolated verifier boom")

    with pytest.raises(CandidateError, match="verification failed"):
        materialize_task_candidate(
            project_root=root,
            project=PROJECT,
            task_id=TASK,
            destination_owner=OWNER,
            destination_repo=REPO,
            destination_branch=BRANCH,
            expected_diff_sha256=_diff_sha(td),
            job_result=_job_success,
            verify_candidate=failing,
        )
    residue = [p for p in candidate_root.rglob(".materialize-*") if p.is_dir()]
    assert residue == []
    assert not any(p.name.startswith(".materialize-") for p in candidate_root.rglob("*"))


def test_existing_receipt_idempotency_does_not_rerun_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _, td = _write_evidence(root, monkeypatch)
    verifier_calls = {"n": 0}

    def verifier(repo: Path, expected_sha: str, checks: list[str]) -> None:
        verifier_calls["n"] += 1
        _verify_success(repo, expected_sha, checks)

    def materialize_once() -> dict[str, object]:
        return materialize_task_candidate(
            project_root=root,
            project=PROJECT,
            task_id=TASK,
            destination_owner=OWNER,
            destination_repo=REPO,
            destination_branch=BRANCH,
            expected_diff_sha256=_diff_sha(td),
            job_result=_job_success,
            verify_candidate=verifier,
        )

    first = materialize_once()
    second = materialize_once()
    assert verifier_calls["n"] == 1
    assert first == second
    assert first["candidate_head_sha"] == second["candidate_head_sha"]


def _build_bundle(tmp_path: Path, source: Path) -> tuple[Path, str]:
    base = _git(source, "rev-parse", "HEAD")
    bundle_path = tmp_path / "bundles" / f"{base}.bundle"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    _git(source, "bundle", "create", str(bundle_path), "HEAD")
    return bundle_path, base


def _init_unrelated_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    (root / "root-marker.txt").write_text("distinct\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "unrelated root")
    return root


def test_materialize_from_verified_bundle_when_local_checkout_lacks_base_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    materialize_globals = materialize_task_candidate.__globals__

    bundle_source = tmp_path / "bundle-source"
    _init_repo(bundle_source)
    bundle_path, base = _build_bundle(tmp_path, bundle_source)
    root = _init_unrelated_root(tmp_path)
    missing = subprocess.run(
        ["git", "cat-file", "-e", f"{base}^{{commit}}"],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )
    assert missing.returncode != 0
    _, td = _write_evidence(root, monkeypatch, base=base)

    def fake_ensure(project: str, ref: str) -> ManagedSourcePublication:
        assert (project, ref) == (PROJECT, base)
        return ManagedSourcePublication(str(bundle_path), "a" * 64)

    monkeypatch.setitem(materialize_globals, "ensure_managed_source_bundle", fake_ensure)
    receipt = materialize_task_candidate(
        project_root=root,
        project=PROJECT,
        task_id=TASK,
        destination_owner=OWNER,
        destination_repo=REPO,
        destination_branch=BRANCH,
        expected_diff_sha256=_diff_sha(td),
        job_result=_job_success,
        verify_candidate=_verify_success,
    )
    checked, staging = validate_task_candidate_for_push(
        project_root=root,
        project=PROJECT,
        task_id=TASK,
        destination_owner=OWNER,
        destination_repo=REPO,
        destination_branch=BRANCH,
        expected_sha=receipt["candidate_head_sha"],
    )
    assert checked == receipt
    assert _git(staging, "show", "HEAD:base.txt") == "candidate"


def test_managed_source_publication_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    materialize_globals = materialize_task_candidate.__globals__

    root = tmp_path / "repo"
    _init_repo(root)
    _, td = _write_evidence(root, monkeypatch)

    def failing_ensure(project: str, ref: str) -> ManagedSourcePublication:
        raise ManagedSourceBundleError("publication boom")

    monkeypatch.setitem(materialize_globals, "ensure_managed_source_bundle", failing_ensure)
    with pytest.raises(CandidateError) as excinfo:
        materialize_task_candidate(
            project_root=root,
            project=PROJECT,
            task_id=TASK,
            destination_owner=OWNER,
            destination_repo=REPO,
            destination_branch=BRANCH,
            expected_diff_sha256=_diff_sha(td),
            job_result=_job_success,
            verify_candidate=_verify_success,
        )
    assert excinfo.value.code == "SOURCE_UNAVAILABLE"
    candidate_root = tmp_path / "candidate-store"
    assert not any(p.name.startswith(".materialize-") for p in candidate_root.rglob("*"))


def test_materialize_git_failure_has_typed_phase_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _init_repo(root)
    _, td = _write_evidence(root, monkeypatch)
    (td / "implementation-diff.patch").write_text("not a git patch\n", encoding="utf-8")

    with pytest.raises(CandidateError) as excinfo:
        materialize_task_candidate(
            project_root=root,
            project=PROJECT,
            task_id=TASK,
            destination_owner=OWNER,
            destination_repo=REPO,
            destination_branch=BRANCH,
            expected_diff_sha256=_diff_sha(td),
            job_result=_job_success,
            verify_candidate=_verify_success,
        )

    assert excinfo.value.code == "GIT_OPERATION_FAILED"
    assert excinfo.value.retryable is False
    assert excinfo.value.details is not None
    assert excinfo.value.details["phase"] == "apply"
    assert excinfo.value.details["command_class"] == "git apply"
    assert excinfo.value.details["returncode"] != 0
    stderr_tail = excinfo.value.details["stderr_tail"]
    assert "error" in stderr_tail.lower()
    assert str(tmp_path) not in stderr_tail


def test_materialize_git_timeout_has_retry_guidance(tmp_path: Path) -> None:
    err = task_candidate_module._candidate_git_error(
        subcommand="clone",
        cwd=tmp_path,
        returncode=None,
        stderr=f"fatal: stalled in {tmp_path}/repo",
        did_timeout=True,
    )

    assert err.code == "GIT_OPERATION_FAILED"
    assert err.retryable is True
    assert err.details is not None
    assert err.details["phase"] == "clone"
    assert err.details["timeout_seconds"] == 60
    assert str(tmp_path) not in err.details["stderr_tail"]
