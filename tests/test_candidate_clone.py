from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.workspace.registry import reset_registry
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


def test_prepare_candidate_clone_git_failure_returns_redacted_diagnostics(
    registry_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from examples.mcp_server import candidate_clone as candidate_clone_module

    _workspace, source, config_dir, journal_root, base = registry_fixture
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
    assert err.code == "TOOL_EXECUTION_FAILED"
    assert err.message == "git resolve base ref failed"
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
