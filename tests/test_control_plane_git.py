"""Tests for MCP control-plane git push boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from examples.mcp_server import control_plane_git as cpg


def test_validate_name_rejects_option_and_refspec() -> None:
    with pytest.raises(ValueError):
        cpg._validate_name("--mirror", "remote")
    with pytest.raises(ValueError):
        cpg._validate_name("origin:main", "remote")


def test_redact_text_hides_token_url_and_project_root(tmp_path: Path) -> None:
    token = "tok-secret"
    project_root = tmp_path / "repo"
    project_root.mkdir()
    raw = f"fatal: could not read {project_root}/a\nhttps://user:{token}@git.example.com/x/y.git"
    out = cpg._redact_text(raw, token=token, project_root=project_root)
    assert token not in out
    assert str(project_root) not in out
    assert "***@" in out


def test_parse_gitea_remote_accepts_git_and_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITEA_FORWARDED_HOST", "git.example.com")
    monkeypatch.setenv("GITEA_API_BASE", "http://gitea:3000/api/v1")
    assert cpg._parse_gitea_remote("git@git.example.com:gpakoh/repo.git") == ("git.example.com", "gpakoh", "repo")
    assert cpg._parse_gitea_remote("https://git.example.com/gpakoh/repo.git") == ("git.example.com", "gpakoh", "repo")


def test_parse_gitea_remote_accepts_configured_local_ssh_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITEA_FORWARDED_HOST", raising=False)
    monkeypatch.setenv("GITEA_API_BASE", "http://gitea:3000/api/v1")
    monkeypatch.setenv("GITEA_GIT_BASE", "http://192.0.2.103:3000")

    assert cpg._parse_gitea_remote(
        "ssh://git@192.0.2.103:2222/gpakoh/repo.git"
    ) == ("192.0.2.103", "gpakoh", "repo")


def test_parse_gitea_remote_accepts_mcp_gitea_service_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITEA_FORWARDED_HOST", raising=False)
    monkeypatch.setenv("GITEA_API_BASE", "http://gitea:3000/api/v1")

    assert cpg._parse_gitea_remote(
        "ssh://git@mcp-gitea:2222/gpakoh/repo.git"
    ) == ("mcp-gitea", "gpakoh", "repo")


def test_parse_gitea_remote_rejects_unknown_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITEA_FORWARDED_HOST", "git.example.com")
    monkeypatch.setenv("GITEA_API_BASE", "http://gitea:3000/api/v1")
    with pytest.raises(RuntimeError, match="GIT_REMOTE_NOT_ALLOWED"):
        cpg._parse_gitea_remote("git@evil.example:owner/repo.git")


def test_classify_push_failure_codes() -> None:
    assert cpg._classify_push_failure("non-fast-forward", "") == "GIT_NON_FAST_FORWARD"
    assert cpg._classify_push_failure("protected branch hook declined", "") == "GIT_PROTECTED_BRANCH"
    assert cpg._classify_push_failure("Authentication failed", "") == "GIT_AUTH_FAILED"
    assert cpg._classify_push_failure("misc failure", "") == "GIT_PUSH_FAILED"


def test_repo_https_target_uses_user_and_clone_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_get(path: str, *, token: str):
        calls.append(path)
        if path == "/user":
            return {"login": "gpakoh"}
        return {"clone_url": "https://git.example.com/gpakoh/repo.git"}

    monkeypatch.setattr(cpg, "_gitea_get", fake_get)
    username, clone_url = cpg._repo_https_target("gpakoh", "repo", token="tok")
    assert username == "gpakoh"
    assert clone_url == "https://git.example.com/gpakoh/repo.git"
    assert calls == ["/user", "/repos/gpakoh/repo"]


def test_staging_git_dir_is_stable(tmp_path: Path) -> None:
    path = cpg._staging_git_dir("proj", tmp_path)
    assert path.name.endswith(".git")
    assert path.parent == Path("/app/data/control-plane-git")


def test_stage_branch_from_checkout_fetches_local_ref(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def fake_run(argv, *, cwd, env=None, timeout=60):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(cpg, "_run_git", fake_run)
    cpg._stage_branch_from_checkout(tmp_path, tmp_path / "stage.git", "feature/x")
    assert len(seen) == 2
    bundle_path = Path(seen[0][9])
    assert seen[0] == [
        "git",
        "-c",
        f"safe.directory={tmp_path}",
        "-c",
        f"safe.directory={tmp_path / '.git'}",
        "-C",
        str(tmp_path),
        "bundle",
        "create",
        str(bundle_path),
        "refs/heads/feature/x",
    ]
    assert seen[1] == [
        "git",
        "--git-dir",
        str(tmp_path / "stage.git"),
        "fetch",
        "--no-tags",
        str(bundle_path),
        "refs/heads/feature/x:refs/heads/feature/x",
    ]


def test_verify_local_branch_trusts_gitdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def fake_run(argv, *, cwd, env=None, timeout=60):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="abc123\n", stderr="")

    monkeypatch.setattr(cpg, "_run_git", fake_run)
    cpg._verify_local_branch(tmp_path, "feature/x")
    assert seen == [[
        "git",
        "-c",
        f"safe.directory={tmp_path}",
        "-c",
        f"safe.directory={tmp_path / '.git'}",
        "rev-parse",
        "--verify",
        "refs/heads/feature/x",
    ]]


def test_push_staged_ref_uses_bare_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def fake_run(argv, *, cwd, env=None, timeout=60):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(cpg, "_run_git", fake_run)
    cpg._push_staged_ref(tmp_path / "stage.git", "https://git.example.com/gpakoh/repo.git", "feature/x", {"GIT_PASSWORD": "tok"})
    assert seen == [[
        "git",
        "--git-dir",
        str(tmp_path / "stage.git"),
        "push",
        "https://git.example.com/gpakoh/repo.git",
        "refs/heads/feature/x:refs/heads/feature/x",
    ]]




def test_git_fetch_ref_control_plane_fetches_exact_remote_head(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    remote_head = "a" * 40
    monkeypatch.setenv("GITEA_TOKEN", "tok")
    monkeypatch.setattr(cpg, "_resolve_project_root", lambda project: tmp_path)
    monkeypatch.setattr(cpg, "_remote_url", lambda cwd, remote: "git@git.example.com:gpakoh/repo.git")
    monkeypatch.setattr(cpg, "_parse_gitea_remote", lambda url: ("git.example.com", "gpakoh", "repo"))
    monkeypatch.setattr(cpg, "_repo_https_target", lambda owner, repo, token: ("gpakoh", "https://git.example.com/gpakoh/repo.git"))
    monkeypatch.setattr(cpg, "_gitea_get", lambda path, token: {"commit": {"id": remote_head}})
    previous_head = "b" * 40
    reads = iter([previous_head, remote_head, remote_head])
    monkeypatch.setattr(cpg, "_read_ref", lambda cwd, ref: next(reads))
    seen: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_run(argv, *, cwd, env=None, timeout=60):
        seen.append((argv, env))
        return subprocess.CompletedProcess(argv, 0, stdout="fetched\n", stderr="")

    monkeypatch.setattr(cpg, "_run_git", fake_run)
    result = cpg.git_fetch_ref_control_plane(
        "proj", remote="origin", branch="main", expected_remote_head=remote_head
    )

    assert result["ok"] is True
    payload = result["result"]
    assert payload["previous_head"] == previous_head
    assert payload["remote_head"] == remote_head
    assert payload["fetched_head"] == remote_head
    assert payload["local_ref"] == "refs/remotes/origin/main"

    fetch_argv, fetch_env = seen[0]
    assert fetch_argv[:5] == [
        "git",
        "fetch",
        "--no-tags",
        "https://git.example.com/gpakoh/repo.git",
        fetch_argv[4],
    ]
    assert fetch_argv[4].startswith("refs/heads/main:refs/mcp-fetch/")
    assert fetch_env is not None
    assert fetch_env["GIT_PASSWORD"] == "tok"

    update_argv, _ = seen[1]
    temp_ref = fetch_argv[4].split(":", 1)[1]
    assert update_argv == [
        "git",
        "update-ref",
        "refs/remotes/origin/main",
        remote_head,
        previous_head,
    ]
    assert seen[2][0] == ["git", "update-ref", "-d", temp_ref]


def test_git_fetch_ref_control_plane_rejects_stale_expected_remote_head(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "tok")
    monkeypatch.setattr(cpg, "_resolve_project_root", lambda project: tmp_path)
    monkeypatch.setattr(cpg, "_remote_url", lambda cwd, remote: "git@git.example.com:gpakoh/repo.git")
    monkeypatch.setattr(cpg, "_parse_gitea_remote", lambda url: ("git.example.com", "gpakoh", "repo"))
    monkeypatch.setattr(cpg, "_repo_https_target", lambda owner, repo, token: ("gpakoh", "https://git.example.com/gpakoh/repo.git"))
    monkeypatch.setattr(cpg, "_gitea_get", lambda path, token: {"commit": {"id": "a" * 40}})
    called = False

    def fake_run(argv, *, cwd, env=None, timeout=60):
        nonlocal called
        called = True
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(cpg, "_run_git", fake_run)
    result = cpg.git_fetch_ref_control_plane(
        "proj", remote="origin", branch="main", expected_remote_head="b" * 40
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "HEAD_MISMATCH"
    assert called is False


def test_git_fetch_ref_control_plane_detects_remote_head_race(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    remote_head = "a" * 40
    monkeypatch.setenv("GITEA_TOKEN", "tok")
    monkeypatch.setattr(cpg, "_resolve_project_root", lambda project: tmp_path)
    monkeypatch.setattr(cpg, "_remote_url", lambda cwd, remote: "git@git.example.com:gpakoh/repo.git")
    monkeypatch.setattr(cpg, "_parse_gitea_remote", lambda url: ("git.example.com", "gpakoh", "repo"))
    monkeypatch.setattr(cpg, "_repo_https_target", lambda owner, repo, token: ("gpakoh", "https://git.example.com/gpakoh/repo.git"))
    monkeypatch.setattr(cpg, "_gitea_get", lambda path, token: {"commit": {"id": remote_head}})
    reads = iter([None, "c" * 40])
    monkeypatch.setattr(cpg, "_read_ref", lambda cwd, ref: next(reads))
    seen: list[list[str]] = []

    def fake_run(argv, *, cwd, env=None, timeout=60):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(cpg, "_run_git", fake_run)

    result = cpg.git_fetch_ref_control_plane("proj", remote="origin", branch="main")

    assert result["ok"] is False
    assert result["error"]["code"] == "HEAD_MISMATCH"
    assert result["error"]["details"]["phase"] == "post_fetch_remote_head_race"
    assert result["error"]["details"]["local_ref_unchanged"] is True
    assert seen[0][0:4] == [
        "git",
        "fetch",
        "--no-tags",
        "https://git.example.com/gpakoh/repo.git",
    ]
    temp_ref = seen[0][4].split(":", 1)[1]
    assert temp_ref.startswith("refs/mcp-fetch/")
    assert seen[1] == ["git", "update-ref", "-d", temp_ref]
    assert all("refs/remotes/origin/main" not in " ".join(argv) for argv in seen)


def test_git_push_control_plane_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    result = cpg.git_push_control_plane("proj")
    assert result["ok"] is False
    assert result["error"]["code"] == "GIT_AUTH_FAILED"


def test_git_push_control_plane_denies_protected_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "tok")
    monkeypatch.setattr(cpg, "_resolve_project_root", lambda project: tmp_path)
    monkeypatch.setattr(cpg, "_current_branch", lambda cwd: "master")
    result = cpg.git_push_control_plane("proj")
    assert result["ok"] is False
    assert result["error"]["code"] == "GIT_PROTECTED_BRANCH"


def test_git_push_control_plane_denies_detached_head(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "tok")
    monkeypatch.setattr(cpg, "_resolve_project_root", lambda project: tmp_path)
    monkeypatch.setattr(cpg, "_current_branch", lambda cwd: (_ for _ in ()).throw(RuntimeError("GIT_DETACHED_HEAD")))
    result = cpg.git_push_control_plane("proj")
    assert result["ok"] is False
    assert result["error"]["code"] == "GIT_DETACHED_HEAD"


def test_git_push_control_plane_does_not_touch_executor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "tok")
    monkeypatch.setattr(cpg, "_resolve_project_root", lambda project: tmp_path)
    monkeypatch.setattr(cpg, "_current_branch", lambda cwd: "feature/x")
    monkeypatch.setattr(cpg, "_verify_local_branch", lambda cwd, branch: None)
    monkeypatch.setattr(cpg, "_staging_git_dir", lambda project, project_root: tmp_path / "stage.git")
    monkeypatch.setattr(cpg, "_ensure_bare_repo", lambda staging_git_dir: None)
    monkeypatch.setattr(cpg, "_stage_branch_from_checkout", lambda project_root, staging_git_dir, branch: None)
    monkeypatch.setattr(cpg, "_verify_staged_ref", lambda staging_git_dir, branch: "abc123")
    monkeypatch.setattr(cpg, "_remote_url", lambda cwd, remote: "git@git.example.com:gpakoh/repo.git")
    monkeypatch.setattr(cpg, "_parse_gitea_remote", lambda url: ("git.example.com", "gpakoh", "repo"))
    monkeypatch.setattr(cpg, "_repo_https_target", lambda owner, repo, token: ("gpakoh", "https://git.example.com/gpakoh/repo.git"))
    seen: dict[str, object] = {}

    def fake_run(argv, *, cwd, env=None, timeout=60):
        seen["argv"] = argv
        seen["env"] = env
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(cpg, "_run_git", fake_run)
    result = cpg.git_push_control_plane("proj", remote="origin", branch="feature/x")
    assert result["ok"] is True
    argv = seen["argv"]
    env = seen["env"]
    assert argv == [
        "git",
        "--git-dir",
        str(tmp_path / "stage.git"),
        "push",
        "https://git.example.com/gpakoh/repo.git",
        "refs/heads/feature/x:refs/heads/feature/x",
    ]
    assert "tok" not in " ".join(argv)
    assert env["GIT_PASSWORD"] == "tok"
    assert env["GIT_ASKPASS"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_git_push_control_plane_non_fast_forward(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "tok")
    monkeypatch.setattr(cpg, "_resolve_project_root", lambda project: tmp_path)
    monkeypatch.setattr(cpg, "_current_branch", lambda cwd: "feature/x")
    monkeypatch.setattr(cpg, "_verify_local_branch", lambda cwd, branch: None)
    monkeypatch.setattr(cpg, "_staging_git_dir", lambda project, project_root: tmp_path / "stage.git")
    monkeypatch.setattr(cpg, "_ensure_bare_repo", lambda staging_git_dir: None)
    monkeypatch.setattr(cpg, "_stage_branch_from_checkout", lambda project_root, staging_git_dir, branch: None)
    monkeypatch.setattr(cpg, "_verify_staged_ref", lambda staging_git_dir, branch: "abc123")
    monkeypatch.setattr(cpg, "_remote_url", lambda cwd, remote: "git@git.example.com:gpakoh/repo.git")
    monkeypatch.setattr(cpg, "_parse_gitea_remote", lambda url: ("git.example.com", "gpakoh", "repo"))
    monkeypatch.setattr(cpg, "_repo_https_target", lambda owner, repo, token: ("gpakoh", "https://git.example.com/gpakoh/repo.git"))
    monkeypatch.setattr(
        cpg,
        "_run_git",
        lambda argv, *, cwd, env=None, timeout=60: subprocess.CompletedProcess(argv, 1, stdout="", stderr="non-fast-forward"),
    )
    result = cpg.git_push_control_plane("proj", remote="origin", branch="feature/x")
    assert result["ok"] is False
    assert result["error"]["code"] == "GIT_NON_FAST_FORWARD"


def test_git_push_control_plane_invalid_remote(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "tok")
    monkeypatch.setattr(cpg, "_resolve_project_root", lambda project: tmp_path)
    monkeypatch.setattr(cpg, "_current_branch", lambda cwd: "feature/x")
    monkeypatch.setattr(cpg, "_verify_local_branch", lambda cwd, branch: None)
    monkeypatch.setattr(cpg, "_staging_git_dir", lambda project, project_root: tmp_path / "stage.git")
    monkeypatch.setattr(cpg, "_ensure_bare_repo", lambda staging_git_dir: None)
    monkeypatch.setattr(cpg, "_stage_branch_from_checkout", lambda project_root, staging_git_dir, branch: None)
    monkeypatch.setattr(cpg, "_verify_staged_ref", lambda staging_git_dir, branch: "abc123")
    monkeypatch.setattr(cpg, "_remote_url", lambda cwd, remote: (_ for _ in ()).throw(RuntimeError("GIT_REMOTE_NOT_ALLOWED")))
    result = cpg.git_push_control_plane("proj", remote="evil", branch="feature/x")
    assert result["ok"] is False
    assert result["error"]["code"] == "GIT_REMOTE_NOT_ALLOWED"


def test_git_push_control_plane_missing_local_ref(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "tok")
    monkeypatch.setattr(cpg, "_resolve_project_root", lambda project: tmp_path)
    monkeypatch.setattr(cpg, "_current_branch", lambda cwd: "feature/x")
    monkeypatch.setattr(cpg, "_verify_local_branch", lambda cwd, branch: (_ for _ in ()).throw(RuntimeError("GIT_LOCAL_REF_MISSING")))
    result = cpg.git_push_control_plane("proj", remote="origin", branch="feature/x")
    assert result["ok"] is False
    assert result["error"]["code"] == "GIT_LOCAL_REF_MISSING"


def test_git_push_control_plane_redacts_token_on_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = "tok-secret"
    monkeypatch.setenv("GITEA_TOKEN", token)
    monkeypatch.setattr(cpg, "_resolve_project_root", lambda project: tmp_path)
    monkeypatch.setattr(cpg, "_current_branch", lambda cwd: "feature/x")
    monkeypatch.setattr(cpg, "_verify_local_branch", lambda cwd, branch: None)
    monkeypatch.setattr(cpg, "_staging_git_dir", lambda project, project_root: tmp_path / "stage.git")
    monkeypatch.setattr(cpg, "_ensure_bare_repo", lambda staging_git_dir: None)
    monkeypatch.setattr(cpg, "_stage_branch_from_checkout", lambda project_root, staging_git_dir, branch: None)
    monkeypatch.setattr(cpg, "_verify_staged_ref", lambda staging_git_dir, branch: "abc123")
    monkeypatch.setattr(cpg, "_remote_url", lambda cwd, remote: "https://user:tok-secret@git.example.com/gpakoh/repo.git")
    monkeypatch.setattr(cpg, "_parse_gitea_remote", lambda url: ("git.example.com", "gpakoh", "repo"))
    monkeypatch.setattr(cpg, "_repo_https_target", lambda owner, repo, token: ("gpakoh", "https://git.example.com/gpakoh/repo.git"))
    monkeypatch.setattr(
        cpg,
        "_run_git",
        lambda argv, *, cwd, env=None, timeout=60: subprocess.CompletedProcess(argv, 1, stdout="", stderr=f"fatal: auth {token} {tmp_path}"),
    )
    result = cpg.git_push_control_plane("proj", remote="origin", branch="feature/x")
    assert result["ok"] is False
    assert token not in str(result)
    assert str(tmp_path) not in str(result)


def test_git_push_control_plane_gitea_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "tok")
    monkeypatch.setattr(cpg, "_resolve_project_root", lambda project: tmp_path)
    monkeypatch.setattr(cpg, "_current_branch", lambda cwd: "feature/x")
    monkeypatch.setattr(cpg, "_verify_local_branch", lambda cwd, branch: None)
    monkeypatch.setattr(cpg, "_staging_git_dir", lambda project, project_root: tmp_path / "stage.git")
    monkeypatch.setattr(cpg, "_ensure_bare_repo", lambda staging_git_dir: None)
    monkeypatch.setattr(cpg, "_stage_branch_from_checkout", lambda project_root, staging_git_dir, branch: None)
    monkeypatch.setattr(cpg, "_verify_staged_ref", lambda staging_git_dir, branch: "abc123")
    monkeypatch.setattr(cpg, "_remote_url", lambda cwd, remote: "git@git.example.com:gpakoh/repo.git")
    monkeypatch.setattr(cpg, "_parse_gitea_remote", lambda url: ("git.example.com", "gpakoh", "repo"))
    monkeypatch.setattr(cpg, "_repo_https_target", lambda owner, repo, token: (_ for _ in ()).throw(RuntimeError("GIT_REMOTE_UNAVAILABLE")))
    result = cpg.git_push_control_plane("proj", remote="origin", branch="feature/x")
    assert result["ok"] is False
    assert result["error"]["code"] == "GIT_REMOTE_UNAVAILABLE"
    assert result["error"]["retryable"] is True
