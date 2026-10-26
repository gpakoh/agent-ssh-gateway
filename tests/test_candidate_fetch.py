"""Real Git regressions for exact default-base import into bundle candidates."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from app.workspace.registry import get_registry, reset_registry
from examples.mcp_server import agent_sources
from examples.mcp_server import control_plane_git as git
from examples.mcp_server.agent_paths import managed_source_bundle_path
from examples.mcp_server.candidate_clone import _project_id


def run(root, *args):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def commit(root, filename, body):
    (root / filename).write_text(body)
    run(root, "add", filename)
    run(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        filename,
    )
    return run(root, "rev-parse", "HEAD")


@pytest.fixture
def managed(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    source = workspace / "source"
    source.mkdir(parents=True)
    run(source, "init", "-q", "--initial-branch=main")
    base = commit(source, "README.md", "base\n")
    run(source, "remote", "add", "origin", "git@git.example.com:owner/repo.git")
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(tmp_path / "sources"))
    monkeypatch.setenv("GITEA_TOKEN", "sensitive-test-token")
    bundle = Path(managed_source_bundle_path("source-project", base))
    bundle.parent.mkdir(parents=True)
    run(source, "bundle", "create", str(bundle), "refs/heads/main")
    feature = "fix/existing-work"
    project = _project_id("source-project", feature, base)
    root = workspace / ".mcp-candidate-clones" / project
    root.parent.mkdir()
    run(workspace, "clone", "-q", str(bundle), str(root))
    run(root, "checkout", "-qb", feature, base)
    head = commit(root, "feature.txt", "keep existing work\n")
    metadata = {
        "version": 1,
        "project_id": project,
        "source_project": "source-project",
        "branch": feature,
        "base_sha": base,
        "root": f".mcp-candidate-clones/{project}",
    }
    metadata_path = root / ".git" / "mcp-candidate-clone.json"
    metadata_path.write_text(json.dumps(metadata))
    config = tmp_path / "projects.yaml"
    config.write_text(
        f"version: 1\nregistry_root: {workspace}\nprojects:\n"
        "  source-project:\n    root: source\n    type: repository\n"
        f"  {project}:\n    root: .mcp-candidate-clones/{project}\n    type: candidate-clone\n"
    )
    reset_registry()
    get_registry(config)
    upstream = tmp_path / "upstream"
    run(tmp_path, "clone", "-q", str(source), str(upstream))
    run(upstream, "fetch", "-q", str(root), f"refs/heads/{feature}:refs/heads/{feature}")
    target = commit(upstream, "README.md", "new main\n")
    published = []

    def api(path, *, token):
        assert token == "sensitive-test-token"
        if path == "/user":
            return {"login": "owner"}
        if path == "/repos/owner/repo":
            return {"clone_url": "https://git.example.com/owner/repo.git", "default_branch": "main"}
        prefix = "/repos/owner/repo/branches/"
        assert path.startswith(prefix), path
        branch = unquote(path[len(prefix) :])
        return {"commit": {"id": run(upstream, "rev-parse", f"refs/heads/{branch}")}}

    def publish(source_root, expected, destination):
        assert source_root == source
        assert expected == target
        published.append(expected)
        run(upstream, "update-ref", "refs/heads/source", expected)
        run(upstream, "bundle", "create", str(destination), "refs/heads/source")
        return SimpleNamespace(path=str(destination))

    monkeypatch.setattr(git, "_gitea_get", api)
    monkeypatch.setattr(agent_sources, "materialize_trusted_remote_bundle", publish)
    try:
        yield SimpleNamespace(
            root=root,
            project=project,
            source=source,
            upstream=upstream,
            base=base,
            head=head,
            target=target,
            feature=feature,
            metadata=metadata,
            metadata_path=metadata_path,
            published=published,
            publish=publish,
        )
    finally:
        reset_registry()


def fetch(m, expected=True, branch="main", remote="origin"):
    return git.git_fetch_ref_control_plane(
        m.project,
        remote=remote,
        branch=branch,
        expected_remote_head=m.target if expected is True else expected,
    )


def assert_preserved(m):
    assert run(m.root, "rev-parse", "HEAD") == m.head
    assert run(m.root, "branch", "--show-current") == m.feature
    assert run(m.root, "status", "--porcelain") == ""
    assert (m.root / "feature.txt").read_text() == "keep existing work\n"
    assert run(m.source, "rev-parse", "HEAD") == m.base
    assert run(m.source, "status", "--porcelain") == ""
    assert run(m.root, "for-each-ref", "refs/mcp-fetch") == ""


def test_import_exact_main_preserves_existing_feature_and_origin(managed):
    m = managed
    config = (m.root / ".git/config").read_bytes()
    result = fetch(m)
    assert result["ok"], result
    receipt = result["result"]
    assert receipt["feature_head"] == m.head
    assert receipt["source_project"] == "source-project"
    assert receipt["owner"] == "owner" and receipt["repo"] == "repo"
    assert receipt["fetched_head"] == m.target
    assert run(m.root, "rev-parse", "refs/remotes/origin/main") == m.target
    assert (m.root / ".git/config").read_bytes() == config
    assert not (m.root / ".git/FETCH_HEAD").exists()
    assert "sensitive-test-token" not in str(result)
    assert_preserved(m)
    assert fetch(m)["ok"]  # repeat imports do not duplicate or rewrite history
    assert_preserved(m)


@pytest.mark.parametrize("expected", [None, "a" * 40])
def test_expected_base_is_mandatory_and_exact(managed, expected):
    result = fetch(managed, expected=expected)
    assert not result["ok"]
    assert result["error"]["code"] in {"INVALID_INPUT", "HEAD_MISMATCH"}
    assert managed.published == []
    assert_preserved(managed)


@pytest.mark.parametrize(
    "change", ["source_project", "base_sha", "branch", "root", "project_id", "version"]
)
def test_lineage_tampering_is_rejected_before_fetch(managed, change):
    managed.metadata[change] = 999 if change == "version" else "foreign"
    managed.metadata_path.write_text(json.dumps(managed.metadata))
    result = fetch(managed)
    assert not result["ok"]
    assert managed.published == []
    assert_preserved(managed)


def test_unrelated_local_bundle_is_not_a_trust_anchor(managed):
    run(managed.root, "remote", "set-url", "origin", "/tmp/unregistered.bundle")
    result = fetch(managed)
    assert not result["ok"] and result["error"]["code"] == "GIT_REMOTE_NOT_ALLOWED"
    assert not managed.published
    assert_preserved(managed)


def test_wrong_default_branch_is_rejected(managed):
    result = fetch(managed, branch=managed.feature)
    assert not result["ok"] and result["error"]["code"] == "GIT_REMOTE_NOT_ALLOWED"
    assert not managed.published
    assert_preserved(managed)


def test_dirty_candidate_is_rejected_without_publishing(managed):
    (managed.root / "untracked.txt").write_text("unfinished")
    result = fetch(managed)
    assert not result["ok"] and result["error"]["code"] == "WORKSPACE_CONTENDED"
    assert not managed.published
    assert run(managed.root, "rev-parse", "HEAD") == managed.head


def test_unpushed_feature_is_rejected(managed):
    commit(managed.root, "unpublished.txt", "unpublished")
    result = fetch(managed)
    assert not result["ok"] and result["error"]["code"] == "HEAD_MISMATCH"
    assert not managed.published


@pytest.mark.parametrize("race", ["remote_base", "remote_feature", "local_head", "tracking_ref"])
def test_races_do_not_overwrite_refs_or_existing_work(managed, monkeypatch, race):
    m = managed
    raced = None

    def publish(*args):
        nonlocal raced
        result = m.publish(*args)
        if race == "remote_base":
            raced = commit(m.upstream, "other.txt", "other")
        elif race == "remote_feature":
            run(m.upstream, "update-ref", f"refs/heads/{m.feature}", m.base)
        elif race == "local_head":
            raced = commit(m.root, "other.txt", "other")
        else:
            raced = m.head
            run(m.root, "update-ref", "refs/remotes/origin/main", m.head)
        return result

    monkeypatch.setattr(agent_sources, "materialize_trusted_remote_bundle", publish)
    result = fetch(m)
    assert not result["ok"], result
    assert result["error"]["code"] in {"HEAD_MISMATCH", "WORKSPACE_CONTENDED"}
    assert run(m.root, "for-each-ref", "refs/mcp-fetch") == ""
    assert run(m.root, "rev-parse", "HEAD") == (raced if race == "local_head" else m.head)
    tracking = git._read_ref(m.root, "refs/remotes/origin/main")
    assert tracking == (m.head if race == "tracking_ref" else m.base)
    # origin/main from the initial bundle stays at base on all rejected imports.


@pytest.mark.parametrize("failure", ["publisher", "partial_fetch", "fetch_timeout", "wrong_commit"])
def test_failed_import_leaves_no_staging_ref_or_tracking_advance(managed, monkeypatch, failure):
    m = managed
    original = git._run_git

    def broken(argv, *, cwd, env=None, timeout=60):
        if len(argv) > 1 and argv[1] == "fetch":
            staging = argv[-1].split(":", 1)[1]
            original(["git", "update-ref", staging, m.head], cwd=cwd)
            if failure == "fetch_timeout":
                raise subprocess.TimeoutExpired(argv, timeout)
            if failure == "wrong_commit":
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 1, "", "sensitive-test-token")
        return original(argv, cwd=cwd, env=env, timeout=timeout)

    if failure == "publisher":

        def fail(*args):
            raise agent_sources.ManagedSourceBundleError("sensitive-test-token")

        monkeypatch.setattr(agent_sources, "materialize_trusted_remote_bundle", fail)
    else:
        monkeypatch.setattr(git, "_run_git", broken)
    result = fetch(m)
    assert not result["ok"]
    assert "sensitive-test-token" not in str(result)
    assert git._read_ref(m.root, "refs/remotes/origin/main") == m.base
    assert_preserved(m)


def test_import_can_be_explicitly_merged_without_losing_existing_history(managed):
    m = managed
    assert fetch(m)["ok"]
    assert_preserved(m)
    run(
        m.root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "merge",
        "--no-edit",
        "refs/remotes/origin/main",
    )
    run(m.root, "merge-base", "--is-ancestor", m.head, "HEAD")
    run(m.root, "merge-base", "--is-ancestor", m.target, "HEAD")
    assert (m.root / "README.md").read_text() == "new main\n"
    assert (m.root / "feature.txt").read_text() == "keep existing work\n"


def test_atomic_publish_rejects_feature_commit_race(managed, monkeypatch):
    from examples.mcp_server import candidate_fetch as fetcher

    m = managed
    original = fetcher._publish
    changed = []

    def raced(*args):
        changed.append(commit(m.root, "last-second.txt", "preserve this"))
        return original(*args)

    monkeypatch.setattr(fetcher, "_publish", raced)
    result = fetch(m)
    assert not result["ok"] and result["error"]["code"] == "WORKSPACE_CONTENDED"
    assert git._read_ref(m.root, "refs/remotes/origin/main") == m.base
    assert run(m.root, "rev-parse", "HEAD") == changed[0]
    assert run(m.root, "for-each-ref", "refs/mcp-fetch") == ""


@pytest.mark.parametrize("other_writer", [False, True])
def test_postpublish_race_restores_only_its_own_exact_ref(managed, monkeypatch, other_writer):
    from examples.mcp_server import candidate_fetch as fetcher

    m = managed
    original = fetcher._publish

    def raced(*args):
        result = original(*args)
        assert result
        if other_writer:
            run(m.root, "update-ref", "refs/remotes/origin/main", m.head)
        else:
            (m.root / "unfinished.txt").write_text("preserve external work")
        return result

    monkeypatch.setattr(fetcher, "_publish", raced)
    result = fetch(m)
    assert not result["ok"]
    assert git._read_ref(m.root, "refs/remotes/origin/main") == (m.head if other_writer else m.base)
    assert run(m.root, "rev-parse", "HEAD") == m.head
    assert run(m.root, "for-each-ref", "refs/mcp-fetch") == ""


def test_removed_source_registration_fails_closed(managed, monkeypatch):
    from app.workspace.policy import WorkspacePolicyError

    registry = get_registry()
    original = registry.project_info

    def info(project):
        if project == "source-project":
            raise WorkspacePolicyError("unregistered source")
        return original(project)

    monkeypatch.setattr(registry, "project_info", info)
    result = fetch(managed)
    assert not result["ok"] and result["error"]["code"] == "GIT_REMOTE_NOT_ALLOWED"
    assert not managed.published
    assert_preserved(managed)


@pytest.mark.parametrize("base", [True, {}, []])
def test_malformed_metadata_is_typed_failure(managed, base):
    managed.metadata["base_sha"] = base
    managed.metadata_path.write_text(json.dumps(managed.metadata))
    result = fetch(managed)
    assert not result["ok"]
    assert not managed.published
    assert_preserved(managed)


def test_two_concurrent_imports_preserve_one_feature_history(managed):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: fetch(managed), range(2)))
    assert all(result["ok"] for result in results), results
    assert git._read_ref(managed.root, "refs/remotes/origin/main") == managed.target
    assert_preserved(managed)


def test_candidate_git_receives_no_transport_credentials_and_keeps_indexes(managed, monkeypatch):
    m = managed
    candidate_index = (m.root / ".git/index").read_bytes()
    source_index = (m.source / ".git/index").read_bytes()
    original = subprocess.run
    environments = []

    def observed(argv, *args, **kwargs):
        if Path(kwargs.get("cwd", ".")) == m.root:
            environment = kwargs.get("env")
            assert isinstance(environment, dict)
            assert "GITEA_TOKEN" not in environment
            assert "GIT_PASSWORD" not in environment
            assert "GIT_ASKPASS" not in environment
            assert "sensitive-test-token" not in str(environment)
            assert "sensitive-test-token" not in str(argv)
            environments.append(environment)
        return original(argv, *args, **kwargs)

    with monkeypatch.context() as scope:
        scope.setattr(subprocess, "run", observed)
        result = fetch(m)
    assert result["ok"], result
    assert environments
    assert (m.root / ".git/index").read_bytes() == candidate_index
    assert (m.source / ".git/index").read_bytes() == source_index
    assert_preserved(m)
