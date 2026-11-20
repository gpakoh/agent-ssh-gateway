"""The scheduled registry sync must not edit a divergent canonical master."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import sync_projects_registry as sync


def git(repo: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *argv],
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout.strip()


@pytest.fixture
def canonical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "canonical"
    repo.mkdir()
    root = tmp_path / "clones"
    (root / "new-service").mkdir(parents=True)
    yaml = repo / "projects.yaml"
    yaml.write_text(
        f"registry_root: {root}\nprojects:\n"
        "  existing:\n    root: existing\n    type: service\n",
        encoding="utf-8",
    )
    git(repo, "init", "-b", "master")
    git(repo, "add", "projects.yaml")
    git(
        repo, "-c", "user.name=CI", "-c", "user.email=ci@example.invalid",
        "commit", "-m", "baseline",
    )
    git(
        repo, "remote", "add", "origin",
        "https://git.example.test/gpakoh/agent-ssh-gateway.git",
    )
    initial = git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(sync, "REPO_ROOT", repo)
    monkeypatch.setattr(sync, "_fetch_gitea_repos", lambda *_: {"new-service": "New service"})
    monkeypatch.setattr(
        sync, "_find_local_clones",
        lambda *_: {"new-service": root / "new-service"},
    )
    for k in ("GITEA_API_BASE", "GITEA_TOKEN", "GITEA_OWNER"):
        monkeypatch.setenv(k, "https://git.example.test/api/v1" if k == "GITEA_API_BASE" else
                           "gpakoh" if k == "GITEA_OWNER" else "test-token")
    monkeypatch.setattr(sys, "argv", ["sync", "--apply", "--yaml", str(yaml)])
    return repo, yaml, initial


def test_apply_fails_closed_before_write_when_canonical_has_local_only_commit(
    canonical, monkeypatch: pytest.MonkeyPatch,
):
    repo, yaml, base = canonical
    (repo / "local.txt").write_text("local-only", encoding="utf-8")
    git(repo, "add", "local.txt")
    git(repo, "-c", "user.name=CI", "-c", "user.email=ci@example.invalid",
        "commit", "-m", "local ahead")
    monkeypatch.setattr(sync, "_get_gitea_branch_head", lambda *_: base)
    original = yaml.read_bytes()
    assert sync.main() == 2
    assert yaml.read_bytes() == original


def test_apply_fails_closed_before_write_when_remote_master_advanced(
    canonical, monkeypatch: pytest.MonkeyPatch,
):
    _repo, yaml, _base = canonical
    monkeypatch.setattr(sync, "_get_gitea_branch_head", lambda *_: "a" * 40)
    original = yaml.read_bytes()
    assert sync.main() == 2
    assert yaml.read_bytes() == original


def test_apply_fails_closed_on_unverifiable_remote_master(
    canonical, monkeypatch: pytest.MonkeyPatch,
):
    _repo, yaml, _base = canonical

    def inaccessible(*_args):
        raise RuntimeError("canonical upstream could not be verified")

    monkeypatch.setattr(sync, "_get_gitea_branch_head", inaccessible)
    original = yaml.read_bytes()
    assert sync.main() == 2
    assert yaml.read_bytes() == original


def test_apply_fails_closed_on_manual_tracked_change(
    canonical, monkeypatch: pytest.MonkeyPatch,
):
    repo, yaml, base = canonical
    (repo / "projects.yaml").write_text(
        yaml.read_text(encoding="utf-8") + "# manual edit\n", encoding="utf-8"
    )
    monkeypatch.setattr(sync, "_get_gitea_branch_head", lambda *_: base)
    original = yaml.read_bytes()
    assert sync.main() == 2
    assert yaml.read_bytes() == original


@pytest.mark.parametrize(
    "reply",
    [
        {"commit": None},
        {"commit": {"id": "short"}},
        {"commit": {"id": 42}},
        {},
    ],
)
def test_remote_branch_receipt_rejects_malformed_payload(
    monkeypatch: pytest.MonkeyPatch, reply,
):
    monkeypatch.setattr(
        sync.urllib.request, "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(json.dumps(reply).encode()),
    )
    with pytest.raises(RuntimeError, match="canonical Gitea branch head is invalid"):
        sync._get_gitea_branch_head(
            "https://git.example.test/api/v1", "secret", "gpakoh",
            "agent-ssh-gateway", "master",
        )


def test_remote_branch_receipt_uses_authenticated_exact_repo(
    monkeypatch: pytest.MonkeyPatch,
):
    expected = "a" * 40

    def response(request, *, timeout):
        assert timeout == 15
        assert request.full_url.endswith(
            "/repos/gpakoh/agent-ssh-gateway/branches/master"
        )
        assert request.get_header("Authorization") == "token secret"
        return io.BytesIO(json.dumps({"commit": {"id": expected}}).encode())

    monkeypatch.setattr(sync.urllib.request, "urlopen", response)
    assert sync._get_gitea_branch_head(
        "https://git.example.test/api/v1", "secret", "gpakoh",
        "agent-ssh-gateway", "master",
    ) == expected


def test_exact_master_allows_append_only_sync(
    canonical, monkeypatch: pytest.MonkeyPatch,
):
    _repo, yaml, base = canonical
    monkeypatch.setattr(sync, "_get_gitea_branch_head", lambda *_: base)
    original = yaml.read_bytes()
    assert sync.main() == 0
    assert yaml.read_bytes().startswith(original)
    assert yaml.read_text(encoding="utf-8").count("  new-service:") == 1
