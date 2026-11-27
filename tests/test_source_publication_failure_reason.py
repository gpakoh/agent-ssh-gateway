"""Source-publication stage diagnostics retain fixed typed causes only."""

import errno
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import httpx
import pytest

from examples.mcp_server import agent_sources as sources
from examples.mcp_server import control_plane_git, managed_git
from examples.mcp_server.candidate_clone import _source_failure_details

_EXPECTED_CAUSE = {
    "timeout": "timeout",
    "storage_full": "unavailable",
    "permission_denied": "permission",
    "authentication_failed": "auth",
    "failed": "source_materialization_failed",
}


def _assert_adapter(error: sources.ManagedSourcePublicationStageError, reason: str) -> None:
    assert _source_failure_details(error) == {
        "source_cause": _EXPECTED_CAUSE[reason],
        "source_phase": error.phase,
        "source_reason": reason,
        "source_elapsed_ms": error.elapsed_ms,
    }


def _raise(error: Exception) -> Callable[..., NoReturn]:
    def fail(*args: object, **kwargs: object) -> NoReturn:
        raise error
    return fail


@pytest.mark.parametrize("route", ["fetch", "git"])
@pytest.mark.parametrize(("error", "reason"), [
    (OSError(errno.ENOSPC, "synthetic private path"), "storage_full"),
    (PermissionError(errno.EACCES, "synthetic private path"), "permission_denied"),
    (OSError(errno.EIO, "synthetic private path"), "failed"),
    (subprocess.TimeoutExpired("git", 120), "timeout"),
])
def test_process_wrapper_preserves_fixed_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, route: str, error: Exception, reason: str,
) -> None:
    monkeypatch.setattr(managed_git, "_minimal_git_env", lambda *_: {})
    monkeypatch.setattr(sources.subprocess, "run", _raise(error))
    with pytest.raises(sources.ManagedSourcePublicationStageError) as caught:
        with sources._publication_phase("remote_fetch"):
            if route == "fetch":
                sources._fetch_remote_object(
                    "fixture", "https://example.invalid/r", "synthetic", "a" * 40, tmp_path,
                )
            else:
                sources._run_git(["bundle", "verify", "fixture"])
    assert caught.value.phase == "remote_fetch"
    assert caught.value.reason == reason
    assert "synthetic private path" not in str(caught.value)
    assert 0 <= caught.value.elapsed_ms <= 600_000
    _assert_adapter(caught.value, reason)


@pytest.mark.parametrize(("failure", "reason"), [
    (RuntimeError("GIT_AUTH_FAILED"), "authentication_failed"),
    (RuntimeError("GIT_REMOTE_UNAVAILABLE"), "failed"),
    (RuntimeError("private-url GIT_AUTH_FAILED"), "failed"),
])
def test_remote_resolution_wrapper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: RuntimeError, reason: str,
) -> None:
    monkeypatch.setenv("GITEA_TOKEN", "synthetic-fixture")
    monkeypatch.setattr(
        sources, "_run_git",
        lambda args, **_: "origin" if args == ["remote"] else "https://example.invalid/o/r.git",
    )
    monkeypatch.setattr(
        control_plane_git, "_parse_gitea_remote", lambda _: ("example.invalid", "o", "r"),
    )
    monkeypatch.setattr(control_plane_git, "_repo_https_target", _raise(failure))
    with pytest.raises(sources.ManagedSourcePublicationStageError) as caught:
        with sources._publication_phase("remote_resolution"):
            sources._resolve_trusted_remote(tmp_path)
    assert caught.value.reason == reason
    assert caught.value.phase == "remote_resolution"
    assert "private-url" not in str(caught.value)
    assert "synthetic-fixture" not in str(caught.value)
    _assert_adapter(caught.value, reason)


@pytest.mark.parametrize("cause", [
    RuntimeError("no space left private-path"), RuntimeError("GIT_AUTH_FAILED"),
])
def test_unrelated_wrapped_text_not_reclassified(cause: RuntimeError) -> None:
    error = sources.ManagedSourceBundleError("unrelated source failure")
    error.__cause__ = cause
    assert sources._publication_failure_reason(error) == "failed"


def test_digest_error_abi_unchanged() -> None:
    error = sources.ManagedSourceDigestError("synthetic digest error")
    with pytest.raises(sources.ManagedSourceDigestError) as caught:
        with sources._publication_phase("digest_binding"):
            raise error
    assert caught.value is error
    assert isinstance(error, ValueError)


@pytest.mark.parametrize("endpoint", ["/user", "/repos/o/r"])
@pytest.mark.parametrize("status", [401, 403, 404])
def test_http_status_survives_remote_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, endpoint: str, status: int,
) -> None:
    """Exercise the actual Gitea HTTP classifier, not a synthetic RuntimeError."""
    monkeypatch.setenv("GITEA_TOKEN", "synthetic-fixture")
    monkeypatch.setenv("GITEA_API_BASE", "https://example.invalid/api/v1")
    monkeypatch.delenv("GITEA_FORWARDED_HOST", raising=False)
    monkeypatch.setattr(
        sources, "_run_git",
        lambda args, **_: "origin" if args == ["remote"] else "https://example.invalid/o/r.git",
    )
    monkeypatch.setattr(
        control_plane_git, "_parse_gitea_remote", lambda _: ("example.invalid", "o", "r"),
    )
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "token synthetic-fixture"
        path = request.url.path.removeprefix("/api/v1")
        paths.append(path)
        if path == endpoint:
            return httpx.Response(status, json={"message": "private-http-response"})
        assert path == "/user"
        return httpx.Response(200, json={"login": "fixture"})

    client_class = httpx.Client
    monkeypatch.setattr(
        control_plane_git.httpx, "Client",
        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs),
    )
    with pytest.raises(sources.ManagedSourcePublicationStageError) as caught:
        with sources._publication_phase("remote_resolution"):
            sources._resolve_trusted_remote(tmp_path)
    reason = "authentication_failed" if status in {401, 403} else "failed"
    assert caught.value.reason == reason
    assert paths == (["/user"] if endpoint == "/user" else ["/user", "/repos/o/r"])
    assert "private-http-response" not in str(caught.value)
    assert "synthetic-fixture" not in str(caught.value)
    _assert_adapter(caught.value, reason)
