"""Git and recovery routes."""

import shlex
import time

from fastapi import APIRouter, Depends, HTTPException, Query

from app import state as _state
from app.auth_middleware import AuthIdentity, require_master_key
from app.models import (
    BackupInfo,
    CreateBackupRequest,
    GitActionResponse,
    GitCommitRequest,
    GitDiffRequest,
    GitDiffResponse,
    GitInfoResponse,
    GitInitRequest,
    GitStatusResponse,
    ListBackupsResponse,
    RecoveryActionResponse,
    RestoreBackupRequest,
)
from app.state import _err

router = APIRouter(tags=["git"])


async def _get_context_or_404(context_id: str):
    """Get context and verify its session is active."""
    ctx = await _state.context_manager.get_context(context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, f"Context {context_id} not found"))
    session = await _state.manager.get_session(ctx.session_id)
    if not session:
        raise HTTPException(status_code=403, detail=_err(403, "Context session is no longer active"))
    return ctx


@router.post("/api/git/init", response_model=GitActionResponse)
async def git_init(req: GitInitRequest, _identity: AuthIdentity = Depends(require_master_key)):
    """Initialize git repository for context."""
    result = await _state.context_manager.init_git(req.context_id, req.remote_url)
    return GitActionResponse(**result)


@router.post("/api/git/commit", response_model=GitActionResponse)
async def git_commit(req: GitCommitRequest, _identity: AuthIdentity = Depends(require_master_key)):
    """Create a git commit for context."""
    result = await _state.context_manager.commit_changes(
        req.context_id,
        req.message,
        req.files
    )
    return GitActionResponse(**result)


@router.post("/api/git/backup", response_model=GitActionResponse)
async def git_backup(context_id: str, _identity: AuthIdentity = Depends(require_master_key), backup_name: str = "auto_backup"):
    """Create a git stash backup."""
    await _get_context_or_404(context_id)
    result = await _state.context_manager.create_backup(context_id, backup_name)
    return GitActionResponse(**result)


@router.post("/api/git/restore", response_model=GitActionResponse)
async def git_restore(context_id: str, _identity: AuthIdentity = Depends(require_master_key)):
    """Restore from stash."""
    await _get_context_or_404(context_id)
    result = await _state.context_manager.restore_backup(context_id)
    return GitActionResponse(**result)


@router.get("/api/git/diff")
async def git_diff_query(context_id: str, _identity: AuthIdentity = Depends(require_master_key)):
    """Get git diff for context."""
    ctx = await _get_context_or_404(context_id)

    from app.git_manager import GitManager
    git = GitManager(_state.manager)
    diff = await git.diff(ctx.session_id, ctx.path)
    return {"context_id": context_id, "diff": diff}


@router.post("/api/git/status")
async def git_status(context_id: str, _identity: AuthIdentity = Depends(require_master_key)):
    """Refresh git status for context."""
    await _get_context_or_404(context_id)
    git_info = await _state.context_manager.update_git_status(context_id)
    return GitInfoResponse(
        status=git_info.status.value,
        branch=git_info.branch,
        has_changes=git_info.has_changes,
        last_commit=git_info.last_commit,
        remote_url=git_info.remote_url,
        message=git_info.message,
        can_commit=git_info.can_commit,
    )


@router.get("/api/git/simple-status")
async def git_simple_status(
    _identity: AuthIdentity = Depends(require_master_key),
    session_id: str = Query(...),
    path: str = Query(default="."),
):
    """Simple git status — branch, modified, staged, untracked files."""
    branch_res = await _state.manager.execute(
        session_id, f"cd {shlex.quote(path)} && git branch --show-current 2>/dev/null || echo 'main'", timeout=10
    )
    branch = branch_res["stdout"].strip() or "main"

    status_res = await _state.manager.execute(
        session_id,
        f"cd {shlex.quote(path)} && git status --porcelain 2>/dev/null || echo 'ERROR'",
        timeout=10,
    )

    modified = []
    staged = []
    untracked = []

    for line in status_res["stdout"].strip().split("\n"):
        if not line or line == "ERROR":
            continue
        if len(line) < 3:
            continue
        status_code = line[:2]
        file_path = line[3:].strip()

        if status_code[0] in "MADRC":
            staged.append(file_path)
        if status_code[1] in "MADRC":
            modified.append(file_path)
        if status_code == "??":
            untracked.append(file_path)

    return GitStatusResponse(
        branch=branch,
        clean=not (modified or staged or untracked),
        modified=modified,
        staged=staged,
        untracked=untracked,
    )


@router.post("/api/git/diff", response_model=GitDiffResponse)
async def git_diff(req: GitDiffRequest, _identity: AuthIdentity = Depends(require_master_key)):
    """Get git diff for working directory or staged changes."""
    flag = "--cached" if req.cached else ""
    result = await _state.manager.execute(
        req.session_id,
        f"cd {shlex.quote(req.path)} && git diff {flag} 2>/dev/null || echo 'ERROR'",
        timeout=30,
    )

    if "ERROR" in result["stdout"]:
        raise HTTPException(status_code=500, detail=_err(500, "Git diff failed"))

    diff = result["stdout"]
    files_changed = diff.count("diff --git")

    return GitDiffResponse(
        path=req.path,
        diff=diff,
        files_changed=files_changed,
    )


@router.post("/api/recovery/backup", response_model=RecoveryActionResponse)
async def recovery_backup(req: CreateBackupRequest, _identity: AuthIdentity = Depends(require_master_key)):
    """Create a backup before making changes."""
    await _get_context_or_404(req.context_id)

    result = await _state.context_manager.create_backup(req.context_id, req.name)

    return RecoveryActionResponse(
        success=result.get("success", False),
        message=result.get("message", ""),
        backup_id=req.name,
    )


@router.post("/api/recovery/restore", response_model=RecoveryActionResponse)
async def recovery_restore(req: RestoreBackupRequest, _identity: AuthIdentity = Depends(require_master_key)):
    """Restore from backup."""
    await _get_context_or_404(req.context_id)

    result = await _state.context_manager.restore_backup(req.context_id)

    return RecoveryActionResponse(
        success=result.get("success", False),
        message=result.get("message", ""),
        restored_files=["all_stashed_files"],
    )


@router.get("/api/recovery/backups")
async def recovery_list_backups(context_id: str, _identity: AuthIdentity = Depends(require_master_key)):
    """List available backups."""
    ctx = await _get_context_or_404(context_id)

    result = await _state.manager.execute(
        ctx.session_id,
        f"cd {shlex.quote(ctx.path)} && git stash list",
        timeout=10
    )

    backups = []
    for line in result["stdout"].strip().split("\n"):
        if line:
            parts = line.split(": ", 1)
            if len(parts) >= 2:
                stash_id = parts[0].strip()
                message = parts[1].strip()
                backups.append(BackupInfo(
                    id=stash_id,
                    name=message,
                    created_at=time.time(),
                ))

    return ListBackupsResponse(backups=backups, count=len(backups))
