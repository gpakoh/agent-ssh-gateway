"""Credential-safe local Git push helpers for the trusted MCP control plane.

The ordinary gateway ``git_push`` tool executes inside the SSH executor and
therefore inherits whatever credentials a checkout happens to contain.  This
module is deliberately different: it never reads a checkout remote, never
accepts a caller-provided URL, and never persists the Gitea token.  The token
is supplied only to one child ``git`` process through environment-backed Git
configuration.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from examples.mcp_client_remote.fleet.shared import validate_repo_owner_or_name

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_PROTECTED_BRANCHES = frozenset({"main", "master"})


class ManagedGitError(RuntimeError):
    """A sanitized managed-Git failure safe to expose through MCP."""


def validate_feature_branch(branch: str) -> str:
    branch = branch.strip()
    if (
        not branch
        or branch in _PROTECTED_BRANCHES
        or not _BRANCH_RE.fullmatch(branch)
        or ".." in branch
        or "//" in branch
        or branch.endswith("/")
        or branch.startswith("-")
    ):
        raise ValueError(f"Invalid or protected destination branch: {branch!r}")
    return branch


def validate_expected_sha(expected_sha: str) -> str:
    value = expected_sha.strip().lower()
    if not _SHA1_RE.fullmatch(value):
        raise ValueError("expected_sha must be a full 40-character lowercase Git SHA-1")
    return value


def configured_gitea_git_base() -> str:
    """Return a credential-free HTTPS Git origin from trusted configuration."""

    explicit = os.environ.get("GITEA_GIT_BASE", "").strip()
    if explicit:
        raw = explicit
    else:
        forwarded_host = os.environ.get("GITEA_FORWARDED_HOST", "").strip()
        forwarded_proto = os.environ.get("GITEA_FORWARDED_PROTO", "https").strip().lower()
        if not forwarded_host:
            raise ManagedGitError(
                "GITEA_GIT_BASE or GITEA_FORWARDED_HOST is required for managed Git push"
            )
        raw = f"{forwarded_proto}://{forwarded_host}"

    parsed = urlsplit(raw)
    if parsed.scheme != "https":
        raise ManagedGitError("managed Git push requires an HTTPS Gitea origin")
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ManagedGitError("managed Git origin must be a credential-free HTTPS origin")
    if parsed.path not in {"", "/"}:
        raise ManagedGitError("managed Git origin must not contain a path prefix")
    return urlunsplit(("https", parsed.netloc, "", "", "")).rstrip("/")


def _minimal_git_env(username: str, token: str) -> dict[str, str]:
    """Build the one-shot environment used by ``git push``.

    The Basic-auth header exists only in the child environment.  It is never
    written to .git/config, a credential helper, a URL, or argv.  Redirects
    are disabled so Git cannot forward that header to a different host.
    """

    encoded = base64.b64encode(f"{username}:{token}".encode()).decode("ascii")
    env: dict[str, str] = {}
    for key in (
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTPS_PROXY",
        "https_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        value = os.environ.get(key)
        if value:
            env[key] = value
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
            "GIT_CONFIG_KEY_1": "http.followRedirects",
            "GIT_CONFIG_VALUE_1": "false",
            "GIT_CONFIG_KEY_2": "credential.helper",
            "GIT_CONFIG_VALUE_2": "",
        }
    )
    return env


def push_exact_sha(
    *,
    project_root: str | Path,
    owner: str,
    repo: str,
    destination_branch: str,
    expected_sha: str,
    username: str,
    token: str,
    git_base: str,
    timeout: int = 60,
) -> None:
    """Push exactly ``expected_sha`` from one registered local repository."""

    owner = validate_repo_owner_or_name(owner, label="owner")
    repo = validate_repo_owner_or_name(repo, label="repo")
    destination_branch = validate_feature_branch(destination_branch)
    expected_sha = validate_expected_sha(expected_sha)
    if not username or not token:
        raise ManagedGitError("managed Git credentials are not configured")

    root = Path(project_root).resolve()
    if not root.is_dir():
        raise ManagedGitError("registered project root does not exist")

    remote = f"{git_base.rstrip('/')}/{owner}/{repo}.git"
    with tempfile.TemporaryDirectory(prefix="mcp-managed-git-") as tmp:
        staging = Path(tmp) / "repo"
        clean_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": tmp,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
        }
        try:
            cloned = subprocess.run(
                [
                    "git",
                    "clone",
                    "--local",
                    "--no-hardlinks",
                    "--no-checkout",
                    str(root),
                    str(staging),
                ],
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
                env=clean_env,
            )
            if cloned.returncode != 0:
                raise ManagedGitError("failed to stage registered Git repository")

            resolved = subprocess.run(
                ["git", "rev-parse", "--verify", f"{expected_sha}^{{commit}}"],
                cwd=staging,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
                env=clean_env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ManagedGitError("failed to stage exact local Git object") from exc
        if resolved.returncode != 0 or resolved.stdout.strip().lower() != expected_sha:
            raise ManagedGitError("expected_sha is not the exact staged commit object")

        auth_env = _minimal_git_env(username, token)
        auth_env["HOME"] = tmp
        auth_env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        try:
            pushed = subprocess.run(
                [
                    "git",
                    "push",
                    "--porcelain",
                    remote,
                    f"{expected_sha}:refs/heads/{destination_branch}",
                ],
                cwd=staging,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=auth_env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ManagedGitError("managed Git push did not complete") from exc
        if pushed.returncode != 0:
            # Never surface raw stdout/stderr: Git can echo remote URLs and auth
            # diagnostics. The caller gets only a stable, credential-free error.
            raise ManagedGitError(
                f"managed Git push failed with exit code {pushed.returncode}"
            )
