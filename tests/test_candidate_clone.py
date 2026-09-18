from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.workspace.registry import reset_registry
from examples.mcp_server.agent_sources import ManagedSourceBundleError, ManagedSourcePublication
from examples.mcp_server.candidate_clone import (
    CandidateCloneError,
    CandidateCloneReceipt,
    prepare_candidate_clone,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _init_repo(root: Path) -> str:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "base")
    return _git(root, "rev-parse", "HEAD")


def _commit(repo: Path, message: str) -> str:
    (repo / "README.md").write_text(message + "\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def registry_fixture(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source"
    base = _init_repo(source)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "projects.yaml").write_text(
        "version: 1\n"
        f"registry_root: {workspace}\n\n"
        "projects:\n"
        "  source-project:\n"
        "    root: source\n"
        "    type: repository\n"
        "    description: source project\n"
        "    tags: [source]\n",
        encoding="utf-8",
    )
    journal_root = tmp_path / "journals"
    reset_registry()
    try:
        yield workspace, source, config_dir, journal_root, base
    finally:
        reset_registry()


def test_prepare_candidate_clone_creates_registered_clean_clone(registry_fixture) -> None:
    workspace, _source, config_dir, journal_root, base = registry_fixture

    receipt = prepare_candidate_clone(
        "source-project",
        "candidate/test-flow",
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )

    data = receipt.as_dict()
    assert data["source_project"] == "source-project"
    assert data["branch"] == "candidate/test-flow"
    assert data["base_sha"] == base
    assert data["head"] == base
    assert data["root"] == "."
    assert data["registered"] is True
    assert data["recovered"] is False
    assert data["clean"] is True
    assert data["git_identity"] == {
        "user.name": "MCP Control Plane",
        "user.email": "control-plane@gateway.invalid",
    }
    clone_root = workspace / ".mcp-candidate-clones" / data["project_id"]
    assert clone_root.is_dir()
    assert _git(clone_root, "rev-parse", "--abbrev-ref", "HEAD") == "candidate/test-flow"
    assert _git(clone_root, "rev-parse", "HEAD") == base
    assert _git(clone_root, "status", "--short") == ""
    assert _git(clone_root, "config", "--local", "user.name") == "MCP Control Plane"
    assert _git(clone_root, "config", "--local", "user.email") == "control-plane@gateway.invalid"
    assert (clone_root / ".git" / "mcp-candidate-clone.json").is_file()
    registry = (config_dir / "projects.yaml").read_text(encoding="utf-8")
    assert data["project_id"] in registry
    assert ".mcp-candidate-clones/" in registry


def test_prepare_candidate_clone_recovers_same_clean_clone(registry_fixture) -> None:
    _workspace, _source, config_dir, journal_root, base = registry_fixture
    first = prepare_candidate_clone(
        "source-project",
        "candidate/recover-flow",
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )

    second = prepare_candidate_clone(
        "source-project",
        "candidate/recover-flow",
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )

    assert second.project_id == first.project_id
    assert second.recovered is True
    assert second.registered is False
    assert second.clean is True


@pytest.mark.parametrize("branch", ["master", "main", "../x", "x..y", "-x", "x:y", "x.lock", "x//y", "x@{1}"])
def test_prepare_candidate_clone_rejects_unsafe_branches(registry_fixture, branch: str) -> None:
    _workspace, _source, config_dir, journal_root, base = registry_fixture
    with pytest.raises(CandidateCloneError) as exc_info:
        prepare_candidate_clone(
            "source-project",
            branch,
            base,
            config_dir=config_dir,
            journal_root=journal_root,
        )
    assert exc_info.value.code == "INVALID_INPUT"


def test_prepare_candidate_clone_source_ownership_failure_returns_typed_redacted_diagnostics(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as candidate_clone_module

    workspace, source, config_dir, journal_root, base = registry_fixture
    captured_commands: list[list[str]] = []

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        command = list(args[0])
        captured_commands.append(command)
        return subprocess.CompletedProcess(
            args=command,
            returncode=128,
            stdout="preflight stdout\n",
            stderr="fatal: detected dubious ownership in repository at '/tmp/private/root/source'\n",
        )

    monkeypatch.setattr(candidate_clone_module.subprocess, "run", fake_run)

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_clone_module.prepare_candidate_clone(
            "source-project",
            "candidate/diagnostic-flow",
            base,
            config_dir=config_dir,
            journal_root=journal_root,
        )

    err = exc_info.value
    assert err.code == "SOURCE_REPO_OWNERSHIP_BLOCKED"
    assert err.message == "source repository ownership is not trusted by Git"
    assert err.retryable is False
    details = err.details
    assert details is not None
    assert details["operation"] == "resolve base ref"
    assert details["exit_code"] == 128
    assert details["stdout_tail"] == "preflight stdout"
    assert "dubious ownership" in details["stderr_tail"]
    assert "<path>" in details["stderr_tail"]
    assert "/tmp/private" not in details["stderr_tail"]
    assert "cwd" not in details
    assert "args" not in details

    assert captured_commands == [
        [
            "git",
            "-c",
            f"safe.directory={source.resolve()}",
            "rev-parse",
            "--verify",
            f"{base}^{{commit}}",
        ]
    ]
    assert "safe.directory=*" not in " ".join(captured_commands[0])
    assert "--global" not in captured_commands[0]
    assert not (workspace / ".mcp-candidate-clones").exists()
    assert "candidate/diagnostic-flow" not in (config_dir / "projects.yaml").read_text(
        encoding="utf-8"
    )


def test_prepare_candidate_clone_shallow_probe_ownership_failure_stays_typed(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as candidate_clone_module

    workspace, source, config_dir, journal_root, base = registry_fixture

    def fail_shallow_probe(_source_root: Path) -> bool:
        raise ManagedSourceBundleError(
            "managed source publication failed during git rev-parse: "
            f"fatal: detected dubious ownership in repository at '{source}'"
        )

    monkeypatch.setattr(candidate_clone_module, "_source_is_shallow", fail_shallow_probe)

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_clone_module.prepare_candidate_clone(
            "source-project",
            "candidate/shallow-ownership-flow",
            base,
            config_dir=config_dir,
            journal_root=journal_root,
        )

    err = exc_info.value
    assert err.code == "SOURCE_REPO_OWNERSHIP_BLOCKED"
    assert err.message == "source repository ownership is not trusted by Git"
    assert err.retryable is False
    assert err.details is not None
    assert err.details["operation"] == "inspect source repository completeness"
    assert "dubious ownership" in err.details["stderr_tail"]
    assert str(source) not in err.details["stderr_tail"]
    assert not (workspace / ".mcp-candidate-clones").exists()
    assert "candidate/shallow-ownership-flow" not in (config_dir / "projects.yaml").read_text(
        encoding="utf-8"
    )


def test_candidate_workspace_dubious_ownership_uses_safe_directory_code(tmp_path: Path) -> None:
    from examples.mcp_server import candidate_clone as candidate_clone_module

    err = candidate_clone_module._git_failure(
        cwd=tmp_path,
        operation="read candidate head",
        exit_code=128,
        stdout="",
        stderr=f"fatal: detected dubious ownership in repository at '{tmp_path / 'clone'}'\n",
    )

    assert err.code == "GIT_SAFE_DIRECTORY_REQUIRED"
    assert err.message == "git safe.directory trust is required for this workspace"
    assert err.retryable is False
    assert err.details is not None
    assert err.details["operation"] == "read candidate head"
    assert "dubious ownership" in err.details["stderr_tail"]
    assert str(tmp_path) not in err.details["stderr_tail"]


def test_prepare_candidate_clone_routes_registered_source_through_bundle_bridge(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as candidate_clone_module
    from examples.mcp_server.registered_source_clone import RegisteredSourceCloneError

    workspace, source, config_dir, journal_root, base = registry_fixture
    captured: dict[str, object] = {}

    def reject_after_capture(**kwargs: object) -> None:
        captured.update(kwargs)
        raise RegisteredSourceCloneError(
            "simulated source materialization failure",
            phase="source_trust",
            retryable=False,
        )

    monkeypatch.setattr(
        candidate_clone_module,
        "clone_registered_commit_via_bundle",
        reject_after_capture,
    )

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_clone_module.prepare_candidate_clone(
            "source-project",
            "candidate/local-clone-gitdir-flow",
            base,
            config_dir=config_dir,
            journal_root=journal_root,
        )

    err = exc_info.value
    assert err.code == "SOURCE_REPO_OWNERSHIP_BLOCKED"
    assert err.retryable is False
    assert err.details == {
        "operation": "clone source repository",
        "phase": "source_trust",
        "exit_code": None,
    }
    assert captured["source_root"] == source.resolve()
    assert captured["expected_sha"] == base
    destination = captured["destination"]
    assert isinstance(destination, Path)
    assert destination.parent == workspace / ".mcp-candidate-clones"
    assert not destination.exists()


def test_prepare_candidate_clone_refuses_dirty_existing_clone(registry_fixture) -> None:
    workspace, _source, config_dir, journal_root, base = registry_fixture
    receipt = prepare_candidate_clone(
        "source-project",
        "candidate/dirty-flow",
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    (clone_root / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(CandidateCloneError) as exc_info:
        prepare_candidate_clone(
            "source-project",
            "candidate/dirty-flow",
            base,
            config_dir=config_dir,
            journal_root=journal_root,
        )

    err = exc_info.value
    assert err.code == "WORKSPACE_CONTENDED"
    assert err.retryable is True
    details = err.details
    assert details is not None
    assert details["project_id"] == receipt.project_id
    assert details["dirty"] is True


def test_prepare_candidate_clone_resolves_symbolic_base_ref(registry_fixture) -> None:
    workspace, source, config_dir, journal_root, base = registry_fixture
    _git(source, "branch", "base-for-candidate", base)

    receipt = prepare_candidate_clone(
        "source-project",
        "candidate/symbolic-base",
        "base-for-candidate",
        config_dir=config_dir,
        journal_root=journal_root,
    )

    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    assert receipt.base_ref == "base-for-candidate"
    assert receipt.base_sha == base
    assert _git(clone_root, "rev-parse", "HEAD") == base


def test_prepare_candidate_clone_uses_trusted_remote_base_when_local_checkout_is_stale(
    registry_fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as candidate_clone_module

    workspace, source, config_dir, journal_root, _base = registry_fixture
    remote_source = tmp_path / "remote-source"
    _git(tmp_path, "clone", "-q", str(source), str(remote_source))
    _git(remote_source, "config", "user.name", "Test")
    _git(remote_source, "config", "user.email", "test@example.invalid")
    (remote_source / "README.md").write_text("remote-newer\n", encoding="utf-8")
    _git(remote_source, "add", "README.md")
    _git(remote_source, "commit", "-q", "-m", "remote newer")
    remote_sha = _git(remote_source, "rev-parse", "HEAD")
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{remote_sha}^{{commit}}"],
        cwd=source,
        capture_output=True,
        check=False,
    ).returncode != 0

    bundle = tmp_path / "remote.bundle"
    _git(remote_source, "bundle", "create", str(bundle), "HEAD")
    monkeypatch.setattr(
        candidate_clone_module,
        "_remote_ref_sha",
        lambda _source, ref: remote_sha if ref == "master" else None,
    )
    monkeypatch.setattr(
        candidate_clone_module,
        "ensure_managed_source_bundle",
        lambda _project, sha: ManagedSourcePublication(str(bundle), "a" * 64)
        if sha == remote_sha
        else None,
    )

    receipt = candidate_clone_module.prepare_candidate_clone(
        "source-project",
        "candidate/remote-base",
        "master",
        config_dir=config_dir,
        journal_root=journal_root,
    )

    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    assert receipt.base_sha == remote_sha
    assert _git(clone_root, "rev-parse", "HEAD") == remote_sha
    assert _git(clone_root, "show", "HEAD:README.md") == "remote-newer"


def test_prepare_candidate_clone_exact_base_timeout_uses_trusted_bundle(
    registry_fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as candidate_clone_module

    workspace, source, config_dir, journal_root, _base = registry_fixture
    remote_source = tmp_path / "remote-exact-source"
    _git(tmp_path, "clone", "-q", str(source), str(remote_source))
    _git(remote_source, "config", "user.name", "Test")
    _git(remote_source, "config", "user.email", "test@example.invalid")
    (remote_source / "README.md").write_text("remote-exact\n", encoding="utf-8")
    _git(remote_source, "add", "README.md")
    _git(remote_source, "commit", "-q", "-m", "remote exact")
    remote_sha = _git(remote_source, "rev-parse", "HEAD")
    bundle = tmp_path / "remote-exact.bundle"
    _git(remote_source, "bundle", "create", str(bundle), "HEAD")
    original_run_git = candidate_clone_module._run_git

    def timeout_exact_local_resolve(
        cwd: Path,
        args: list[str],
        **kwargs,
    ) -> str:
        if (
            cwd == source
            and args == ["rev-parse", "--verify", f"{remote_sha}^{{commit}}"]
            and kwargs.get("operation") == "resolve base ref"
        ):
            raise CandidateCloneError(
                "TOOL_EXECUTION_FAILED",
                "git resolve base ref did not complete",
                retryable=True,
                details={"operation": "resolve base ref", "timeout_s": 60},
            )
        return original_run_git(cwd, args, **kwargs)

    monkeypatch.setattr(candidate_clone_module, "_run_git", timeout_exact_local_resolve)
    monkeypatch.setattr(
        candidate_clone_module,
        "_remote_ref_sha",
        lambda _source, ref: remote_sha if ref == remote_sha else None,
    )
    monkeypatch.setattr(
        candidate_clone_module,
        "ensure_managed_source_bundle",
        lambda _project, sha: ManagedSourcePublication(str(bundle), "a" * 64)
        if sha == remote_sha
        else None,
    )

    receipt = candidate_clone_module.prepare_candidate_clone(
        "source-project",
        "candidate/exact-timeout-remote-base",
        remote_sha,
        config_dir=config_dir,
        journal_root=journal_root,
    )

    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    assert receipt.base_sha == remote_sha
    assert _git(clone_root, "rev-parse", "HEAD") == remote_sha
    assert _git(clone_root, "show", "HEAD:README.md") == "remote-exact"


def test_prepare_candidate_clone_missing_base_returns_typed_source_ref_error(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as candidate_clone_module

    _workspace, _source, config_dir, journal_root, _base = registry_fixture
    monkeypatch.setattr(candidate_clone_module, "_remote_ref_sha", lambda _source, _ref: None)

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_clone_module.prepare_candidate_clone(
            "source-project",
            "candidate/missing-base",
            "does-not-exist",
            config_dir=config_dir,
            journal_root=journal_root,
        )

    assert exc_info.value.code == "SOURCE_REF_NOT_AVAILABLE"
    assert exc_info.value.retryable is True


def test_prepare_candidate_clone_remote_base_without_managed_source_returns_stale_error(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as candidate_clone_module

    _workspace, _source, config_dir, journal_root, _base = registry_fixture
    remote_sha = "a" * 40
    monkeypatch.setattr(candidate_clone_module, "_remote_ref_sha", lambda _source, _ref: remote_sha)
    monkeypatch.setattr(candidate_clone_module, "ensure_managed_source_bundle", lambda _project, _sha: None)

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_clone_module.prepare_candidate_clone(
            "source-project",
            "candidate/stale-source",
            "master",
            config_dir=config_dir,
            journal_root=journal_root,
        )

    assert exc_info.value.code == "SOURCE_REPO_STALE"
    assert exc_info.value.retryable is True


def test_prepare_candidate_clone_same_lineage_new_base_fails_closed(registry_fixture) -> None:
    workspace, source, config_dir, journal_root, base = registry_fixture
    first = prepare_candidate_clone(
        "source-project",
        "candidate/lineage-new-base",
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    new_base = _commit(source, "lineage-new-base")
    clones_root = workspace / ".mcp-candidate-clones"
    before = {p.name for p in clones_root.iterdir() if p.name.startswith("candidate-")}
    registry_before = (config_dir / "projects.yaml").read_text(encoding="utf-8")

    with pytest.raises(CandidateCloneError) as exc_info:
        prepare_candidate_clone(
            "source-project",
            "candidate/lineage-new-base",
            new_base,
            config_dir=config_dir,
            journal_root=journal_root,
        )

    err = exc_info.value
    assert err.code == "CANDIDATE_LINEAGE_EXISTS"
    assert err.retryable is False
    assert err.message == "a candidate clone already exists for this source project and branch"
    details = err.details
    assert details is not None
    assert details["branch"] == "candidate/lineage-new-base"
    assert details["requested_base_sha"] == new_base
    assert details["existing_project_id"] == first.project_id
    assert details["existing_base_sha"] == base
    after = {p.name for p in clones_root.iterdir() if p.name.startswith("candidate-")}
    assert after == before
    assert (config_dir / "projects.yaml").read_text(encoding="utf-8") == registry_before


def test_prepare_candidate_clone_allows_distinct_branches(registry_fixture) -> None:
    workspace, _source, config_dir, journal_root, base = registry_fixture
    first = prepare_candidate_clone(
        "source-project",
        "candidate/distinct-a",
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    second = prepare_candidate_clone(
        "source-project",
        "candidate/distinct-b",
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )

    assert second.project_id != first.project_id
    assert second.recovered is False
    assert second.registered is True
    assert second.clean is True
    assert (workspace / ".mcp-candidate-clones" / first.project_id).is_dir()
    assert (workspace / ".mcp-candidate-clones" / second.project_id).is_dir()
    registry = (config_dir / "projects.yaml").read_text(encoding="utf-8")
    assert first.project_id in registry
    assert second.project_id in registry


def test_prepare_candidate_clone_denies_candidate_sources(registry_fixture) -> None:
    workspace, _source, config_dir, journal_root, base = registry_fixture
    first = prepare_candidate_clone(
        "source-project",
        "candidate/deny-source",
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    registry_path = config_dir / "projects.yaml"
    registry_path.write_text(
        registry_path.read_text(encoding="utf-8")
        + "\n"
        + "  candidate-typed-source:\n"
        + "    root: source\n"
        + "    type: candidate-clone\n"
        + "    description: candidate typed source\n"
        + "    tags: [test]\n"
        + "\n"
        + "  candidate-path-source:\n"
        + f"    root: .mcp-candidate-clones/{first.project_id}\n"
        + "    type: repository\n"
        + "    description: candidate root source\n"
        + "    tags: [test]\n",
        encoding="utf-8",
    )
    reset_registry()

    for denied_project in ("candidate-typed-source", "candidate-path-source"):
        with pytest.raises(CandidateCloneError) as exc_info:
            prepare_candidate_clone(
                denied_project,
                "candidate/denied-flow",
                base,
                config_dir=config_dir,
                journal_root=journal_root,
            )
        err = exc_info.value
        assert err.code == "CANDIDATE_SOURCE_DENIED"
        assert err.retryable is False
        assert err.details == {"source_project": denied_project}

    candidate_dirs = {
        p.name
        for p in (workspace / ".mcp-candidate-clones").iterdir()
        if p.name.startswith("candidate-")
    }
    assert candidate_dirs == {first.project_id}


def test_prepare_candidate_clone_serializes_concurrent_same_lineage(registry_fixture) -> None:
    workspace, _source, config_dir, journal_root, base = registry_fixture

    def call(_: int) -> CandidateCloneReceipt:
        return prepare_candidate_clone(
            "source-project",
            "candidate/concurrent-lineage",
            base,
            config_dir=config_dir,
            journal_root=journal_root,
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        receipts = list(pool.map(call, range(3)))

    assert len({receipt.project_id for receipt in receipts}) == 1
    assert sum(receipt.registered for receipt in receipts) == 1
    assert sum(receipt.recovered for receipt in receipts) == 2
    assert all(receipt.clean for receipt in receipts)
    candidate_dirs = {
        p.name
        for p in (workspace / ".mcp-candidate-clones").iterdir()
        if p.name.startswith("candidate-")
    }
    assert candidate_dirs == {receipts[0].project_id}
    registry = (config_dir / "projects.yaml").read_text(encoding="utf-8")
    registered_ids = [
        line.split(":")[0].strip()
        for line in registry.splitlines()
        if line.startswith(f"  {receipts[0].project_id}:")
    ]
    assert registered_ids == [receipts[0].project_id]


@pytest.mark.parametrize("corruption", ["invalid-json", "mismatched-id"])
def test_prepare_candidate_clone_malformed_lineage_metadata_fails_closed(
    registry_fixture,
    corruption: str,
) -> None:
    workspace, source, config_dir, journal_root, base = registry_fixture
    first = prepare_candidate_clone(
        "source-project",
        "candidate/malformed-lineage",
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / first.project_id
    metadata_path = clone_root / ".git" / "mcp-candidate-clone.json"
    if corruption == "invalid-json":
        metadata_path.write_text("{ not json\n", encoding="utf-8")
    else:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
        data["project_id"] = "candidate-impostor"
        metadata_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    new_base = _commit(source, "malformed-lineage-new-base")
    with pytest.raises(CandidateCloneError) as exc_info:
        prepare_candidate_clone(
            "source-project",
            "candidate/malformed-lineage",
            new_base,
            config_dir=config_dir,
            journal_root=journal_root,
        )

    err = exc_info.value
    assert err.code == "CANDIDATE_LINEAGE_SCAN_FAILED"
    assert err.retryable is True
    assert not (workspace / ".mcp-candidate-clones" / "candidate-impostor").exists()


@pytest.mark.parametrize("link_target", ["metadata-file", "git-dir"])
def test_prepare_candidate_clone_symlink_lineage_metadata_fails_closed(
    registry_fixture,
    link_target: str,
) -> None:
    workspace, source, config_dir, journal_root, base = registry_fixture
    first = prepare_candidate_clone(
        "source-project",
        "candidate/symlink-lineage",
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / first.project_id
    if link_target == "metadata-file":
        metadata_path = clone_root / ".git" / "mcp-candidate-clone.json"
        backup = metadata_path.with_name(metadata_path.name + ".real")
        metadata_path.rename(backup)
        metadata_path.symlink_to(backup)
    else:
        git_dir = clone_root / ".git"
        backup = git_dir.with_name(git_dir.name + ".real")
        git_dir.rename(backup)
        git_dir.symlink_to(backup)

    new_base = _commit(source, "symlink-lineage-new-base")
    with pytest.raises(CandidateCloneError) as exc_info:
        prepare_candidate_clone(
            "source-project",
            "candidate/symlink-lineage",
            new_base,
            config_dir=config_dir,
            journal_root=journal_root,
        )

    err = exc_info.value
    assert err.code == "CANDIDATE_LINEAGE_SCAN_FAILED"
    assert err.retryable is True


def test_prepare_candidate_clone_symlink_lock_storage_fails_closed(registry_fixture) -> None:
    workspace, _source, config_dir, journal_root, base = registry_fixture
    clones_root = workspace / ".mcp-candidate-clones"
    clones_root.mkdir(parents=True, exist_ok=True)
    os.symlink(str(workspace / "lock-storage-target"), str(clones_root / ".locks"))

    with pytest.raises(CandidateCloneError) as exc_info:
        prepare_candidate_clone(
            "source-project",
            "candidate/symlink-storage",
            base,
            config_dir=config_dir,
            journal_root=journal_root,
        )

    err = exc_info.value
    assert err.code == "CANDIDATE_LOCK_FAILED"
    assert err.retryable is True
    assert "candidate/symlink-storage" not in (config_dir / "projects.yaml").read_text(
        encoding="utf-8"
    )
    candidate_dirs = {
        p.name for p in clones_root.iterdir() if p.name != ".locks" and p.name.startswith("candidate-")
    }
    assert candidate_dirs == set()


def test_prepare_candidate_clone_symlink_lock_file_fails_closed(registry_fixture) -> None:
    from examples.mcp_server import candidate_clone as candidate_clone_module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    clones_root = workspace / ".mcp-candidate-clones"
    clones_root.mkdir(parents=True, exist_ok=True)
    (clones_root / ".locks").mkdir()
    lock_name = candidate_clone_module._lineage_lock_name(
        "source-project",
        "candidate/symlink-lockfile",
    )
    target = workspace / "lock-file-target"
    target.write_text("", encoding="utf-8")
    os.symlink(str(target), str(clones_root / ".locks" / lock_name))

    with pytest.raises(CandidateCloneError) as exc_info:
        prepare_candidate_clone(
            "source-project",
            "candidate/symlink-lockfile",
            base,
            config_dir=config_dir,
            journal_root=journal_root,
        )

    err = exc_info.value
    assert err.code == "CANDIDATE_LOCK_FAILED"
    assert err.retryable is True
    assert "candidate/symlink-lockfile" not in (config_dir / "projects.yaml").read_text(
        encoding="utf-8"
    )
    candidate_dirs = {
        p.name
        for p in clones_root.iterdir()
        if p.name != ".locks" and p.name.startswith("candidate-")
    }
    assert candidate_dirs == set()
