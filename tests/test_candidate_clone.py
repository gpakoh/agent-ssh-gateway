from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.workspace.registry import reset_registry
from examples.mcp_server.agent_sources import (
    ManagedSourceBundleError,
    ManagedSourcePublication,
)
from examples.mcp_server.candidate_clone import (
    CandidateCloneError,
    CandidateCloneReceipt,
    candidate_cleanup,
    prepare_candidate_clone,
)
from examples.mcp_server.project_registry_control import register_project

THREAD_SYNC_TIMEOUT_SECONDS = float(
    os.environ.get("MCP_TEST_THREAD_SYNC_TIMEOUT_SECONDS", "30")
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


def test_prepare_candidate_clone_accepts_source_from_named_registry_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    astro_sites = tmp_path / "astro-sites"
    workspace.mkdir()
    source = astro_sites / "example"
    base = _init_repo(source)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "projects.yaml").write_text(
        "version: 1\n"
        f"registry_root: {workspace}\n"
        "registry_roots:\n"
        f"  astro-sites: {astro_sites}\n\n"
        "projects:\n"
        "  example:\n"
        "    root: example\n"
        "    root_selector: astro-sites\n"
        "    type: astro-site\n"
        "    description: XLOUD site\n"
        "    tags: [astro]\n",
        encoding="utf-8",
    )
    journal_root = tmp_path / "journals"
    reset_registry()
    try:
        receipt = prepare_candidate_clone(
            "example",
            "candidate/external-root-flow",
            base,
            config_dir=config_dir,
            journal_root=journal_root,
        )

        clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
        assert clone_root.is_dir()
        assert source.resolve().is_relative_to(astro_sites.resolve())
        assert not clone_root.resolve().is_relative_to(astro_sites.resolve())
        assert clone_root.resolve().is_relative_to(workspace.resolve())
        assert _git(clone_root, "rev-parse", "HEAD") == base
        assert _git(clone_root, "rev-parse", "--abbrev-ref", "HEAD") == (
            "candidate/external-root-flow"
        )
        assert receipt.clean is True
    finally:
        reset_registry()


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


def test_prepare_candidate_clone_rejects_exact_root_symlink_before_git_access(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as candidate_clone_module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    receipt = prepare_candidate_clone(
        "source-project",
        "candidate/exact-root-symlink",
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    target = workspace / "exact-root-symlink-target"
    clone_root.rename(target)
    clone_root.symlink_to(target, target_is_directory=True)
    sentinel = target / "sentinel.txt"
    sentinel.write_text("untouched\n", encoding="utf-8")

    def unexpected_status(_repo: Path) -> tuple[bool, str, int]:
        raise AssertionError("candidate status must not follow an exact-root symlink")

    monkeypatch.setattr(candidate_clone_module, "_status_state", unexpected_status)

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_clone_module.prepare_candidate_clone(
            "source-project",
            "candidate/exact-root-symlink",
            base,
            config_dir=config_dir,
            journal_root=journal_root,
        )

    assert exc_info.value.code == "WORKSPACE_CONTENDED"
    assert exc_info.value.retryable is False
    assert sentinel.read_text(encoding="utf-8") == "untouched\n"


def test_prepare_candidate_clone_rejects_exact_git_symlink_before_status(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as candidate_clone_module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    receipt = prepare_candidate_clone(
        "source-project",
        "candidate/exact-git-symlink",
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    git_dir = clone_root / ".git"
    target = workspace / "exact-git-symlink-target"
    git_dir.rename(target)
    git_dir.symlink_to(target, target_is_directory=True)
    sentinel = target / "sentinel.txt"
    sentinel.write_text("untouched\n", encoding="utf-8")

    def unexpected_status(_repo: Path) -> tuple[bool, str, int]:
        raise AssertionError("candidate status must not follow an exact .git symlink")

    monkeypatch.setattr(candidate_clone_module, "_status_state", unexpected_status)

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_clone_module.prepare_candidate_clone(
            "source-project",
            "candidate/exact-git-symlink",
            base,
            config_dir=config_dir,
            journal_root=journal_root,
        )

    assert exc_info.value.code == "WORKSPACE_CONTENDED"
    assert exc_info.value.retryable is False
    assert sentinel.read_text(encoding="utf-8") == "untouched\n"


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


def test_register_project_rejects_symlinked_registry_mutation_lock(
    registry_fixture,
    tmp_path: Path,
) -> None:
    from examples.mcp_server import project_registry_control as registry_module

    workspace, _source, config_dir, journal_root, _base = registry_fixture
    new_root = workspace / "lock-symlink-project"
    new_root.mkdir()
    journal_root.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "registry-lock-victim"
    victim.write_text("sentinel\n", encoding="utf-8")
    (journal_root / ".project-registry.lock").symlink_to(victim)

    with pytest.raises(registry_module.ProjectRegistrationError):
        registry_module.register_project(
            config_dir=config_dir,
            journal_root=journal_root,
            project_id="lock-symlink-project",
            root="lock-symlink-project",
            project_type="test",
            persist_to_source=True,
        )

    assert victim.read_text(encoding="utf-8") == "sentinel\n"
    assert "lock-symlink-project:" not in (config_dir / "projects.yaml").read_text(
        encoding="utf-8"
    )


def test_registry_mutation_lock_same_process_contention_is_bounded(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import project_registry_control as registry_module

    workspace, _source, config_dir, journal_root, _base = registry_fixture
    new_root = workspace / "bounded-lock-project"
    new_root.mkdir()
    entered = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with registry_module.project_registry_mutation_lock(journal_root):
            entered.set()
            assert release.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold_lock)
        assert entered.wait(timeout=5)
        monkeypatch.setattr(registry_module, "_REGISTRY_LOCK_TIMEOUT_S", 0.05)

        started = time.monotonic()
        with pytest.raises(registry_module.ProjectRegistrationError) as exc_info:
            registry_module.register_project(
                config_dir=config_dir,
                journal_root=journal_root,
                project_id="bounded-lock-project",
                root="bounded-lock-project",
                project_type="test",
                persist_to_source=True,
            )
        elapsed = time.monotonic() - started

        assert exc_info.value.code == "WORKSPACE_CONTENDED"
        assert elapsed < 1.0
        release.set()
        holder.result(timeout=5)

    assert "bounded-lock-project:" not in (config_dir / "projects.yaml").read_text(
        encoding="utf-8"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Candidate cleanup core (AO-022 internal)
# ─────────────────────────────────────────────────────────────────────────────


def _mock_cleanup_remote_probe(
    monkeypatch: pytest.MonkeyPatch,
    module,
    *,
    preserved_ref: str,
    head: str,
    delivery_branch: str,
    delivery: str = "absent",
    preserved: str = "found",
) -> None:
    probe_type = module.RemoteRefProbe
    status_type = module.RemoteRefStatus

    def _probe(_source: Path, ref: str):
        if ref == preserved_ref:
            if preserved == "unknown":
                return probe_type(status_type.UNKNOWN)
            if preserved == "absent":
                return probe_type(status_type.ABSENT)
            return probe_type(status_type.FOUND, head)
        if ref == delivery_branch:
            if delivery == "published":
                return probe_type(status_type.FOUND, head)
            if delivery == "unknown":
                return probe_type(status_type.UNKNOWN)
            return probe_type(status_type.ABSENT)
        return probe_type(status_type.ABSENT)

    monkeypatch.setattr(module, "_probe_remote_ref", _probe)


def _idle_guard() -> None:
    return None


def test_candidate_cleanup_removes_preserved_candidate_and_is_idempotent(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-success"
    preserved_ref = "archive/candidate-cleanup-success"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    cleaned = candidate_cleanup(
        receipt.project_id,
        base,
        branch,
        "source-project",
        preserved_ref,
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )

    assert cleaned.registry_removed is True
    assert cleaned.directory_removed is True
    assert cleaned.already_cleaned is False
    assert not clone_root.exists()
    assert receipt.project_id not in (config_dir / "projects.yaml").read_text(encoding="utf-8")

    repeated = candidate_cleanup(
        receipt.project_id,
        base,
        branch,
        "source-project",
        preserved_ref,
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )
    assert repeated.already_cleaned is True
    assert repeated.registry_removed is False
    assert repeated.directory_removed is False


def test_candidate_cleanup_requires_reference_guard(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-guard-required"
    preserved_ref = "archive/candidate-cleanup-guard-required"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    with pytest.raises(TypeError):
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
        )
    assert clone_root.exists()
    assert receipt.project_id in (config_dir / "projects.yaml").read_text(encoding="utf-8")

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard="not-callable",
        )
    assert exc_info.value.code == "INVALID_INPUT"


def test_candidate_cleanup_guard_denial_leaves_everything_intact(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-guard-blocked"
    preserved_ref = "archive/candidate-cleanup-guard-blocked"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    def deny_guard() -> None:
        raise CandidateCloneError("WORKSPACE_CONTENDED", "open PR still references the clone")

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=deny_guard,
        )
    assert exc_info.value.code == "WORKSPACE_CONTENDED"
    assert clone_root.exists()
    assert receipt.project_id in (config_dir / "projects.yaml").read_text(encoding="utf-8")
    tombstone_path, _tombstone_id = module._cleanup_tombstone_path(journal_root, receipt.project_id)
    assert not tombstone_path.exists()


def test_candidate_cleanup_rejects_symlinked_tombstone_directory(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-journal-dir-symlink"
    preserved_ref = "archive/candidate-cleanup-journal-dir-symlink"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    journal_root.mkdir(parents=True, exist_ok=True)
    victim_dir = tmp_path / "cleanup-journal-victim"
    victim_dir.mkdir()
    (journal_root / "candidate-cleanup").symlink_to(victim_dir, target_is_directory=True)

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )

    assert exc_info.value.code == "WORKSPACE_CONTENDED"
    assert clone_root.exists()
    assert receipt.project_id in (config_dir / "projects.yaml").read_text(encoding="utf-8")
    assert list(victim_dir.iterdir()) == []


def test_candidate_cleanup_rejects_symlinked_final_tombstone(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-final-tombstone-symlink"
    preserved_ref = "archive/candidate-cleanup-final-tombstone-symlink"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    tombstone_path, _ = module._cleanup_tombstone_path(journal_root, receipt.project_id)
    tombstone_path.parent.mkdir(parents=True)
    victim = tmp_path / "cleanup-tombstone-victim.json"
    victim.write_text("sentinel\n", encoding="utf-8")
    tombstone_path.symlink_to(victim)

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )

    assert exc_info.value.code == "WORKSPACE_CONTENDED"
    assert victim.read_text(encoding="utf-8") == "sentinel\n"
    assert clone_root.exists()
    assert receipt.project_id in (config_dir / "projects.yaml").read_text(encoding="utf-8")


def test_candidate_cleanup_rejects_hardlinked_final_tombstone(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-final-tombstone-hardlink"
    preserved_ref = "archive/candidate-cleanup-final-tombstone-hardlink"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    tombstone_path, _ = module._cleanup_tombstone_path(journal_root, receipt.project_id)
    tombstone_path.parent.mkdir(parents=True)
    victim = tmp_path / "cleanup-tombstone-hardlink-victim.json"
    victim.write_text("sentinel\n", encoding="utf-8")
    os.link(victim, tombstone_path)

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )

    assert exc_info.value.code == "WORKSPACE_CONTENDED"
    assert victim.read_text(encoding="utf-8") == "sentinel\n"
    assert clone_root.exists()
    assert receipt.project_id in (config_dir / "projects.yaml").read_text(encoding="utf-8")


def test_candidate_cleanup_ignores_poisoned_legacy_predictable_tmp_symlink(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-random-temp"
    preserved_ref = "archive/candidate-cleanup-random-temp"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    tombstone_path, _ = module._cleanup_tombstone_path(journal_root, receipt.project_id)
    tombstone_path.parent.mkdir(parents=True)
    victim = tmp_path / "cleanup-legacy-temp-victim.txt"
    victim.write_text("sentinel\n", encoding="utf-8")
    legacy_tmp = tombstone_path.with_name(tombstone_path.name + ".tmp")
    legacy_tmp.symlink_to(victim)

    cleaned = candidate_cleanup(
        receipt.project_id,
        base,
        branch,
        "source-project",
        preserved_ref,
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )

    assert cleaned.directory_removed is True
    assert not clone_root.exists()
    assert victim.read_text(encoding="utf-8") == "sentinel\n"
    assert legacy_tmp.is_symlink()
    assert tombstone_path.is_file()


def test_candidate_cleanup_calls_guard_before_unregister_and_before_rmtree(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-guard-order"
    preserved_ref = "archive/candidate-cleanup-guard-order"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    events: list[str] = []
    guard_calls: list[int] = []
    original_unregister = module._unregister_candidate
    original_rmtree = module.shutil.rmtree

    def tracked_unregister(**kwargs):
        events.append("unregister")
        return original_unregister(**kwargs)

    def tracked_rmtree(path: Path) -> None:
        events.append("rmtree")
        return original_rmtree(path)

    def guard() -> None:
        guard_calls.append(len(events))
        events.append("guard")

    monkeypatch.setattr(module, "_unregister_candidate", tracked_unregister)
    monkeypatch.setattr(module.shutil, "rmtree", tracked_rmtree)

    candidate_cleanup(
        receipt.project_id,
        base,
        branch,
        "source-project",
        preserved_ref,
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=guard,
    )

    assert guard_calls == [0, 2]
    assert events == ["guard", "unregister", "guard", "rmtree"]


def test_candidate_cleanup_guard_recheck_blocks_rmtree_then_recovers(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-guard-recheck"
    preserved_ref = "archive/candidate-cleanup-guard-recheck"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    guard_calls: list[str] = []

    def deny_recheck() -> None:
        guard_calls.append("call")
        if len(guard_calls) == 2:
            raise CandidateCloneError("WORKSPACE_CONTENDED", "PR opened during cleanup")

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=deny_recheck,
        )
    assert exc_info.value.code == "WORKSPACE_CONTENDED"
    assert guard_calls == ["call", "call"]
    assert clone_root.exists()
    assert receipt.project_id not in (config_dir / "projects.yaml").read_text(encoding="utf-8")

    recovered = candidate_cleanup(
        receipt.project_id,
        base,
        branch,
        "source-project",
        preserved_ref,
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )
    assert recovered.directory_removed is True
    assert recovered.registry_removed is False
    assert not clone_root.exists()


def test_candidate_cleanup_refuses_dirty_candidate(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-dirty"
    preserved_ref = "archive/candidate-cleanup-dirty"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    (clone_root / "dirty.txt").write_text("keep me\n", encoding="utf-8")
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert exc_info.value.code == "WORKSPACE_CONTENDED"
    assert clone_root.exists()
    assert receipt.project_id in (config_dir / "projects.yaml").read_text(encoding="utf-8")


def test_candidate_cleanup_refuses_identity_mismatch(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    _workspace, source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-identity"
    preserved_ref = "archive/candidate-cleanup-identity"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )
    monkeypatch.setattr(
        module,
        "_source_root",
        lambda _config, _project, *, workspace_root: source.resolve(),
    )

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "different-source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert exc_info.value.code == "WORKSPACE_CONTENDED"


def test_candidate_cleanup_requires_exact_remote_preservation(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-unpreserved"
    preserved_ref = "archive/candidate-cleanup-unpreserved"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
        preserved="absent",
    )

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert exc_info.value.code == "CHECK_FAILED"
    assert exc_info.value.retryable is True
    assert clone_root.exists()
    assert receipt.project_id in (config_dir / "projects.yaml").read_text(encoding="utf-8")


def test_candidate_cleanup_fails_closed_on_unknown_preservation_remote(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-preserve-unknown"
    preserved_ref = "archive/candidate-cleanup-preserve-unknown"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
        preserved="unknown",
    )

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert exc_info.value.code == "CHECK_FAILED"
    assert exc_info.value.retryable is True


def test_candidate_cleanup_fails_closed_on_unknown_delivery_remote(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-delivery-unknown"
    preserved_ref = "archive/candidate-cleanup-delivery-unknown"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="unknown",
    )

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert exc_info.value.code == "CHECK_FAILED"
    assert exc_info.value.retryable is True
    assert clone_root.exists()
    assert receipt.project_id in (config_dir / "projects.yaml").read_text(encoding="utf-8")


def test_candidate_cleanup_refuses_published_delivery_branch(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-published"
    preserved_ref = "archive/candidate-cleanup-published"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="published",
    )

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert exc_info.value.code == "WORKSPACE_CONTENDED"
    assert exc_info.value.retryable is False
    assert clone_root.exists()
    assert receipt.project_id in (config_dir / "projects.yaml").read_text(encoding="utf-8")


def test_candidate_cleanup_refuses_symlinked_candidate_root(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-symlink-root"
    preserved_ref = "archive/candidate-cleanup-symlink-root"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    moved = workspace / "moved-candidate"
    clone_root.rename(moved)
    clone_root.symlink_to(moved, target_is_directory=True)
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert exc_info.value.code == "POLICY_DENIED"
    assert moved.exists()


def test_candidate_cleanup_rejects_symlinked_git_before_metadata(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-git-symlink"
    preserved_ref = "archive/candidate-cleanup-git-symlink"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    git_dir = clone_root / ".git"
    target = workspace / "cleanup-git-symlink-target"
    git_dir.rename(target)
    git_dir.symlink_to(target, target_is_directory=True)
    sentinel = target / "sentinel.txt"
    sentinel.write_text("untouched\n", encoding="utf-8")
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    def unexpected_metadata(_root: Path) -> dict:
        raise AssertionError("candidate metadata must not be read behind a .git symlink")

    def unexpected_status(_repo: Path) -> tuple[bool, str, int]:
        raise AssertionError("candidate status must not run behind a .git symlink")

    monkeypatch.setattr(module, "_read_metadata", unexpected_metadata)
    monkeypatch.setattr(module, "_status_state", unexpected_status)

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert exc_info.value.code == "WORKSPACE_CONTENDED"
    assert git_dir.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "untouched\n"
    assert receipt.project_id in (config_dir / "projects.yaml").read_text(encoding="utf-8")


def test_candidate_cleanup_refuses_active_task_evidence(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-active"
    preserved_ref = "archive/candidate-cleanup-active"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    (clone_root / ".ai-bridge" / "tasks" / "live-task").mkdir(parents=True)
    monkeypatch.setattr(module, "_status_state", lambda _root: (False, "0" * 64, 0))
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert exc_info.value.code == "WORKSPACE_CONTENDED"
    assert clone_root.exists()


def test_candidate_cleanup_refuses_registered_descendant(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-descendant"
    preserved_ref = "archive/candidate-cleanup-descendant"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    (clone_root / "child").mkdir()
    register_project(
        config_dir=config_dir,
        journal_root=journal_root,
        project_id="candidate-child",
        root=f".mcp-candidate-clones/{receipt.project_id}/child",
        project_type="candidate-child",
        parent=receipt.project_id,
        persist_to_source=True,
    )
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert exc_info.value.code == "WORKSPACE_CONTENDED"
    assert clone_root.exists()


def test_candidate_cleanup_fails_closed_on_malformed_unrelated_registry_entry(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-malformed-sibling"
    preserved_ref = "archive/candidate-cleanup-malformed-sibling"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    registry_path = config_dir / "projects.yaml"
    registry_path.write_text(
        registry_path.read_text(encoding="utf-8") + "  malformed-sibling: []\n",
        encoding="utf-8",
    )
    reset_registry()
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )

    assert exc_info.value.code == "TOOL_EXECUTION_FAILED"
    assert clone_root.exists()
    assert receipt.project_id in registry_path.read_text(encoding="utf-8")


def test_candidate_cleanup_reconciles_missing_dir_still_registered(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-missing-dir"
    preserved_ref = "archive/candidate-cleanup-missing-dir"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    shutil.rmtree(clone_root)
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    cleaned = candidate_cleanup(
        receipt.project_id,
        base,
        branch,
        "source-project",
        preserved_ref,
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )
    assert cleaned.registry_removed is True
    assert cleaned.directory_removed is False
    assert cleaned.already_cleaned is False
    assert receipt.project_id not in (config_dir / "projects.yaml").read_text(encoding="utf-8")

    repeated = candidate_cleanup(
        receipt.project_id,
        base,
        branch,
        "source-project",
        preserved_ref,
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )
    assert repeated.already_cleaned is True
    assert repeated.registry_removed is False
    assert repeated.directory_removed is False


def test_candidate_cleanup_complete_tombstone_reconciles_reappeared_registry_entry(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-complete-registry-replay"
    preserved_ref = "archive/candidate-cleanup-complete-registry-replay"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    shutil.rmtree(clone_root)
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    first = candidate_cleanup(
        receipt.project_id,
        base,
        branch,
        "source-project",
        preserved_ref,
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )
    assert first.registry_removed is True
    assert first.directory_removed is False

    registry_path = config_dir / "projects.yaml"
    registry_path.write_text(
        registry_path.read_text(encoding="utf-8")
        + (
            f"  {receipt.project_id}:\n"
            f"    root: .mcp-candidate-clones/{receipt.project_id}\n"
            "    type: candidate-clone\n"
            "    description: simulated stale replay\n"
            "    tags: [candidate]\n"
        ),
        encoding="utf-8",
    )
    reset_registry()
    assert receipt.project_id in registry_path.read_text(encoding="utf-8")

    reconciled = candidate_cleanup(
        receipt.project_id,
        base,
        branch,
        "source-project",
        preserved_ref,
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )

    assert reconciled.registry_removed is True
    assert reconciled.directory_removed is False
    assert reconciled.already_cleaned is False
    assert receipt.project_id not in (config_dir / "projects.yaml").read_text(encoding="utf-8")


def test_candidate_cleanup_recovers_missing_dir_after_unregister_before_complete_tombstone(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-missing-dir-post-unregister"
    preserved_ref = "archive/candidate-cleanup-missing-dir-post-unregister"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    shutil.rmtree(clone_root)
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    real_write = module._write_cleanup_tombstone
    failed_complete = False

    def fail_first_complete(path: Path, data: dict[str, object]) -> None:
        nonlocal failed_complete
        if data.get("phase") == "complete" and not failed_complete:
            failed_complete = True
            raise CandidateCloneError(
                "TOOL_EXECUTION_FAILED",
                "simulated complete tombstone interruption",
                retryable=True,
            )
        real_write(path, data)

    monkeypatch.setattr(module, "_write_cleanup_tombstone", fail_first_complete)
    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )

    assert exc_info.value.code == "TOOL_EXECUTION_FAILED"
    assert receipt.project_id not in (config_dir / "projects.yaml").read_text(encoding="utf-8")
    tombstone_path, _ = module._cleanup_tombstone_path(journal_root, receipt.project_id)
    assert module._read_cleanup_tombstone(tombstone_path)["phase"] == "prepared"

    monkeypatch.setattr(module, "_write_cleanup_tombstone", real_write)
    recovered = candidate_cleanup(
        receipt.project_id,
        base,
        branch,
        "source-project",
        preserved_ref,
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )

    assert recovered.registry_removed is False
    assert recovered.directory_removed is False
    assert recovered.already_cleaned is True
    assert module._read_cleanup_tombstone(tombstone_path)["phase"] == "complete"


def test_candidate_cleanup_recovers_missing_dir_with_prepared_tombstone(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-prepared-tombstone"
    preserved_ref = "archive/candidate-cleanup-prepared-tombstone"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )
    real_unregister = module._unregister_candidate

    def fail_unregister(**kwargs):
        raise CandidateCloneError("TOOL_EXECUTION_FAILED", "simulated unregister interruption", retryable=True)

    monkeypatch.setattr(module, "_unregister_candidate", fail_unregister)
    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert exc_info.value.code == "TOOL_EXECUTION_FAILED"
    assert clone_root.exists()
    assert receipt.project_id in (config_dir / "projects.yaml").read_text(encoding="utf-8")

    monkeypatch.setattr(module, "_unregister_candidate", real_unregister)
    shutil.rmtree(clone_root)
    cleaned = candidate_cleanup(
        receipt.project_id,
        base,
        branch,
        "source-project",
        preserved_ref,
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )
    assert cleaned.registry_removed is True
    assert cleaned.directory_removed is False
    assert not clone_root.exists()
    assert receipt.project_id not in (config_dir / "projects.yaml").read_text(encoding="utf-8")


def test_candidate_cleanup_recovers_after_unregister_before_delete(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-recovery"
    preserved_ref = "archive/candidate-cleanup-recovery"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )
    original_rmtree = module.shutil.rmtree

    def _interrupt(_path: Path) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr(module.shutil, "rmtree", _interrupt)
    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert exc_info.value.code == "TOOL_EXECUTION_FAILED"
    assert exc_info.value.retryable is True
    assert clone_root.exists()
    assert receipt.project_id not in (config_dir / "projects.yaml").read_text(encoding="utf-8")

    monkeypatch.setattr(module.shutil, "rmtree", original_rmtree)
    recovered = candidate_cleanup(
        receipt.project_id,
        base,
        branch,
        "source-project",
        preserved_ref,
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )
    assert recovered.directory_removed is True
    assert recovered.registry_removed is False
    assert not clone_root.exists()


def test_candidate_cleanup_recovers_registry_removed_then_stops_new_dirty(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-recovery-dirty"
    preserved_ref = "archive/candidate-cleanup-recovery-dirty"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    def _interrupt(_path: Path) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr(module.shutil, "rmtree", _interrupt)
    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert exc_info.value.code == "TOOL_EXECUTION_FAILED"
    assert receipt.project_id not in (config_dir / "projects.yaml").read_text(encoding="utf-8")

    (clone_root / "newly-dirty.txt").write_text("dirties after registry removal\n", encoding="utf-8")
    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert exc_info.value.code == "WORKSPACE_CONTENDED"
    assert clone_root.exists()
    assert (clone_root / "newly-dirty.txt").read_text(encoding="utf-8") == (
        "dirties after registry removal\n"
    )


def test_candidate_cleanup_recovers_registry_removed_then_stops_new_active_evidence(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-recovery-active"
    preserved_ref = "archive/candidate-cleanup-recovery-active"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    def _interrupt(_path: Path) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr(module.shutil, "rmtree", _interrupt)
    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert exc_info.value.code == "TOOL_EXECUTION_FAILED"
    assert receipt.project_id not in (config_dir / "projects.yaml").read_text(encoding="utf-8")

    (clone_root / ".ai-bridge" / "tasks" / "live-task").mkdir(parents=True)
    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
    assert exc_info.value.code == "WORKSPACE_CONTENDED"
    assert clone_root.exists()
    assert (clone_root / ".ai-bridge" / "tasks" / "live-task").is_dir()


def test_candidate_cleanup_holds_lineage_lock_across_registry_and_delete(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-lock"
    preserved_ref = "archive/candidate-cleanup-lock"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    lock_events: list[object] = []
    real_lock = module._lineage_lock

    class _Recorder:
        def __init__(self, cm):
            self._cm = cm

        def __enter__(self):
            lock_events.append("enter")
            return self._cm.__enter__()

        def __exit__(self, *args):
            lock_events.append("exit")
            return self._cm.__exit__(*args)

    def rec_lock(workspace_root: Path, project: str, branch: str):
        lock_events.append(("lock", project, branch))
        return _Recorder(real_lock(workspace_root, project, branch))

    monkeypatch.setattr(module, "_lineage_lock", rec_lock)

    original_rmtree = module.shutil.rmtree

    def guarded_rmtree(path: Path) -> None:
        assert lock_events[-1] == "enter"
        lock_events.append("rmtree")
        return original_rmtree(path)

    monkeypatch.setattr(module.shutil, "rmtree", guarded_rmtree)

    candidate_cleanup(
        receipt.project_id,
        base,
        branch,
        "source-project",
        preserved_ref,
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )

    assert ("lock", "source-project", branch) in lock_events
    assert lock_events.index("enter") < lock_events.index("rmtree")
    assert lock_events.index("rmtree") < lock_events.index("exit")


def test_candidate_cleanup_holds_registry_lock_through_rmtree(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module
    from examples.mcp_server import project_registry_control as registry_module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-registry-lock"
    preserved_ref = "archive/candidate-cleanup-registry-lock"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / receipt.project_id
    child_root = clone_root / "late-child"
    child_root.mkdir()
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    monkeypatch.setenv("MCP_SUPERVISOR_JOURNAL_ROOT", str(journal_root))
    entered_rmtree = threading.Event()
    release_rmtree = threading.Event()
    real_rmtree = module.shutil.rmtree

    def blocking_rmtree(path: Path) -> None:
        entered_rmtree.set()
        assert release_rmtree.wait(timeout=THREAD_SYNC_TIMEOUT_SECONDS)
        real_rmtree(path)

    monkeypatch.setattr(module.shutil, "rmtree", blocking_rmtree)

    with ThreadPoolExecutor(max_workers=2) as pool:
        cleanup_future = pool.submit(
            candidate_cleanup,
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )
        assert entered_rmtree.wait(timeout=THREAD_SYNC_TIMEOUT_SECONDS)

        register_future = pool.submit(
            registry_module.register_project,
            config_dir=config_dir,
            journal_root=journal_root,
            project_id="late-child",
            root=f".mcp-candidate-clones/{receipt.project_id}/late-child",
            project_type="candidate-child",
            persist_to_source=False,
        )
        time.sleep(0.2)
        assert not register_future.done()

        release_rmtree.set()
        cleaned = cleanup_future.result(timeout=THREAD_SYNC_TIMEOUT_SECONDS)
        assert cleaned.directory_removed is True

        with pytest.raises(registry_module.ProjectRegistrationError) as exc_info:
            register_future.result(timeout=THREAD_SYNC_TIMEOUT_SECONDS)
        assert exc_info.value.code == "INVALID_INPUT"

    assert not clone_root.exists()
    assert "late-child" not in (config_dir / "projects.yaml").read_text(encoding="utf-8")


def test_candidate_cleanup_serializes_concurrent_callers(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-concurrent"
    preserved_ref = "archive/candidate-cleanup-concurrent"
    receipt = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )

    rmtree_calls: list[Path] = []
    original_rmtree = module.shutil.rmtree

    def counting_rmtree(path: Path) -> None:
        rmtree_calls.append(path)
        return original_rmtree(path)

    monkeypatch.setattr(module.shutil, "rmtree", counting_rmtree)

    def call(_: int):
        return candidate_cleanup(
            receipt.project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=_idle_guard,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(call, range(2)))

    assert len(rmtree_calls) == 1
    assert len({result.tombstone_id for result in results}) == 1
    assert sum(result.directory_removed for result in results) == 1
    assert sum(result.already_cleaned for result in results) == 1
    assert not (workspace / ".mcp-candidate-clones" / receipt.project_id).exists()


def test_candidate_cleanup_releases_lineage_for_prepare(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-relock"
    preserved_ref = "archive/candidate-cleanup-relock"
    first = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    clone_root = workspace / ".mcp-candidate-clones" / first.project_id
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )
    candidate_cleanup(
        first.project_id,
        base,
        branch,
        "source-project",
        preserved_ref,
        config_dir=config_dir,
        journal_root=journal_root,
        reference_guard=_idle_guard,
    )
    assert not clone_root.exists()

    second = prepare_candidate_clone(
        "source-project",
        branch,
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    second_root = workspace / ".mcp-candidate-clones" / second.project_id
    assert second_root.is_dir()
    assert _git(second_root, "rev-parse", "--abbrev-ref", "HEAD") == branch


def test_candidate_cleanup_never_registered_fails_closed_without_guard(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/cleanup-never-registered"
    preserved_ref = "archive/candidate-cleanup-never-registered"
    never_project_id = "candidate-never-created"
    _mock_cleanup_remote_probe(
        monkeypatch,
        module,
        preserved_ref=preserved_ref,
        head=base,
        delivery_branch=branch,
        delivery="absent",
    )
    guard_calls: list[int] = []

    def guard() -> None:
        guard_calls.append(1)

    with pytest.raises(CandidateCloneError) as exc_info:
        candidate_cleanup(
            never_project_id,
            base,
            branch,
            "source-project",
            preserved_ref,
            config_dir=config_dir,
            journal_root=journal_root,
            reference_guard=guard,
        )
    assert exc_info.value.code == "PROJECT_NOT_FOUND"
    assert guard_calls == []
    assert never_project_id not in (config_dir / "projects.yaml").read_text(encoding="utf-8")
    assert not (workspace / ".mcp-candidate-clones" / never_project_id).exists()


@pytest.mark.parametrize("corruption", ["missing", "malformed", "git-dir-symlink"])
@pytest.mark.parametrize("base_ref_kind", ["exact", "symbolic"])
def test_candidate_preparation_isolated_from_unrelated_lineage(
    registry_fixture, corruption: str, base_ref_kind: str,
) -> None:
    workspace, source, config_dir, journal_root, base = registry_fixture
    unrelated = prepare_candidate_clone(
        "source-project", "candidate/other-lineage", base,
        config_dir=config_dir, journal_root=journal_root,
    )
    other_root = workspace / ".mcp-candidate-clones" / unrelated.project_id
    metadata = other_root / ".git" / "mcp-candidate-clone.json"
    if corruption == "missing":
        metadata.unlink()
    elif corruption == "malformed":
        metadata.write_text("{ invalid", encoding="utf-8")
    else:
        git_dir = other_root / ".git"
        backup = other_root / ".git-backup"
        git_dir.rename(backup)
        git_dir.symlink_to(backup)
    _git(source, "branch", "trusted-base", base)
    source_status = _git(source, "status", "--porcelain=v1")
    ref = base if base_ref_kind == "exact" else "trusted-base"
    first = prepare_candidate_clone(
        "source-project", "candidate/unblocked", ref,
        config_dir=config_dir, journal_root=journal_root,
    )
    second = prepare_candidate_clone(
        "source-project", "candidate/unblocked", ref,
        config_dir=config_dir, journal_root=journal_root,
    )
    assert first.head == base
    assert first.clean
    assert second.project_id == first.project_id
    assert second.recovered
    assert _git(source, "status", "--porcelain=v1") == source_status
    assert _git(source, "rev-parse", "HEAD") == base
    assert other_root.exists()


def test_missing_metadata_in_same_lineage_remains_actionable(registry_fixture) -> None:
    from examples.mcp_server.tool_results import tool_error

    workspace, source, config_dir, journal_root, base = registry_fixture
    first = prepare_candidate_clone(
        "source-project", "candidate/missing-metadata", base,
        config_dir=config_dir, journal_root=journal_root,
    )
    (workspace / ".mcp-candidate-clones" / first.project_id /
     ".git" / "mcp-candidate-clone.json").unlink()
    newer = _commit(source, "new base")
    registry_before = (config_dir / "projects.yaml").read_bytes()
    with pytest.raises(CandidateCloneError) as caught:
        prepare_candidate_clone(
            "source-project", "candidate/missing-metadata", newer,
            config_dir=config_dir, journal_root=journal_root,
        )
    err = caught.value
    assert err.code == "CANDIDATE_LINEAGE_SCAN_FAILED"
    assert err.details["candidate_project_id"] == first.project_id
    assert "repair_action" in err.details
    assert tool_error(code=err.code)["error"]["code"] == err.code
    assert (config_dir / "projects.yaml").read_bytes() == registry_before

def test_probe_remote_ref_uses_resolved_username_for_basic_auth(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from examples.mcp_server import candidate_clone as module

    captured: dict[str, object] = {}
    sha = "a" * 40
    monkeypatch.setattr(
        module,
        "_resolve_trusted_remote",
        lambda _root: (
            "resolved-user",
            "https://git.example.test/gpakoh/test-repo.git",
            "fixture-token",
        ),
    )

    def fake_env(username: str, token: str) -> dict[str, str]:
        captured["auth"] = (username, token)
        return {}

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=f"{sha}\trefs/heads/main\n",
            stderr="",
        )

    monkeypatch.setattr(module, "_minimal_git_env", fake_env)
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    probe = module._probe_remote_ref(tmp_path, "main")

    assert probe.status is module.RemoteRefStatus.FOUND
    assert probe.sha == sha
    assert captured["auth"] == ("resolved-user", "fixture-token")
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[:3] == ["git", "ls-remote", "--exit-code"]
    assert "fixture-token" not in " ".join(str(part) for part in argv)


@pytest.mark.parametrize("identity", ["foreign", "same", "unavailable", "empty"])
def test_legacy_candidate_requires_proven_foreign_repository(
    registry_fixture, monkeypatch: pytest.MonkeyPatch, identity: str,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, source, config_dir, journal_root, base = registry_fixture
    legacy = workspace / ".mcp-candidate-clones" / "candidate-legacy-manual"
    _init_repo(legacy)
    before = (config_dir / "projects.yaml").read_bytes()
    requested = "https://trusted.invalid/owner/requested.git"

    def resolve(root: Path) -> tuple[str, str, str]:
        if root == source:
            return "fixture-user", requested, "unused-test-token"
        assert root == legacy
        if identity == "unavailable":
            raise ManagedSourceBundleError("ambiguous or untrusted repository")
        if identity == "empty":
            return "fixture-user", "", "unused-test-token"
        return (
            "fixture-user",
            "https://trusted.invalid/owner/foreign.git" if identity == "foreign" else requested,
            "unused-test-token",
        )

    monkeypatch.setattr(module, "_resolve_trusted_remote", resolve)
    if identity == "foreign":
        receipt = prepare_candidate_clone(
            "source-project", "candidate/legacy-isolation", base,
            config_dir=config_dir, journal_root=journal_root,
        )
        assert receipt.head == base
        assert receipt.clean
        assert legacy.is_dir()
        assert not (legacy / ".git" / "mcp-candidate-clone.json").exists()
    else:
        with pytest.raises(CandidateCloneError) as caught:
            prepare_candidate_clone(
                "source-project", "candidate/legacy-isolation", base,
                config_dir=config_dir, journal_root=journal_root,
            )
        assert caught.value.code == "CANDIDATE_LINEAGE_SCAN_FAILED"
        assert caught.value.details["candidate_project_id"] == legacy.name
        assert (config_dir / "projects.yaml").read_bytes() == before


def test_legacy_candidate_follows_safe_sibling_origin_to_foreign_repository(
    registry_fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, source, config_dir, journal_root, base = registry_fixture
    candidates = workspace / ".mcp-candidate-clones"
    terminal = candidates / "candidate-legacy-terminal"
    chained = candidates / "candidate-legacy-chained"
    _init_repo(terminal)
    _init_repo(chained)
    subprocess.run(
        ["git", "remote", "add", "origin", str(terminal)],
        cwd=chained,
        check=True,
        capture_output=True,
    )
    requested = "https://trusted.invalid/owner/requested.git"
    foreign = "https://trusted.invalid/owner/foreign.git"

    def resolve(root: Path) -> tuple[str, str, str]:
        if root == source:
            return "fixture-user", requested, "unused-test-token"
        if root == terminal:
            return "fixture-user", foreign, "unused-test-token"
        if root == chained:
            raise ManagedSourceBundleError("legacy clone has only a local origin")
        raise AssertionError(f"unexpected repository root: {root}")

    monkeypatch.setattr(module, "_resolve_trusted_remote", resolve)
    receipt = prepare_candidate_clone(
        "source-project",
        "candidate/chained-legacy-isolation",
        base,
        config_dir=config_dir,
        journal_root=journal_root,
    )
    assert receipt.head == base
    assert receipt.clean
    assert terminal.is_dir()
    assert chained.is_dir()


def test_legacy_candidate_local_chain_with_same_identity_fails_closed(
    registry_fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, source, _config_dir, _journal_root, _base = registry_fixture
    candidates = workspace / ".mcp-candidate-clones"
    terminal = candidates / "candidate-legacy-same-terminal"
    chained = candidates / "candidate-legacy-same-chained"
    _init_repo(terminal)
    _init_repo(chained)
    subprocess.run(
        ["git", "remote", "add", "origin", str(terminal)],
        cwd=chained,
        check=True,
        capture_output=True,
    )
    requested = "https://trusted.invalid/owner/requested.git"

    def resolve(root: Path) -> tuple[str, str, str]:
        if root in (source, terminal):
            return "fixture-user", requested, "unused-test-token"
        if root == chained:
            raise ManagedSourceBundleError("legacy clone has only a local origin")
        raise AssertionError(f"unexpected repository root: {root}")

    monkeypatch.setattr(module, "_resolve_trusted_remote", resolve)
    assert not module._is_verified_foreign_repository(chained, source)


def test_legacy_candidate_local_origin_cycle_fails_closed(
    registry_fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, _config_dir, _journal_root, _base = registry_fixture
    candidates = workspace / ".mcp-candidate-clones"
    first = candidates / "candidate-legacy-cycle-a"
    second = candidates / "candidate-legacy-cycle-b"
    _init_repo(first)
    _init_repo(second)
    subprocess.run(
        ["git", "remote", "add", "origin", str(second)],
        cwd=first,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(first)],
        cwd=second,
        check=True,
        capture_output=True,
    )

    def unavailable(_root: Path) -> tuple[str, str]:
        raise ManagedSourceBundleError("legacy clone has only a local origin")

    monkeypatch.setattr(module, "_resolve_trusted_remote", unavailable)
    assert module._legacy_trusted_remote(first) is None


def test_legacy_candidate_local_origin_symlink_fails_closed(
    registry_fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, _config_dir, _journal_root, _base = registry_fixture
    candidates = workspace / ".mcp-candidate-clones"
    chained = candidates / "candidate-legacy-symlink-source"
    terminal = candidates / "candidate-legacy-symlink-terminal"
    linked = candidates / "candidate-legacy-symlink-target"
    _init_repo(chained)
    _init_repo(terminal)
    linked.symlink_to(terminal, target_is_directory=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(linked)],
        cwd=chained,
        check=True,
        capture_output=True,
    )

    def unavailable(_root: Path) -> tuple[str, str]:
        raise ManagedSourceBundleError("legacy clone has only a local origin")

    monkeypatch.setattr(module, "_resolve_trusted_remote", unavailable)
    assert module._legacy_trusted_remote(chained) is None


def test_legacy_candidate_local_origin_outside_sibling_root_fails_closed(
    registry_fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, _config_dir, _journal_root, _base = registry_fixture
    candidates = workspace / ".mcp-candidate-clones"
    chained = candidates / "candidate-legacy-external"
    outside = workspace / "candidate-outside"
    _init_repo(chained)
    _init_repo(outside)
    subprocess.run(
        ["git", "remote", "add", "origin", str(outside)],
        cwd=chained,
        check=True,
        capture_output=True,
    )

    def unavailable(_root: Path) -> tuple[str, str]:
        raise ManagedSourceBundleError("legacy clone has only a local origin")

    monkeypatch.setattr(module, "_resolve_trusted_remote", unavailable)
    assert module._legacy_trusted_remote(chained) is None


def test_matching_managed_lineage_cannot_be_excluded_by_foreign_remote(
    registry_fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, source, config_dir, journal_root, base = registry_fixture
    branch = "candidate/matching-remote"
    first = prepare_candidate_clone(
        "source-project", branch, base,
        config_dir=config_dir, journal_root=journal_root,
    )
    metadata = workspace / ".mcp-candidate-clones" / first.project_id / ".git" / "mcp-candidate-clone.json"
    metadata.unlink()
    newer = _commit(source, "new matching base")

    def forbidden_probe(*args) -> bool:
        raise AssertionError("matching managed ids must not use a remote exclusion")

    monkeypatch.setattr(module, "_is_verified_foreign_repository", forbidden_probe)
    with pytest.raises(CandidateCloneError) as caught:
        prepare_candidate_clone(
            "source-project", branch, newer,
            config_dir=config_dir, journal_root=journal_root,
        )
    assert caught.value.code == "CANDIDATE_LINEAGE_SCAN_FAILED"


def test_legacy_candidate_git_symlink_cannot_use_remote_exclusion(
    registry_fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as module

    workspace, _source, config_dir, journal_root, base = registry_fixture
    legacy = workspace / ".mcp-candidate-clones" / "candidate-legacy-symlink"
    _init_repo(legacy)
    (legacy / ".git").rename(legacy / ".git-backup")
    (legacy / ".git").symlink_to(legacy / ".git-backup")

    def forbidden_probe(root: Path) -> tuple[str, str]:
        if root == legacy:
            raise AssertionError("unsafe git metadata must not be probed")
        raise ManagedSourceBundleError("test source has no trusted remote")

    monkeypatch.setattr(module, "_resolve_trusted_remote", forbidden_probe)
    with pytest.raises(CandidateCloneError) as caught:
        prepare_candidate_clone(
            "source-project", "candidate/safe-legacy", base,
            config_dir=config_dir, journal_root=journal_root,
        )
    assert caught.value.code == "CANDIDATE_LINEAGE_SCAN_FAILED"
