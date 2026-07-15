"""File operations, AST, and batch routes."""

import asyncio
import logging
import os
import shlex
import time
import uuid
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import PlainTextResponse

from app import state as _state
from app.ast_refactor import ASTRefactor
from app.auth_middleware import AuthIdentity, ensure_session_owner, require_scope, ws_auth_check
from app.config import settings
from app.models import (
    ASTAnalyzeRequest,
    ASTAnalyzeResponse,
    ASTRefactorExtractRequest,
    ASTRefactorExtractResponse,
    ASTRefactorFileResult,
    ASTRefactorRenameRequest,
    ASTRefactorRenameResponse,
    BatchEditRequest,
    BatchEditResponse,
    BatchEditResult,
    BatchReadRequest,
    BatchReadResponse,
    FileEditRequest,
    FileEditResponse,
    FileMetadata,
    FileReadRequest,
    FileReadResponse,
    FileUploadRequest,
    FileUploadResponse,
    FileWriteRequest,
    FileWriteResponse,
    PatchApplyRequest,
    PatchApplyResponse,
    ProjectPatchApplyRequest,
    ProjectPatchApplyResponse,
    ProjectPatchFileResult,
    ProjectStructureRequest,
    ProjectStructureResponse,
)
from app.patch_apply import (
    HashMismatchError,
    PatchApplier,
    PatchValidationError,
    RollbackFailedError,
)
from app.security import rate_limit_mutation, validate_path
from app.state import _err

logger = logging.getLogger(__name__)

router = APIRouter(tags=["files"])


async def _check_session_ownership(session_id: str, request: Request) -> None:
    """Check session ownership if caller identity is available."""
    _identity: AuthIdentity | None = getattr(request.state, "auth_identity", None)
    if _identity is None:
        return
    session = await _state.manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=_err(404, "Session not found"))
    ensure_session_owner(session, _identity)


# ---------------------------------------------------------------------------
# File Edit
# ---------------------------------------------------------------------------


@router.post("/api/file/read", response_model=FileReadResponse)
async def file_read(
    req: FileReadRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:files")),
):
    """Read a file from a remote server."""
    await _check_session_ownership(req.session_id, request)

    try:
        validated = validate_path(req.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc))) from exc

    _state.audit_logger.log_file_access(req.session_id, validated, "READ", request.client.host)
    try:
        content = await _state.file_editor.read_file(req.session_id, validated)
    except Exception as exc:
        err_msg = str(exc)
        logger.warning(
            "File read failed: session=%s path=%s error=%s", req.session_id, validated, err_msg
        )
        if "Cannot read" in err_msg:
            raise HTTPException(status_code=404, detail=_err(404, err_msg)) from exc
        raise HTTPException(
            status_code=500, detail=_err(500, f"File read failed: {err_msg}")
        ) from exc
    return FileReadResponse(path=validated, content=content)


def _assert_rw() -> None:
    if settings.workspace_readonly:
        raise HTTPException(
            status_code=403,
            detail=_err(403, "WORKSPACE_READONLY: write operations are disabled"),
        )


@router.patch("/api/file/edit", response_model=FileEditResponse)
@rate_limit_mutation(30, "minute")
async def file_edit(
    req: FileEditRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:files")),
):
    """Edit a remote file using patch operations."""
    _assert_rw()
    await _check_session_ownership(req.session_id, request)

    try:
        validated = validate_path(req.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc))) from exc
    try:
        logger.info(
            f"File edit request: session={req.session_id}, path={validated}, ops={len(req.operations)}"
        )
        result = await _state.file_editor.edit_file(
            req.session_id,
            validated,
            [op.model_dump() for op in req.operations],
        )
        logger.info(f"File edit result: {result}")
        return FileEditResponse(**result)
    except Exception as exc:
        logger.error(f"File edit failed: {exc}")
        raise HTTPException(status_code=500, detail=_err(500, f"File edit failed: {exc}")) from exc


@router.post("/api/file/patch", response_model=PatchApplyResponse)
async def file_patch(
    req: PatchApplyRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:files")),
):
    """Apply a unified diff patch."""
    _assert_rw()
    await _check_session_ownership(req.session_id, request)

    result = await _state.file_editor.apply_patch(
        req.session_id,
        req.patch,
        req.strip,
    )
    return PatchApplyResponse(**result)


@router.post("/api/projects/{project}/apply-patch", response_model=ProjectPatchApplyResponse)
async def project_apply_patch(
    project: str,
    req: ProjectPatchApplyRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("project:patch")),
):
    """Apply a unified diff patch to project files with hash verification and rollback."""
    # Session ownership
    session = await _state.manager.get_session(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=_err(404, "Session not found"))
    ensure_session_owner(session, _identity)

    applier = PatchApplier()
    rid = uuid.uuid4().hex[:12]

    try:
        # Validate patch size
        applier._validate_patch_size(len(req.patch.encode("utf-8")))

        # Parse patch
        files = applier._parse_patch(req.patch, strip=req.strip)

        # Validate limits
        applier._validate_file_count(len(files))
        total_hunks = sum(f["hunk_count"] for f in files)
        applier._validate_hunk_count(total_hunks)

        # Validate forbidden ops
        applier._validate_no_forbidden_ops(files)

        # Resolve project path via registry
        from examples.mcp_server.project_registry import get_project_registry

        registry = get_project_registry()
        try:
            project_root = registry.resolve(project)
        except ValueError as exc:
            raise HTTPException(
                status_code=404, detail=_err(404, str(exc))
            ) from exc

        file_results: list[ProjectPatchFileResult] = []

        # Dry run: apply in memory only
        if req.dry_run:
            preview_parts = []
            for f in files:
                full_path = project_root / f["path"]
                if full_path.exists():
                    original = full_path.read_text(encoding="utf-8", errors="replace")
                else:
                    original = ""
                new_content = applier._apply_in_memory(original, f)
                if original != new_content:
                    preview_parts.append(f"--- {f['path']}\n+++ {f['path']}\n{new_content}")
                file_results.append(
                    ProjectPatchFileResult(
                        path=f["path"],
                        status="dry_run",
                        hunks_applied=f["hunk_count"],
                    )
                )
            return ProjectPatchApplyResponse(
                success=True,
                files_applied=len(files),
                files_failed=0,
                hunks_applied=total_hunks,
                preview="\n".join(preview_parts),
                files=file_results,
            )

        # Apply: read, verify hashes, apply in memory
        prepared: list[tuple[object, str, str, int]] = []

        for f in files:
            full_path = project_root / f["path"]

            # Check file size
            if full_path.exists():
                file_size = full_path.stat().st_size
                if file_size > applier.MAX_FILE_SIZE:
                    raise PatchValidationError(
                        f"File '{f['path']}' is {file_size} bytes, exceeds {applier.MAX_FILE_SIZE} limit"
                    )
                original = full_path.read_text(encoding="utf-8", errors="replace")
            else:
                original = ""

            # Verify hash for existing files
            if f["path"] in req.expected_hashes and full_path.exists():
                applier._check_hash(f["path"], original, req.expected_hashes[f["path"]])

            new_content = applier._apply_in_memory(original, f)
            prepared.append((full_path, original, new_content, f["hunk_count"]))

        # Transactional write with rollback
        completed: list[tuple[object, object]] = []

        for full_path, _original, new_content, _hunk_count in prepared:
            import shutil

            backup = full_path.parent / f".{full_path.name}.mcp-patch-{rid}.bak"
            tmp = full_path.parent / f".{full_path.name}.mcp-patch-{rid}.tmp"

            try:
                # Backup
                if full_path.exists():
                    shutil.copy2(str(full_path), str(backup))
                else:
                    backup.write_text("", encoding="utf-8")

                # Write temp file
                tmp.write_text(new_content, encoding="utf-8")

                # fsync
                fd = os.open(str(tmp), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)

                # Atomic rename
                os.rename(str(tmp), str(full_path))

                completed.append((full_path, backup))

            except Exception as exc:
                logger.error("Patch write failed for %s: %s", full_path, exc)
                # Rollback completed files
                rollback_errors = []
                for rb_path, rb_backup in completed:
                    try:
                        os.rename(str(rb_backup), str(rb_path))
                    except Exception as rb_exc:
                        rollback_errors.append(f"{rb_path}: {rb_exc}")
                        logger.error("Rollback failed for %s: %s", rb_path, rb_exc)

                # Cleanup temp files
                for rb_path, _ in completed:
                    tmp_rb = rb_path.parent / f".{rb_path.name}.mcp-patch-{rid}.tmp"
                    try:
                        tmp_rb.unlink(missing_ok=True)
                    except Exception:
                        pass

                if rollback_errors:
                    raise RollbackFailedError(
                        f"Write failed for {full_path} and rollback also failed: "
                        + "; ".join(rollback_errors)
                    ) from exc

                file_results.append(
                    ProjectPatchFileResult(
                        path=str(full_path.relative_to(project_root)),
                        status="failed",
                        error=str(exc),
                    )
                )
                return ProjectPatchApplyResponse(
                    success=False,
                    files_applied=0,
                    files_failed=1,
                    hunks_applied=0,
                    errors=file_results,
                    files=file_results,
                )

        # Cleanup backups on success
        for rb_path, rb_backup in completed:
            try:
                rb_backup.unlink(missing_ok=True)
            except Exception:
                pass
            tmp = rb_path.parent / f".{rb_path.name}.mcp-patch-{rid}.tmp"
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

        file_results = [
            ProjectPatchFileResult(
                path=str(p.relative_to(project_root)),
                status="applied",
                hunks_applied=h,
            )
            for p, _, _, h in prepared
        ]

        return ProjectPatchApplyResponse(
            success=True,
            files_applied=len(prepared),
            files_failed=0,
            hunks_applied=sum(h for _, _, _, h in prepared),
            files=file_results,
        )

    except (PatchValidationError, HashMismatchError) as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc))) from exc
    except RollbackFailedError as exc:
        raise HTTPException(status_code=500, detail=_err(500, str(exc))) from exc
    except Exception as exc:
        logger.error("Patch apply failed: %s", exc)
        raise HTTPException(
            status_code=500, detail=_err(500, f"Patch apply failed: {exc}")
        ) from exc


# ---------------------------------------------------------------------------
# Raw File
# ---------------------------------------------------------------------------


@router.get("/api/file/raw", response_class=PlainTextResponse)
async def file_raw(
    request: Request,
    session_id: str = Query(...),
    path: str = Query(...),
    offset: int = Query(0, ge=0),
    limit: int = Query(0, ge=0),
    range_header: str | None = Header(None, alias="range"),
    _identity: AuthIdentity = Depends(require_scope("ssh:files")),
):
    """Read a remote file and return raw content as text/plain.

    Supports Range header (bytes=start-end) or offset/limit query params.
    """
    await _check_session_ownership(session_id, request)

    try:
        path = validate_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc))) from exc
    try:
        content = await _state.file_editor.read_file(session_id, path)
    except Exception as exc:
        err_msg = str(exc)
        logger.warning(
            "File raw read failed: session=%s path=%s error=%s", session_id, path, err_msg
        )
        if "Cannot read" in err_msg:
            raise HTTPException(status_code=404, detail=_err(404, err_msg)) from exc
        raise HTTPException(
            status_code=500, detail=_err(500, f"File read failed: {err_msg}")
        ) from exc

    if range_header and range_header.startswith("bytes="):
        try:
            range_str = range_header[6:]
            raw_start, raw_end = range_str.split("-")
            rstart = int(raw_start) if raw_start else 0
            rend = int(raw_end) if raw_end else len(content)
            original_len = len(content)
            content = content[rstart:rend]
            return Response(
                content=content,
                media_type="text/plain",
                status_code=206,
                headers={
                    "Content-Range": f"bytes {rstart}-{rend - 1}/{original_len}",
                    "Accept-Ranges": "bytes",
                },
            )
        except (ValueError, IndexError):
            pass

    if offset > 0 or limit > 0:
        lines = content.split("\n")
        start = offset
        end = offset + limit if limit > 0 else len(lines)
        content = "\n".join(lines[start:end])

    return Response(
        content=content,
        media_type="text/plain",
    )


# ---------------------------------------------------------------------------
# Batch Read
# ---------------------------------------------------------------------------


@router.post("/api/batch/read", response_model=BatchReadResponse)
async def batch_read(
    req: BatchReadRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:files")),
):
    """Read multiple files in a single request."""
    await _check_session_ownership(req.session_id, request)

    files = {}
    errors = {}

    for path in req.paths:
        try:
            validated = validate_path(path)
            content = await _state.file_editor.read_file(req.session_id, validated)
            files[path] = content
        except ValueError as exc:
            errors[path] = str(exc)
        except Exception as exc:
            errors[path] = str(exc)

    return BatchReadResponse(files=files, errors=errors)


# ---------------------------------------------------------------------------
# File Upload/download
# ---------------------------------------------------------------------------


@router.post("/api/file/upload")
@rate_limit_mutation(20, "minute")
async def file_upload(
    request: Request,
    session_id: str = Query(...),
    path: str = Query(...),
    content: str = Query(...),
    _identity: AuthIdentity = Depends(require_scope("ssh:files")),
):
    """Upload file to remote server (base64 encoded via query params)."""
    await _check_session_ownership(session_id, request)

    import base64

    try:
        path = validate_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc))) from exc
    decoded = base64.b64decode(content).decode("utf-8", errors="replace")
    await _state.file_editor.write_file(session_id, path, decoded)
    return {"success": True, "path": path, "size": len(decoded), "deprecated": True}


@router.post("/api/file/upload/json", response_model=FileUploadResponse)
async def file_upload_json(
    req: FileUploadRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:files")),
):
    """Upload file via JSON body (base64 encoded).

    Preferred for large files (>2KB) where query params may fail.
    """
    await _check_session_ownership(req.session_id, request)

    import base64

    try:
        validated = validate_path(req.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc))) from exc
    decoded = base64.b64decode(req.content).decode("utf-8", errors="replace")
    await _state.file_editor.write_file(req.session_id, validated, decoded)
    return FileUploadResponse(path=validated, size=len(decoded))


@router.get("/api/file/download", response_class=Response)
async def file_download(
    request: Request,
    session_id: str = Query(...),
    path: str = Query(...),
    _identity: AuthIdentity = Depends(require_scope("ssh:files")),
):
    """Download file from remote server."""
    await _check_session_ownership(session_id, request)

    try:
        path = validate_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc))) from exc
    try:
        content = await _state.file_editor.read_file(session_id, path)
    except Exception as exc:
        err_msg = str(exc)
        logger.warning(
            "File download failed: session=%s path=%s error=%s", session_id, path, err_msg
        )
        if "Cannot read" in err_msg:
            raise HTTPException(status_code=404, detail=_err(404, err_msg)) from exc
        raise HTTPException(
            status_code=500, detail=_err(500, f"File read failed: {err_msg}")
        ) from exc
    return Response(content=content, media_type="application/octet-stream")


@router.post("/api/file/write", response_model=FileWriteResponse)
async def file_write(
    req: FileWriteRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:files")),
):
    """Write file via JSON body (atomic, no heredoc escaping).

    Use for Python code with quotes, special chars, or large content.
    Mode: 'write' (overwrite) or 'append' (append to end).
    """
    _assert_rw()
    await _check_session_ownership(req.session_id, request)

    try:
        validated = validate_path(req.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc))) from exc

    if req.mode == "append":
        existing = await _state.file_editor.read_file(req.session_id, validated)
        content = existing + req.content
    else:
        content = req.content

    await _state.file_editor.write_file(req.session_id, validated, content)
    return FileWriteResponse(path=validated, size=len(content), mode=req.mode)


# ---------------------------------------------------------------------------
# AST Refactor
# ---------------------------------------------------------------------------


@router.post("/api/ast/rename", response_model=ASTRefactorRenameResponse)
async def ast_rename(
    req: ASTRefactorRenameRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:files")),
):
    """Rename a symbol (function, class, variable) using AST.

    Supports single file ('path') or multiple files ('files' array).
    """
    await _check_session_ownership(req.session_id, request)

    # Validate All Paths First
    try:
        if req.files:
            for file_path in req.files:
                validate_path(file_path)
        else:
            single_path = req.path
            if single_path is None:
                raise HTTPException(status_code=400, detail=_err(400, "Path is required"))
            validate_path(single_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc))) from exc

    if req.files:
        results: list[ASTRefactorFileResult] = []
        total_replacements = 0
        files_changed = 0

        for file_path in req.files:
            try:
                code = await _state.file_editor.read_file(req.session_id, file_path)
                refactored, count = ASTRefactor.rename_symbol(code, req.old_name, req.new_name)

                if count > 0:
                    await _state.file_editor.write_file(req.session_id, file_path, refactored)
                    total_replacements += count
                    files_changed += 1
                    results.append(
                        ASTRefactorFileResult(
                            path=file_path,
                            success=True,
                            replacements=count,
                        )
                    )
                else:
                    results.append(
                        ASTRefactorFileResult(
                            path=file_path,
                            success=True,
                            replacements=0,
                        )
                    )
            except Exception as exc:
                results.append(
                    ASTRefactorFileResult(
                        path=file_path,
                        success=False,
                        replacements=0,
                        error=str(exc),
                    )
                )

        return ASTRefactorRenameResponse(
            old_name=req.old_name,
            new_name=req.new_name,
            replacements=total_replacements,
            files=results,
            total_files=len(req.files),
            files_changed=files_changed,
        )
    else:
        single_path = req.path
        if single_path is None:
            raise HTTPException(status_code=400, detail=_err(400, "Path is required"))
        try:
            code = await _state.file_editor.read_file(req.session_id, single_path)
            refactored, count = ASTRefactor.rename_symbol(code, req.old_name, req.new_name)

            if count > 0:
                await _state.file_editor.write_file(req.session_id, single_path, refactored)

            return ASTRefactorRenameResponse(
                path=single_path,
                old_name=req.old_name,
                new_name=req.new_name,
                replacements=count,
                code=refactored,
                total_files=1,
                files_changed=1 if count > 0 else 0,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=_err(500, f"AST rename failed: {exc}")
            ) from exc


@router.post("/api/refactor/rename", response_model=ASTRefactorRenameResponse)
async def refactor_rename(
    req: ASTRefactorRenameRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:files")),
):
    """Alias for /api/ast/rename — AST-aware symbol renaming."""
    return await ast_rename(req, request)


@router.post("/api/ast/extract", response_model=ASTRefactorExtractResponse)
async def ast_extract(
    req: ASTRefactorExtractRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:files")),
):
    """Extract a block of code into a new function."""
    await _check_session_ownership(req.session_id, request)

    try:
        validated = validate_path(req.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc))) from exc
    try:
        code = await _state.file_editor.read_file(req.session_id, validated)
        refactored = ASTRefactor.extract_function(code, req.start_line, req.end_line, req.func_name)

        await _state.file_editor.write_file(
            req.session_id,
            validated,
            refactored,
        )

        return ASTRefactorExtractResponse(
            path=validated,
            func_name=req.func_name,
            code=refactored,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=_err(500, f"AST extract failed: {exc}")
        ) from exc


@router.post("/api/ast/analyze", response_model=ASTAnalyzeResponse)
async def ast_analyze(
    req: ASTAnalyzeRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:files")),
):
    """Analyze Python code structure using AST."""
    await _check_session_ownership(req.session_id, request)

    try:
        validated = validate_path(req.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc))) from exc
    try:
        code = await _state.file_editor.read_file(req.session_id, validated)
        analysis = ASTRefactor.analyze_code(code)

        return ASTAnalyzeResponse(
            path=validated,
            **analysis,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=_err(500, f"AST analysis failed: {exc}")
        ) from exc


# ---------------------------------------------------------------------------
# Project Introspection
# ---------------------------------------------------------------------------


@router.get("/api/project/tree")
async def project_tree(
    session_id: str = Query(...),
    path: str = Query(default="."),
    max_depth: int = Query(default=3, ge=1, le=10),
    _identity: AuthIdentity = Depends(require_scope("ssh:files")),
):
    """Simple project tree — list files and directories.

    Returns flat list with type, path, size for quick introspection.
    """
    try:
        validated = validate_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc))) from exc

    cmd = f"cd {shlex.quote(validated)} && find . -maxdepth {max_depth} -not -path '*/\\.*' -not -path '*/node_modules/*' -not -path '*/__pycache__/*' -not -path '*/venv/*' -printf '%y|%p|%s\\n' 2>/dev/null || echo 'ERROR'"
    result = await _state.manager.execute(session_id, cmd, timeout=30)

    if result["exit_code"] != 0 or "ERROR" in result["stdout"]:
        raise HTTPException(
            status_code=500, detail=_err(500, f"Cannot read directory: {result['stderr']}")
        )

    items = []
    for line in result["stdout"].strip().split("\n"):
        if not line or line == "ERROR":
            continue
        parts = line.split("|", 3)
        if len(parts) < 3:
            continue

        ftype, fpath, fsize = parts
        fpath = fpath.lstrip("./")
        if not fpath:
            continue

        items.append(
            {
                "type": "directory" if ftype == "d" else "file",
                "path": fpath,
                "size": int(fsize) if fsize and ftype == "f" else None,
            }
        )

    return {"items": items, "count": len(items)}


@router.post("/api/project/files/structure", response_model=ProjectStructureResponse)
async def project_structure_files(
    req: ProjectStructureRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:files")),
):
    """Get project structure with metadata and git status."""
    await _check_session_ownership(req.session_id, request)

    try:
        validated_path = validate_path(req.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc))) from exc

    cmd = f"cd {shlex.quote(validated_path)} && find . -maxdepth {req.max_depth} -printf '%y|%p|%s|%m|%TY-%Tm-%Td %TH:%TM:%TS\\n' 2>/dev/null || echo 'ERROR'"
    result = await _state.manager.execute(req.session_id, cmd, timeout=30)

    if result["exit_code"] != 0 or "ERROR" in result["stdout"]:
        raise HTTPException(
            status_code=500, detail=_err(500, f"Cannot read directory: {result['stderr']}")
        )

    files = []
    total_files = 0
    total_directories = 0

    for line in result["stdout"].strip().split("\n"):
        if not line or line == "ERROR":
            continue
        parts = line.split("|", 4)
        if len(parts) < 5:
            continue

        file_type, path, size, permissions, mtime = parts
        path = path.lstrip("./")

        if not path:
            continue

        type_map = {"f": "file", "d": "directory", "l": "symlink"}
        file_type = type_map.get(file_type, "file")

        if file_type == "file":
            total_files += 1
        elif file_type == "directory":
            total_directories += 1

        extension = None
        if "." in path and file_type == "file":
            extension = path.split(".")[-1]

        files.append(
            FileMetadata(
                name=path.split("/")[-1] if "/" in path else path,
                path=path,
                type=file_type,
                size=int(size) if size else 0,
                permissions=permissions,
                modified_at=mtime if mtime else None,
                extension=extension,
            )
        )

    if req.include_git_status:
        git_cmd = f"cd {shlex.quote(validated_path)} && git status --short 2>/dev/null || echo ''"
        git_result = await _state.manager.execute(req.session_id, git_cmd, timeout=10)

        git_status_map = {}
        for line in git_result["stdout"].strip().split("\n"):
            if line and len(line) > 3:
                status = line[:2].strip()
                file_path = line[3:].strip()
                git_status_map[file_path] = status

        for file_meta in files:
            if file_meta.path in git_status_map:
                file_meta.git_status = git_status_map[file_meta.path]

    tree: dict[str, Any] = {"name": ".", "type": "directory", "children": {}}

    for file_meta in files:
        parts = file_meta.path.split("/")
        current = tree

        for i, part in enumerate(parts):
            if not part:
                continue

            if current.get("children") is None:
                current["children"] = {}

            if part not in current["children"]:
                current["children"][part] = {
                    "name": part,
                    "type": file_meta.type if i == len(parts) - 1 else "directory",
                    "children": {} if i < len(parts) - 1 else None,
                }

            current = current["children"][part]

    return ProjectStructureResponse(
        path=req.path,
        total_files=total_files,
        total_directories=total_directories,
        files=files,
        tree=tree,
    )


# ---------------------------------------------------------------------------
# Batch Edit
# ---------------------------------------------------------------------------


@router.patch("/api/batch/edit", response_model=BatchEditResponse)
async def batch_edit(
    req: BatchEditRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:files")),
):
    """Edit multiple files in a single request."""
    _assert_rw()
    await _check_session_ownership(req.session_id, request)

    results = []
    files_changed = 0
    total_operations = 0

    for file_op in req.files:
        try:
            validate_path(file_op.path)
        except ValueError as exc:
            results.append(
                BatchEditResult(
                    path=file_op.path,
                    success=False,
                    operations_applied=0,
                    changed=False,
                    error=str(exc),
                )
            )
            continue

        try:
            result = await _state.file_editor.edit_file(
                req.session_id,
                file_op.path,
                [op.model_dump() for op in file_op.operations],
            )
            results.append(
                BatchEditResult(
                    path=file_op.path,
                    success=True,
                    operations_applied=result.get("operations_applied", 0),
                    changed=result.get("changed", False),
                )
            )
            total_operations += result.get("operations_applied", 0)
            if result.get("changed", False):
                files_changed += 1
        except Exception as exc:
            results.append(
                BatchEditResult(
                    path=file_op.path,
                    success=False,
                    operations_applied=0,
                    changed=False,
                    error=str(exc),
                )
            )

    return BatchEditResponse(
        results=results,
        total_files=len(req.files),
        files_changed=files_changed,
        total_operations=total_operations,
    )


# ---------------------------------------------------------------------------
# Bulk Read/edit
# ---------------------------------------------------------------------------


@router.post("/api/bulk/read")
async def bulk_read_files(
    req: BatchReadRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:files")),
):
    """Read multiple files concurrently."""
    await _check_session_ownership(req.session_id, request)

    files = await _state.bulk_ops.read_files_bulk(
        req.session_id,
        req.paths,
        _state.file_editor,
        max_concurrency=20,
    )
    return BatchReadResponse(files=files, errors={})


@router.post("/api/bulk/edit", response_model=BatchEditResponse)
async def bulk_edit_files(
    req: BatchEditRequest,
    request: Request,
    _identity: AuthIdentity = Depends(require_scope("ssh:files")),
):
    """Edit multiple files concurrently.

    Example:
        {
            "session_id": "...",
            "files": [
                {
                    "path": "app/main.py",
                    "operations": [
                        {"type": "replace", "old": "def old():", "new": "def new():"}
                    ]
                },
                {
                    "path": "app/config.py",
                    "operations": [
                        {"type": "replace", "old": "DEBUG = True", "new": "DEBUG = False"}
                    ]
                }
            ]
        }
    """
    _assert_rw()
    await _check_session_ownership(req.session_id, request)

    results = []
    files_changed = 0
    total_operations = 0

    for file_op in req.files:
        try:
            result = await _state.file_editor.edit_file(
                req.session_id,
                file_op.path,
                [op.model_dump() for op in file_op.operations],
            )
            results.append(
                BatchEditResult(
                    path=file_op.path,
                    success=True,
                    operations_applied=result.get("operations_applied", 0),
                    changed=result.get("changed", False),
                )
            )
            total_operations += result.get("operations_applied", 0)
            if result.get("changed", False):
                files_changed += 1
        except Exception as exc:
            results.append(
                BatchEditResult(
                    path=file_op.path,
                    success=False,
                    operations_applied=0,
                    changed=False,
                    error=str(exc),
                )
            )

    return BatchEditResponse(
        results=results,
        total_files=len(req.files),
        files_changed=files_changed,
        total_operations=total_operations,
    )


# ---------------------------------------------------------------------------
# File Watch Websocket
# ---------------------------------------------------------------------------


@router.websocket("/api/file/watch")
async def file_watch_stream(websocket: WebSocket):
    """Watch file changes in real-time via WebSocket.

    Usage:
    1. Connect to /api/file/watch
    2. Send: {"session_id": "...", "path": "/var/log/app.log", "tail": true}
    3. Receive file updates as they happen
    """
    identity = await ws_auth_check(
        websocket, settings, _state.agent_token_store, required_scope="ssh:files"
    )
    if isinstance(identity, tuple):
        await websocket.close(code=identity[0], reason=identity[1])
        return
    await websocket.accept()
    _state.active_websockets.add(websocket)
    session_id = None
    watch_task = None

    try:
        data = await asyncio.wait_for(websocket.receive_json(), timeout=30)
        session_id = data.get("session_id", "")
        path = data.get("path", "")
        tail = data.get("tail", True)
        interval = data.get("interval", 1.0)

        if not session_id or not path:
            await websocket.send_json({"type": "error", "data": "session_id and path required"})
            await websocket.close()
            return

        record = await _state.manager.get_session(session_id)
        if not record:
            await websocket.send_json({"type": "error", "data": "Session not found"})
            await websocket.close()
            return

        try:
            ensure_session_owner(record, identity)
        except HTTPException:
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "SESSION_OWNERSHIP",
                    "message": "Agent token cannot access this session",
                }
            )
            await websocket.close()
            return

        try:
            path = validate_path(path)
        except ValueError as exc:
            await websocket.send_json(
                {
                    "type": "error",
                    "data": f"Invalid path: {exc}",
                }
            )
            await websocket.close()
            return

        last_content = ""
        last_size = 0

        await websocket.send_json({"type": "status", "data": f"Watching {path} (tail={tail})"})

        while True:
            try:
                try:
                    msg = await asyncio.wait_for(websocket.receive_json(), timeout=interval)
                    if msg.get("action") == "stop":
                        break
                except TimeoutError:
                    pass

                result = await _state.manager.execute(
                    session_id,
                    f"cat {shlex.quote(path)} 2>/dev/null || echo '__FILE_NOT_FOUND__'",
                    timeout=10,
                )

                if "__FILE_NOT_FOUND__" in result["stdout"]:
                    await websocket.send_json({"type": "error", "data": f"File not found: {path}"})
                    await asyncio.sleep(interval)
                    continue

                content = result["stdout"]

                if tail:
                    if len(content) > last_size:
                        new_content = content[last_size:]
                        lines = new_content.strip().split("\n")
                        for line in lines:
                            if line:
                                await websocket.send_json(
                                    {"type": "line", "data": line, "timestamp": time.time()}
                                )
                        last_size = len(content)
                else:
                    if content != last_content:
                        await websocket.send_json(
                            {"type": "content", "data": content, "timestamp": time.time()}
                        )
                        last_content = content

            except Exception as exc:
                logger.error("File watch error: %s", exc)
                await websocket.send_json({"type": "error", "data": str(exc)})
                await asyncio.sleep(interval)

    except WebSocketDisconnect:
        logger.info("File Watch Client Disconnected")
    except Exception as exc:
        logger.error("File watch error: %s", exc)
    finally:
        _state.active_websockets.discard(websocket)
        if watch_task:
            watch_task.cancel()
        try:
            await websocket.close()
        except Exception:
            pass
