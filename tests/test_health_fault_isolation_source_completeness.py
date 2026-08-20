"""Regression tests for Finding #31: source completeness in error paths.

Two sub-findings:
1. _run_git drops stderr on non-zero exit — callers never see the actual git
   diagnostic output (e.g. "error: pathspec 'x' did not match any files").
2. _bundle_head catches ManagedSourceBundleError and returns None, erasing
   the original error context. Callers cannot distinguish a corrupted bundle
   from a missing file from a permission error — the source of truth is lost.

These tests are RED on commit 5400dc2b (master) and should stay RED until
the source-completeness fix lands.
"""

from __future__ import annotations

import unittest.mock
from pathlib import Path

import pytest

from examples.mcp_server.agent_sources import (
    ManagedSourceBundleError,
    _bundle_head,
    _run_git,
)

# ---------------------------------------------------------------------------
# Sub-finding 1: _run_git non-zero exit must include stderr in the error
# ---------------------------------------------------------------------------


class TestRunGitPreservesStderr:
    """When a git subcommand fails, the error message must include the stderr
    output so callers can see the actual diagnostic (e.g. missing ref,
    permission denied, corrupt object).

    On 5400dc2b the error is:
        "managed source publication failed during git {subcmd}"
    with no stderr content — source completeness is broken.
    """

    def test_nonzero_exit_includes_stderr_in_error(self):
        """stderr from a failed git command must appear in the exception."""
        fake_result = unittest.mock.Mock(
            returncode=128,
            stdout="",
            stderr="fatal: not a valid object name: abc123",
        )
        with unittest.mock.patch("subprocess.run", return_value=fake_result):
            with pytest.raises(ManagedSourceBundleError, match="not a valid object name"):
                _run_git(["cat-file", "-e", "abc123^{commit}"])

    def test_nonzero_exit_preserves_stderr_even_when_stdout_empty(self):
        """Even with empty stdout, stderr must not be discarded."""
        fake_result = unittest.mock.Mock(
            returncode=1,
            stdout="",
            stderr="error: unknown option `--bogus'",
        )
        with unittest.mock.patch("subprocess.run", return_value=fake_result):
            with pytest.raises(ManagedSourceBundleError, match="unknown option"):
                _run_git(["--bogus", "rev-parse", "HEAD"])

    def test_nonzero_exit_preserves_stderr_for_bundle_create(self):
        """Bundle creation failure must surface git's diagnostic, not just the subcommand name."""
        fake_result = unittest.mock.Mock(
            returncode=128,
            stdout="",
            stderr="fatal: 'refs/heads/missing' does not point to a commit",
        )
        with unittest.mock.patch("subprocess.run", return_value=fake_result):
            with pytest.raises(
                ManagedSourceBundleError,
                match="does not point to a commit",
            ):
                _run_git(
                    ["--git-dir=/tmp/x", "bundle", "create", "out.bndl", "refs/heads/missing"]
                )


# ---------------------------------------------------------------------------
# Sub-finding 2: _bundle_head must not erase ManagedSourceBundleError context
# ---------------------------------------------------------------------------


class TestBundleHeadPreservesSourceContext:
    """_bundle_head currently catches ManagedSourceBundleError and returns None.
    Callers (ensure_managed_source_bundle) cannot tell whether the bundle is
    missing, corrupted, or git itself failed — the error context is gone.

    On 5400dc2b this is the case: _bundle_head returns None on ANY
    ManagedSourceBundleError, and the only caller treats None as "skip
    verification" rather than "source error — abort."
    """

    def test_bundle_head_returns_none_on_error(self):
        """Current behavior: _bundle_head swallows the error.

        This test documents the source-completeness gap: when _run_git
        raises ManagedSourceBundleError (e.g. git bundle list-heads fails),
        _bundle_head returns None instead of propagating the error. This
        means callers cannot distinguish 'bundle is valid but has multiple
        heads' from 'git crashed with a diagnostic we need to see'.
        """
        with pytest.raises(ManagedSourceBundleError, match="git bundle"):
            _bundle_head(Path("/nonexistent/bundle"))

    def test_bundle_head_error_message_preserves_stderr_context(self):
        """When _bundle_head fails, the propagated error must include
        the diagnostic context from the underlying _run_git call.
        """
        fake_result = unittest.mock.Mock(
            returncode=128,
            stdout="",
            stderr="fatal: '/tmp/x.bndl' does not look like a bundle file",
        )
        with unittest.mock.patch("subprocess.run", return_value=fake_result):
            with pytest.raises(
                ManagedSourceBundleError,
                match="does not look like a bundle file",
            ):
                _bundle_head(Path("/tmp/x.bndl"))
