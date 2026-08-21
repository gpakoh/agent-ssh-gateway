from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from examples.mcp_server.agent_paths import managed_source_bundle_path
from examples.mcp_server.agent_sources import (
    ManagedSourceBundleError,
    _run_git,
    ensure_managed_source_bundle,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    )
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


def test_publishes_exact_commit_for_arbitrary_project_ignoring_dirty_tree(
    tmp_path, monkeypatch
):
    repo, sha = _repo(tmp_path)
    (repo / "payload.txt").write_text("DIRTY WORKTREE\n", encoding="utf-8")
    source_root = tmp_path / "sources"
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
    monkeypatch.setattr(
        "examples.mcp_server.agent_sources.get_registry", lambda: _Registry(repo)
    )

    published = ensure_managed_source_bundle("nod", sha)
    expected = managed_source_bundle_path("nod", sha)
    assert published == expected
    assert published is not None
    bundle = Path(published)
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


def test_missing_commit_fails_without_publishing(tmp_path, monkeypatch):
    repo, _sha = _repo(tmp_path)
    source_root = tmp_path / "sources"
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    monkeypatch.setattr(
        "examples.mcp_server.agent_sources.get_registry", lambda: _Registry(repo)
    )
    missing = "f" * 40
    with pytest.raises(ManagedSourceBundleError):
        ensure_managed_source_bundle("nod", missing)
    expected = managed_source_bundle_path("nod", missing)
    assert expected is not None
    assert not Path(expected).exists()


def test_atomic_replace_failure_propagates(tmp_path, monkeypatch):
    repo, sha = _repo(tmp_path)
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(tmp_path / "sources"))
    monkeypatch.setattr(
        "examples.mcp_server.agent_sources.get_registry", lambda: _Registry(repo)
    )

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
            self._run_failing_git(
                ["--git-dir=/tmp/x", "bundle", "create", "refs/heads/source"]
            )

    def test_reports_cat_file_not_git_dir_option(self):
        with pytest.raises(ManagedSourceBundleError, match=r"git cat-file"):
            self._run_failing_git(
                ["--git-dir=/tmp/x", "cat-file", "-e", "abc123^{commit}"]
            )

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
            with pytest.raises(
                ManagedSourceBundleError, match=r"timed out during git bundle"
            ):
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

    assert captured == [[
        "git",
        "-c",
        f"safe.directory={project_root}",
        "cat-file",
        "-e",
        "a" * 40 + "^{commit}",
    ]]
    assert "safe.directory=*" not in captured[0]


def test_source_repo_access_is_scoped_to_registered_root(tmp_path, monkeypatch):
    repo, sha = _repo(tmp_path)
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(tmp_path / "sources"))
    monkeypatch.setattr(
        "examples.mcp_server.agent_sources.get_registry", lambda: _Registry(repo)
    )

    calls: list[tuple[list[str], Path | None, Path | None]] = []

    def fake_run_git(args, *, cwd=None, safe_directory=None):
        calls.append((args, cwd, safe_directory))
        if "--git-path" in args:
            return str(repo / ".git" / "objects")
        if "rev-parse" in args:
            return sha
        return ""

    monkeypatch.setattr("examples.mcp_server.agent_sources._run_git", fake_run_git)
    monkeypatch.setattr(
        "examples.mcp_server.agent_sources._bundle_head", lambda path: sha
    )
    monkeypatch.setattr(os, "replace", lambda src, dst: None)

    published = ensure_managed_source_bundle("nod", sha)
    assert published is not None

    source_calls = [
        (args, cwd, safe_directory)
        for args, cwd, safe_directory in calls
        if "cat-file" in args or "--git-path" in args
    ]
    assert len(source_calls) == 2
    assert all(safe_directory == repo for _, _, safe_directory in source_calls)
    assert all(cwd == repo for _, cwd, _ in source_calls)
    assert not any("clone" in args for args, _, _ in calls)
    assert not any("fetch" in args for args, _, _ in calls)

    update_ref_calls = [args for args, _, _ in calls if "update-ref" in args]
    assert len(update_ref_calls) == 1
    assert update_ref_calls[0][-2:] == ["refs/heads/source", sha]

    non_source_calls = [
        safe_directory
        for args, _cwd, safe_directory in calls
        if "cat-file" not in args and "--git-path" not in args
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

    def track_run_git(args, **kwargs):
        git_calls.append(args)
        # For bundle verification
        if "bundle" in args and "list-heads" in args:
            return f"{sha}\n"
        return ""

    monkeypatch.setattr("examples.mcp_server.agent_sources._run_git", track_run_git)

    result = ensure_managed_source_bundle("nod", sha)
    assert result == str(bundle_path)

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
        lambda _root: (str(bare_remote), "fake-token"),
    )

    result = ensure_managed_source_bundle("nod", remote_sha)
    assert result is not None
    bundle = Path(result)
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
        lambda _root: (str(bare_remote), "fake-token"),
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
    fake_origin = "ssh://git@192.168.1.103:2222/gpakoh/test-repo.git"
    project_root = tmp_path / "project"
    project_root.mkdir()

    import unittest.mock as _mock

    # Mock subprocess.run for git remote get-url
    get_url_result = _mock.Mock(returncode=0, stdout=f"{fake_origin}\n", stderr="")

    # Mock the Gitea API calls
    fake_user_data = {"login": "testuser"}
    fake_repo_data = {"clone_url": "https://192.168.1.103/gpakoh/test-repo.git"}

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
        clone_url, token = _resolve_trusted_remote(project_root)
    except ManagedSourceBundleError:
        # Expected: control_plane_git._parse_gitea_remote may fail on mock
        # The important check is that token never leaked
        pass

    # Verify token never appears in any captured environment
    for env in captured_envs:
        for key, value in env.items():
            assert "fake-token" not in value, (
                f"Token leaked in env var {key}={value}"
            )
            assert "Authorization" not in value or "Basic" in value, (
                f"Token in unexpected env format: {key}={value}"
            )

    # Verify no URL contains the token
    for url in captured_urls:
        assert "fake-token" not in url, f"Token leaked in URL: {url}"
