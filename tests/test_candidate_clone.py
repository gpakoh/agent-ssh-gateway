from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.workspace.registry import reset_registry
from examples.mcp_server.agent_sources import ManagedSourceBundleError, ManagedSourcePublication
from examples.mcp_server.candidate_clone import (
    CandidateCloneError,
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


def test_prepare_candidate_clone_local_clone_trusts_source_gitdir(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as candidate_clone_module

    _workspace, source, config_dir, journal_root, base = registry_fixture
    captured_commands: list[list[str]] = []
    captured_envs: list[dict[str, str] | None] = []

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        command = list(args[0])
        captured_commands.append(command)
        captured_envs.append(kwargs.get("env"))
        if "rev-parse" in command and "--verify" in command:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=f"{base}\n",
                stderr="",
            )
        if "rev-parse" in command and "--is-shallow-repository" in command:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="false\n",
                stderr="",
            )
        if "clone" in command:
            return subprocess.CompletedProcess(
                args=command,
                returncode=128,
                stdout="",
                stderr=(
                    "fatal: detected dubious ownership in repository at "
                    f"'{source / '.git'}'\n"
                ),
            )
        raise AssertionError(f"unexpected git command: {command!r}")

    monkeypatch.setattr(candidate_clone_module.subprocess, "run", fake_run)

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
    assert err.message == "source repository ownership is not trusted by Git"
    assert err.details is not None
    assert err.details["operation"] == "clone source repository"

    clone_command = next(command for command in captured_commands if "clone" in command)
    assert clone_command[:5] == [
        "git",
        "-c",
        f"safe.directory={source.resolve()}",
        "-c",
        f"safe.directory={(source / '.git').resolve()}",
    ]
    assert clone_command[5] == "clone"
    assert "safe.directory=*" not in " ".join(clone_command)
    assert "--global" not in clone_command
    clone_index = captured_commands.index(clone_command)
    clone_env = captured_envs[clone_index]
    assert clone_env is not None
    count = int(clone_env["GIT_CONFIG_COUNT"])
    inherited = {
        (clone_env[f"GIT_CONFIG_KEY_{index}"], clone_env[f"GIT_CONFIG_VALUE_{index}"])
        for index in range(count)
    }
    assert ("safe.directory", str(source.resolve())) in inherited
    assert ("safe.directory", str((source / ".git").resolve())) in inherited
    assert ("safe.directory", "*") not in inherited
    assert clone_env.get("GIT_CONFIG_GLOBAL") != str(Path.home() / ".gitconfig")


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
