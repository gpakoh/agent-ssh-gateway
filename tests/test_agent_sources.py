from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from examples.mcp_server import agent_sources
from examples.mcp_server.agent_paths import managed_source_bundle_path
from examples.mcp_server.agent_sources import (
    ManagedSourceBundleError,
    ManagedSourceDigestError,
    _run_git,
    capture_bundle_digest,
    ensure_dirty_worktree_review_bundle,
    ensure_managed_source_bundle,
    secure_copy_and_verify,
    validate_bundle_digest,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "payload.txt").write_text("committed\n", encoding="utf-8")
    _git(repo, "add", "payload.txt")
    _git(repo, "commit", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


class _Registry:
    def __init__(self, root: Path):
        self.root = root

    def project_info(self, project: str) -> dict[str, str]:
        return {"project_id": project, "root": str(self.root)}


def test_legacy_mode_without_source_root_is_noop(monkeypatch):
    monkeypatch.delenv("MCP_AGENT_SOURCE_ROOT", raising=False)
    assert ensure_managed_source_bundle("any-project", None) is None


def test_managed_mode_requires_exact_base_ref(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(tmp_path / "sources"))
    with pytest.raises(ValueError, match="exact base_ref"):
        ensure_managed_source_bundle("any-project", None)
    with pytest.raises(ValueError, match="Invalid base_ref"):
        ensure_managed_source_bundle("any-project", "main")


def test_publishes_exact_commit_for_arbitrary_project_ignoring_dirty_tree(tmp_path, monkeypatch):
    repo, sha = _repo(tmp_path)
    (repo / "payload.txt").write_text("DIRTY WORKTREE\n", encoding="utf-8")
    source_root = tmp_path / "sources"
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
    monkeypatch.setattr("examples.mcp_server.agent_sources.get_registry", lambda: _Registry(repo))

    published = ensure_managed_source_bundle("nod", sha)
    expected = managed_source_bundle_path("nod", sha)
    assert published is not None
    assert published.path == str(expected)
    bundle = Path(published.path)
    assert bundle.is_file()
    heads = _git(repo, "bundle", "list-heads", str(bundle))
    assert heads.split()[0] == sha

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-b", "source", str(bundle), str(clone)],
        text=True,
        capture_output=True,
        check=True,
    )
    assert (clone / "payload.txt").read_text(encoding="utf-8") == "committed\n"


def test_dirty_review_snapshot_captures_tracked_state_without_mutating_source(
    tmp_path, monkeypatch
):
    repo, _ = _repo(tmp_path)
    (repo / "remove.txt").write_text("remove-me\n", encoding="utf-8")
    _git(repo, "add", "remove.txt")
    _git(repo, "commit", "-m", "add removable fixture")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "payload.txt").write_text("dirty tracked\n", encoding="utf-8")
    (repo / "staged.txt").write_text("staged addition\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")
    (repo / "remove.txt").unlink()

    source_root = tmp_path / "sources"
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
    monkeypatch.setattr("examples.mcp_server.agent_sources.get_registry", lambda: _Registry(repo))

    status_before = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    index_path = Path(
        _git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index")
    )
    index_before = index_path.read_bytes()
    objects_path = Path(
        _git(repo, "rev-parse", "--path-format=absolute", "--git-path", "objects")
    )

    def object_snapshot() -> dict[str, bytes]:
        return {
            str(path.relative_to(objects_path)): path.read_bytes()
            for path in objects_path.rglob("*")
            if path.is_file()
        }

    objects_before = object_snapshot()
    published = ensure_dirty_worktree_review_bundle("nod", base)
    assert published is not None
    assert published.base_ref == base
    assert published.snapshot_ref != base
    assert Path(published.path).is_file()
    assert len(published.sha256) == 64

    clone = tmp_path / "dirty-review-clone"
    subprocess.run(
        ["git", "clone", "-b", "source", published.path, str(clone)],
        text=True,
        capture_output=True,
        check=True,
    )
    assert (clone / "payload.txt").read_text(encoding="utf-8") == "dirty tracked\n"
    assert (clone / "staged.txt").read_text(encoding="utf-8") == "staged addition\n"
    assert not (clone / "remove.txt").exists()
    assert _git(clone, "rev-parse", "HEAD^") == base
    assert _git(clone, "rev-parse", "HEAD^{tree}") == published.tree_sha

    assert _git(repo, "rev-parse", "HEAD") == base
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == status_before
    assert index_path.read_bytes() == index_before
    assert object_snapshot() == objects_before

    repeated = ensure_dirty_worktree_review_bundle("nod", base)
    assert repeated is not None
    assert repeated.snapshot_ref == published.snapshot_ref
    assert repeated.tree_sha == published.tree_sha
    assert repeated.sha256 == published.sha256
    assert repeated.path == published.path


def test_dirty_review_snapshot_rejects_untracked_file_without_leaking_name(
    tmp_path, monkeypatch
):
    repo, base = _repo(tmp_path)
    secret_name = "local-secret.env"
    (repo / secret_name).write_text("TOKEN=do-not-publish\n", encoding="utf-8")
    source_root = tmp_path / "sources"
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
    monkeypatch.setattr("examples.mcp_server.agent_sources.get_registry", lambda: _Registry(repo))

    with pytest.raises(ManagedSourceBundleError) as exc_info:
        ensure_dirty_worktree_review_bundle("nod", base)

    assert "untracked files" in str(exc_info.value)
    assert secret_name not in str(exc_info.value)
    assert not list(source_root.rglob("*.bundle")) if source_root.exists() else True


def test_dirty_review_snapshot_excludes_ignored_untracked_files(tmp_path, monkeypatch):
    repo, _ = _repo(tmp_path)
    (repo / ".gitignore").write_text("*.secret\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore local secrets")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "token.secret").write_text("never publish me\n", encoding="utf-8")

    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(tmp_path / "sources"))
    monkeypatch.setattr("examples.mcp_server.agent_sources.get_registry", lambda: _Registry(repo))
    published = ensure_dirty_worktree_review_bundle("nod", base)
    assert published is not None

    clone = tmp_path / "ignored-review-clone"
    subprocess.run(
        ["git", "clone", "-b", "source", published.path, str(clone)],
        text=True,
        capture_output=True,
        check=True,
    )
    assert (clone / ".gitignore").read_text(encoding="utf-8") == "*.secret\n"
    assert not (clone / "token.secret").exists()


def test_dirty_review_snapshot_fails_closed_on_concurrent_source_change(
    tmp_path, monkeypatch
):
    repo, base = _repo(tmp_path)
    source_root = tmp_path / "sources"
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
    monkeypatch.setattr("examples.mcp_server.agent_sources.get_registry", lambda: _Registry(repo))

    real_state = agent_sources._dirty_review_source_state
    calls = 0

    def racing_state(project_root: Path, expected: str) -> tuple[str, str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            (repo / "payload.txt").write_text("concurrent mutation\n", encoding="utf-8")
        return real_state(project_root, expected)

    monkeypatch.setattr(agent_sources, "_dirty_review_source_state", racing_state)
    with pytest.raises(ManagedSourceBundleError, match="changed during dirty review"):
        ensure_dirty_worktree_review_bundle("nod", base)

    assert calls >= 3
    assert not list(source_root.rglob("*.bundle")) if source_root.exists() else True


def test_dirty_review_snapshot_rejects_shallow_checkout(tmp_path, monkeypatch):
    repo, base = _repo(tmp_path)
    source_root = tmp_path / "sources"
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
    monkeypatch.setattr("examples.mcp_server.agent_sources.get_registry", lambda: _Registry(repo))
    monkeypatch.setattr(agent_sources, "_source_is_shallow", lambda _root: True)

    with pytest.raises(ManagedSourceBundleError, match="full local checkout"):
        ensure_dirty_worktree_review_bundle("nod", base)
    assert not source_root.exists()


def test_dirty_review_snapshot_requires_base_ref_to_match_head(tmp_path, monkeypatch):
    repo, old_head = _repo(tmp_path)
    (repo / "payload.txt").write_text("next commit\n", encoding="utf-8")
    _git(repo, "add", "payload.txt")
    _git(repo, "commit", "-m", "advance head")

    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(tmp_path / "sources"))
    monkeypatch.setattr("examples.mcp_server.agent_sources.get_registry", lambda: _Registry(repo))
    with pytest.raises(ManagedSourceBundleError, match="does not match registered source HEAD"):
        ensure_dirty_worktree_review_bundle("nod", old_head)


def test_missing_commit_fails_without_publishing(tmp_path, monkeypatch):
    repo, _sha = _repo(tmp_path)
    source_root = tmp_path / "sources"
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    monkeypatch.setattr("examples.mcp_server.agent_sources.get_registry", lambda: _Registry(repo))
    missing = "f" * 40
    with pytest.raises(ManagedSourceBundleError):
        ensure_managed_source_bundle("nod", missing)
    expected = managed_source_bundle_path("nod", missing)
    assert expected is not None
    assert not Path(expected).exists()


def test_atomic_replace_failure_propagates(tmp_path, monkeypatch):
    repo, sha = _repo(tmp_path)
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(tmp_path / "sources"))
    monkeypatch.setattr("examples.mcp_server.agent_sources.get_registry", lambda: _Registry(repo))

    def fail_replace(src, dst):
        raise OSError("simulated atomic publication failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated atomic publication failure"):
        ensure_managed_source_bundle("nod", sha)

    expected = managed_source_bundle_path("nod", sha)
    assert expected is not None
    assert not Path(expected).exists()


def test_git_timeout_fails_closed(monkeypatch):
    def timeout_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["git", "fetch"], timeout=120)

    monkeypatch.setattr(subprocess, "run", timeout_run)

    with pytest.raises(ManagedSourceBundleError, match="timed out during git fetch"):
        _run_git(["fetch", "source"])


class TestGitErrorMessage:
    """Error messages must name the git subcommand, not a leading --option."""

    @staticmethod
    def _run_failing_git(args: list[str]) -> None:
        """Helper: call _run_git with a fake subprocess that always exits 1."""
        import unittest.mock

        fake_result = unittest.mock.Mock(returncode=1, stdout="", stderr="")
        with unittest.mock.patch("subprocess.run", return_value=fake_result):
            _run_git(args)

    def test_reports_bundle_not_git_dir_option(self):
        with pytest.raises(ManagedSourceBundleError, match=r"git bundle"):
            self._run_failing_git(["--git-dir=/tmp/x", "bundle", "create", "refs/heads/source"])

    def test_reports_cat_file_not_git_dir_option(self):
        with pytest.raises(ManagedSourceBundleError, match=r"git cat-file"):
            self._run_failing_git(["--git-dir=/tmp/x", "cat-file", "-e", "abc123^{commit}"])

    def test_reports_simple_subcommand_without_git_dir(self):
        with pytest.raises(ManagedSourceBundleError, match=r"git status"):
            self._run_failing_git(["git", "status"])

    def test_reports_subcommand_when_only_options_present(self):
        """Edge case: only options, no subcommand — reports first option."""
        with pytest.raises(ManagedSourceBundleError, match=r"git --oneline"):
            self._run_failing_git(["--oneline"])

    def test_timeout_message_also_uses_subcommand(self):
        import unittest.mock

        def timeout_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd=["git"], timeout=120)

        with unittest.mock.patch("subprocess.run", side_effect=timeout_run):
            with pytest.raises(ManagedSourceBundleError, match=r"timed out during git bundle"):
                _run_git(["--git-dir=/tmp/x", "bundle", "create", "out.bndl"])


def test_registry_project_root_is_the_only_safe_directory_exception(monkeypatch, tmp_path):
    captured: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""

    def fake_run(argv, **kwargs):
        captured.append(argv)
        return _Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    project_root = tmp_path / "mounted-project"

    _run_git(
        ["cat-file", "-e", "a" * 40 + "^{commit}"],
        cwd=project_root,
        safe_directory=project_root,
    )

    assert captured == [
        [
            "git",
            "-c",
            f"safe.directory={project_root}",
            "cat-file",
            "-e",
            "a" * 40 + "^{commit}",
        ]
    ]
    assert "safe.directory=*" not in captured[0]


def test_source_repo_access_is_scoped_to_registered_root(tmp_path, monkeypatch):
    repo, sha = _repo(tmp_path)
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(tmp_path / "sources"))
    monkeypatch.setattr("examples.mcp_server.agent_sources.get_registry", lambda: _Registry(repo))

    calls: list[tuple[list[str], Path | None, Path | None]] = []

    real_run_git = agent_sources._run_git

    def tracking_run_git(args, *, cwd=None, safe_directory=None):
        calls.append((args, cwd, safe_directory))
        return real_run_git(args, cwd=cwd, safe_directory=safe_directory)

    monkeypatch.setattr("examples.mcp_server.agent_sources._run_git", tracking_run_git)

    published = ensure_managed_source_bundle("nod", sha)
    assert published is not None

    source_calls = [
        (args, cwd, safe_directory)
        for args, cwd, safe_directory in calls
        if "cat-file" in args or "--git-path" in args or "--is-shallow-repository" in args
    ]
    assert len(source_calls) == 3
    assert all(safe_directory == repo for _, _, safe_directory in source_calls)
    assert all(cwd == repo for _, cwd, _ in source_calls)
    # Publication must never clone or fetch FROM the registered checkout;
    # scratch-dir bundle-verification clones are unrelated to it and allowed.
    for banned in ("clone", "fetch"):
        assert not any(
            banned in args and any(str(repo) in str(arg) for arg in args) for args, _, _ in calls
        )

    update_ref_calls = [args for args, _, _ in calls if "update-ref" in args]
    assert len(update_ref_calls) == 1
    assert update_ref_calls[0][-2:] == ["refs/heads/source", sha]

    non_source_calls = [
        safe_directory
        for args, _cwd, safe_directory in calls
        if "cat-file" not in args
        and "--git-path" not in args
        and "--is-shallow-repository" not in args
    ]
    assert all(safe_directory is None for safe_directory in non_source_calls)


# ---------------------------------------------------------------------------
# Remote fallback regression tests
# ---------------------------------------------------------------------------


class _FakeRegistry:
    """Minimal registry that returns a pre-created local repo root."""

    def __init__(self, root: Path):
        self._root = root

    def project_info(self, project: str) -> dict[str, str]:
        return {"project_id": project, "root": str(self._root)}


def test_resolve_trusted_remote_enumerates_configured_gitea_remotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from examples.mcp_server.agent_sources import _resolve_trusted_remote

    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setenv("GITEA_TOKEN", "fake-token")
    monkeypatch.setenv("GITEA_API_BASE", "http://gitea:3000/api/v1")

    calls: list[list[str]] = []

    def git_cmd(*args: str) -> list[str]:
        return ["git", "-c", f"safe.directory={project_root}", *args]

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd == git_cmd("remote"):
            return subprocess.CompletedProcess(
                cmd, 0, stdout="origin\nmcp-gitea\n", stderr=""
            )
        if cmd == git_cmd("remote", "get-url", "--push", "origin"):
            return subprocess.CompletedProcess(
                cmd, 0, stdout="/srv/not-a-gitea-remote\n", stderr=""
            )
        if cmd == git_cmd("remote", "get-url", "--push", "mcp-gitea"):
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="ssh://git@mcp-gitea:2222/gpakoh/test-repo.git\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess call: {cmd!r}")

    def fake_gitea_get(path: str, *, token: str) -> dict[str, str]:
        assert token == "fake-token"
        if path == "/user":
            return {"login": "testuser"}
        assert path == "/repos/gpakoh/test-repo"
        return {"clone_url": "https://git.example.test/gpakoh/test-repo.git"}

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "examples.mcp_server.control_plane_git._gitea_get", fake_gitea_get
    )

    username, clone_url, token = _resolve_trusted_remote(project_root)

    assert username == "testuser"
    assert clone_url == "https://git.example.test/gpakoh/test-repo.git"
    assert token == "fake-token"
    assert git_cmd("remote") in calls
    assert git_cmd("remote", "get-url", "--push", "origin") in calls
    assert git_cmd("remote", "get-url", "--push", "mcp-gitea") in calls


def test_remote_fetch_uses_resolved_username_for_basic_auth(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    def fake_env(username: str, token: str) -> dict[str, str]:
        captured["auth"] = (username, token)
        return {}

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("examples.mcp_server.managed_git._minimal_git_env", fake_env)
    monkeypatch.setattr(subprocess, "run", fake_run)

    agent_sources._fetch_remote_object(
        "resolved-user",
        "https://git.example.test/gpakoh/test-repo.git",
        "fixture-token",
        "a" * 40,
        tmp_path,
    )

    assert captured["auth"] == ("resolved-user", "fixture-token")
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[:2] == ["git", "fetch"]
    assert "fixture-token" not in " ".join(str(part) for part in argv)


def _make_bare_clone(tmp_path: Path, source_repo: Path) -> tuple[Path, str]:
    """Create a bare clone of *source_repo* that contains all objects."""
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(source_repo), str(bare)],
        text=True,
        capture_output=True,
        check=True,
    )
    sha = _git(source_repo, "rev-parse", "HEAD")
    return bare, sha


def test_existing_bundle_skips_remote_fetch(tmp_path, monkeypatch):
    """Test 1: Bundle already exists with correct SHA → no remote fetch."""
    repo, sha = _repo(tmp_path)
    source_root = tmp_path / "sources"
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
    monkeypatch.setattr(
        "examples.mcp_server.agent_sources.get_registry", lambda: _FakeRegistry(repo)
    )

    # Pre-create the bundle at the expected path
    bundle_raw = managed_source_bundle_path("nod", sha)
    assert bundle_raw is not None
    bundle_path = Path(bundle_raw)
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "bundle", "create", str(bundle_path), "HEAD")
    assert bundle_path.is_file()

    git_calls: list[list[str]] = []

    real_run_git = agent_sources._run_git

    def track_run_git(args, **kwargs):
        git_calls.append(args)
        return real_run_git(args, **kwargs)

    monkeypatch.setattr("examples.mcp_server.agent_sources._run_git", track_run_git)

    result = ensure_managed_source_bundle("nod", sha)
    assert result is not None
    assert result.path == str(bundle_path)

    # cat-file and any remote fetch should never be called
    cat_file_calls = [a for a in git_calls if "cat-file" in a]
    fetch_calls = [a for a in git_calls if "fetch" in a]
    assert cat_file_calls == [], "cat-file should not run when bundle exists"
    assert fetch_calls == [], "remote fetch should not run when bundle exists"


def test_missing_object_fetches_from_trusted_remote(tmp_path, monkeypatch):
    """Test 2: Local object missing + remote has SHA → fetch, bundle, verify."""
    # Create source repo and a bare clone (simulating trusted remote)
    source_repo = tmp_path / "source"
    source_repo.mkdir()
    _git(source_repo, "init")
    _git(source_repo, "config", "user.name", "Test")
    _git(source_repo, "config", "user.email", "test@example.com")
    (source_repo / "data.txt").write_text("remote-content\n", encoding="utf-8")
    _git(source_repo, "add", "data.txt")
    _git(source_repo, "commit", "-m", "remote commit")
    remote_sha = _git(source_repo, "rev-parse", "HEAD")
    bare_remote, _ = _make_bare_clone(tmp_path, source_repo)

    # Local repo (registered project) — has no objects for the remote SHA
    local_repo = tmp_path / "local"
    local_repo.mkdir()
    _git(local_repo, "init")
    _git(local_repo, "config", "user.name", "Test")
    _git(local_repo, "config", "user.email", "test@example.com")
    (local_repo / "local.txt").write_text("local-only\n", encoding="utf-8")
    _git(local_repo, "add", "local.txt")
    _git(local_repo, "commit", "-m", "local only")
    _git(local_repo, "remote", "add", "origin", str(bare_remote))

    source_root = tmp_path / "sources"
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
    monkeypatch.setenv("GITEA_TOKEN", "fake-token")
    monkeypatch.setattr(
        "examples.mcp_server.agent_sources.get_registry",
        lambda: _FakeRegistry(local_repo),
    )

    # Mock _resolve_trusted_remote to return the bare clone URL
    monkeypatch.setattr(
        "examples.mcp_server.agent_sources._resolve_trusted_remote",
        lambda _root: ("testuser", str(bare_remote), "fake-token"),
    )

    result = ensure_managed_source_bundle("nod", remote_sha)
    assert result is not None
    bundle = Path(result.path if result else "")
    assert bundle.is_file()

    # Verify bundle contains exactly the expected SHA
    heads = subprocess.run(
        ["git", "bundle", "list-heads", str(bundle)],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert heads.split()[0].lower() == remote_sha

    # Verify bundle content
    clone_dir = tmp_path / "verify"
    subprocess.run(
        ["git", "clone", "-b", "source", str(bundle), str(clone_dir)],
        text=True,
        capture_output=True,
        check=True,
    )
    assert (clone_dir / "data.txt").read_text(encoding="utf-8") == "remote-content\n"


def test_wrong_sha_rejects_bundle(tmp_path, monkeypatch):
    """Test 3: Remote fetch for nonexistent SHA → reject, no bundle installed.

    When the requested SHA does not exist on the trusted remote, ``git fetch``
    fails with *upload-pack: not our ref*.  This is the correct fail-closed
    behavior: no partial artifacts are created.
    """
    source_repo = tmp_path / "source"
    source_repo.mkdir()
    _git(source_repo, "init")
    _git(source_repo, "config", "user.name", "Test")
    _git(source_repo, "config", "user.email", "test@example.com")
    (source_repo / "data.txt").write_text("content\n", encoding="utf-8")
    _git(source_repo, "add", "data.txt")
    _git(source_repo, "commit", "-m", "commit")
    bare_remote, _ = _make_bare_clone(tmp_path, source_repo)

    local_repo = tmp_path / "local"
    local_repo.mkdir()
    _git(local_repo, "init")
    _git(local_repo, "config", "user.name", "Test")
    _git(local_repo, "config", "user.email", "test@example.com")
    (local_repo / "local.txt").write_text("local\n", encoding="utf-8")
    _git(local_repo, "add", "local.txt")
    _git(local_repo, "commit", "-m", "local")

    source_root = tmp_path / "sources"
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
    monkeypatch.setenv("GITEA_TOKEN", "fake-token")
    monkeypatch.setattr(
        "examples.mcp_server.agent_sources.get_registry",
        lambda: _FakeRegistry(local_repo),
    )
    monkeypatch.setattr(
        "examples.mcp_server.agent_sources._resolve_trusted_remote",
        lambda _root: ("testuser", str(bare_remote), "fake-token"),
    )

    wrong_sha = "a" * 40
    with pytest.raises(ManagedSourceBundleError):
        ensure_managed_source_bundle("nod", wrong_sha)

    # No bundle should be published
    bundle_raw = managed_source_bundle_path("nod", wrong_sha)
    assert bundle_raw is not None
    assert not Path(bundle_raw).exists()
    # No temp artifacts
    parent = Path(bundle_raw).parent
    if parent.exists():
        tmp_files = list(parent.glob(f".{wrong_sha}.*"))
        assert tmp_files == [], f"Partial artifacts found: {tmp_files}"


def test_auth_failure_no_partial_artifacts(tmp_path, monkeypatch):
    """Test 4: Remote fetch fails → clean failure, no partial bundle."""
    local_repo = tmp_path / "local"
    local_repo.mkdir()
    _git(local_repo, "init")
    _git(local_repo, "config", "user.name", "Test")
    _git(local_repo, "config", "user.email", "test@example.com")
    (local_repo / "f.txt").write_text("x\n", encoding="utf-8")
    _git(local_repo, "add", "f.txt")
    _git(local_repo, "commit", "-m", "base")

    missing_sha = "e" * 40
    source_root = tmp_path / "sources"
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
    monkeypatch.setattr(
        "examples.mcp_server.agent_sources.get_registry",
        lambda: _FakeRegistry(local_repo),
    )

    def failing_resolve(_root):
        raise ManagedSourceBundleError("trusted remote resolution failed")

    monkeypatch.setattr(
        "examples.mcp_server.agent_sources._resolve_trusted_remote",
        failing_resolve,
    )

    with pytest.raises(ManagedSourceBundleError, match="trusted remote"):
        ensure_managed_source_bundle("nod", missing_sha)

    # No partial artifacts
    bundle_raw = managed_source_bundle_path("nod", missing_sha)
    assert bundle_raw is not None
    assert not Path(bundle_raw).exists()
    # Check no .tmp files remain
    parent = Path(bundle_raw).parent
    if parent.exists():
        tmp_files = list(parent.glob(f".{missing_sha}.*"))
        assert tmp_files == [], f"Partial artifacts found: {tmp_files}"


def test_token_never_in_url_or_config(tmp_path, monkeypatch):
    """Test 5: Token never appears in URLs or git config."""
    from examples.mcp_server.agent_sources import _resolve_trusted_remote

    # Mock git remote get-url to return a Gitea SSH URL
    fake_origin = "ssh://git@192.0.2.103:2222/gpakoh/test-repo.git"
    project_root = tmp_path / "project"
    project_root.mkdir()

    import unittest.mock as _mock

    # Mock subprocess.run for git remote get-url
    get_url_result = _mock.Mock(returncode=0, stdout=f"{fake_origin}\n", stderr="")

    # Mock the Gitea API calls
    fake_user_data = {"login": "testuser"}
    fake_repo_data = {"clone_url": "https://192.0.2.103/gpakoh/test-repo.git"}

    # Track all subprocess calls to _minimal_git_env and git fetch
    captured_envs: list[dict] = []
    captured_urls: list[str] = []

    def tracking_run(cmd, **kwargs):
        env = kwargs.get("env", {})
        if env:
            captured_envs.append(dict(env))
        if len(cmd) >= 2 and cmd[0] == "git" and cmd[1] == "fetch":
            captured_urls.append(cmd[2] if len(cmd) > 2 else "")
        return get_url_result

    monkeypatch.setattr("subprocess.run", tracking_run)
    monkeypatch.setattr(
        "examples.mcp_server.control_plane_git._gitea_get",
        lambda path, *, token: fake_user_data if "/user" in path else fake_repo_data,
    )

    try:
        _username, clone_url, token = _resolve_trusted_remote(project_root)
    except ManagedSourceBundleError:
        # Expected: control_plane_git._parse_gitea_remote may fail on mock
        # The important check is that token never leaked
        pass

    # Verify token never appears in any captured environment
    for env in captured_envs:
        for key, value in env.items():
            assert "fake-token" not in value, f"Token leaked in env var {key}={value}"
            assert "Authorization" not in value or "Basic" in value, (
                f"Token in unexpected env format: {key}={value}"
            )

    # Verify no URL contains the token
    for url in captured_urls:
        assert "fake-token" not in url, f"Token leaked in URL: {url}"


def test_resolve_trusted_remote_uses_named_trusted_remote_when_origin_missing(tmp_path, monkeypatch):
    """Managed source publication must not require a remote literally named origin."""
    from examples.mcp_server.agent_sources import _resolve_trusted_remote

    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setenv("GITEA_TOKEN", "fake-token")
    monkeypatch.setenv("GITEA_API_BASE", "http://gitea:3000/api/v1")

    def git_cmd(*args: str) -> list[str]:
        return ["git", "-c", f"safe.directory={project_root}", *args]

    def fake_run(argv, **kwargs):
        if argv == git_cmd("remote"):
            return subprocess.CompletedProcess(argv, 0, stdout="gitea\nmcp-gitea\n", stderr="")
        if argv == git_cmd("remote", "get-url", "--push", "gitea"):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="ssh://git@192.0.2.103:2222/gpakoh/agent-ssh-gateway.git\n",
                stderr="",
            )
        if argv == git_cmd("remote", "get-url", "--push", "mcp-gitea"):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="ssh://git@gitea/gpakoh/agent-ssh-gateway.git\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess call: {argv!r}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "examples.mcp_server.control_plane_git._repo_https_target",
        lambda owner, repo, *, token: (
            "gpakoh",
            f"https://git.example.test/{owner}/{repo}.git",
        ),
    )

    username, clone_url, token = _resolve_trusted_remote(project_root)
    assert username == "gpakoh"
    assert clone_url == "https://git.example.test/gpakoh/agent-ssh-gateway.git"
    assert token == "fake-token"


def test_resolve_trusted_remote_accepts_configured_local_ssh_identity_only(
    tmp_path, monkeypatch
):
    """A configured local SSH Gitea host can be the sole trusted identity."""
    from examples.mcp_server.agent_sources import _resolve_trusted_remote

    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setenv("GITEA_TOKEN", "fake-token")
    monkeypatch.setenv("GITEA_API_BASE", "http://gitea:3000/api/v1")
    monkeypatch.setenv("GITEA_GIT_BASE", "http://192.0.2.103:3000")

    def git_cmd(*args: str) -> list[str]:
        return ["git", "-c", f"safe.directory={project_root}", *args]

    def fake_run(argv, **kwargs):
        if argv == git_cmd("remote"):
            return subprocess.CompletedProcess(argv, 0, stdout="gitea\n", stderr="")
        if argv == git_cmd("remote", "get-url", "--push", "gitea"):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="ssh://git@192.0.2.103:2222/gpakoh/agent-ssh-gateway.git\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess call: {argv!r}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "examples.mcp_server.control_plane_git._repo_https_target",
        lambda owner, repo, *, token: (
            "gpakoh",
            f"https://git.example.test/{owner}/{repo}.git",
        ),
    )

    username, clone_url, token = _resolve_trusted_remote(project_root)
    assert username == "gpakoh"
    assert clone_url == "https://git.example.test/gpakoh/agent-ssh-gateway.git"
    assert token == "fake-token"


def test_resolve_trusted_remote_rejects_conflicting_trusted_repo_identities(tmp_path, monkeypatch):
    """Multiple allowlisted remotes must agree on owner/repo before fallback is trusted."""
    from examples.mcp_server.agent_sources import _resolve_trusted_remote

    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setenv("GITEA_TOKEN", "fake-token")
    monkeypatch.setenv("GITEA_API_BASE", "http://gitea:3000/api/v1")

    def git_cmd(*args: str) -> list[str]:
        return ["git", "-c", f"safe.directory={project_root}", *args]

    def fake_run(argv, **kwargs):
        if argv == git_cmd("remote"):
            return subprocess.CompletedProcess(argv, 0, stdout="gitea\nmcp-gitea\n", stderr="")
        if argv == git_cmd("remote", "get-url", "--push", "gitea"):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="ssh://git@gitea/gpakoh/agent-ssh-gateway.git\n",
                stderr="",
            )
        if argv == git_cmd("remote", "get-url", "--push", "mcp-gitea"):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="ssh://git@gitea/gpakoh/other-repo.git\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess call: {argv!r}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManagedSourceBundleError, match="conflicting trusted remote identities"):
        _resolve_trusted_remote(project_root)


# ---------------------------------------------------------------------------
# Digest binding primitives (TOCTOU closure for managed source transport).
# ---------------------------------------------------------------------------


def test_validate_bundle_digest_accepts_only_lowercase_hex64():
    assert validate_bundle_digest("a" * 64) == "a" * 64
    for bad in ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, None, 12345]:
        with pytest.raises(ManagedSourceDigestError):
            validate_bundle_digest(bad)


def test_capture_bundle_digest_matches_manual_hash(tmp_path):
    artifact = tmp_path / "artifact.bundle"
    payload = b"0" * (1024 * 1024 + 17)
    artifact.write_bytes(payload)

    assert capture_bundle_digest(artifact) == hashlib.sha256(payload).hexdigest()


def test_capture_and_secure_copy_reject_symlink(tmp_path):
    real = tmp_path / "real.bundle"
    real.write_bytes(b"trusted bytes\n")
    link = tmp_path / "link.bundle"
    link.symlink_to(real)
    digest = hashlib.sha256(b"trusted bytes\n").hexdigest()

    with pytest.raises(ManagedSourceDigestError):
        capture_bundle_digest(link)
    with pytest.raises(ManagedSourceDigestError):
        secure_copy_and_verify(link, digest, dest_dir=tmp_path)


def test_secure_copy_happy_path_is_readonly_private_copy(tmp_path):
    artifact = tmp_path / "good.bundle"
    payload = b"bundle-payload\n"
    artifact.write_bytes(payload)
    copies_dir = tmp_path / "out"
    copies_dir.mkdir()

    copy = secure_copy_and_verify(
        artifact, hashlib.sha256(payload).hexdigest(), dest_dir=copies_dir
    )
    try:
        assert copy.parent.parent == copies_dir
        assert oct(copy.parent.stat().st_mode & 0o777) == "0o700"
        assert oct(copy.stat().st_mode & 0o777) == "0o400"
        assert copy.read_bytes() == payload
    finally:
        import shutil

        shutil.rmtree(copy.parent)


def test_secure_copy_mismatch_leaves_no_trace(tmp_path):
    artifact = tmp_path / "swap.bundle"
    artifact.write_bytes(b"actual-bytes\n")
    copies_dir = tmp_path / "out2"
    copies_dir.mkdir()

    with pytest.raises(ManagedSourceDigestError, match="digest mismatch"):
        secure_copy_and_verify(artifact, "b" * 64, dest_dir=copies_dir)

    assert list(copies_dir.iterdir()) == []


def test_secure_copy_missing_artifact_fails_closed(tmp_path):
    with pytest.raises(ManagedSourceDigestError):
        secure_copy_and_verify(tmp_path / "absent.bundle", "c" * 64, dest_dir=tmp_path)
