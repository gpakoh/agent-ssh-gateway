from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from examples.mcp_server import agent_sources
from examples.mcp_server.agent_paths import managed_source_bundle_path
from examples.mcp_server.agent_sources import ManagedSourceBundleError
from examples.mcp_server.source_publication_policy import (
    LocalSourceState,
    PublicationRoute,
    SourceFailureCause,
    choose_publication_route,
    classify_source_failure_message,
)


class _Registry:
    def __init__(self, root: Path):
        self.root = root

    def project_info(self, project: str) -> dict[str, str]:
        return {"project_id": project, "root": str(self.root)}


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _init_history_repo(root: Path, commits: int = 5) -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Source Publication Tests")
    for i in range(commits):
        (root / f"f{i}.txt").write_text(f"commit {i}\n", encoding="utf-8")
        _git(root, "add", f"f{i}.txt")
        _git(root, "commit", "-q", "-m", f"c{i}")
    return _git(root, "rev-parse", "HEAD")


def _make_shallow_clone(source: Path, destination: Path, depth: int) -> str:
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--depth",
            str(depth),
            f"file://{source}",
            str(destination),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return _git(destination, "rev-parse", "HEAD")


def _make_bare_remote(source: Path, destination: Path) -> Path:
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(source), str(destination)],
        check=True,
        capture_output=True,
        text=True,
    )
    return destination


def _install_managed_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_root: Path,
    project_root: Path,
    remote: Path | None = None,
) -> None:
    monkeypatch.setenv("MCP_AGENT_SOURCE_ROOT", str(source_root))
    monkeypatch.setattr(agent_sources, "get_registry", lambda: _Registry(project_root))
    if remote is not None:
        monkeypatch.setattr(
            agent_sources,
            "_resolve_trusted_remote",
            lambda _root: (str(remote), "fixture-token"),
        )


def _assert_bundle_clones_to(bundle_path: Path, expected: str, clone_dir: Path) -> None:
    subprocess.run(
        ["git", "clone", "-q", "--no-hardlinks", str(bundle_path), str(clone_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(clone_dir, "checkout", "-q", "--detach", expected)
    assert _git(clone_dir, "rev-parse", "HEAD") == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (LocalSourceState.FULL, PublicationRoute.LOCAL),
        (LocalSourceState.SHALLOW, PublicationRoute.TRUSTED_REMOTE),
        (LocalSourceState.MISSING_COMMIT, PublicationRoute.TRUSTED_REMOTE),
    ],
)
def test_publication_route_is_explicit(state, expected):
    assert choose_publication_route(state) is expected


def test_unknown_local_state_fails_closed():
    with pytest.raises(ValueError, match="unsupported local source state"):
        choose_publication_route("partial")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("source repository is shallow", SourceFailureCause.SHALLOW),
        ("remote did not contain the requested commit", SourceFailureCause.MISSING_REF),
        ("fatal: Authentication failed for https://git.example/repo", SourceFailureCause.AUTH),
        ("fatal: unable to access url: The requested URL returned error: 403", SourceFailureCause.AUTH),
        ("trusted remote fetch timed out", SourceFailureCause.TIMEOUT),
        ("Permission denied while reading objects", SourceFailureCause.PERMISSION),
        ("git bundle verify failed: invalid bundle", SourceFailureCause.CORRUPTION),
        ("pack has unresolved deltas: corrupt pack", SourceFailureCause.CORRUPTION),
        ("server certificate verification failed", SourceFailureCause.TRUST),
        ("Host key verification failed", SourceFailureCause.TRUST),
        ("fatal: unable to access url: Could not resolve host: git.example", SourceFailureCause.NETWORK),
        ("git could not start", SourceFailureCause.UNAVAILABLE),
        ("unexpected source failure", SourceFailureCause.UNKNOWN),
    ],
)
def test_source_failure_classification_is_sanitized(message, expected):
    assert classify_source_failure_message(message) is expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Host key verification failed", SourceFailureCause.TRUST),
        (
            "trusted remote fetch failed: could not resolve host: build403.example",
            SourceFailureCause.NETWORK,
        ),
        (
            "I/O failure while reading object 4010123456789abcdef0123456789abcdef0123456",
            SourceFailureCause.UNKNOWN,
        ),
        (
            "published managed source bundle verification failed",
            SourceFailureCause.UNKNOWN,
        ),
    ],
)
def test_r2_classifier_does_not_overclassify_ambiguous_diagnostics(message, expected):
    assert classify_source_failure_message(message) is expected


@pytest.mark.parametrize("depth", [1, 2])
def test_shallow_source_publishes_from_trusted_full_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int
):
    origin = tmp_path / f"origin-depth-{depth}"
    head = _init_history_repo(origin)
    remote = _make_bare_remote(origin, tmp_path / f"remote-depth-{depth}.git")
    shallow = tmp_path / f"shallow-depth-{depth}"
    assert _make_shallow_clone(origin, shallow, depth=depth) == head
    assert _git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    before_head = _git(shallow, "rev-parse", "HEAD")
    before_status = _git(shallow, "status", "--porcelain=v1")

    _install_managed_env(
        monkeypatch,
        source_root=tmp_path / "source-root",
        project_root=shallow,
        remote=remote,
    )

    publication = agent_sources.ensure_managed_source_bundle("proj", head)

    assert publication is not None
    bundle = Path(publication.path)
    assert bundle.is_file()
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == publication.sha256
    _assert_bundle_clones_to(bundle, head, tmp_path / f"verify-depth-{depth}")
    assert _git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    assert _git(shallow, "rev-parse", "HEAD") == before_head
    assert _git(shallow, "status", "--porcelain=v1") == before_status


def test_full_local_source_publishes_without_trusted_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    origin = tmp_path / "origin-full"
    head = _init_history_repo(origin)
    _install_managed_env(
        monkeypatch,
        source_root=tmp_path / "source-root",
        project_root=origin,
    )

    def forbidden_remote(_root: Path) -> tuple[str, str]:
        raise AssertionError("full local source must not consult trusted remote")

    monkeypatch.setattr(agent_sources, "_resolve_trusted_remote", forbidden_remote)

    publication = agent_sources.ensure_managed_source_bundle("proj-full", head)

    assert publication is not None
    _assert_bundle_clones_to(Path(publication.path), head, tmp_path / "verify-full")


def test_shallow_source_without_trusted_remote_fails_without_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    origin = tmp_path / "origin-no-remote"
    head = _init_history_repo(origin)
    shallow = tmp_path / "shallow-no-remote"
    assert _make_shallow_clone(origin, shallow, depth=1) == head
    _install_managed_env(
        monkeypatch,
        source_root=tmp_path / "source-root",
        project_root=shallow,
    )

    def unavailable_remote(_root: Path) -> tuple[str, str]:
        raise ManagedSourceBundleError("trusted remote resolution failed")

    monkeypatch.setattr(agent_sources, "_resolve_trusted_remote", unavailable_remote)

    with pytest.raises(ManagedSourceBundleError, match="trusted remote"):
        agent_sources.ensure_managed_source_bundle("proj-no-remote", head)

    target = managed_source_bundle_path("proj-no-remote", head)
    assert target is not None
    assert not Path(target).exists()


def test_generic_local_inspection_error_does_not_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    origin = tmp_path / "origin-inspection"
    head = _init_history_repo(origin)
    _install_managed_env(
        monkeypatch,
        source_root=tmp_path / "source-root",
        project_root=origin,
    )
    remote_called = False

    def failing_shallow_probe(_root: Path) -> bool:
        raise ManagedSourceBundleError("permission denied inspecting source repository")

    def forbidden_remote(_root: Path) -> tuple[str, str]:
        nonlocal remote_called
        remote_called = True
        return ("/must/not/be/used", "fixture-token")

    monkeypatch.setattr(agent_sources, "_source_is_shallow", failing_shallow_probe)
    monkeypatch.setattr(agent_sources, "_resolve_trusted_remote", forbidden_remote)

    with pytest.raises(ManagedSourceBundleError, match="permission denied"):
        agent_sources.ensure_managed_source_bundle("proj-inspection", head)

    assert remote_called is False
    target = managed_source_bundle_path("proj-inspection", head)
    assert target is not None
    assert not Path(target).exists()
