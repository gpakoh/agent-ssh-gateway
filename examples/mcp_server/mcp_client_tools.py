"""ChatGPT-safe high-level MCP tools.

These tools avoid exposing a generic SSH execute surface to ChatGPT
by wrapping fixed allowlisted commands in semantic functions.
"""

from __future__ import annotations

import errno
import fnmatch
import hashlib
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from control_plane_git import git_fetch_ref_control_plane, git_push_control_plane
from gateway_client import GatewayClient, GatewayClientError, _script_stdin_wrapper
from git_write_capabilities import probe_git_refresh_worktree_paths, probe_git_write_capabilities
from tool_results import build_command_result, tool_error, tool_success


def _resolve_project(project_name: str) -> Path:
    """Resolve a project name to a validated filesystem path via the workspace registry."""
    try:
        from app.workspace.registry import get_registry
        from app.workspace_policy import WorkspacePolicyError

        info = get_registry().project_info(project_name)
        if info.get("type") == "coordination-only":
            raise WorkspacePolicyError("Gitea-only project has no local workspace")
        return Path(info["root"])
    except WorkspacePolicyError as exc:
        body = {"detail": {"code": "PROJECT_NOT_FOUND", "retryable": False}}
        raise GatewayClientError(str(exc), status_code=404, body=body) from exc
    except Exception as exc:
        body = {"detail": {"code": "INTERNAL_ERROR", "retryable": False}}
        raise GatewayClientError(str(exc), status_code=500, body=body) from exc


def _redact_project_root(text: str | None, project_dir: str) -> str:
    """Replace the host project-root prefix in tool output with '.'.

    Runner fallbacks pass absolute host paths (mypy cannot read a relative
    path that resolves through a symlink), so pytest/mypy/ruff echo
    <repo-root>/... in stdout/stderr while info/tree keep ``root: "."``.
    Redaction here keeps the same host-layout invariant for execution tools.
    """
    if not text or not project_dir:
        return text or ""
    root = str(project_dir).rstrip("/")
    if not root or root == "/":
        return text
    redacted = text.replace(f"{root}/", "./").replace(root, ".")
    return redacted


def _validate_project(project: str) -> str:
    if not project:
        raise ValueError("Project name must not be empty")
    if not re.match(r"^[A-Za-z0-9._\-\/]+$", project):
        raise ValueError(f"Invalid project name: {project!r}")
    if ".." in project.split("/"):
        raise ValueError(f"Path traversal blocked: {project!r}")
    return project.strip("/")


# Valid inclusive range for list_tree()/tree()'s depth argument.
DEPTH_MIN = 1
DEPTH_MAX = 5


def _validate_depth(depth: int) -> int:
    """Validate depth is an int in [DEPTH_MIN, DEPTH_MAX]. Raises ValueError otherwise.

    Never silently clamps out-of-range input — a caller passing depth=0 or
    depth=-1 gets a clear validation error instead of a silently substituted
    depth=1, which would otherwise look like a successful call at the wrong depth.
    """
    if isinstance(depth, bool) or not isinstance(depth, int):
        raise ValueError(f"depth must be an integer between {DEPTH_MIN} and {DEPTH_MAX}, got {depth!r}")
    if not (DEPTH_MIN <= depth <= DEPTH_MAX):
        raise ValueError(f"depth must be between {DEPTH_MIN} and {DEPTH_MAX}, got {depth}")
    return depth


# ── Safety helpers ──────────────────────────────────────────────

_OUTPUT_LINE_LIMIT = 2000
_ALLOWED_PATH_RE = re.compile(r"^[a-zA-Z0-9_./\[\]-]+$")
_ALLOWED_GLOB_RE = re.compile(r"^[a-zA-Z0-9_./*?\[\]-]+$")


def _safe_relpath(path: str) -> str:
    if not path:
        raise ValueError("path is required")
    if path.startswith("/"):
        raise ValueError(f"absolute path not allowed: {path!r}")
    if ".." in path.split("/"):
        raise ValueError(f"path traversal not allowed: {path!r}")
    if not _ALLOWED_PATH_RE.match(path):
        raise ValueError(f"invalid characters in path: {path!r}")
    return path


def _validate_glob_pattern(glob: str) -> str:
    """Validate a glob pattern for tree(): wildcards (* ? [...]) are allowed,
    but shell metacharacters, absolute paths and traversal are not."""
    if not glob:
        raise ValueError("glob is required")
    if glob.startswith("/"):
        raise ValueError(f"absolute glob not allowed: {glob!r}")
    if ".." in glob.split("/"):
        raise ValueError(f"path traversal not allowed in glob: {glob!r}")
    if any(ch in glob for ch in ";|`$"):
        raise ValueError(f"shell metacharacters not allowed in glob: {glob!r}")
    if not _ALLOWED_GLOB_RE.match(glob):
        raise ValueError(f"invalid characters in glob: {glob!r}")
    return glob


def _safe_test_target(target: str) -> str:
    if not target:
        raise ValueError("target is required")
    if target.startswith("/"):
        raise ValueError(f"absolute target not allowed: {target!r}")
    if ".." in target.split("/"):
        raise ValueError(f"path traversal not allowed: {target!r}")
    if ";" in target or "|" in target or "`" in target or "$" in target:
        raise ValueError(f"shell metacharacters not allowed: {target!r}")
    return target


def _execution_duration_ms(raw: dict[str, Any]) -> int | None:
    """Extract job duration in milliseconds from a raw job result.

    Regression: the gateway's /api/jobs/{id}/wait endpoint returns
    job.to_dict(), which carries ``duration`` in SECONDS and no
    ``execution_duration_ms`` key at all — only the polling fallback in
    GatewayClient.wait_job() converted the units. Every MCP tool that
    built build_command_result(execution_duration_ms=raw.get(
    "execution_duration_ms")) therefore reported null for run_pytest /
    run_ruff / run_mypy and friends, even though meta.duration_ms was set.
    This helper mirrors the fallback's conversion for all call sites.
    """
    ms = raw.get("execution_duration_ms")
    if ms is not None:
        return int(ms)
    seconds = raw.get("duration")
    if seconds is not None:
        return int(seconds * 1000)
    return None


# ── uv runner helpers ───────────────────────────────────────────

_UV_TOOL_MAP: dict[str, list[str]] = {
    "ruff": ["ruff", "check"],
    "mypy": ["mypy"],
    "pytest": ["pytest"],
}

# compileall is not uvx-installable, so it is handled separately (Python
# walk below) instead of living in _UV_TOOL_MAP.

# Directories pruned at the walk level when compiling a tree. The stdlib
# compileall -x regex only filters files — it still descends into .git,
# .venv (and nested venvs), caches and node_modules, wasting I/O on
# thousands of files. Pruning the dirs list avoids the descent entirely.
_COMPILEALL_EXCLUDED_DIRS = frozenset({
    ".git",
    ".ai-bridge",
    ".hypothesis",
    ".state",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".benchmarks",
})

_COMPILEALL_WALK_TEMPLATE = """\
import compileall
import os
import py_compile
import sys

_EXCLUDED = {excluded}
_LIMIT = {limit}

def _compile_one(path):
    try:
        ok = compileall.compile_file(path, quiet=1)
    except compileall.PyCompileError:
        ok = False
    if not ok:
        try:
            py_compile.compile(path, doraise=True)
        except py_compile.PyCompileError as exc:
            return False, "%s: %s" % (path, exc)
        return False, "%s: failed to compile" % path
    return True, "Compiling '%s'..." % path

def _main() -> int:
    out = []
    failed = 0
    skipped = 0
    for root in sys.argv[1:]:
        if os.path.isfile(root):
            ok, line = _compile_one(root)
            if not ok:
                failed += 1
            if len(out) < _LIMIT:
                out.append(line)
            else:
                skipped += 1
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames if d not in _EXCLUDED and not d.endswith(".egg-info")
            )
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                ok, line = _compile_one(path)
                if not ok:
                    failed += 1
                if len(out) < _LIMIT:
                    out.append(line)
                else:
                    skipped += 1
    if skipped:
        out.append("[... output truncated: %d more lines not shown ...]" % skipped)
    if out:
        sys.stdout.write("\\n".join(out) + "\\n")
    return 1 if failed else 0

sys.exit(_main())
"""


def _build_compileall_walk_code() -> str:
    """Inline Python that walks targets pruning service dirs and caps output.

    Runs on the SSH target via ``python -c`` (compileall is stdlib, no
    uvx package), so the excluded-dir list and the line limit are baked in
    at build time.
    """
    return _COMPILEALL_WALK_TEMPLATE.format(
        excluded=repr(sorted(_COMPILEALL_EXCLUDED_DIRS)),
        limit=_OUTPUT_LINE_LIMIT,
    )


def _compileall_command() -> list[str]:
    return ["python3", "-c", _build_compileall_walk_code()]


def _validate_targets(project_dir: str, targets: list[str]) -> list[str]:
    """Validate all targets: relative, no traversal, inside project root."""
    root = Path(project_dir).resolve()
    validated = []
    for t in targets:
        p = Path(t)
        if p.is_absolute():
            t = str(Path(*p.parts[1:])) if len(p.parts) > 1 else "."
            p = Path(t)
        if ".." in p.parts:
            raise ValueError(f"POLICY_DENIED: path traversal in target: {t}")
        resolved = (root / p).resolve()
        resolved.relative_to(root)
        validated.append(t)
    return validated


_VERIFIER_ROOT_ENV = "MCP_VERIFIER_ROOT"
_VERIFIER_ROOT_DEFAULT = "/var/lib/mcp-agent/verifier"


def _verifier_project_cache_root(project_dir: str) -> str:
    """Return the server-owned cache root for one project."""
    return _verifier_project_root(project_dir) + "/cache"


def _verifier_root_config() -> str:
    """Return the configured absolute server-owned verifier root."""
    raw = os.environ.get(_VERIFIER_ROOT_ENV, "").strip()
    value = raw or _VERIFIER_ROOT_DEFAULT
    if "\x00" in value:
        raise ValueError(f"{_VERIFIER_ROOT_ENV} contains a NUL byte")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{_VERIFIER_ROOT_ENV} must be an absolute executor path")
    if str(path) == "/":
        raise ValueError(f"{_VERIFIER_ROOT_ENV} must not be filesystem root")
    return str(path).rstrip("/")


def _verifier_project_root(project_dir: str) -> str:
    """Return the stable per-project verifier root."""
    proj = str(Path(project_dir).resolve())
    ident = hashlib.sha1(proj.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(proj).name) or "project"
    return f"{_verifier_root_config()}/{slug}-{ident}"


def _verifier_root_bootstrap(root_abs: str) -> list[str]:
    """Validate the deployment-provisioned verifier root fail-closed."""
    q = shlex.quote(root_abs)
    return [
        f"VR={q}",
        'CUR="$(dirname "$VR")"',
        'while [ "$CUR" != "/" ] && [ "$CUR" != "." ]; do',
        '  if [ -L "$CUR" ]; then',
        '    echo "VERIFIER_ROOT_UNSAFE: symlink in trusted path: $CUR" >&2',
        '    exit 90',
        '  fi',
        '  if [ -d "$CUR" ]; then',
        '    _p000=000',
        '    _p000="$(stat -c %a "$CUR" 2>/dev/null || echo 000)"',
        '    if [ $(( 0$_p000 & 18 )) -ne 0 ]; then',
        '      echo "VERIFIER_ROOT_UNSAFE: writable directory in trusted path: $CUR" >&2',
        '      exit 90',
        '    fi',
        '  fi',
        '  CUR="$(dirname "$CUR")"',
        'done',
        'if [ ! -d "$VR" ] || [ -L "$VR" ]; then',
        '  echo "VERIFIER_ROOT_UNSAFE: not a real directory: $VR" >&2',
        '  exit 90',
        'fi',
        'if [ "$(stat -c %U "$VR" 2>/dev/null)" != "$(id -un)" ]; then',
        '  echo "VERIFIER_ROOT_UNSAFE: wrong owner for $VR" >&2',
        '  exit 90',
        'fi',
        'perms=000',
        'perms="$(stat -c %a "$VR" 2>/dev/null || echo 000)"',
        'if [ $(( 0$perms & 18 )) -ne 0 ]; then',
        '  echo "VERIFIER_ROOT_UNSAFE: unsafe permissions on $VR" >&2',
        '  exit 90',
        'fi',
    ]


def _verifier_project_prologue(proj_root_abs: str) -> list[str]:
    """Create and lock one per-project verifier subroot safely."""
    q = shlex.quote(proj_root_abs)
    return [
        f"PPROJ={q}",
        'if [ -L "$PPROJ" ]; then',
        '  echo "VERIFIER_ROOT_UNSAFE: per-project root is a symlink: $PPROJ" >&2',
        '  exit 90',
        'fi',
        'if [ ! -e "$PPROJ" ]; then',
        '  mkdir -p "$PPROJ"',
        'fi',
        'if [ ! -d "$PPROJ" ]; then',
        '  echo "VERIFIER_ROOT_UNSAFE: per-project root is not a directory: $PPROJ" >&2',
        '  exit 90',
        'fi',
        'LOCK="$PPROJ/.sync.lock"',
        'exec 9>"$LOCK"',
        'flock 9',
    ]


def _verifier_isolation_env(project_dir: str, tool_key: str) -> list[str]:
    """Build the env prefix that keeps verifier state outside source."""
    _ = tool_key
    venv_dir = f"{_verifier_project_root(project_dir)}/.venv"
    return [
        "env",
        f"UV_PROJECT_ENVIRONMENT={venv_dir}",
        "PYTHONPYCACHEPREFIX=" + _verifier_project_cache_root(project_dir) + "/pycache",
    ]


def _tool_cache_args(tool: str, project_dir: str) -> list[str]:
    """Redirect tool caches into the per-project verifier root."""
    cache_root = _verifier_project_cache_root(project_dir)
    if tool == "pytest":
        return ["-o", "cache_dir=" + cache_root + "/pytest"]
    if tool == "ruff":
        return ["--cache-dir", cache_root + "/ruff"]
    if tool == "mypy":
        return ["--cache-dir", cache_root + "/mypy"]
    return []


_ENV_FAIL_EXIT_CODES = frozenset({2, 3, 6, 90, 127})


def _classify_env_failure(stderr: str | None, exit_code: int | None) -> str | None:
    """Classify only bootstrap failures, never a real check failure."""
    if not stderr or exit_code is None or exit_code not in _ENV_FAIL_EXIT_CODES:
        return None
    low = stderr.lower()
    env_markers = (
        ".venv/.lock",
        "failed to create virtualenv",
        "failed to remove virtualenv",
        "cannot create virtualenv",
        "error: virtualenv",
        "failed to remove directory",
        "read-only file system",
        "uv: error",
        "verifier_root_unsafe",
    )
    if "verifier_root_unsafe" in low:
        return "VERIFIER_BOOTSTRAP_FAILED"
    return "VERIFIER_ENV_UNAVAILABLE" if any(marker in low for marker in env_markers) else None


def _uv_run_args(tool: str, project_dir: str, targets: list[str]) -> list[str]:
    """Build the uv command arguments for a verifier run."""
    if tool not in _UV_TOOL_MAP and tool != "compileall":
        raise ValueError(f"INVALID_INPUT: unknown tool '{tool}'")
    if not targets:
        raise ValueError("INVALID_INPUT: at least one target required")
    validated = _validate_targets(project_dir, targets)
    cmd = _compileall_command() if tool == "compileall" else _UV_TOOL_MAP[tool]
    uv_args = (
        ["uv", "run", "--no-project"]
        if tool == "compileall"
        else ["uv", "run", "--frozen", "--all-extras"]
    )
    return uv_args + ["--directory", project_dir, "--"] + cmd + _tool_cache_args(tool, project_dir) + ["--"] + validated


def _build_uv_argv(tool: str, project_dir: str, targets: list[str]) -> list[str]:
    """Build the legacy direct argv shape used by compatibility callers/tests.

    Live verifier execution uses ``_build_primary_script`` so trust-boundary
    bootstrap/locking is never represented as a bare argv-only contract.
    """
    if tool not in _UV_TOOL_MAP and tool != "compileall":
        raise ValueError(f"INVALID_INPUT: unknown tool '{tool}'")
    if not targets:
        raise ValueError("INVALID_INPUT: at least one target required")
    validated = _validate_targets(project_dir, targets)
    cmd = _compileall_command() if tool == "compileall" else _UV_TOOL_MAP[tool]
    uv_args = (
        ["uv", "run", "--no-project"]
        if tool == "compileall"
        else ["uv", "run", "--frozen", "--all-extras"]
    )
    return uv_args + ["--directory", project_dir, "--"] + cmd + ["--"] + validated


def _build_primary_script(tool: str, project_dir: str, targets: list[str]) -> str:
    """Build a primary verifier script with trusted-root validation and locking."""
    validated = _validate_targets(project_dir, targets)
    proj = str(Path(project_dir))
    base = _verifier_root_config()
    proj_root = _verifier_project_root(project_dir)
    venv = f"{proj_root}/.venv"
    target_args = " ".join(shlex.quote(t) for t in validated)
    lines = ["set -eu"]
    lines += _verifier_root_bootstrap(base)
    lines += _verifier_project_prologue(proj_root)
    lines += [
        f"export UV_PROJECT_ENVIRONMENT={shlex.quote(venv)}",
        f"export PYTHONPYCACHEPREFIX={shlex.quote(_verifier_project_cache_root(project_dir) + '/pycache')}",
    ]
    if tool == "compileall":
        walk = _build_compileall_walk_code()
        # Syntax-only compilation needs no project venv, uv.lock or native
        # dependencies. Keep the trusted verifier-root lock and external
        # PYTHONPYCACHEPREFIX while letting uv provide its managed Python.
        run = (
            f"uv run --no-project --directory {shlex.quote(proj)} -- "
            f"python3 -c {shlex.quote(walk)} -- {target_args}"
        )
    else:
        cmd_s = " ".join(shlex.quote(a) for a in _UV_TOOL_MAP[tool])
        cache_s = " ".join(shlex.quote(a) for a in _tool_cache_args(tool, project_dir))
        run = (
            f"uv run --frozen --all-extras --directory {shlex.quote(proj)} -- "
            f"{cmd_s} {cache_s} -- {target_args}"
        )
    lines.append(run + " 2>&1")
    lines.append("flock -u 9")
    return "\n".join(lines)


def _build_uvx_argv(tool: str, targets: list[str]) -> list[str]:
    """Build argv list for ``uvx`` fallback (no project venv needed)."""
    if tool not in _UV_TOOL_MAP:
        raise ValueError(f"INVALID_INPUT: unknown tool '{tool}'")
    if not targets:
        raise ValueError("INVALID_INPUT: at least one target required")
    validated = _validate_targets(".", targets)
    cmd = _UV_TOOL_MAP[tool]
    return ["uvx", "--from", tool] + cmd + ["--"] + validated


def _map_uv_exit_code(tool: str, exit_code: int) -> tuple[str | None, str | None]:
    """Map uv exit code to (outcome, error_code).

    Returns ``(outcome, None)`` for success/check-failed and
    ``(None, error_code)`` for infrastructure errors.
    """
    if tool == "pytest":
        if exit_code == 0:
            return ("passed", None)
        elif exit_code == 1:
            return ("failed", None)
        elif exit_code == 5:
            return ("failed", None)  # NO_TESTS
        else:
            return (None, "TOOL_EXECUTION_FAILED")

    if exit_code == 0:
        return ("passed", None)
    elif exit_code == 1:
        return ("failed", None)
    else:
        return (None, "TOOL_EXECUTION_FAILED")


# ── Basic read-only helpers ─────────────────────────────────────


def run_readonly_command(
    client: GatewayClient, command: str, session_id: str | None = None
) -> dict[str, Any]:
    job = client.execute_restricted(command, session_id=session_id)
    raw = client.wait_job(job["job_id"])
    return build_command_result(
        outcome="passed" if raw.get("exit_code", 1) == 0 else "failed",
        exit_code=raw.get("exit_code", -1),
        stdout=raw.get("stdout") or raw.get("output", ""),
        stderr=raw.get("stderr", ""),
        execution_duration_ms=_execution_duration_ms(raw),
        job_id=job.get("job_id"),
    )


# ── Project file tools ──────────────────────────────────────────


def read_file(
    client: GatewayClient,
    project: str,
    path: str,
) -> dict[str, Any]:
    """Read a UTF-8 text file inside a registered project with secret/hidden path filtering."""
    from app.workspace.files import project_file_read
    from app.workspace.registry import get_registry
    from app.workspace_policy import (
        FileNotFoundInWorkspaceError,
        HiddenPathError,
        WorkspacePolicyError,
    )

    safe = _safe_relpath(path)
    try:
        result = project_file_read(
            project_id=project,
            relative_path=safe,
            registry=get_registry(),
        )
        return tool_success(tool="read_file", result=result)
    except HiddenPathError as exc:
        return tool_error(
            tool="read_file",
            code="SECRET_PATH_DENIED",
            message=str(exc),
            retryable=False,
        )
    except FileNotFoundInWorkspaceError as exc:
        return tool_error(
            tool="read_file",
            code="FILE_NOT_FOUND",
            message=str(exc),
            retryable=False,
        )
    except WorkspacePolicyError as exc:
        return tool_error(
            tool="read_file",
            code="POLICY_DENIED",
            message=str(exc),
            retryable=False,
        )
    except Exception as exc:
        return tool_error(
            tool="read_file",
            code="FILE_READ_ERROR",
            message=str(exc),
            retryable=False,
        )


def search_text(
    client: GatewayClient,
    project: str,
    query: str,
    glob: str | None = None,
) -> dict[str, Any]:
    """Search for text across project files using pure Python pathlib — no shell execution."""
    _validate_project(project)
    project_dir = _resolve_project(project)
    if not project_dir.is_dir():
        raise ValueError(f"Project directory not found: {project_dir}")

    from app.services.project_search import search_text

    return search_text(
        root=project_dir,
        query=query,
        glob=glob,
    )


EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".ai-bridge",
        ".hypothesis",
        ".state",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".benchmarks",
    }
)
MAX_GLOB_RESULTS = 200
MAX_GLOB_DEPTH = 20
GLOB_TIMEOUT_S = 5


def _is_excluded_runtime_name(name: str) -> bool:
    return name in EXCLUDE_DIRS or name.endswith(".egg-info")


def _match_glob_parts(path_parts: tuple[str, ...], pattern_parts: tuple[str, ...]) -> bool:
    if not pattern_parts:
        return not path_parts
    head = pattern_parts[0]
    if head == "**":
        return _match_glob_parts(path_parts, pattern_parts[1:]) or (
            bool(path_parts)
            and _match_glob_parts(path_parts[1:], pattern_parts)
        )
    return bool(path_parts) and fnmatch.fnmatchcase(path_parts[0], head) and _match_glob_parts(
        path_parts[1:], pattern_parts[1:]
    )


def _glob_path_matches(relative_path: Path, pattern: str, *, rglob: bool = False) -> bool:
    pattern_parts = tuple(part for part in pattern.split("/") if part not in {"", "."})
    if rglob:
        pattern_parts = ("**", *pattern_parts)
    return _match_glob_parts(relative_path.parts, pattern_parts)


def _iter_pruned_paths(project_dir: Path, *, max_depth: int | None = None):
    """Walk a project without ever descending into generated/runtime trees."""
    root = project_dir.resolve()
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(dirpath)
        rel_dir = current.relative_to(root)
        current_depth = 0 if rel_dir == Path(".") else len(rel_dir.parts)
        entry_depth = current_depth + 1

        allowed_dirs = sorted(
            name for name in dirnames if not _is_excluded_runtime_name(name)
        )
        if max_depth is None or entry_depth <= max_depth:
            for name in allowed_dirs:
                yield current / name
            for name in sorted(filenames):
                if not _is_excluded_runtime_name(name):
                    yield current / name

        if max_depth is not None and entry_depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = allowed_dirs


def _safe_glob(
    project_dir: Path,
    pattern: str,
    max_results: int = MAX_GLOB_RESULTS,
) -> dict[str, Any]:
    """Safe glob: returns {"files": [...], "count": N, "truncated": bool}."""
    if pattern.startswith("/"):
        raise ValueError("POLICY_DENIED: absolute pattern not allowed")
    if ".." in Path(pattern).parts:
        raise ValueError("POLICY_DENIED: path traversal in pattern")

    project_root = project_dir.resolve()
    results: list[str] = []
    start = time.monotonic()
    timed_out = False

    for path in _iter_pruned_paths(project_root):
        if time.monotonic() - start > GLOB_TIMEOUT_S:
            timed_out = True
            break
        structural_rel = path.relative_to(project_root)
        if not _glob_path_matches(structural_rel, pattern):
            continue
        try:
            resolved = path.resolve()
            rel = resolved.relative_to(project_root)
        except (OSError, ValueError):
            continue
        if not resolved.is_file():
            continue
        if len(rel.parts) > MAX_GLOB_DEPTH:
            continue
        results.append(str(structural_rel))
        if len(results) >= max_results:
            break

    results.sort()
    return {
        "files": results,
        "count": len(results),
        "truncated": timed_out or len(results) >= max_results,
    }


def find_files(
    project: str,
    pattern: str,
) -> dict[str, Any]:
    project_dir = _resolve_project(project)
    try:
        glob_result = _safe_glob(project_dir, pattern)
    except ValueError as e:
        code, msg = str(e).split(":", 1) if ":" in str(e) else ("INVALID_INPUT", str(e))
        return tool_error(
            "find_files",
            code=code.strip(),
            message=msg.strip(),
            tool_name="find_files",
        )

    result = {
        "pattern": pattern,
        "files": glob_result["files"],
        "count": glob_result["count"],
    }
    meta = tool_success("find_files", result, tool_name="find_files")["meta"]
    meta["truncated"] = glob_result.get("truncated", False)
    return {
        "ok": True,
        "result": result,
        "error": None,
        "meta": meta,
    }


def list_files(client: GatewayClient, project: str, pattern: str) -> dict[str, Any]:
    """Find files by glob pattern using Python pathlib — no shell execution."""
    _validate_project(project)
    if not pattern or ".." in pattern:
        raise ValueError(f"Invalid pattern: {pattern!r}")

    project_dir = _resolve_project(_validate_project(project))

    files: list[str] = []
    start = time.monotonic()
    timed_out = False
    project_root = project_dir.resolve()
    for p in _iter_pruned_paths(project_root):
        if time.monotonic() - start > GLOB_TIMEOUT_S:
            timed_out = True
            break
        structural_rel = p.relative_to(project_root)
        if not _glob_path_matches(structural_rel, pattern, rglob=True):
            continue
        # Resolve before returning a path so a symlinked file cannot escape
        # the registered project root. os.walk(followlinks=False) already
        # prevents descent through symlinked directories.
        try:
            p.resolve().relative_to(project_root)
        except (OSError, ValueError):
            continue
        if p.is_file():
            files.append(str(structural_rel))

    total = len(files)
    files.sort()
    files = files[:200]
    truncated = total > 200 or timed_out

    return {
        "project": project,
        "pattern": pattern,
        # "." represents the project's own root in the client-facing,
        # project-relative namespace — never the real host filesystem path.
        # See _resolve_project()/project (the logical id) for the caller's
        # own reference to "which project", without exposing server layout.
        "root": ".",
        "files": files,
        "count": len(files),
        "truncated": truncated,
    }



def list_tree(
    client: GatewayClient,
    project: str,
    depth: int = 2,
    max_results: int = MAX_GLOB_RESULTS,
) -> dict[str, Any]:
    """List project directory tree using Python pathlib — no shell execution.

    depth must be an integer between DEPTH_MIN and DEPTH_MAX (inclusive);
    out-of-range values raise ValueError rather than being silently clamped.
    """
    depth = _validate_depth(depth)
    project = _validate_project(project)
    project_dir = _resolve_project(project)

    entries: list[str] = []
    for p in sorted(_iter_pruned_paths(project_dir, max_depth=depth)):
        rel = p.relative_to(project_dir)
        suffix = "/" if p.is_dir() else ""
        entries.append(f"{rel}{suffix}")

    total = len(entries)
    entries = entries[:max_results]

    return {
        "project": project,
        # "." represents the project's own root in the client-facing,
        # project-relative namespace — never the real host filesystem path.
        # See _resolve_project()/project (the logical id) for the caller's
        # own reference to "which project", without exposing server layout.
        "root": ".",
        "depth": depth,
        "entries": entries,
        "count": len(entries),
        "truncated": total > max_results,
    }


def tree(
    client: GatewayClient,
    project: str,
    depth: int = 2,
    glob: str | None = None,
    max_results: int = MAX_GLOB_RESULTS,
) -> dict[str, Any]:
    """List project directory tree using Python pathlib — no shell execution.

    depth must be an integer between DEPTH_MIN and DEPTH_MAX (inclusive);
    out-of-range values raise ValueError rather than being silently clamped.
    """
    if glob:
        glob = _validate_glob_pattern(glob)
    depth = _validate_depth(depth)
    project = _validate_project(project)
    project_dir = _resolve_project(project)

    entries: list[str] = []
    for p in sorted(_iter_pruned_paths(project_dir, max_depth=depth)):
        rel = p.relative_to(project_dir)
        suffix = "/" if p.is_dir() else ""
        name = str(rel)
        if glob and not p.match(glob):
            continue
        entries.append(f"{name}{suffix}")

    total = len(entries)
    entries = entries[:max_results]

    return {
        "project": project,
        # "." represents the project's own root in the client-facing,
        # project-relative namespace — never the real host filesystem path.
        # See _resolve_project()/project (the logical id) for the caller's
        # own reference to "which project", without exposing server layout.
        "root": ".",
        "depth": depth,
        "entries": entries,
        "count": len(entries),
        "truncated": total > max_results,
    }


# ── Project git diff tools ──────────────────────────────────────


def _truncate_stdout(result: dict[str, Any], max_lines: int = 500) -> dict[str, Any]:
    if "stdout" in result:
        lines = result["stdout"].splitlines(keepends=True)
        if len(lines) > max_lines:
            result["stdout"] = "".join(lines[:max_lines])
            result["stdout_truncated"] = True
            result["total_lines"] = len(lines)
    return result


def git_diff(
    client: GatewayClient,
    project: str,
    path: str | None = None,
) -> dict[str, Any]:
    cmd = "git diff --no-color"
    if path:
        cmd += f" -- {_safe_relpath(path)}"
    return _truncate_stdout(run_project_command(client, project, cmd))


def git_diff_cached(
    client: GatewayClient,
    project: str,
    path: str | None = None,
) -> dict[str, Any]:
    cmd = "git diff --cached --no-color"
    if path:
        cmd += f" -- {_safe_relpath(path)}"
    return _truncate_stdout(run_project_command(client, project, cmd))


def show_file_diff(
    client: GatewayClient,
    project: str,
    path: str,
) -> dict[str, Any]:
    safe = _safe_relpath(path)
    cmd = f"git diff --no-color -- {safe}"
    return _truncate_stdout(run_project_command(client, project, cmd))


# ── Project test target tools ───────────────────────────────────


def _build_readonly_fallback_script(
    tool_key: str,
    project_dir: str,
    targets: list[str],
) -> str:
    """Build a check script whose mutable environment lives outside source.

    A read-only source checkout must stay the authoritative project root: test
    discovery, config lookup, package-base inference, and relative imports all
    depend on running checks from the real checkout.  Only the uv environment,
    tool caches, and dependency-resolution state live under the server-owned
    verifier root.

    When ``uv.lock`` exists, uv syncs the *real* project with ``--frozen`` and
    ``UV_PROJECT_ENVIRONMENT`` pointing at the external venv.  This preserves
    editable/project installation semantics without creating ``.venv`` or
    lock/cache files in source.  For projects without a lockfile, dependency
    metadata is copied to the temp root and synced with ``--no-install-project``
    so uv is never allowed to create a new lockfile inside the read-only repo.
    """
    project_hash = hashlib.sha1(project_dir.encode("utf-8")).hexdigest()[:8]
    project_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(project_dir).name) or "project"
    root_base = _verifier_root_config()
    tmp_root = (
        f"{root_base}/{project_name}-{project_hash}"
        f"-u${{_MCP_UID}}-${{_MCP_USER}}"
    )
    project_q = shlex.quote(project_dir)
    abs_targets = []
    for t in targets:
        p = Path(t)
        if p.is_absolute():
            p = Path(t).relative_to("/") if len(p.parts) > 1 else Path(".")
        abs_targets.append(str(Path(project_dir) / p))
    target_args = " ".join(shlex.quote(t) for t in abs_targets)

    lines = [
        "set -eu",
        '_MCP_UID="$(id -u)"',
        '_MCP_USER="$(id -un)"',
        f"PROJECT_DIR={project_q}",
        f"TMP_ROOT={tmp_root}",
        f"VR={shlex.quote(root_base)}",
        'CUR="$(dirname "$VR")"',
        'while [ "$CUR" != "/" ] && [ "$CUR" != "." ]; do',
        '  if [ -L "$CUR" ]; then echo "VERIFIER_ROOT_UNSAFE: symlink in trusted path: $CUR" >&2; exit 90; fi',
        '  if [ -d "$CUR" ]; then _p000="$(stat -c %a "$CUR" 2>/dev/null || echo 000)"; if [ $(( 0$_p000 & 18 )) -ne 0 ]; then echo "VERIFIER_ROOT_UNSAFE: writable directory in trusted path: $CUR" >&2; exit 90; fi; fi',
        '  CUR="$(dirname "$CUR")"',
        'done',
        'if [ ! -d "$VR" ] || [ -L "$VR" ]; then echo "VERIFIER_ROOT_UNSAFE: not a real directory: $VR" >&2; exit 90; fi',
        'if [ "$(stat -c %U "$VR" 2>/dev/null)" != "$(id -un)" ]; then echo "VERIFIER_ROOT_UNSAFE: wrong owner for $VR" >&2; exit 90; fi',
        'perms="$(stat -c %a "$VR" 2>/dev/null || echo 000)"',
        'if [ $(( 0$perms & 18 )) -ne 0 ]; then echo "VERIFIER_ROOT_UNSAFE: unsafe permissions on $VR" >&2; exit 90; fi',
        'export WORKSPACE_REGISTRY_ROOT="$PROJECT_DIR"',
        'export UV_PROJECT_ENVIRONMENT="$TMP_ROOT/.venv"',
        'if [ -L "$TMP_ROOT" ]; then echo "VERIFIER_ROOT_UNSAFE: per-project root is a symlink: $TMP_ROOT" >&2; exit 90; fi',
        'if [ ! -e "$TMP_ROOT" ]; then mkdir -p "$TMP_ROOT"; fi',
        'if [ ! -d "$TMP_ROOT" ]; then echo "VERIFIER_ROOT_UNSAFE: per-project root is not a directory: $TMP_ROOT" >&2; exit 90; fi',
        'LOCK="$TMP_ROOT/.sync.lock"',
        'exec 9>"$LOCK"',
        'flock 9',
        'SOURCE_META="$TMP_ROOT/.source-pyproject.toml"',
        'SOURCE_META_NEW="$TMP_ROOT/.source-pyproject.toml.new"',
        'SOURCE_LOCK="$TMP_ROOT/.source-uv.lock"',
        'STAMP="$TMP_ROOT/.uv_sync_stamp"',
        'cp "$PROJECT_DIR/pyproject.toml" "$SOURCE_META_NEW"',
        "NEED_SYNC=0",
        'if [ ! -f "$STAMP" ]; then NEED_SYNC=1; fi',
        'if [ ! -f "$SOURCE_META" ] || ! diff -q "$SOURCE_META_NEW" "$SOURCE_META" >/dev/null 2>&1; then NEED_SYNC=1; fi',
        'if [ -f "$PROJECT_DIR/uv.lock" ]; then',
        "  HAS_LOCK=1",
        '  if [ ! -f "$SOURCE_LOCK" ] || ! diff -q "$PROJECT_DIR/uv.lock" "$SOURCE_LOCK" >/dev/null 2>&1; then NEED_SYNC=1; fi',
        "else",
        "  HAS_LOCK=0",
        '  if [ -f "$SOURCE_LOCK" ]; then NEED_SYNC=1; fi',
        "  for req in requirements.txt requirements-dev.txt requirements-test.txt; do",
        '    req_src="$PROJECT_DIR/$req"',
        '    req_cache="$TMP_ROOT/.source-$req"',
        '    if [ -f "$req_src" ]; then',
        '      if [ ! -f "$req_cache" ] || ! diff -q "$req_src" "$req_cache" >/dev/null 2>&1; then NEED_SYNC=1; fi',
        '    elif [ -f "$req_cache" ]; then',
        "      NEED_SYNC=1",
        "    fi",
        "  done",
        "fi",
        # Stale/broken venv (dangling interpreter symlink, unreadable base
        # python, partial sync from a killed run) must not be reused.
        'if [ ! -x "$TMP_ROOT/.venv/bin/python3" ] || [ ! -r "$TMP_ROOT/.venv/bin/python3" ]; then',
        "  NEED_SYNC=1",
        '  if [ -e "$TMP_ROOT/.venv" ]; then rm -rf "$TMP_ROOT/.venv"; fi',
        '  rm -f "$STAMP"',
        "fi",
        "if [ $NEED_SYNC -eq 1 ]; then",
        "  if [ $HAS_LOCK -eq 1 ]; then",
        # The real project can be installed into the external venv safely:
        # --frozen forbids lock mutation and UV_PROJECT_ENVIRONMENT prevents
        # .venv creation inside the source checkout.
        '    cd "$PROJECT_DIR"',
        "    uv sync --all-extras --frozen 2>&1",
        '    cp "$PROJECT_DIR/uv.lock" "$SOURCE_LOCK"',
        '    SYNC_PROJECT="$PROJECT_DIR"',
        "  else",
        # No source lockfile: resolve from copied metadata in TMP_ROOT.  Do
        # not install the project itself from an incomplete synthetic tree.
        '    cp "$SOURCE_META_NEW" "$TMP_ROOT/pyproject.toml"',
        '    rm -f "$TMP_ROOT/uv.lock"',
        '    cd "$TMP_ROOT"',
        "    uv sync --all-extras --no-install-project 2>&1",
        "    for req in requirements.txt requirements-dev.txt requirements-test.txt; do",
        '      if [ -f "$PROJECT_DIR/$req" ]; then',
        '        uv pip install --python "$UV_PROJECT_ENVIRONMENT/bin/python3" -r "$PROJECT_DIR/$req" 2>&1',
        '        cp "$PROJECT_DIR/$req" "$TMP_ROOT/.source-$req"',
        "      else",
        '        rm -f "$TMP_ROOT/.source-$req"',
        "      fi",
        "    done",
        '    rm -f "$SOURCE_LOCK"',
        '    SYNC_PROJECT="$TMP_ROOT"',
        "  fi",
        '  cp "$SOURCE_META_NEW" "$SOURCE_META"',
        '  touch "$STAMP"',
        "else",
        '  if [ $HAS_LOCK -eq 1 ]; then SYNC_PROJECT="$PROJECT_DIR"; else SYNC_PROJECT="$TMP_ROOT"; fi',
        "fi",
        'flock -u 9',
        'rm -f "$SOURCE_META_NEW"',
        'cd "$PROJECT_DIR"',
    ]

    # All tools run from the real checkout. --no-sync is important here:
    # dependency synchronization was completed above and a subsequent uv run
    # must not decide to mutate project metadata or recreate a source-local env.
    run_prefix = 'uv run --project "$SYNC_PROJECT" --no-sync'
    cache_root = _verifier_project_cache_root(project_dir)
    if tool_key == "pytest":
        lines.append(
            f'{run_prefix} pytest -o cache_dir={cache_root}/pytest {target_args} 2>&1'
        )
    elif tool_key == "ruff":
        lines.append(
            f'{run_prefix} ruff check --cache-dir {cache_root}/ruff {target_args} 2>&1'
        )
    elif tool_key == "mypy":
        lines.append(
            f'{run_prefix} mypy --cache-dir {cache_root}/mypy {target_args} 2>&1'
        )

    return "\n".join(lines)


def _venv_usable_on_target(client: Any, project_dir: str) -> bool:
    """Probe whether the project venv is executable on the SSH target.

    On read-only workspaces (e.g. the sshd container mounts the project
    root ro while the venv points at a non-mounted base interpreter) the
    venv is a dangling symlink: ``uv run --frozen`` would try to recreate
    it, fail to remove ``.venv/.lock`` on the read-only FS and exit 2
    before the tool ever runs. Detect that upfront so async runners can
    fall back to the writable temp project instead.
    """
    probe = client.execute_raw(
        f"test -x {shlex.quote(project_dir)}/.venv/bin/python3",
        redact_path_prefix=project_dir,
    )
    raw = client.wait_job(probe["job_id"])
    return raw.get("exit_code", 1) == 0


def _project_has_uv_lock(project_dir: str | Path) -> bool:
    """Return True only when the project has a uv lockfile for --frozen runs."""
    return (Path(project_dir) / "uv.lock").is_file()


def _uv_run_needs_external_fallback(raw: dict[str, Any]) -> bool:
    """Detect uv-run failures that happen before the requested tool starts."""
    if raw.get("exit_code", 0) == 0:
        return False
    stderr_lower = (raw.get("stderr", "") or "").lower()
    return (
        "read-only file system" in stderr_lower
        or "failed to remove directory" in stderr_lower
        or (
            "unable to find lockfile" in stderr_lower
            and "--frozen" in stderr_lower
        )
    )


def _fallback_result(
    tool_key: str, tool_name: str, r2: dict[str, Any], project_dir: str = ""
) -> dict[str, Any]:
    """Build the tool_error/tool_success envelope for a fallback command's raw job result."""
    env_failure = _classify_env_failure(r2.get("stderr", ""), r2.get("exit_code"))
    if env_failure:
        return tool_error(
            code=env_failure,
            message=f"{tool_key} could not run in an isolated environment (exit code {r2.get('exit_code', -1)})",
            result=build_command_result(
                outcome="error",
                exit_code=r2.get("exit_code", -1),
                stdout=_redact_project_root(r2.get("stdout") or r2.get("output", ""), project_dir),
                stderr=_redact_project_root(r2.get("stderr", ""), project_dir),
                execution_duration_ms=_execution_duration_ms(r2),
                job_id=r2.get("job_id"),
                cwd=".",
            ),
            tool_name=tool_name,
        )
    outcome2, error_code2 = _map_uv_exit_code(tool_key, r2.get("exit_code", -1))
    if error_code2 or outcome2 == "failed":
        return tool_error(
            code=error_code2 or "CHECK_FAILED",
            message=f"{tool_key} exit code {r2.get('exit_code', -1)}",
            result=build_command_result(
                outcome=outcome2 or "error",
                exit_code=r2.get("exit_code", -1),
                stdout=_redact_project_root(r2.get("stdout") or r2.get("output", ""), project_dir),
                stderr=_redact_project_root(r2.get("stderr", ""), project_dir),
                execution_duration_ms=_execution_duration_ms(r2),
                job_id=r2.get("job_id"),
                cwd=".",
            ),
            tool_name=tool_name,
        )
    return tool_success(
        tool=tool_name,
        result=build_command_result(
            outcome=outcome2 or "passed",
            exit_code=r2.get("exit_code", 0),
            stdout=_redact_project_root(r2.get("stdout") or r2.get("output", ""), project_dir),
            stderr=_redact_project_root(r2.get("stderr", ""), project_dir),
            execution_duration_ms=_execution_duration_ms(r2),
            job_id=r2.get("job_id"),
            cwd=".",
        ),
    )


def _reconcile_waited_job_id(
    submitted_job_id: Any, wait_result: Any
) -> tuple[str | None, dict[str, str] | None]:
    """Reconcile the submitted job identity with what ``wait_job`` reports.

    ``wait_job`` is polled with the submitted id, so the submitted id is the
    authoritative identity: a payload may omit its own ``job_id`` (incomplete
    result / gateway shape variation) and must never lose the only id the
    caller can poll. A payload that claims a *different* non-empty job id is
    untrusted -- its exit code and output may belong to another job -- so it
    is returned as a mismatch for the caller to fail safe on instead of being
    silently preferred, echoed, or polled.
    """
    def _normalize(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        return value.strip() or None

    submitted = _normalize(submitted_job_id)
    reported = _normalize(wait_result.get("job_id")) if isinstance(wait_result, dict) else None
    if submitted and reported and submitted != reported:
        return submitted, {
            "submitted_job_id": submitted,
            "reported_job_id": reported,
        }
    return submitted or reported, None


def _run_uv_tool(
    client: GatewayClient,
    project: str,
    tool_key: str,
    tool_name: str,
    target: list[str] | str | None = None,
    *,
    async_submit: bool = False,
) -> dict[str, Any]:
    """Run a uv-backed tool via SSH gateway. Shared by all uv runner tools.

    With ``async_submit=True`` the tool returns immediately after the job
    is queued, carrying ``{"job_id", "status": "running"}`` so the caller
    can poll with gateway_job_status / gateway_job_result instead of
    blocking on the full run (run_tests over a whole suite would otherwise
    outlive the sync wait window).
    """
    project_dir = _resolve_project(project)
    if isinstance(target, str):
        target = [target]
    raw_targets = target or ["."]
    try:
        targets = _validate_targets(str(project_dir), raw_targets)
    except ValueError as e:
        code, msg = str(e).split(":", 1) if ":" in str(e) else ("INVALID_INPUT", str(e))
        return tool_error(code=code.strip(), message=msg.strip(), tool_name=tool_name)
    script = _build_primary_script(tool_key, str(project_dir), targets)

    check_result = client.execute_raw("command -v uv")
    check_job = client.wait_job(check_result["job_id"])
    if check_job.get("exit_code", 1) != 0:
        return tool_error(
            code="DEPENDENCY_MISSING",
            message="Required executable 'uv' was not found",
            hint="Install uv on the SSH target or configure another backend",
            retryable=False,
            details={"required_binary": "uv"},
            tool_name=tool_name,
        )

    if async_submit:
        # Audit: run_tests over a full suite must not block on a sync wait
        # that the gateway can outlive; return the job_id immediately and
        # let the caller poll job_status/job_result.
        # Regression: on read-only workspaces the project venv can be a
        # dangling symlink (base interpreter not mounted). Plain
        # `uv run --frozen` then dies with exit 2 (failed to remove
        # .venv/.lock on a read-only FS) before the tool ever runs.
        # Detect that upfront (before queuing the doomed job) and submit
        # the writable temp-project script asynchronously so the async
        # contract (job_id now, result later) still holds.
        if tool_key == "pytest":
            fallback_reason = None
            if not _project_has_uv_lock(project_dir):
                fallback_reason = "Project has no uv.lock for a frozen uv run"
            elif not _venv_usable_on_target(client, str(project_dir)):
                fallback_reason = "Read-only or unusable project venv detected"
            if fallback_reason is not None:
                script = _build_readonly_fallback_script(tool_key, str(project_dir), targets)
                r2 = client.execute_project_script_async(project, script)
                return tool_success(
                    tool=tool_name,
                    result={
                        "job_id": r2.get("job_id"),
                        "status": "running",
                        "outcome": "started",
                    },
                    source="gateway",
                    warnings=[
                        f"{fallback_reason}: tests run in the writable "
                        "server-owned verifier root; poll with "
                        "gateway_job_status then gateway_job_result"
                    ],
                )

    result = client.execute_raw(
        "sh",
        stdin=_script_stdin_wrapper(script),
        redact_path_prefix=str(project_dir),
        timeout_s=client.async_job_timeout if async_submit else None,
    )

    if async_submit:
        return tool_success(
            tool=tool_name,
            result={
                "job_id": result.get("job_id"),
                "status": "running",
                "outcome": "started",
            },
            source="gateway",
            warnings=[
                "Job submitted asynchronously; poll with gateway_job_status "
                "then gateway_job_result"
            ],
        )

    raw = client.wait_job(result["job_id"])

    # Identity boundary: ``result["job_id"]`` is what wait_job was polled
    # with, so it stays authoritative when the payload omits its own
    # job_id; a payload claiming a different job fails safe instead of
    # being trusted or polled.
    job_id, job_id_mismatch = _reconcile_waited_job_id(result.get("job_id"), raw)
    if job_id_mismatch is not None:
        return tool_error(
            code="JOB_ID_MISMATCH",
            message=f"{tool_key} wait result reports a different job than the submitted job",
            retryable=False,
            hint=(
                "This wait result does not belong to the submitted job, so its exit "
                "code and output are untrusted. Poll job_status then job_result with "
                "error.details.submitted_job_id for the submitted job's own state; "
                "do not submit the command again, and do not poll "
                "error.details.reported_job_id."
            ),
            details=job_id_mismatch,
            result=build_command_result(
                outcome="error",
                exit_code=None,
                # Mismatched job output and timing are not attributable to this job.
                # Keep only the authoritative submitted identity for reconciliation.
                stdout="",
                stderr="",
                execution_duration_ms=None,
                job_id=job_id,
                cwd=".",
            ),
            tool_name=tool_name,
        )

    # If the gateway/job result has no concrete exit code but indicates a
    # still-running/ambiguous state or a wait timeout, treat as non-passing
    # without inventing semantics; preserve job_id and poll guidance.
    if raw.get("exit_code") is None:
        status = raw.get("status")
        if raw.get("wait_timed_out") or status in {"running", "cancelling", "pending"}:
            return tool_error(
                code="WAIT_TIMEOUT",
                message=f"{tool_key} did not finish before timeout",
                retryable=True,
                hint=(
                    "Poll job_status then job_result with error.details.job_id for "
                    "this job's final result; do not submit the command again, a "
                    "duplicate execution is not safe while it is still running."
                ),
                details={"job_id": job_id, "status": status} if job_id is not None or status is not None else None,
                result=build_command_result(
                    outcome="error",
                    exit_code=None,
                    stdout=_redact_project_root(raw.get("stdout") or raw.get("output", "") or "", str(project_dir)),
                    stderr=_redact_project_root(raw.get("stderr", "") or "", str(project_dir)),
                    execution_duration_ms=_execution_duration_ms(raw),
                    job_id=job_id,
                    cwd=".",
                ),
                tool_name=tool_name,
            )
        # Unknown/final-but-missing-exit classification: fail safe with
        # accurate non-success envelope, no false passed outcome, preserve
        # job_id, and useful next action.
        return tool_error(
            code="TOOL_EXECUTION_FAILED",
            message=f"{tool_key} returned no exit code",
            retryable=False,
            hint=(
                "Poll job_status then job_result with error.details.job_id to "
                "inspect this job's final state before deciding any next step; do "
                "not submit the command again on the strength of this envelope."
            ),
            details={"job_id": job_id, "status": status} if job_id is not None or status is not None else None,
            result=build_command_result(
                outcome="error",
                exit_code=None,
                stdout=_redact_project_root(raw.get("stdout") or raw.get("output", "") or "", str(project_dir)),
                stderr=_redact_project_root(raw.get("stderr", "") or "", str(project_dir)),
                execution_duration_ms=_execution_duration_ms(raw),
                job_id=job_id,
                cwd=".",
            ),
            tool_name=tool_name,
        )

    env_failure = _classify_env_failure(raw.get("stderr", ""), raw.get("exit_code"))
    if env_failure:
        return tool_error(
            code=env_failure,
            message=f"{tool_key} could not run in an isolated environment (exit code {raw.get('exit_code', -1)})",
            result=build_command_result(
                outcome="error",
                exit_code=raw.get("exit_code", -1),
                stdout=_redact_project_root(raw.get("stdout") or raw.get("output", ""), str(project_dir)),
                stderr=_redact_project_root(raw.get("stderr", ""), str(project_dir)),
                execution_duration_ms=_execution_duration_ms(raw),
                job_id=job_id,
                cwd=".",
            ),
            tool_name=tool_name,
        )

    needs_external_fallback = _uv_run_needs_external_fallback(raw)

    if needs_external_fallback:
        if tool_key in ("pytest", "mypy"):
            script = _build_readonly_fallback_script(tool_key, str(project_dir), targets)
            r2 = client.execute_project_script(project, script, timeout_s=300)
            return _fallback_result(tool_key, tool_name, r2, str(project_dir))

        if tool_key == "compileall":
            # Regression: compileall is a stdlib module, not an installable
            # PyPI tool like ruff/mypy/pytest — `uvx --from compileall ...`
            # below has no package to resolve and always fails with
            # "compileall was not found in the package registry", hiding the
            # actual syntax error the caller needed. It also needs no
            # project venv (`--no-project` skips lockfile/venv sync — the SSH
            # target has no system `python3`, only the interpreter uv
            # manages itself), just its bytecode-cache writes redirected off
            # the read-only mount.
            #
            # Regression: execute_argv's own default timeout_s is only 30s —
            # fine once uv's managed interpreter is already cached locally,
            # but a cold cache means uv has to download and extract a full
            # CPython build first, which reported a 504 at ~30s in practice.
            # Match execute_project_script's fallback budget (pytest/mypy)
            # rather than relying on the bare default.
            r2 = client.execute_argv(
                [
                    "env",
                    "PYTHONPYCACHEPREFIX=" + _verifier_project_cache_root(str(project_dir)) + "/pycache",
                    "uv",
                    "run",
                    "--no-project",
                    "python3",
                    "-c",
                    _build_compileall_walk_code(),
                    "--",
                    *targets,
                ],
                cwd=str(project_dir),
                timeout_s=300,
            )
            return _fallback_result(tool_key, tool_name, r2, str(project_dir))

        try:
            uvx_argv = _build_uvx_argv(tool_key, targets)
        except ValueError:
            uvx_argv = None
        if uvx_argv:
            r2 = client.execute_argv(
                ["env", "RUFF_CACHE_DIR=" + _verifier_project_cache_root(str(project_dir)) + "/ruff"] + uvx_argv,
                cwd=str(project_dir),
            )
            return _fallback_result(tool_key, tool_name, r2, str(project_dir))

    outcome, error_code = _map_uv_exit_code(tool_key, raw.get("exit_code", -1))
    if error_code or outcome == "failed":
        return tool_error(
            code=error_code or "CHECK_FAILED",
            message=f"{tool_key} exit code {raw.get('exit_code', -1)}",
            result=build_command_result(
                outcome=outcome or "error",
                exit_code=raw.get("exit_code", -1),
                stdout=_redact_project_root(raw.get("stdout") or raw.get("output", ""), str(project_dir)),
                stderr=_redact_project_root(raw.get("stderr", ""), str(project_dir)),
                execution_duration_ms=_execution_duration_ms(raw),
                job_id=job_id,
                cwd=".",
            ),
            tool_name=tool_name,
        )
    return tool_success(
        tool=tool_name,
        result=build_command_result(
            outcome=outcome or "passed",
            exit_code=raw.get("exit_code", 0),
            stdout=_redact_project_root(raw.get("stdout") or raw.get("output", ""), str(project_dir)),
            stderr=_redact_project_root(raw.get("stderr", ""), str(project_dir)),
            execution_duration_ms=_execution_duration_ms(raw),
            job_id=job_id,
            cwd=".",
        ),
    )


def run_pytest(
    client: GatewayClient,
    project: str,
    target: list[str] | str | None = None,
) -> dict[str, Any]:
    return _run_uv_tool(client, project, "pytest", "run_pytest", target)


def run_ruff(
    client: GatewayClient,
    project: str,
    target: list[str] | str | None = None,
) -> dict[str, Any]:
    return _run_uv_tool(client, project, "ruff", "run_ruff", target)


def run_mypy(
    client: GatewayClient,
    project: str,
    target: list[str] | str | None = None,
) -> dict[str, Any]:
    return _run_uv_tool(client, project, "mypy", "run_mypy", target)


# ── Project git info tools ──────────────────────────────────────


def remotes(
    client: GatewayClient,
    project: str,
) -> dict[str, Any]:
    result = run_project_command(client, project, "git remote -v")
    if "stdout" in result:
        import re
        original = result["stdout"]
        output = re.sub(
            r"(https?://)[^@/]+:[^@/]+@",
            r"\1***:***@",
            original,
        )
        output = re.sub(
            r"(https?://)[A-Za-z0-9_]+@",
            r"\1***@",
            output,
        )
        result["stdout"] = output
        # Signal to run_tool() whether a credential was actually stripped,
        # so the reported envelope's meta.redacted reflects reality instead
        # of always defaulting to False regardless of what happened here.
        result["redacted"] = output != original
    return result


def current_branch(
    client: GatewayClient,
    project: str,
) -> dict[str, Any]:
    return run_project_command(client, project, "git rev-parse --abbrev-ref HEAD")


def commit_head(
    client: GatewayClient,
    project: str,
) -> dict[str, Any]:
    return run_project_command(client, project, "git rev-parse HEAD")


# ── Project-scoped handoff ──────────────────────────────────────


def read_handoff(
    client: GatewayClient,
    project: str,
) -> dict[str, Any]:
    result = run_project_command(client, project, "cat .ai-bridge/current-plan.md")
    if result.get("exit_code") != 0:
        stderr = result.get("stderr", "")
        if "No such file" in stderr or "does not exist" in stderr:
            return build_command_result(
                outcome="passed", exit_code=0, stdout="(no handoff plan)", stderr=""
            )
        return result
    return result


def _handoff_write_registry(registry: Any | None = None):
    """Return the registry used for the fixed handoff write path.

    The live MCP adapter injects its server-owned write-scoped registry after
    ``mcp:handoff`` / write-mode authorization.  Direct Python callers do not
    get an implicit privilege upgrade: without an injected registry they see
    the normal read-scoped singleton, whose write policy fails closed.
    """
    if registry is not None:
        return registry

    from app.workspace.registry import get_registry

    return get_registry()


def _handoff_workspace_not_writeable_error(project: str) -> dict[str, Any]:
    return tool_error(
        tool="write_handoff_plan",
        code="WORKSPACE_NOT_WRITEABLE",
        message="Registered project root is not writable for handoff coordination files",
        retryable=False,
        hint=(
            "Use a managed writable candidate clone for handoff/task writes, "
            "or restore write access to the registered workspace before retrying."
        ),
        details={
            "project": project,
            "required_write_plane": "writeable_candidate_clone",
            "mutation_occurred": False,
        },
    )


def write_handoff_plan(
    client: GatewayClient,
    project: str,
    task: str,
    agent: str = "opencode",
    notes: str | None = None,
    *,
    registry: Any | None = None,
) -> dict[str, Any]:
    """Write the project handoff plan in the host workspace namespace."""
    from handoff import build_handoff_plan
    from write_modes import WritePermissionError, assert_handoff_write_allowed

    from app.config import settings
    from app.workspace.edit import project_file_write

    assert_handoff_write_allowed()
    if settings.workspace_readonly:
        raise WritePermissionError("Workspace is in read-only mode")

    # ``client`` is intentionally unused here.  Project IDs belong to the
    # host workspace registry; SSH/file APIs operate in the target session's
    # filesystem namespace and cannot safely translate host project roots.
    _ = client
    from app.workspace.policy import WorkspacePolicyError

    handoff_path = ".ai-bridge/current-plan.md"
    plan = build_handoff_plan(task=task, agent=agent, notes=notes)
    try:
        write_registry = _handoff_write_registry(registry)

        # project_file_write deliberately does not create arbitrary parents.
        # Handoff needs exactly one fixed coordination directory, so validate
        # the fixed target first and create only that one directory (never a
        # caller-provided mkdir -p surface).  The subsequent write revalidates
        # symlinks/path policy after this potential race.
        target = write_registry._policy.validate_write(project, handoff_path)
        parent = target.parent
        project_root = parent.parent
        if not os.access(project_root, os.W_OK):
            return _handoff_workspace_not_writeable_error(project)
        if not parent.exists():
            try:
                parent.mkdir(mode=0o755, parents=False)
            except FileExistsError:
                # A concurrent creator won the race. Revalidation below
                # decides whether the resulting path is acceptable.
                pass
        if not parent.is_dir():
            raise WorkspacePolicyError("Handoff coordination path is not a directory")

        project_file_write(
            project_id=project,
            relative_path=handoff_path,
            content=plan,
            registry=write_registry,
        )
    except WorkspacePolicyError:
        # Workspace policy messages can contain absolute server paths.  The
        # MCP surface only needs the classification; keep host topology out of
        # the response.
        return tool_error(
            tool="write_handoff_plan",
            code="POLICY_DENIED",
            message="Handoff path rejected by workspace policy",
            retryable=False,
        )
    except OSError as exc:
        if getattr(exc, "errno", None) in {errno.EACCES, errno.EPERM, errno.EROFS}:
            return _handoff_workspace_not_writeable_error(project)
        safe_reason = getattr(exc, "strerror", None) or type(exc).__name__
        return tool_error(
            tool="write_handoff_plan",
            code="HANDOFF_WRITE_FAILED",
            message=f"Handoff plan write failed: {safe_reason}",
            retryable=False,
        )

    return build_command_result(
        outcome="passed", exit_code=0, stdout="Handoff plan written", stderr=""
    )


def show_handoff_status(
    client: GatewayClient,
    project: str,
) -> dict[str, Any]:
    result = run_project_command(client, project, "ls -la .ai-bridge/")
    if result.get("exit_code") != 0:
        return build_command_result(
            outcome="passed",
            exit_code=0,
            stdout="(no .ai-bridge directory)",
            stderr="",
        )
    return result


# ── Shell escape helper ─────────────────────────────────────────


def _shell_escape(text: str) -> str:
    escaped = text.replace("'", "'\\''")
    return f"'{escaped}'"


# ── Project-aware tools (cd into MCP_GATEWAY_PROJECT_ROOT/{project}) ──


def run_project_command(
    client: GatewayClient,
    project: str,
    command: str,
) -> dict[str, Any]:
    result = client.execute_project_command(project, command)
    try:
        root = str(_resolve_project(project))
    except GatewayClientError:
        root = ""
    return build_command_result(
        outcome="passed" if result.get("exit_code", 1) == 0 else "failed",
        exit_code=result.get("exit_code", -1),
        stdout=_redact_project_root(result.get("stdout") or result.get("output", ""), root),
        stderr=_redact_project_root(result.get("stderr", ""), root),
        execution_duration_ms=_execution_duration_ms(result),
        job_id=result.get("job_id"),
        cwd=".",
    )


def working_directory(client: GatewayClient, project: str) -> dict[str, Any]:
    """Report the project working directory as the project-relative
    namespace "." — never the real host path (mirrors info()'s
    "resolved_path": "." convention). No shell invocation needed."""
    _validate_project(project)
    _resolve_project(project)
    return {
        "outcome": "passed",
        "exit_code": 0,
        "stdout": ".",
        "stderr": "",
        "execution_duration_ms": 0,
        "job_id": None,
    }




_SHELL_ONLY_CHARS = frozenset("&;|<>*?[")


def _has_shell_only_token(argv: list[str]) -> bool:
    """True when a token only carries meaning through shell re-interpretation.

    ``shlex`` splits on whitespace, not on shell syntax, so a token-equality
    check against operators misses glued forms: ``app;echo`` and
    ``2>/dev/null`` arrive as a single token, and fd redirections,
    backgrounding, comments, tilde targets and globs are handed to the
    program literally when argv runs without a shell — the advertised command
    then cannot run as written. ``$VAR``/``$(...)`` and backticks are
    expansion for the same reason and are rejected here too.
    """
    for token in argv:
        if _SHELL_ONLY_CHARS.intersection(token):
            return True
        if "$" in token or "`" in token:
            return True
        if token.startswith(("#", "~")):
            return True
    return False


def _makefile_target_uv_argv(
    resolved: Path, target: str, tool_name: str
) -> list[str] | None:
    """Extract one simple repo-owned ``uv run`` recipe as executable argv.

    This intentionally supports only a single direct recipe line. Complex make
    recipes, variable expansion, pipelines and shell control operators are not
    advertised because ``info()`` must return commands that callers can execute
    verbatim without requiring ``make`` or re-interpreting project shell code.
    """
    target_pattern = re.compile(rf"^[ \t]*{re.escape(target)}[ \t]*:[^=\n]*$")
    for name in ("GNUmakefile", "Makefile", "makefile"):
        makefile = resolved / name
        if not makefile.is_file():
            continue
        try:
            lines = makefile.read_text(encoding="utf-8")[:200_000].splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines):
            if not target_pattern.match(line):
                continue
            recipes: list[str] = []
            for following in lines[index + 1 :]:
                if following.startswith("\t"):
                    command = following[1:].strip()
                    if command and not command.startswith("#"):
                        recipes.append(command)
                    continue
                if not following.strip() or following.lstrip().startswith("#"):
                    continue
                break
            if len(recipes) != 1:
                return None
            recipe = recipes[0]
            if recipe.startswith("@"):
                recipe = recipe[1:].lstrip()
            try:
                argv = shlex.split(recipe, posix=True)
            except ValueError:
                return None
            if len(argv) < 3 or argv[:2] != ["uv", "run"]:
                return None
            if tool_name not in argv[2:]:
                return None
            if _has_shell_only_token(argv):
                return None
            return argv
    return None


def _mypy_config_has_files(parsed: dict[str, Any]) -> bool:
    """Return True when [tool.mypy] owns the target set via ``files``."""
    tool = parsed.get("tool")
    if not isinstance(tool, dict):
        return False
    mypy = tool.get("mypy")
    if not isinstance(mypy, dict):
        return False
    files = mypy.get("files")
    if isinstance(files, str):
        return bool(files.strip())
    if isinstance(files, list):
        return any(isinstance(item, str) and item.strip() for item in files)
    return False


def _project_verification_hints(resolved: Path) -> dict[str, Any]:
    """Return host-path-free, verbatim-executable verification hints."""
    hints: dict[str, Any] = {
        "cwd": ".",
        "cwd_required": True,
        "note": "Run verification commands from the registered project root; do not use --project as a cwd substitute.",
        "commands": [],
    }
    pyproject = resolved / "pyproject.toml"
    if not pyproject.is_file():
        return hints
    try:
        raw_content = pyproject.read_text(encoding="utf-8")[:200_000]
    except OSError:
        return hints

    content = raw_content.lower()
    has_dev_extra = False
    parsed: dict[str, Any] = {}
    try:
        import tomllib

        loaded = tomllib.loads(raw_content)
        parsed = loaded if isinstance(loaded, dict) else {}
        project = parsed.get("project")
        optional = (
            project.get("optional-dependencies")
            if isinstance(project, dict)
            else None
        )
        has_dev_extra = isinstance(optional, dict) and "dev" in optional
    except tomllib.TOMLDecodeError:
        has_dev_extra = False

    commands: list[dict[str, Any]] = []
    base = ["uv", "run"]
    if has_dev_extra:
        base.extend(["--extra", "dev"])
    if "pytest" in content:
        commands.append({"name": "pytest", "argv": [*base, "pytest"]})
    if "ruff" in content:
        commands.append({"name": "ruff", "argv": [*base, "ruff", "check"]})
    makefile_mypy_argv = _makefile_target_uv_argv(resolved, "mypy", "mypy")
    if (
        makefile_mypy_argv is not None
        and has_dev_extra
        and "--extra" not in makefile_mypy_argv
        and "--all-extras" not in makefile_mypy_argv
    ):
        makefile_mypy_argv = [
            *makefile_mypy_argv[:2],
            "--extra",
            "dev",
            *makefile_mypy_argv[2:],
        ]
    if makefile_mypy_argv is not None:
        commands.append(
            {
                "name": "mypy",
                "argv": makefile_mypy_argv,
                "contract_source": "Makefile:mypy",
            }
        )
    elif "mypy" in content and _mypy_config_has_files(parsed):
        commands.append(
            {
                "name": "mypy",
                "argv": [*base, "mypy"],
                "contract_source": "pyproject.toml:[tool.mypy].files",
            }
        )
    elif "mypy" in content:
        hints.setdefault("omitted_commands", []).append(
            {
                "name": "mypy",
                "reason": (
                    "mypy is configured/installed but no project-owned target set was found; "
                    "not advertising a bare command that can fail with Missing target module/package/files"
                ),
            }
        )
    hints["commands"] = commands
    return hints


def _local_git_output(resolved: Path, args: list[str]) -> str | None:
    """Run a fixed read-only git query without a shell; return stdout on success.

    Project roots mounted into the MCP control plane can be owned by a
    different Unix user than the process executing this local metadata probe.
    Use a scoped safe.directory exception for the registered root so Git's
    ownership guard does not make info()/branch-merge guards report opaque
    git_state_unavailable while project-level SSH git commands still work.
    Never use safe.directory=*.
    """
    try:
        completed = subprocess.run(
            ["git", "-c", f"safe.directory={resolved}", *args],
            cwd=str(resolved),
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _local_git_bytes(resolved: Path, args: list[str]) -> bytes | None:
    """Run a fixed read-only Git query and preserve stdout byte-for-byte."""
    try:
        completed = subprocess.run(
            ["git", "-c", f"safe.directory={resolved}", *args],
            cwd=str(resolved),
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _project_git_state(resolved: Path) -> dict[str, Any]:
    """Return host-path-free Git state plus a content-bound index lease digest.

    ``status_sha256`` intentionally remains the porcelain-shape digest for
    backwards-compatible diagnostics. ``index_sha256`` hashes Git's canonical
    index listing (mode, blob object id, stage and exact path), so staged byte,
    mode or rename-target changes alter the lease even when porcelain status
    letters remain unchanged. ``git ls-files`` is read-only and creates no Git
    objects, unlike ``git write-tree``.
    """
    if not _is_real_git_repo(resolved):
        return {"available": False, "reason": "not_git_repo"}

    branch_raw = _local_git_output(resolved, ["rev-parse", "--abbrev-ref", "HEAD"])
    head_raw = _local_git_output(resolved, ["rev-parse", "HEAD"])
    status = _local_git_output(resolved, ["status", "--porcelain=v1"])
    index = _local_git_bytes(resolved, ["ls-files", "--stage", "-z"])
    if branch_raw is None or head_raw is None or status is None or index is None:
        return {"available": False, "reason": "git_state_unavailable"}

    branch = branch_raw.strip()
    head = head_raw.strip().lower()
    status_sha256 = hashlib.sha256(status.encode("utf-8", "surrogateescape")).hexdigest()
    index_sha256 = hashlib.sha256(index).hexdigest()
    return {
        "available": True,
        "branch": branch,
        "detached": branch == "HEAD",
        "head": head,
        "dirty": bool(status.strip()),
        "status_sha256": status_sha256,
        "index_sha256": index_sha256,
        "status_entries": len([line for line in status.splitlines() if line.strip()]),
    }


def _workspace_guard_mismatches(
    state: dict[str, Any],
    *,
    expected_branch: str | None,
    expected_head: str | None,
    expected_status_sha256: str | None,
    expected_index_sha256: str | None,
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    checks = (
        ("branch", expected_branch, state.get("branch")),
        ("head", expected_head.lower() if expected_head else None, state.get("head")),
        (
            "status_sha256",
            expected_status_sha256.lower() if expected_status_sha256 else None,
            state.get("status_sha256"),
        ),
        (
            "index_sha256",
            expected_index_sha256.lower() if expected_index_sha256 else None,
            state.get("index_sha256"),
        ),
    )
    for field, expected, actual in checks:
        if expected is not None and actual != expected:
            mismatches.append({"field": field, "expected": expected, "actual": actual})
    return mismatches


def _guarded_git_commit_error(
    project: str,
    state: dict[str, Any],
    mismatches: list[dict[str, Any]],
) -> dict[str, Any]:
    return tool_error(
        tool="git_commit",
        code="WORKSPACE_CONTENDED",
        message="Project workspace changed since the caller's expected git state snapshot",
        retryable=True,
        hint="Refresh info(project), review git_status/git_diff_cached, then retry with the new expected_branch, expected_head, expected_status_sha256, and expected_index_sha256.",
        details={
            "project": project,
            "mismatches": mismatches,
            "current": {
                "branch": state.get("branch"),
                "head": state.get("head"),
                "dirty": state.get("dirty"),
                "status_sha256": state.get("status_sha256"),
                "index_sha256": state.get("index_sha256"),
                "status_entries": state.get("status_entries"),
            },
        },
    )


def _project_workspace_hints(resolved: Path) -> dict[str, Any]:
    """Return host-path-free workspace write-plane hints for agents."""
    try:
        from app.config import settings as _settings

        configured_readonly: bool | None = bool(_settings.workspace_readonly)
    except Exception:
        configured_readonly = None

    filesystem_writeable = bool(resolved.is_dir() and os.access(resolved, os.W_OK))
    needs_candidate_clone = bool(configured_readonly or not filesystem_writeable)
    return {
        "root": ".",
        "configured_readonly": configured_readonly,
        "filesystem_writeable": filesystem_writeable,
        "git_write_capabilities": {
            "status": "not_probed",
            "tool": "git_write_capabilities",
            "note": (
                "filesystem_writeable covers the worktree root only; it is not proof "
                "that Git index, objects, refs, or HEAD metadata are writeable on the SSH execution plane."
            ),
        },
        "recommended_write_plane": (
            "writeable_candidate_clone" if needs_candidate_clone else "workspace"
        ),
        "git_mutation_write_plane": (
            "writeable_candidate_clone" if needs_candidate_clone else "preflight_required"
        ),
        "note": (
            "Treat workspace writes as unavailable; create/use a writeable candidate clone before editing."
            if needs_candidate_clone
            else (
                "Workspace root appears writeable for ordinary file writes. Git mutations must still pass "
                "git_write_capabilities; do not infer index/objects/refs/HEAD writeability from this boolean."
            )
        ),
    }


def _is_real_git_repo(path: Path) -> bool:
    """Check for an actual git worktree, not just a path named ".git".

    A worktree/submodule's ".git" is a FILE containing "gitdir: <path>" --
    that alone is a real marker. A normal repo's ".git" is a directory
    that always contains a HEAD file. Checking existence of ".git" alone
    (the previous behavior) reported is_git_repo=True for any directory
    happening to contain a ".git"-named path for unrelated reasons -- e.g.
    quart-platform/.git is a scratch metadata directory left by another
    tool (contains only "info/" and "mimocode-project-id", no HEAD/objects/
    refs), which made every registered project under an umbrella root with
    such a stray ".git" report as a git repo when git itself, correctly,
    says "fatal: not a git repository".
    """
    git_marker = path / ".git"
    if git_marker.is_file():
        return True
    return (git_marker / "HEAD").is_file()


def info(client: GatewayClient, project: str) -> dict[str, Any]:
    """Resolve project path metadata — no shell execution."""
    project = _validate_project(project)
    resolved = _resolve_project(project)
    is_git_repo = _is_real_git_repo(resolved)
    workspace = _project_workspace_hints(resolved)
    workspace["git_state"] = _project_git_state(resolved)
    return {
        "project": project,
        # "." — project-relative namespace, never the real host path
        # (see the "root" fields in list_tree()/tree()/list_files()).
        "root": ".",
        "resolved_path": ".",
        "exists": resolved.exists(),
        "is_dir": resolved.is_dir(),
        "is_git_repo": is_git_repo,
        "verification": _project_verification_hints(resolved),
        "workspace": workspace,
    }


def git_status(client: GatewayClient, project: str) -> dict[str, Any]:
    return run_project_command(client, project, "git status --short")


def recent_commits(client: GatewayClient, project: str) -> dict[str, Any]:
    return run_project_command(client, project, "git log --oneline -10")


def git_diff_stat(client: GatewayClient, project: str) -> dict[str, Any]:
    return run_project_command(client, project, "git diff --stat")


def show_changes(client: GatewayClient, project: str) -> dict[str, Any]:
    gs = git_status(client, project)
    gds = git_diff_stat(client, project)
    result: dict[str, Any] = {
        "git_status": gs,
        "git_diff_stat": gds,
    }
    if gs.get("exit_code", -1) != 0 and gds.get("exit_code", -1) != 0:
        result["ok"] = False
        result["error"] = {
            "code": "CHECK_FAILED",
            "message": "git status and git diff both failed",
            "retryable": False,
            "details": {"git_status": gs, "git_diff_stat": gds},
        }
    return result


def git_write_capabilities(
    client: GatewayClient,
    project: str,
) -> dict[str, Any]:
    """Probe index/objects/refs/HEAD/FETCH_HEAD writeability on the Git mutation plane."""
    _validate_project(project)
    return probe_git_write_capabilities(client, project)


def _git_write_preflight_from_probe(
    project: str,
    *,
    tool_name: str,
    action: str,
    required_components: tuple[str, ...],
    probe: dict[str, Any],
) -> dict[str, Any] | None:
    """Validate one already-collected non-mutating capability snapshot."""
    if not probe.get("available"):
        return tool_error(
            tool=tool_name,
            code="GIT_WRITE_PREFLIGHT_UNAVAILABLE",
            message="Git write-capability preflight could not be completed; refusing mutation",
            retryable=bool(probe.get("retryable", True)),
            hint=(
                "Retry after restoring the SSH project execution plane, or create/use a writeable "
                "candidate clone instead of mutating the registered workspace."
            ),
            details={
                "project": project,
                "action": action,
                "required_components": list(required_components),
                "preflight": probe,
                "mutation_occurred": False,
            },
            source="gateway",
        )

    components = probe.get("components", {})
    blocked = [
        name
        for name in required_components
        if not bool(components.get(name, {}).get("writeable"))
    ]
    if blocked:
        return tool_error(
            tool=tool_name,
            code="GIT_WRITE_CAPABILITY_BLOCKED",
            message="Registered workspace lacks required Git metadata write capability",
            retryable=False,
            hint=(
                "Use prepare_candidate_clone or another managed writeable candidate; do not change "
                "global Git safe.directory/ownership settings to bypass this guard."
            ),
            details={
                "project": project,
                "action": action,
                "required_components": list(required_components),
                "blocked_components": blocked,
                "preflight": probe,
                "mutation_occurred": False,
            },
            source="gateway",
        )
    return None


def _git_write_preflight(
    client: GatewayClient,
    project: str,
    *,
    tool_name: str,
    action: str,
    required_components: tuple[str, ...],
    branch: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Collect and validate one non-mutating capability snapshot."""
    probe = probe_git_write_capabilities(client, project, branch=branch)
    return (
        _git_write_preflight_from_probe(
            project,
            tool_name=tool_name,
            action=action,
            required_components=required_components,
            probe=probe,
        ),
        probe,
    )


def _git_runtime_permission_components(stderr: str) -> list[str]:
    """Classify post-preflight permission failures without exposing host paths."""
    text = (stderr or "").lower()
    permission_markers = (
        "permission denied",
        "operation not permitted",
        "read-only file system",
        "insufficient permission",
    )
    if not any(marker in text for marker in permission_markers):
        return []

    components: list[str] = []
    if "index.lock" in text:
        components.append("index")
    if "repository database" in text or "/objects" in text or ".git/objects" in text:
        components.append("objects")
    if "refs/heads" in text or "cannot lock ref" in text:
        components.append("refs")
    if "head.lock" in text or "ref 'head'" in text:
        components.append("head")
    if "fetch_head" in text:
        components.append("fetch_head")
    return list(dict.fromkeys(components))


def _git_mutation_result(
    *,
    tool_name: str,
    project: str,
    action: str,
    required_components: tuple[str, ...],
    preflight: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Preserve normal Git failures, but type permission drift after preflight."""
    if result.get("exit_code", 1) == 0:
        return result
    components = _git_runtime_permission_components(str(result.get("stderr", "")))
    if not components:
        return result
    return tool_error(
        tool=tool_name,
        code="GIT_WRITE_CAPABILITY_BLOCKED",
        message="Git metadata permissions changed or were insufficient after preflight",
        retryable=True,
        hint=(
            "Refresh git_write_capabilities and use a managed candidate clone. "
            "Do not blindly retry the mutation because partial Git metadata/object writes may have occurred."
        ),
        details={
            "project": project,
            "action": action,
            "required_components": list(required_components),
            "blocked_components": components,
            "preflight": preflight,
            "mutation_occurred": "unknown",
        },
        result=result,
        source="gateway",
    )


def _run_git_mutation_with_preflight(
    client: GatewayClient,
    project: str,
    command: str,
    *,
    tool_name: str,
    action: str,
    required_components: tuple[str, ...],
    branch: str | None = None,
) -> dict[str, Any]:
    preflight_error, probe = _git_write_preflight(
        client,
        project,
        tool_name=tool_name,
        action=action,
        required_components=required_components,
        branch=branch,
    )
    if preflight_error is not None:
        return preflight_error
    result = run_project_command(client, project, command)
    return _git_mutation_result(
        tool_name=tool_name,
        project=project,
        action=action,
        required_components=required_components,
        preflight=probe,
        result=result,
    )


def git_set_gitlink(
    client: GatewayClient,
    project: str,
    path: str,
    old_sha: str,
    new_sha: str,
    expected_head: str,
    expected_branch: str,
    expected_index_sha256: str,
) -> dict[str, Any]:
    """Stage exactly one pre-existing gitlink under a content-bound index lease.

    The target SHA is a commit in the *child* repository, so it need not exist
    in the parent's object database. This tool only stages mode 160000; normal
    commit and remote delivery remain separate guarded operations.
    """
    hashes = (old_sha, new_sha, expected_head, expected_index_sha256)
    if any(re.fullmatch(r"[0-9a-f]{40}", s) is None for s in hashes[:3]):
        raise ValueError("INVALID_INPUT: old_sha/new_sha/expected_head must be 40-char lowercase Git SHAs")
    if re.fullmatch(r"[0-9a-f]{64}", hashes[3]) is None:
        raise ValueError("INVALID_INPUT: expected_index_sha256 must be a 64-char lowercase SHA-256")
    if old_sha == new_sha:
        raise ValueError("INVALID_INPUT: gitlink target already equals old SHA")
    if (
        not path
        or path.startswith(("-", "/"))
        or "\\" in path
        or any(part in ("", ".", "..", ".git") for part in path.split("/"))
        or re.fullmatch(r"[A-Za-z0-9_./-]+", path) is None
    ):
        raise ValueError("INVALID_INPUT: gitlink path must be safe and relative")
    if (
        expected_branch in ("main", "master", "HEAD")
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", expected_branch) is None
        or ".." in expected_branch
    ):
        raise ValueError("INVALID_INPUT: a non-protected exact feature branch is required")

    q = shlex.quote
    script = "\n".join(
        [
            "set -eu",
            f"EXPECTED_BRANCH={q(expected_branch)}",
            f"EXPECTED_HEAD={q(expected_head)}",
            f"EXPECTED_INDEX={q(expected_index_sha256)}",
            f"OLD_SHA={q(old_sha)}",
            f"NEW_SHA={q(new_sha)}",
            f"SUBMODULE_PATH={q(path)}",
            'INDEX_PATH="$(git rev-parse --git-path index)"',
            'INDEX_LOCK="${INDEX_PATH}.lock"',
            'INDEX_DIR="$(dirname "$INDEX_PATH")"',
            'SNAPSHOT="$(mktemp "$INDEX_DIR/.mcp-gitlink-index.XXXXXX")"',
            'LOCK_OWNED=0',
            'cleanup() { rm -f "$SNAPSHOT"; if [ "$LOCK_OWNED" = 1 ]; then rm -f "$INDEX_LOCK"; fi; }',
            'trap cleanup EXIT',
            'trap \'exit 99\' HUP INT TERM',
            'if ( set -C; : > "$INDEX_LOCK" ) 2>/dev/null; then LOCK_OWNED=1; else echo "GITLINK_BLOCKED=index_lock"; exit 96; fi',
            'cp "$INDEX_PATH" "$SNAPSHOT"',
            'BRANCH="$(git rev-parse --abbrev-ref HEAD)"',
            'HEAD_SHA="$(git rev-parse HEAD)"',
            'if [ "$BRANCH" != "$EXPECTED_BRANCH" ] || [ "$HEAD_SHA" != "$EXPECTED_HEAD" ]; then echo "GITLINK_MISMATCH=head_or_branch"; exit 97; fi',
            'if [ -n "$(GIT_OPTIONAL_LOCKS=0 git status --porcelain=v1)" ]; then echo "GITLINK_MISMATCH=dirty_worktree"; exit 97; fi',
            'INDEX_SHA="$(GIT_INDEX_FILE="$SNAPSHOT" git ls-files --stage -z | sha256sum | awk \'{print $1}\')"',
            'if [ "$INDEX_SHA" != "$EXPECTED_INDEX" ]; then echo "GITLINK_MISMATCH=index"; exit 97; fi',
            'TREE_ENTRY="$(git ls-tree HEAD -- "$SUBMODULE_PATH")"',
            'EXPECTED_TREE="$(printf "160000 commit %s\\t%s" "$OLD_SHA" "$SUBMODULE_PATH")"',
            'if [ "$TREE_ENTRY" != "$EXPECTED_TREE" ]; then echo "GITLINK_MISMATCH=tree_entry"; exit 97; fi',
            'INDEX_ENTRY="$(GIT_INDEX_FILE="$SNAPSHOT" git ls-files --stage -- "$SUBMODULE_PATH")"',
            'EXPECTED_ENTRY="$(printf "160000 %s 0\\t%s" "$OLD_SHA" "$SUBMODULE_PATH")"',
            'if [ "$INDEX_ENTRY" != "$EXPECTED_ENTRY" ]; then echo "GITLINK_MISMATCH=index_entry"; exit 97; fi',
            'GIT_INDEX_FILE="$SNAPSHOT" git update-index --cacheinfo "160000,$NEW_SHA,$SUBMODULE_PATH"',
            'UPDATED="$(GIT_INDEX_FILE="$SNAPSHOT" git ls-files --stage -- "$SUBMODULE_PATH")"',
            'EXPECTED_UPDATED="$(printf "160000 %s 0\\t%s" "$NEW_SHA" "$SUBMODULE_PATH")"',
            'CHANGED="$(GIT_INDEX_FILE="$SNAPSHOT" git diff --cached --name-only)"',
            'if [ "$UPDATED" != "$EXPECTED_UPDATED" ] || [ "$CHANGED" != "$SUBMODULE_PATH" ]; then echo "GITLINK_MISMATCH=staged_scope"; exit 98; fi',
            'if [ "$(git rev-parse HEAD)" != "$EXPECTED_HEAD" ]; then echo "GITLINK_MISMATCH=head_changed"; exit 97; fi',
            'mv -f "$SNAPSHOT" "$INDEX_PATH"',
            'echo "GITLINK_STAGED=1"',
            'echo "GITLINK_OLD=$OLD_SHA"',
            'echo "GITLINK_NEW=$NEW_SHA"',
        ]
    )
    # Multiline shell guards require the project-script transport (stdin
    # wrapper); execute_project_command splits text into argv and cannot
    # execute shell constructs. Keep the same preflight/permission typing as
    # other Git mutations, without routing through the argv-only transport.
    required_components = ("index",)
    preflight_error, probe = _git_write_preflight(
        client,
        project,
        tool_name="git_set_gitlink",
        action="stage_gitlink_exact",
        required_components=required_components,
        branch=expected_branch,
    )
    if preflight_error is not None:
        return preflight_error
    raw = client.execute_project_script(project, script, timeout_s=60)
    root = str(_resolve_project(project))
    result = build_command_result(
        outcome="passed" if raw.get("exit_code") == 0 else "failed",
        exit_code=raw.get("exit_code", -1),
        stdout=_redact_project_root(str(raw.get("stdout") or raw.get("output", "")), root),
        stderr=_redact_project_root(str(raw.get("stderr", "")), root),
        execution_duration_ms=_execution_duration_ms(raw),
        job_id=raw.get("job_id"),
        cwd=".",
    )
    return _git_mutation_result(
        tool_name="git_set_gitlink",
        project=project,
        action="stage_gitlink_exact",
        required_components=required_components,
        preflight=probe,
        result=result,
    )


def git_add(
    client: GatewayClient,
    project: str,
    paths: list[str],
) -> dict[str, Any]:
    if not paths:
        raise ValueError("INVALID_INPUT: paths must not be empty")
    for p in paths:
        if p.startswith("-") or not p.strip():
            raise ValueError(f"INVALID_INPUT: path {p!r} must not start with '-'")
    quoted = " ".join(shlex.quote(p) for p in paths)
    return _run_git_mutation_with_preflight(
        client,
        project,
        f"git add -- {quoted}",
        tool_name="git_add",
        action="stage_paths",
        required_components=("index", "objects"),
    )


def _guarded_git_commit_script(
    *,
    message: str,
    expected_branch: str | None,
    expected_head: str | None,
    expected_status_sha256: str | None,
    expected_index_sha256: str,
) -> str:
    """Build one target-plane critical section for a content-bound commit.

    The reviewed index digest is checked against a private snapshot while the
    real index lock is held. The commit then reads that exact snapshot through
    ``GIT_INDEX_FILE``. This closes the check/commit TOCTOU where another
    ``git add`` could previously replace staged bytes after lease validation.

    Repository hooks are disabled for this trusted automated commit: a hook
    that stages files would otherwise mutate the private index after its digest
    was verified and defeat the content lease.
    """
    expected_branch_q = shlex.quote(expected_branch) if expected_branch is not None else ""
    expected_head_q = shlex.quote(expected_head.lower()) if expected_head is not None else ""
    expected_status_q = (
        shlex.quote(expected_status_sha256.lower())
        if expected_status_sha256 is not None
        else ""
    )
    expected_index_q = shlex.quote(expected_index_sha256.lower())
    message_q = shlex.quote(message)
    return "\n".join(
        [
            "set -eu",
            "MCP_GIT_GUARD=1",
            'INDEX_PATH="$(git rev-parse --git-path index)" || exit 98',
            'INDEX_LOCK="${INDEX_PATH}.lock"',
            'SNAPSHOT="$(mktemp \"${TMPDIR:-/tmp}/mcp-git-index.XXXXXX\")" || exit 98',
            'INDEX_LISTING="$(mktemp \"${TMPDIR:-/tmp}/mcp-git-index-listing.XXXXXX\")" || { rm -f "$SNAPSHOT"; exit 98; }',
            'STATUS_FILE="$(mktemp \"${TMPDIR:-/tmp}/mcp-git-status.XXXXXX\")" || { rm -f "$SNAPSHOT" "$INDEX_LISTING"; exit 98; }',
            "LOCK_OWNED=0",
            'cleanup() { rm -f "$SNAPSHOT" "$INDEX_LISTING" "$STATUS_FILE"; if [ "$LOCK_OWNED" = "1" ]; then LOCK_OWNED=0; rm -f "$INDEX_LOCK"; fi; }',
            "trap cleanup EXIT",
            "trap 'cleanup; exit 99' HUP INT TERM",
            'if ( set -C; : > "$INDEX_LOCK" ) 2>/dev/null; then LOCK_OWNED=1; else printf "MCP_GIT_GUARD_BUSY=index\\n"; exit 96; fi',
            'cp "$INDEX_PATH" "$SNAPSHOT" || { printf "MCP_GIT_GUARD_ERROR=index_snapshot\\n"; exit 98; }',
            'BRANCH="$(git rev-parse --abbrev-ref HEAD)" || { printf "MCP_GIT_GUARD_ERROR=branch\\n"; exit 98; }',
            'HEAD_SHA="$(git rev-parse HEAD)" || { printf "MCP_GIT_GUARD_ERROR=head\\n"; exit 98; }',
            'GIT_OPTIONAL_LOCKS=0 git status --porcelain=v1 > "$STATUS_FILE" || { printf "MCP_GIT_GUARD_ERROR=status\\n"; exit 98; }',
            'GIT_INDEX_FILE="$SNAPSHOT" git ls-files --stage -z > "$INDEX_LISTING" || { printf "MCP_GIT_GUARD_ERROR=index_listing\\n"; exit 98; }',
            'STATUS_SHA="$(sha256sum "$STATUS_FILE" | awk \'{print $1}\')" || { printf "MCP_GIT_GUARD_ERROR=status_digest\\n"; exit 98; }',
            'INDEX_SHA="$(sha256sum "$INDEX_LISTING" | awk \'{print $1}\')" || { printf "MCP_GIT_GUARD_ERROR=index_digest\\n"; exit 98; }',
            f"EXPECTED_BRANCH={expected_branch_q}" if expected_branch is not None else "EXPECTED_BRANCH=",
            f"EXPECTED_HEAD={expected_head_q}" if expected_head is not None else "EXPECTED_HEAD=",
            f"EXPECTED_STATUS={expected_status_q}" if expected_status_sha256 is not None else "EXPECTED_STATUS=",
            f"EXPECTED_INDEX={expected_index_q}",
            'if [ -n "$EXPECTED_BRANCH" ] && [ "$BRANCH" != "$EXPECTED_BRANCH" ]; then printf "MCP_GIT_GUARD_MISMATCH=branch\\nMCP_GIT_GUARD_ACTUAL=%s\\n" "$BRANCH"; exit 97; fi',
            'if [ -n "$EXPECTED_HEAD" ] && [ "$HEAD_SHA" != "$EXPECTED_HEAD" ]; then printf "MCP_GIT_GUARD_MISMATCH=head\\nMCP_GIT_GUARD_ACTUAL=%s\\n" "$HEAD_SHA"; exit 97; fi',
            'if [ -n "$EXPECTED_STATUS" ] && [ "$STATUS_SHA" != "$EXPECTED_STATUS" ]; then printf "MCP_GIT_GUARD_MISMATCH=status_sha256\\nMCP_GIT_GUARD_ACTUAL=%s\\n" "$STATUS_SHA"; exit 97; fi',
            'if [ "$INDEX_SHA" != "$EXPECTED_INDEX" ]; then printf "MCP_GIT_GUARD_MISMATCH=index_sha256\\nMCP_GIT_GUARD_ACTUAL=%s\\n" "$INDEX_SHA"; exit 97; fi',
            (
                'GIT_INDEX_FILE="$SNAPSHOT" git '
                "-c core.hooksPath=/dev/null "
                f"-c user.name={shlex.quote('MCP Gateway')} "
                f"-c user.email={shlex.quote('mcp-gateway@gateway.invalid')} "
                f"commit --no-verify -m {message_q}"
            ),
        ]
    )


def _guarded_git_commit_result(
    client: GatewayClient,
    project: str,
    *,
    message: str,
    expected_branch: str | None,
    expected_head: str | None,
    expected_status_sha256: str | None,
    expected_index_sha256: str,
) -> dict[str, Any]:
    script = _guarded_git_commit_script(
        message=message,
        expected_branch=expected_branch,
        expected_head=expected_head,
        expected_status_sha256=expected_status_sha256,
        expected_index_sha256=expected_index_sha256,
    )
    raw = client.execute_project_script(project, script, timeout_s=60)
    project_root = str(_resolve_project(project))
    stdout = _redact_project_root(
        str(raw.get("stdout") or raw.get("output", "")), project_root
    )
    stderr = _redact_project_root(str(raw.get("stderr", "")), project_root)
    exit_code = int(raw.get("exit_code", -1))

    if exit_code == 97:
        field = None
        actual = None
        for line in stdout.splitlines():
            if line.startswith("MCP_GIT_GUARD_MISMATCH="):
                field = line.split("=", 1)[1].strip()
            elif line.startswith("MCP_GIT_GUARD_ACTUAL="):
                actual = line.split("=", 1)[1].strip()
        expected_by_field = {
            "branch": expected_branch,
            "head": expected_head.lower() if expected_head else None,
            "status_sha256": (
                expected_status_sha256.lower() if expected_status_sha256 else None
            ),
            "index_sha256": expected_index_sha256.lower(),
        }
        mismatch = {
            "field": field or "unknown",
            "expected": expected_by_field.get(field or ""),
            "actual": actual,
        }
        return tool_error(
            tool="git_commit",
            code="WORKSPACE_CONTENDED",
            message="Project workspace changed after review; refusing guarded commit",
            retryable=True,
            hint="Refresh info(project), review git_status/git_diff_cached, then retry with fresh lease values.",
            details={
                "project": project,
                "mismatches": [mismatch],
                "mutation_occurred": False,
            },
            source="gateway",
        )
    if exit_code == 96:
        return tool_error(
            tool="git_commit",
            code="WORKSPACE_CONTENDED",
            message="Git index is already locked by another operation; refusing guarded commit",
            retryable=True,
            hint="Wait for the current Git mutation to finish, refresh info(project), and review the staged diff again before retrying.",
            details={"project": project, "mutation_occurred": False},
            source="gateway",
        )
    if exit_code in {98, 99}:
        return tool_error(
            tool="git_commit",
            code="CHECK_FAILED",
            message="Could not establish the guarded Git commit critical section",
            retryable=True,
            hint="Refresh info(project) and git_write_capabilities before retrying; no commit was attempted.",
            details={"project": project, "mutation_occurred": False},
            source="gateway",
        )

    return build_command_result(
        outcome="passed" if exit_code == 0 else "failed",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        execution_duration_ms=_execution_duration_ms(raw),
        job_id=raw.get("job_id"),
        cwd=".",
    )


def git_commit(
    client: GatewayClient,
    project: str,
    message: str,
    expected_branch: str | None = None,
    expected_head: str | None = None,
    expected_status_sha256: str | None = None,
    expected_index_sha256: str | None = None,
) -> dict[str, Any]:
    """Commit staged changes, optionally guarded by an info(project) Git snapshot.

    Any guarded commit requires ``expected_index_sha256`` so a reviewed lease is
    bound to staged blob bytes/modes/paths, not only porcelain status shape.
    """
    project = _validate_project(project)
    lease_requested = any(
        value is not None
        for value in (
            expected_branch,
            expected_head,
            expected_status_sha256,
            expected_index_sha256,
        )
    )
    if lease_requested and expected_index_sha256 is None:
        return tool_error(
            tool="git_commit",
            code="INVALID_INPUT",
            message="Guarded git_commit requires expected_index_sha256 from info(project)",
            retryable=True,
            hint="Refresh info(project), review git_diff_cached, and retry with the current index_sha256.",
            details={"project": project, "mutation_occurred": False},
        )
    if expected_index_sha256 is not None and not re.fullmatch(
        r"[0-9a-fA-F]{64}", expected_index_sha256
    ):
        raise ValueError("INVALID_INPUT: expected_index_sha256 must be a 64-character hex SHA-256")

    if lease_requested:
        state = _project_git_state(_resolve_project(project))
        if not state.get("available"):
            return tool_error(
                tool="git_commit",
                code="CHECK_FAILED",
                message="Current project git state is unavailable; refusing guarded commit",
                retryable=True,
                hint="Refresh info(project) and verify the project is a readable Git worktree before retrying.",
                details={"project": project, "state": state},
            )
        mismatches = _workspace_guard_mismatches(
            state,
            expected_branch=expected_branch,
            expected_head=expected_head,
            expected_status_sha256=expected_status_sha256,
            expected_index_sha256=expected_index_sha256,
        )
        if mismatches:
            return _guarded_git_commit_error(project, state, mismatches)
    probe = probe_git_write_capabilities(client, project)
    required_components = (
        ("index", "objects", "head")
        if probe.get("detached") is True
        else ("index", "objects", "refs")
    )
    preflight_error = _git_write_preflight_from_probe(
        project,
        tool_name="git_commit",
        action="commit_staged_changes",
        required_components=required_components,
        probe=probe,
    )
    if preflight_error is not None:
        return preflight_error
    if lease_requested:
        # The guarded-input validation above fails closed unless this lease is
        # present; keep that runtime invariant explicit for type checking too.
        assert expected_index_sha256 is not None
        # Re-check and commit on the same execution plane while the real index
        # is locked. The Python-side snapshot above remains a cheap early
        # rejection; this target-side critical section is authoritative.
        result = _guarded_git_commit_result(
            client,
            project,
            message=message,
            expected_branch=expected_branch,
            expected_head=expected_head,
            expected_status_sha256=expected_status_sha256,
            expected_index_sha256=expected_index_sha256,
        )
        if result.get("ok") is False:
            return result
    else:
        result = run_project_command(
            client,
            project,
            "git "
            f"-c user.name={shlex.quote('MCP Gateway')} "
            f"-c user.email={shlex.quote('mcp-gateway@gateway.invalid')} "
            f"commit -m {shlex.quote(message)}",
        )
    return _git_mutation_result(
        tool_name="git_commit",
        project=project,
        action="commit_staged_changes",
        required_components=required_components,
        preflight=probe,
        result=result,
    )


# git remote/branch names: no leading '-', no refspec ':', no whitespace.
_GIT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*$")


def _validate_git_name(value: str, field: str) -> str:
    """Reject git option injection (--mirror/--force/--delete, refspec ':').

    shlex.quote() protects against shell injection but not against git
    option injection; a strict name pattern is the actual control.
    """
    if not value or not _GIT_NAME_RE.match(value):
        raise ValueError(f"INVALID_INPUT: {field} {value!r} is not a valid git remote/branch name")
    return value


def git_fetch_ref(
    client: GatewayClient,
    project: str,
    remote: str = "origin",
    branch: str = "master",
    expected_remote_head: str | None = None,
) -> dict[str, Any]:
    """Fetch one trusted remote branch into refs/remotes/<remote>/<branch>."""
    project = _validate_project(project)
    remote = _validate_git_name(remote, "remote")
    branch = _validate_git_name(branch, "branch")
    expected_remote_head = _validate_expected_git_head(expected_remote_head)
    preflight_error, _preflight = _git_write_preflight(
        client,
        project,
        tool_name="git_fetch_ref",
        action="fetch_guarded_remote_ref",
        required_components=("objects", "refs", "fetch_head"),
    )
    if preflight_error is not None:
        return preflight_error
    return git_fetch_ref_control_plane(
        project=project,
        remote=remote,
        branch=branch,
        expected_remote_head=expected_remote_head,
    )


def git_refresh_branch_to_head(
    client: GatewayClient,
    project: str,
    branch: str,
    expected_current_head: str,
    target_head: str,
) -> dict[str, Any]:
    """Fast-forward the current clean local branch to one exact fetched commit."""
    branch = _validate_git_name(branch, "branch")
    normalized_current_head = _validate_expected_git_head(expected_current_head)
    normalized_target_head = _validate_expected_git_head(target_head)
    if normalized_current_head is None or normalized_target_head is None:
        raise ValueError("INVALID_INPUT: expected_current_head and target_head are required")
    expected_current_head = normalized_current_head
    target_head = normalized_target_head

    project = _validate_project(project)
    resolved = _resolve_project(project)
    state = _project_git_state(resolved)
    if not state.get("available"):
        return tool_error(
            tool="git_refresh_branch_to_head",
            code="CHECK_FAILED",
            message="Current project git state is unavailable; refusing workspace refresh.",
            retryable=True,
            hint="Refresh info(project) and verify the project is a readable Git worktree before retrying.",
            details={"project": project, "state": state},
            source="gateway",
        )
    if state.get("dirty"):
        return tool_error(
            tool="git_refresh_branch_to_head",
            code="WORKSPACE_CONTENDED",
            message="Working tree is dirty; refusing exact-head refresh.",
            retryable=False,
            hint="Commit, stash, or discard local changes before refreshing the workspace.",
            details={
                "project": project,
                "branch": state.get("branch"),
                "head": state.get("head"),
                "status_entries": state.get("status_entries"),
            },
            source="gateway",
        )
    if state.get("detached") or state.get("branch") == "HEAD":
        return tool_error(
            tool="git_refresh_branch_to_head",
            code="GIT_DETACHED_HEAD",
            message="Workspace is detached; refusing to move a local branch implicitly.",
            retryable=False,
            details={"project": project, "head": state.get("head")},
            source="gateway",
        )

    current_branch = str(state.get("branch") or "")
    current_head = str(state.get("head") or "").strip().lower()
    if current_branch != branch:
        return tool_error(
            tool="git_refresh_branch_to_head",
            code="WORKSPACE_CONTENDED",
            message="Workspace is not currently on the requested branch; refusing implicit branch switch.",
            retryable=True,
            details={
                "project": project,
                "expected_branch": branch,
                "actual_branch": current_branch,
                "actual_head": current_head,
            },
            source="gateway",
        )
    if current_head != expected_current_head:
        return tool_error(
            tool="git_refresh_branch_to_head",
            code="HEAD_MISMATCH",
            message="Current branch HEAD does not match expected_current_head; refusing refresh.",
            retryable=True,
            details={
                "project": project,
                "branch": branch,
                "expected_current_head": expected_current_head,
                "actual_head": current_head,
            },
            source="gateway",
        )

    target_commit = _local_git_output(resolved, ["rev-parse", "--verify", f"{target_head}^{{commit}}"])
    if target_commit is None or target_commit.strip().lower() != target_head:
        return tool_error(
            tool="git_refresh_branch_to_head",
            code="GIT_LOCAL_REF_MISSING",
            message="Target commit is not available locally; fetch it before requesting workspace refresh.",
            retryable=False,
            details={"project": project, "branch": branch, "target_head": target_head},
            source="gateway",
        )

    if _local_git_output(resolved, ["merge-base", "--is-ancestor", current_head, target_head]) is None:
        return tool_error(
            tool="git_refresh_branch_to_head",
            code="GIT_NON_FAST_FORWARD",
            message="Target commit is not a descendant of the current HEAD; refusing history rewrite.",
            retryable=False,
            details={
                "project": project,
                "branch": branch,
                "current_head": current_head,
                "target_head": target_head,
            },
            source="gateway",
        )

    preflight_error, preflight = _git_write_preflight(
        client,
        project,
        tool_name="git_refresh_branch_to_head",
        action="refresh_current_branch_to_exact_head",
        required_components=("index", "refs", "head"),
        branch=branch,
    )
    if preflight_error is not None:
        return preflight_error

    # Metadata writeability does not prove that checkout can create new files
    # in root-owned paths such as scripts/. Probe all changed path parents on
    # the SSH execution plane before reset --keep mutates any worktree content.
    worktree_preflight = probe_git_refresh_worktree_paths(
        client, project, current_head=current_head, target_head=target_head
    )
    if not worktree_preflight["available"]:
        return tool_error(
            tool="git_refresh_branch_to_head",
            code="GIT_WRITE_PREFLIGHT_UNAVAILABLE",
            message="Cannot prove exact-head worktree destination writeability; no refresh attempted.",
            retryable=True,
            hint="Restore same-plane path inspection before retrying; do not retry a partial Git checkout blindly.",
            details={
                "project": project,
                "reason": worktree_preflight.get("reason"),
                "mutation_occurred": False,
            },
            source="gateway",
        )
    if worktree_preflight["blocked_count"]:
        return tool_error(
            tool="git_refresh_branch_to_head",
            code="GIT_WRITE_CAPABILITY_BLOCKED",
            message="Exact-head refresh would write tracked files under non-writeable or unsafe parent directories.",
            retryable=False,
            hint="Review the blocked relative paths and obtain narrowly scoped owner-approved remediation; no checkout was attempted.",
            details={
                "project": project,
                "branch": branch,
                "current_head": current_head,
                "target_head": target_head,
                "checked_count": worktree_preflight["checked_count"],
                "blocked_count": worktree_preflight["blocked_count"],
                "blocked_paths": worktree_preflight["blocked_paths"],
                "mutation_occurred": False,
            },
            source="gateway",
        )

    result = run_project_command(
        client,
        project,
        f"git reset --keep {shlex.quote(target_head)}",
    )
    if result.get("exit_code") != 0:
        return _git_mutation_result(
            tool_name="git_refresh_branch_to_head",
            project=project,
            action="refresh_current_branch_to_exact_head",
            required_components=("index", "refs", "head"),
            preflight=preflight,
            result=result,
        )

    final_state = _project_git_state(resolved)
    final_head = str(final_state.get("head") or "").strip().lower()
    if (
        not final_state.get("available")
        or final_state.get("dirty")
        or final_state.get("branch") != branch
        or final_head != target_head
    ):
        return tool_error(
            tool="git_refresh_branch_to_head",
            code="WORKSPACE_VERIFICATION_FAILED",
            message="Exact-head refresh command returned success but workspace postconditions were not met.",
            retryable=False,
            details={
                "project": project,
                "branch": branch,
                "previous_head": current_head,
                "target_head": target_head,
                "final_state": final_state,
            },
            source="gateway",
        )

    return {
        "outcome": "passed",
        "exit_code": 0,
        "stdout": str(result.get("stdout", "")),
        "stderr": str(result.get("stderr", "")),
        "execution_duration_ms": _execution_duration_ms(result),
        "job_id": result.get("job_id"),
        "project": project,
        "branch": branch,
        "previous_head": current_head,
        "target_head": target_head,
        "new_head": final_head,
        "clean": True,
        "fast_forward_only": True,
    }


def git_create_branch(
    client: GatewayClient,
    project: str,
    branch: str,
) -> dict[str, Any]:
    """Create and switch to a new non-protected local branch.

    This is deliberately narrower than a generic checkout/switch surface: it
    cannot move to arbitrary refs and cannot create ``main``/``master``. It
    exists so protected-master deployments can follow the intended
    feature-branch -> PR workflow instead of deadlocking after ``git commit``.
    """
    branch = _validate_git_name(branch, "branch")
    if branch in {"main", "master"}:
        raise ValueError(f"POLICY_DENIED: creating protected branch {branch!r} is not allowed")
    return _run_git_mutation_with_preflight(
        client,
        project,
        f"git switch -c {shlex.quote(branch)}",
        tool_name="git_create_branch",
        action="create_and_switch_branch",
        required_components=("index", "refs", "head"),
        branch=branch,
    )


def _validate_expected_git_head(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"INVALID_INPUT: expected_head must be a string or None, got {type(value).__name__}")
    normalized = value.strip().lower()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", normalized):
        raise ValueError(
            "INVALID_INPUT: expected_head must be a full 40- or 64-character hex commit id"
        )
    return normalized


def _git_unmerged_paths(status: str | None) -> list[str]:
    """Return unmerged/conflicted paths from porcelain v1 status output."""
    paths: list[str] = []
    for line in (status or "").splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        if "U" not in code and code not in {"AA", "DD"}:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        paths.append(path)
    return paths


def _git_merge_conflict_error(
    client: GatewayClient,
    project: str,
    branch: str,
    source_branch: str,
    target_head: str,
    source_head: str,
    original_branch: str,
    original_ref: str,
    original_head: str,
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    """Type a merge conflict, run merge --abort, and restore the caller's branch.

    Fails closed: if abort/restore does not fully succeed the error is upgraded
    to WORKSPACE_CONTENDED and the contaminated state is not hidden.
    """
    status_before_recovery = _local_git_output(_resolve_project(project), ["status", "--porcelain=v1"])
    conflicted_paths = _git_unmerged_paths(status_before_recovery)

    abort_result = run_project_command(client, project, "git merge --abort")
    merge_aborted = abort_result.get("exit_code") == 0

    switch_back_result: dict[str, Any] | None = None
    if original_ref == "DETACHED_HEAD":
        switch_back_result = run_project_command(
            client, project, f"git switch --detach {shlex.quote(original_head)}"
        )
    elif original_branch and original_branch != branch:
        switch_back_result = run_project_command(
            client, project, f"git switch {shlex.quote(original_branch)}"
        )
    restored_original_ref = switch_back_result is None or switch_back_result.get("exit_code") == 0

    recovery_state = _project_git_state(_resolve_project(project))
    final_branch = recovery_state.get("branch") if recovery_state.get("available") else None
    final_head = recovery_state.get("head") if recovery_state.get("available") else None
    final_status_entries = (
        recovery_state.get("status_entries") if recovery_state.get("available") else None
    )
    recovered = (
        merge_aborted
        and restored_original_ref
        and recovery_state.get("available")
        and final_head == original_head
        and not recovery_state.get("dirty")
    )

    recovery = {
        "merge_aborted": merge_aborted,
        "restored_original_ref": restored_original_ref,
        "final_branch": final_branch,
        "final_head": final_head,
        "final_status_entries": final_status_entries,
    }

    if recovered:
        message = "Merge conflict while updating branch; merge aborted and original clean HEAD restored"
        hint = (
            f"Resolve the conflict on {branch!r} and retry, or update {source_branch!r} and merge again. "
            "The working tree was restored to its pre-merge state."
        )
    else:
        message = "Merge conflict while updating branch; automatic recovery did not fully clean the worktree"
        hint = "Inspect `git status` and the recovery metadata before retrying; the workspace may still be contaminated."

    return tool_error(
        tool="git_update_branch_by_merge",
        code="MERGE_CONFLICT" if recovered else "WORKSPACE_CONTENDED",
        message=message,
        retryable=True,
        hint=hint,
        details={
            "project": project,
            "branch": branch,
            "source_branch": source_branch,
            "previous_head": target_head,
            "source_head": source_head,
            "original_branch": original_branch,
            "original_ref": original_ref,
            "original_head": original_head,
            "conflicted_paths": conflicted_paths,
            "recovery": recovery,
        },
        result={
            "outcome": "failed",
            "exit_code": 1,
            "stdout": stdout,
            "stderr": stderr,
        },
        source="gateway",
    )


def git_update_branch_by_merge(
    client: GatewayClient,
    project: str,
    branch: str,
    source_branch: str = "master",
    expected_head: str | None = None,
) -> dict[str, Any]:
    """Switch to an existing feature branch and merge a source branch into it.

    This closes the safe-tool gap between create-only branch tooling and a
    generic shell. It refuses protected targets, dirty worktrees, missing local
    refs, and optional expected-head mismatches before running ``git merge``.
    A real merge conflict aborts the merge, restores the caller's original
    branch/HEAD, and returns a typed ``MERGE_CONFLICT`` error.
    """
    branch = _validate_git_name(branch, "branch")
    source_branch = _validate_git_name(source_branch, "source_branch")
    expected_head = _validate_expected_git_head(expected_head)
    if branch in {"main", "master"}:
        raise ValueError(f"POLICY_DENIED: updating protected branch {branch!r} is not allowed")
    if branch == source_branch:
        raise ValueError("INVALID_INPUT: branch and source_branch must be different")

    project = _validate_project(project)
    resolved = _resolve_project(project)
    state = _project_git_state(resolved)
    if not state.get("available"):
        return tool_error(
            tool="git_update_branch_by_merge",
            code="CHECK_FAILED",
            message="Current project git state is unavailable; refusing branch update",
            retryable=True,
            hint="Refresh info(project) and verify the project is a readable Git worktree before retrying.",
            details={"project": project, "state": state},
            source="gateway",
        )
    if state.get("dirty"):
        return tool_error(
            tool="git_update_branch_by_merge",
            code="WORKSPACE_CONTENDED",
            message="Working tree is dirty; refusing to switch branches or merge",
            retryable=False,
            hint="Commit, stash, or discard local changes before updating a branch by merge.",
            details={
                "project": project,
                "branch": state.get("branch"),
                "head": state.get("head"),
                "status_entries": state.get("status_entries"),
            },
            source="gateway",
        )

    original_branch = str(state.get("branch", ""))
    original_head = str(state.get("head", "")).lower()
    if original_branch == "HEAD":
        original_ref = "DETACHED_HEAD"
    else:
        original_ref = f"refs/heads/{original_branch}"
        if original_branch:
            _validate_git_name(original_branch, "current_branch")

    target_ref = f"refs/heads/{branch}^{{commit}}"
    target_head = _local_git_output(resolved, ["rev-parse", "--verify", target_ref])
    if target_head is None:
        return tool_error(
            tool="git_update_branch_by_merge",
            code="GIT_LOCAL_REF_MISSING",
            message=f"Local target branch {branch!r} was not found",
            retryable=False,
            hint="Create or fetch the target branch before requesting a merge update.",
            details={"project": project, "branch": branch},
            source="gateway",
        )
    target_head = target_head.strip().lower()
    if expected_head and target_head != expected_head:
        return tool_error(
            tool="git_update_branch_by_merge",
            code="HEAD_MISMATCH",
            message="Target branch HEAD does not match expected_head; refusing merge",
            retryable=True,
            hint="Refresh the branch state and retry with the current target HEAD if the change is intentional.",
            details={
                "project": project,
                "branch": branch,
                "expected_head": expected_head,
                "actual_head": target_head,
            },
            source="gateway",
        )

    source_ref = f"{source_branch}^{{commit}}"
    source_head = _local_git_output(resolved, ["rev-parse", "--verify", source_ref])
    if source_head is None:
        return tool_error(
            tool="git_update_branch_by_merge",
            code="GIT_LOCAL_REF_MISSING",
            message=f"Source branch/ref {source_branch!r} was not found",
            retryable=False,
            hint="Fetch or create the source branch/ref before requesting a merge update.",
            details={"project": project, "source_branch": source_branch},
            source="gateway",
        )
    source_head = source_head.strip().lower()

    preflight_error, preflight = _git_write_preflight(
        client,
        project,
        tool_name="git_update_branch_by_merge",
        action="switch_and_merge_branch",
        required_components=("index", "objects", "refs", "head"),
        branch=branch,
    )
    if preflight_error is not None:
        return preflight_error

    commands = [
        f"git switch {shlex.quote(branch)}",
        f"git merge --no-ff --no-edit {shlex.quote(source_branch)}",
        "git rev-parse HEAD",
    ]
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    last: dict[str, Any] | None = None
    for command in commands:
        last = run_project_command(client, project, command)
        stdout_parts.append(str(last.get("stdout", "")))
        stderr_parts.append(str(last.get("stderr", "")))
        if last.get("exit_code") != 0:
            stdout = "".join(stdout_parts)
            stderr = "".join(stderr_parts)
            status_after_failure = _local_git_output(resolved, ["status", "--porcelain=v1"])
            has_merge_head = (
                _local_git_output(resolved, ["rev-parse", "--verify", "-q", "MERGE_HEAD"]) is not None
            )
            if has_merge_head or _git_unmerged_paths(status_after_failure):
                return _git_merge_conflict_error(
                    client,
                    project,
                    branch,
                    source_branch,
                    target_head,
                    source_head,
                    original_branch,
                    original_ref,
                    original_head,
                    stdout,
                    stderr,
                )
            last["stdout"] = stdout
            last["stderr"] = stderr
            last["branch"] = branch
            last["source_branch"] = source_branch
            last["previous_head"] = target_head
            last["source_head"] = source_head
            return _git_mutation_result(
                tool_name="git_update_branch_by_merge",
                project=project,
                action="switch_and_merge_branch",
                required_components=("index", "objects", "refs", "head"),
                preflight=preflight,
                result=last,
            )

    new_head = str(last.get("stdout", "")).strip() if last else ""
    return {
        "outcome": "passed",
        "exit_code": 0,
        "stdout": "".join(stdout_parts),
        "stderr": "".join(stderr_parts),
        "execution_duration_ms": None,
        "job_id": None,
        "branch": branch,
        "source_branch": source_branch,
        "previous_head": target_head,
        "source_head": source_head,
        "new_head": new_head,
    }


def git_push(
    _client: GatewayClient,
    project: str,
    remote: str = "origin",
    branch: str | None = None,
) -> dict[str, Any]:
    _validate_git_name(remote, "remote")
    if branch is not None:
        _validate_git_name(branch, "branch")
    return git_push_control_plane(project=project, remote=remote, branch=branch)


def run_tests(client: GatewayClient, project: str) -> dict[str, Any]:
    """Submit the full test suite (pytest -q) as a background job.

    Returns immediately with the job_id; poll via gateway_job_status /
    gateway_job_result. run_pytest() covers targeted synchronous runs.
    """
    return _run_uv_tool(client, project, "pytest", "run_tests", ["."], async_submit=True)


def run_lint(client: GatewayClient, project: str) -> dict[str, Any]:
    return _run_uv_tool(client, project, "ruff", "run_lint", ["."])


def run_compileall(
    client: GatewayClient,
    project: str,
    target: list[str] | None = None,
) -> dict[str, Any]:
    return _run_uv_tool(client, project, "compileall", "run_compileall", target)
