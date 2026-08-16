"""Trusted publication of immutable Git sources for managed agent workers.

The worker/executor only receives ``MCP_AGENT_SOURCE_ROOT`` read-only. This
module runs in the MCP control plane, resolves a registered project root, and
materializes one content-addressed Git bundle for an exact commit id. A dirty
working tree is deliberately irrelevant: only committed Git objects are
fetched into a temporary bare repository before the final bundle is published
atomically.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from app.workspace.registry import get_registry
from examples.mcp_server.agent_paths import managed_source_bundle_path
from examples.mcp_server.agent_tasks import validate_base_ref

_GIT_TIMEOUT_SECONDS = 120


class ManagedSourceBundleError(RuntimeError):
    """Raised when trusted source publication cannot prove the requested SHA."""


def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    safe_directory: Path | None = None,
) -> str:
    command = ["git"]
    if safe_directory is not None:
        command.extend(["-c", f"safe.directory={safe_directory}"])
    command.extend(args)
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            text=True,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ManagedSourceBundleError(
            f"managed source publication timed out during git {args[0]}"
        ) from exc
    except OSError as exc:
        raise ManagedSourceBundleError(
            "git is unavailable for managed source publication"
        ) from exc
    if result.returncode != 0:
        # Git failures commonly include the authoritative host path. Keep the
        # MCP error useful without reflecting that path to a worker/client.
        raise ManagedSourceBundleError(
            f"managed source publication failed during git {args[0]}"
        )
    return result.stdout


def _bundle_head(path: Path) -> str | None:
    try:
        output = _run_git(["bundle", "list-heads", str(path)])
    except ManagedSourceBundleError:
        return None
    heads = [
        line.split(maxsplit=1)[0].lower()
        for line in output.splitlines()
        if line.strip()
    ]
    return heads[0] if len(heads) == 1 else None


def ensure_managed_source_bundle(project: str, base_ref: str | None) -> str | None:
    """Publish/reuse a self-contained bundle for ``project`` at ``base_ref``.

    Returns ``None`` when managed source storage is not configured (legacy
    local/dev mode). Once ``MCP_AGENT_SOURCE_ROOT`` is configured, an exact
    full commit id is mandatory and publication fails closed.
    """

    validate_base_ref(base_ref)
    if not base_ref:
        if not os.environ.get("MCP_AGENT_SOURCE_ROOT", "").strip():
            return None
        raise ValueError("managed OpenCode tasks require an exact base_ref")

    bundle_raw = managed_source_bundle_path(project, base_ref)
    if bundle_raw is None:
        return None

    expected = base_ref.lower()
    bundle_path = Path(bundle_raw)
    bundle_path.parent.mkdir(parents=True, exist_ok=True)

    if bundle_path.is_file() and _bundle_head(bundle_path) == expected:
        return str(bundle_path)

    project_root = Path(get_registry().project_info(project)["root"])
    _run_git(
        ["cat-file", "-e", f"{base_ref}^{{commit}}"],
        cwd=project_root,
        safe_directory=project_root,
    )

    temp_fd, temp_name = tempfile.mkstemp(
        prefix=f".{expected}.", suffix=".bundle.tmp", dir=bundle_path.parent
    )
    os.close(temp_fd)
    temp_bundle = Path(temp_name)
    temp_bundle.unlink()

    try:
        with tempfile.TemporaryDirectory(prefix="mcp-agent-source-") as bare_dir:
            bare = Path(bare_dir) / "source.git"
            _run_git(["init", "--bare", str(bare)])
            _run_git(
                [
                    f"--git-dir={bare}",
                    "fetch",
                    "--no-tags",
                    "--no-recurse-submodules",
                    str(project_root),
                    f"{base_ref}:refs/heads/source",
                ],
                safe_directory=project_root,
            )
            fetched = _run_git(
                [f"--git-dir={bare}", "rev-parse", "refs/heads/source^{commit}"]
            ).strip().lower()
            if fetched != expected:
                raise ManagedSourceBundleError(
                    "managed source fetch resolved to an unexpected commit"
                )
            _run_git(
                [
                    f"--git-dir={bare}",
                    "bundle",
                    "create",
                    str(temp_bundle),
                    "refs/heads/source",
                ]
            )

        if _bundle_head(temp_bundle) != expected:
            raise ManagedSourceBundleError("managed source bundle verification failed")

        os.replace(temp_bundle, bundle_path)
        if _bundle_head(bundle_path) != expected:
            bundle_path.unlink(missing_ok=True)
            raise ManagedSourceBundleError(
                "published managed source bundle verification failed"
            )
        return str(bundle_path)
    finally:
        temp_bundle.unlink(missing_ok=True)
