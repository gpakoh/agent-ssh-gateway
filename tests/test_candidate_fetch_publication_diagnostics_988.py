"""Scoped #988: trusted source publication errors are phased, redacted and ref-safe."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from examples.mcp_server import agent_sources
from tests.test_candidate_fetch import assert_preserved, fetch, managed  # noqa: F401


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "source"
    root.mkdir()
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True, text=True, capture_output=True,
        )
        return result.stdout.strip()
    git("init", "-q", "--initial-branch=main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (root / "file.txt").write_text("source\n")
    git("add", "file.txt")
    git("commit", "-qm", "base")
    return root, git("rev-parse", "HEAD")


def test_candidate_import_reports_sanitized_publisher_phase_without_touching_refs(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller receives actionable enums, never the publisher's sensitive message."""
    sample = request.getfixturevalue("managed")
    secret = "private-auth-header-and-source-path"
    def failing_publisher(*_args: object) -> None:
        raise agent_sources.ManagedSourcePublicationStageError(
            phase="remote_fetch", reason="timeout", elapsed_ms=70123
        ) from RuntimeError(secret)

    monkeypatch.setattr(agent_sources, "materialize_trusted_remote_bundle", failing_publisher)
    result = fetch(sample)
    assert result["ok"] is False
    assert result["error"]["code"] == "GIT_OPERATION_FAILED"
    details = result["error"]["details"]
    assert details["stage"] == "source_publication"
    assert details["publication_phase"] == "remote_fetch"
    assert details["publication_reason"] == "timeout"
    assert details["publication_elapsed_ms"] == 70123
    assert details["tracking_ref_update_attempted"] is False
    assert "next_step" in details and "trusted" in details["next_step"].lower()
    assert secret not in str(result)
    assert_preserved(sample)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        ("trusted remote fetch timed out with bearer=SECRET_LEAK", "timeout"),
        ("upload-pack: not our ref bearer=SECRET_LEAK", "commit_unavailable"),
        ("sensitive auth=SECRET_LEAK", "failed"),
    ],
)
def test_remote_fetch_errors_are_phased_and_secret_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str, reason: str
) -> None:
    root = tmp_path / "registered"
    root.mkdir()
    monkeypatch.setattr(agent_sources, "_resolve_trusted_remote",
                        lambda _root: ("user", "https://example.test/private", "SECRET_LEAK"))
    def bad_fetch(*_args: object) -> None:
        raise agent_sources.ManagedSourceBundleError(error)
    monkeypatch.setattr(agent_sources, "_fetch_remote_object", bad_fetch)
    destination = tmp_path / "source.bundle"
    with pytest.raises(agent_sources.ManagedSourcePublicationStageError) as caught:
        agent_sources.materialize_trusted_remote_bundle(root, "a" * 40, destination)
    exc = caught.value
    assert exc.phase == "remote_fetch"
    assert exc.reason == reason
    assert isinstance(exc.elapsed_ms, int) and exc.elapsed_ms >= 0
    assert "SECRET_LEAK" not in str(exc)
    assert not destination.exists()
    assert not list(tmp_path.glob("*.bundle.tmp"))


def test_bundle_create_error_has_distinct_phase_and_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, sha = _repo(tmp_path)
    monkeypatch.setattr(agent_sources, "_resolve_trusted_remote",
                        lambda _root: ("user", str(source), "SECRET_LEAK"))
    real_run = subprocess.run
    def broken_create(argv: list[str], **kwargs: object):
        if "bundle" in argv and "create" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "SECRET_LEAK")
        return real_run(argv, **kwargs)
    monkeypatch.setattr(agent_sources.subprocess, "run", broken_create)
    dst = tmp_path / "out.bundle"
    with pytest.raises(agent_sources.ManagedSourcePublicationStageError) as caught:
        agent_sources.materialize_trusted_remote_bundle(source, sha, dst)
    assert caught.value.phase == "bundle_creation"
    assert caught.value.reason == "failed"
    assert "SECRET_LEAK" not in str(caught.value)
    assert not dst.exists()


def test_bundle_verify_error_has_distinct_phase_and_no_partial_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, sha = _repo(tmp_path)
    monkeypatch.setattr(agent_sources, "_resolve_trusted_remote",
                        lambda _root: ("user", str(source), "SECRET_LEAK"))
    monkeypatch.setattr(
        agent_sources, "_bundle_head",
        lambda _bundle: (_ for _ in ()).throw(
            agent_sources.ManagedSourceBundleError("verification SECRET_LEAK")
        ),
    )
    dst = tmp_path / "out.bundle"
    with pytest.raises(agent_sources.ManagedSourcePublicationStageError) as caught:
        agent_sources.materialize_trusted_remote_bundle(source, sha, dst)
    assert caught.value.phase == "bundle_verification"
    assert caught.value.reason == "failed"
    assert "SECRET_LEAK" not in str(caught.value)
    assert not dst.exists()


def test_git_fetch_failure_never_copies_raw_stderr_or_remote_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "PRIVATE_FETCH_TOKEN"
    clone_url = f"https://user:{secret}@example.test/private.git"
    monkeypatch.setattr(
        "examples.mcp_server.managed_git._minimal_git_env", lambda _u, _t: {},
    )
    def failed_run(argv: list[str], **_kwargs: object):
        return subprocess.CompletedProcess(
            argv, 1, "", f"fatal: upload-pack: not our ref; remote={clone_url}",
        )
    monkeypatch.setattr(agent_sources.subprocess, "run", failed_run)
    with pytest.raises(agent_sources.ManagedSourceBundleError) as caught:
        agent_sources._fetch_remote_object("user", clone_url, secret, "a" * 40, tmp_path)
    assert secret not in str(caught.value)
    assert clone_url not in str(caught.value)
    assert "not our ref" in str(caught.value).lower()


def test_digest_failure_keeps_valueerror_fail_closed_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The publication wrapper must not erase ManagedSourceDigestError's ValueError ABI."""
    source, sha = _repo(tmp_path)
    monkeypatch.setattr(
        agent_sources, "_resolve_trusted_remote",
        lambda _root: ("user", str(source), "dummy-token"),
    )
    def fail_digest(_path: Path, _expected: str):
        raise agent_sources.ManagedSourceDigestError("managed source digest mismatch")
    monkeypatch.setattr(agent_sources, "bind_publication_bytes", fail_digest)
    with pytest.raises(agent_sources.ManagedSourceDigestError):
        agent_sources.materialize_trusted_remote_bundle(source, sha, tmp_path / "out.bundle")
