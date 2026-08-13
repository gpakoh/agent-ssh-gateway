"""Control-plane git operations for MCP write mode.

Credentials stay in the MCP control plane. SSH executors may create local
objects/branches/commits, but push happens here against the bind-mounted
project root using an ephemeral HTTPS credential boundary.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from tool_results import build_command_result, tool_error, tool_success

_GIT_TIMEOUT = 60
_GIT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*$")
_REDACTED = "***"
_PROTECTED_BRANCHES = {"main", "master"}


def _validate_name(value: str, field: str) -> str:
    if not value or not _GIT_NAME_RE.match(value):
        raise ValueError(f"INVALID_INPUT: {field} {value!r} is not a valid git remote/branch name")
    return value


def _resolve_project_root(project: str) -> Path:
    from app.workspace.registry import get_registry

    info = get_registry().project_info(project)
    return Path(info["root"])


def _redact_text(text: str, *, token: str, project_root: Path) -> str:
    if not text:
        return ""
    redacted = text.replace(token, _REDACTED)
    redacted = redacted.replace(str(project_root) + "/", "./")
    redacted = redacted.replace(str(project_root), ".")
    redacted = re.sub(r"(https?://)[^@\s/]+@", r"\1***@", redacted)
    redacted = re.sub(r"(https?://)[^:@\s/]+:[^@\s/]+@", r"\1***:***@", redacted)
    return redacted


def _run_git(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    base_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/usr/local/bin"),
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if env:
        base_env.update(env)
    return subprocess.run(
        ["git", "-c", f"safe.directory={cwd}", *argv[1:]] if argv and argv[0] == "git" else argv,
        cwd=str(cwd),
        env=base_env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _current_branch(cwd: Path) -> str:
    result = _run_git(["git", "branch", "--show-current"], cwd=cwd)
    branch = result.stdout.strip()
    if result.returncode != 0 or not branch:
        raise RuntimeError("GIT_DETACHED_HEAD")
    return branch


def _remote_url(cwd: Path, remote: str) -> str:
    result = _run_git(["git", "remote", "get-url", "--push", remote], cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError("GIT_REMOTE_NOT_ALLOWED")
    url = result.stdout.strip()
    if not url:
        raise RuntimeError("GIT_REMOTE_NOT_ALLOWED")
    return url


def _verify_local_branch(cwd: Path, branch: str) -> None:
    result = _run_git(["git", "rev-parse", "--verify", f"refs/heads/{branch}"], cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError("GIT_LOCAL_REF_MISSING")


def _parse_gitea_remote(remote_url: str) -> tuple[str, str, str]:
    if remote_url.startswith("git@") and ":" in remote_url:
        host_part, repo_part = remote_url.split(":", 1)
        host = host_part.split("@", 1)[1]
        path = repo_part
    elif remote_url.startswith("ssh://") or remote_url.startswith("http://") or remote_url.startswith("https://"):
        parsed = urlparse(remote_url)
        host = parsed.hostname or ""
        path = parsed.path.lstrip("/")
    else:
        raise RuntimeError("GIT_REMOTE_NOT_ALLOWED")

    path = path.removesuffix(".git")
    parts = path.split("/")
    if len(parts) != 2:
        raise RuntimeError("GIT_REMOTE_NOT_ALLOWED")
    owner, repo = parts

    allowed_hosts = set()
    forwarded_host = os.environ.get("GITEA_FORWARDED_HOST", "").strip()
    if forwarded_host:
        allowed_hosts.add(forwarded_host.split(":", 1)[0])
    api_base = os.environ.get("GITEA_API_BASE", "").strip()
    if api_base:
        api_host = urlparse(api_base).hostname
        if api_host:
            allowed_hosts.add(api_host)
    allowed_hosts.add("gitea")
    allowed_hosts.add("192.168.1.103")
    allowed_hosts.add("git.xloud.ru")

    if host not in allowed_hosts:
        raise RuntimeError("GIT_REMOTE_NOT_ALLOWED")
    return host, owner, repo


def _gitea_headers(token: str) -> dict[str, str]:
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/json",
        "User-Agent": "agent-ssh-gateway-mcp/1.0",
    }
    forwarded_host = os.environ.get("GITEA_FORWARDED_HOST", "").strip()
    forwarded_proto = os.environ.get("GITEA_FORWARDED_PROTO", "https").strip() or "https"
    if forwarded_host:
        headers["X-Forwarded-Host"] = forwarded_host
        headers["X-Forwarded-Proto"] = forwarded_proto
        headers["Host"] = forwarded_host
    return headers


def _gitea_get(path: str, *, token: str) -> dict:
    api_base = os.environ.get("GITEA_API_BASE", "").strip()
    if not api_base:
        raise RuntimeError("GIT_REMOTE_UNAVAILABLE")
    try:
        with httpx.Client(base_url=api_base, headers=_gitea_headers(token), timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            resp = client.get(path)
            if resp.status_code in (401, 403):
                raise RuntimeError("GIT_AUTH_FAILED")
            resp.raise_for_status()
            return resp.json()
    except RuntimeError:
        raise
    except httpx.TransportError as exc:
        raise RuntimeError("GIT_REMOTE_UNAVAILABLE") from exc
    except httpx.HTTPStatusError as exc:
        raise RuntimeError("GIT_PUSH_FAILED") from exc


def _repo_https_target(owner: str, repo: str, *, token: str) -> tuple[str, str]:
    user = _gitea_get("/user", token=token)
    repo_data = _gitea_get(f"/repos/{owner}/{repo}", token=token)
    username = str(user.get("login") or "").strip()
    clone_url = str(repo_data.get("clone_url") or "").strip()
    if not username or not clone_url:
        raise RuntimeError("GIT_PUSH_FAILED")
    return username, clone_url


def _classify_push_failure(stderr: str, stdout: str) -> str:
    text = f"{stderr}\n{stdout}".lower()
    if "authentication failed" in text or "access denied" in text or "forbidden" in text:
        return "GIT_AUTH_FAILED"
    if "non-fast-forward" in text or "fetch first" in text or "rejected" in text and "non-fast-forward" in text:
        return "GIT_NON_FAST_FORWARD"
    if "protected branch" in text or "protected" in text and "branch" in text:
        return "GIT_PROTECTED_BRANCH"
    return "GIT_PUSH_FAILED"


def _write_askpass() -> str:
    script = "#!/bin/sh\ncase \"$1\" in\n*Username*) printf '%s' \"$GIT_USERNAME\" ;;\n*Password*) printf '%s' \"$GIT_PASSWORD\" ;;\n*) exit 1 ;;\nesac\n"
    fd, path = tempfile.mkstemp(prefix="git-askpass-", suffix=".sh")
    os.close(fd)
    p = Path(path)
    p.write_text(script, encoding="utf-8")
    p.chmod(0o700)
    return path


def git_push_control_plane(project: str, remote: str = "origin", branch: str | None = None) -> dict[str, Any]:
    remote = _validate_name(remote, "remote")
    token = os.environ.get("GITEA_TOKEN", "").strip()
    if not token:
        return tool_error(tool="git_push", code="GIT_AUTH_FAILED", message="GITEA_TOKEN not configured", retryable=False, source="gitea")

    try:
        project_root = _resolve_project_root(project)
    except Exception:
        return tool_error(tool="git_push", code="PROJECT_NOT_FOUND", message="Project is not registered or unavailable.", retryable=False)

    try:
        branch_name = _current_branch(project_root) if branch is None else _validate_name(branch, "branch")
    except RuntimeError as exc:
        if str(exc) == "GIT_DETACHED_HEAD":
            return tool_error(tool="git_push", code="GIT_DETACHED_HEAD", message="Cannot push from detached HEAD.", retryable=False)
        raise

    if branch_name in _PROTECTED_BRANCHES:
        return tool_error(tool="git_push", code="GIT_PROTECTED_BRANCH", message=f"Pushing protected branch {branch_name!r} is not allowed.", retryable=False)

    try:
        _verify_local_branch(project_root, branch_name)
        remote_url = _remote_url(project_root, remote)
        _host, owner, repo = _parse_gitea_remote(remote_url)
        username, clone_url = _repo_https_target(owner, repo, token=token)
    except RuntimeError as exc:
        code = str(exc)
        messages = {
            "GIT_LOCAL_REF_MISSING": "Local branch/ref does not exist.",
            "GIT_REMOTE_NOT_ALLOWED": "Remote is invalid or not allowed for server-side push.",
            "GIT_AUTH_FAILED": "Authentication with Gitea failed.",
            "GIT_REMOTE_UNAVAILABLE": "Gitea API is unavailable.",
            "GIT_PUSH_FAILED": "Could not resolve push target from Gitea.",
        }
        return tool_error(tool="git_push", code=code if code in {"GIT_LOCAL_REF_MISSING", "GIT_REMOTE_NOT_ALLOWED", "GIT_AUTH_FAILED", "GIT_REMOTE_UNAVAILABLE", "GIT_PUSH_FAILED"} else "GIT_PUSH_FAILED", message=messages.get(code, "Server-side git push failed."), retryable=code == "GIT_REMOTE_UNAVAILABLE", source="gitea")

    askpass = _write_askpass()
    try:
        env = {
            "GIT_ASKPASS": askpass,
            "GIT_USERNAME": username,
            "GIT_PASSWORD": token,
            "GIT_TERMINAL_PROMPT": "0",
        }
        refspec = f"refs/heads/{branch_name}:refs/heads/{branch_name}"
        result = _run_git(["git", "push", clone_url, refspec], cwd=project_root, env=env, timeout=_GIT_TIMEOUT)
    except FileNotFoundError:
        return tool_error(tool="git_push", code="GIT_DEPENDENCY_MISSING", message="git binary not found in MCP control plane image.", retryable=False)
    except subprocess.TimeoutExpired:
        return tool_error(tool="git_push", code="TIMEOUT", message="git push timed out.", retryable=True)
    finally:
        Path(askpass).unlink(missing_ok=True)

    stdout = _redact_text(result.stdout, token=token, project_root=project_root)
    stderr = _redact_text(result.stderr, token=token, project_root=project_root)
    if result.returncode != 0:
        code = _classify_push_failure(stderr, stdout)
        retryable = code == "GIT_REMOTE_UNAVAILABLE"
        return tool_error(
            tool="git_push",
            code=code,
            message=f"git push failed: {stderr.strip() or stdout.strip() or 'unknown error'}",
            result=build_command_result(outcome="failed", exit_code=result.returncode, stdout=stdout, stderr=stderr),
            retryable=retryable,
            redacted=(stdout != result.stdout or stderr != result.stderr),
            source="gitea",
        )

    return tool_success(
        tool="git_push",
        result=build_command_result(outcome="passed", exit_code=0, stdout=stdout, stderr=stderr),
        redacted=(stdout != result.stdout or stderr != result.stderr),
        source="gitea",
    )
