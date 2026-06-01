"""File operations, AST, and batch routes."""

import logging
import asyncio
import time
import shlex

from fastapi import APIRouter, Depends, HTTPException, Query, Header, Response, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from typing import Any, Optional

from app.state import _err
from app import state as _state
from app.config import settings
from app.security import validate_path, rate_limit_mutation
from app.models import (
    FileReadRequest,
    FileReadResponse,
    FileEditRequest,
    FileEditResponse,
    PatchApplyRequest,
    PatchApplyResponse,
    BatchReadRequest,
    BatchReadResponse,
    FileUploadRequest,
    FileUploadResponse,
    FileWriteRequest,
    FileWriteResponse,
    ASTRefactorRenameRequest,
    ASTRefactorRenameResponse,
    ASTRefactorFileResult,
    ASTRefactorExtractRequest,
    ASTRefactorExtractResponse,
    ASTAnalyzeRequest,
    ASTAnalyzeResponse,
    BatchEditRequest,
    BatchEditResponse,
    BatchEditResult,
    ProjectStructureRequest,
    ProjectStructureResponse,
    FileMetadata,
)
from app.ast_refactor import ASTRefactor
from app.auth_middleware import ws_auth_check, AuthIdentity, ensure_session_owner, require_scope

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
async def file_read(req: FileReadRequest, request: Request, _identity: AuthIdentity = Depends(require_scope("ssh:files"))):
    """Read a file from a remote server."""
    await _check_session_ownership(req.session_id, request)

    try:
        validated = validate_path(req.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc)))

    _state.audit_logger.log_file_access(req.session_id, validated, "READ", request.client.host)
    content = await _state.file_editor.read_file(req.session_id, validated)
    return FileReadResponse(path=validated, content=content)


@router.patch("/api/file/edit", response_model=FileEditResponse)
@rate_limit_mutation(30, "minute")
async def file_edit(req: FileEditRequest, request: Request, _identity: AuthIdentity = Depends(require_scope("ssh:files"))):
    """Edit a remote file using patch operations."""
    await _check_session_ownership(req.session_id, request)

    try:
        validated = validate_path(req.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc)))
    try:
        logger.info(f"File edit request: session={req.session_id}, path={validated}, ops={len(req.operations)}")
        result = await _state.file_editor.edit_file(
            req.session_id,
            validated,
            [op.model_dump() for op in req.operations],
        )
        logger.info(f"File edit result: {result}")
        return FileEditResponse(**result)
    except Exception as exc:
        logger.error(f"File edit failed: {exc}")
        raise HTTPException(status_code=500, detail=_err(500, f"File edit failed: {exc}"))


@router.post("/api/file/patch", response_model=PatchApplyResponse)
async def file_patch(req: PatchApplyRequest, request: Request, _identity: AuthIdentity = Depends(require_scope("ssh:files"))):
    """Apply a unified diff patch."""
    await _check_session_ownership(req.session_id, request)

    result = await _state.file_editor.apply_patch(
        req.session_id,
        req.patch,
        req.strip,
    )
    return PatchApplyResponse(**result)


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
    range_header: Optional[str] = Header(None, alias="range"),
    _identity: AuthIdentity = Depends(require_scope("ssh:files")),
):
    """Read a remote file and return raw content as text/plain.

    Supports Range header (bytes=start-end) or offset/limit query params.
    """
    await _check_session_ownership(session_id, request)

    try:
        path = validate_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc)))
    content = await _state.file_editor.read_file(session_id, path)

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
                    "Content-Range": f"bytes {rstart}-{rend-1}/{original_len}",
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
async def batch_read(req: BatchReadRequest, request: Request, _identity: AuthIdentity = Depends(require_scope("ssh:files"))):
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
        raise HTTPException(status_code=400, detail=_err(400, str(exc)))
    decoded = base64.b64decode(content).decode("utf-8", errors="replace")
    await _state.file_editor.write_file(session_id, path, decoded)
    return {"success": True, "path": path, "size": len(decoded), "deprecated": True}


@router.post("/api/file/upload/json", response_model=FileUploadResponse)
async def file_upload_json(req: FileUploadRequest, request: Request, _identity: AuthIdentity = Depends(require_scope("ssh:files"))):
    """Upload file via JSON body (base64 encoded).

    Preferred for large files (>2KB) where query params may fail.
    """
    await _check_session_ownership(req.session_id, request)

    import base64

    try:
        validated = validate_path(req.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc)))
    decoded = base64.b64decode(req.content).decode("utf-8", errors="replace")
    await _state.file_editor.write_file(req.session_id, validated, decoded)
    return FileUploadResponse(path=validated, size=len(decoded))


@router.get("/api/file/download", response_class=Response)
async def file_download(request: Request, session_id: str = Query(...), path: str = Query(...), _identity: AuthIdentity = Depends(require_scope("ssh:files"))):
    """Download file from remote server."""
    await _check_session_ownership(session_id, request)

    try:
        path = validate_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc)))
    content = await _state.file_editor.read_file(session_id, path)
    return Response(content=content, media_type="application/octet-stream")


@router.post("/api/file/write", response_model=FileWriteResponse)
async def file_write(req: FileWriteRequest, request: Request, _identity: AuthIdentity = Depends(require_scope("ssh:files"))):
    """Write file via JSON body (atomic, no heredoc escaping).

    Use for Python code with quotes, special chars, or large content.
    Mode: 'write' (overwrite) or 'append' (append to end).
    """
    await _check_session_ownership(req.session_id, request)

    try:
        validated = validate_path(req.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc)))

    if req.mode == "append":
        existing = await _state.file_editor.read_file(req.session_id, validated)
        content = existing + req.content
    else:
        content = req.content

    await _state.file_editor.write_file(req.session_id, validated, content)
    return FileWriteResponse(
        path=validated, size=len(content), mode=req.mode
    )


# ---------------------------------------------------------------------------
# AST Refactor
# ---------------------------------------------------------------------------

@router.post("/api/ast/rename", response_model=ASTRefactorRenameResponse)
async def ast_rename(req: ASTRefactorRenameRequest, _identity: AuthIdentity = Depends(require_scope("ssh:files"))):
    """Rename a symbol (function, class, variable) using AST.

    Supports single file ('path') or multiple files ('files' array).
    """
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
        raise HTTPException(status_code=400, detail=_err(400, str(exc)))

    if req.files:
        results: list[ASTRefactorFileResult] = []
        total_replacements = 0
        files_changed = 0

        for file_path in req.files:
            try:
                code = await _state.file_editor.read_file(req.session_id, file_path)
                refactored, count = ASTRefactor.rename_symbol(
                    code, req.old_name, req.new_name
                )

                if count > 0:
                    await _state.file_editor.write_file(
                        req.session_id, file_path, refactored
                    )
                    total_replacements += count
                    files_changed += 1
                    results.append(ASTRefactorFileResult(
                        path=file_path,
                        success=True,
                        replacements=count,
                    ))
                else:
                    results.append(ASTRefactorFileResult(
                        path=file_path,
                        success=True,
                        replacements=0,
                    ))
            except Exception as exc:
                results.append(ASTRefactorFileResult(
                    path=file_path,
                    success=False,
                    replacements=0,
                    error=str(exc),
                ))

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
            refactored, count = ASTRefactor.rename_symbol(
                code, req.old_name, req.new_name
            )

            if count > 0:
                await _state.file_editor.write_file(
                    req.session_id, single_path, refactored
                )

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
            raise HTTPException(status_code=500, detail=_err(500, f"AST rename failed: {exc}"))


@router.post("/api/refactor/rename", response_model=ASTRefactorRenameResponse)
async def refactor_rename(req: ASTRefactorRenameRequest, _identity: AuthIdentity = Depends(require_scope("ssh:files"))):
    """Alias for /api/ast/rename — AST-aware symbol renaming."""
    return await ast_rename(req)


@router.post("/api/ast/extract", response_model=ASTRefactorExtractResponse)
async def ast_extract(req: ASTRefactorExtractRequest, _identity: AuthIdentity = Depends(require_scope("ssh:files"))):
    """Extract a block of code into a new function."""
    try:
        validated = validate_path(req.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc)))
    try:
        code = await _state.file_editor.read_file(req.session_id, validated)
        refactored = ASTRefactor.extract_function(
            code, req.start_line, req.end_line, req.func_name
        )

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
        raise HTTPException(status_code=500, detail=_err(500, f"AST extract failed: {exc}"))


@router.post("/api/ast/analyze", response_model=ASTAnalyzeResponse)
async def ast_analyze(req: ASTAnalyzeRequest, _identity: AuthIdentity = Depends(require_scope("ssh:files"))):
    """Analyze Python code structure using AST."""
    try:
        validated = validate_path(req.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc)))
    try:
        code = await _state.file_editor.read_file(req.session_id, validated)
        analysis = ASTRefactor.analyze_code(code)

        return ASTAnalyzeResponse(
            path=validated,
            **analysis,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_err(500, f"AST analysis failed: {exc}"))


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
    cmd = f"cd {shlex.quote(path)} && find . -maxdepth {max_depth} -not -path '*/\\.*' -not -path '*/node_modules/*' -not -path '*/__pycache__/*' -not -path '*/venv/*' -printf '%y|%p|%s\\n' 2>/dev/null || echo 'ERROR'"
    result = await _state.manager.execute(session_id, cmd, timeout=30)

    if result["exit_code"] != 0 or "ERROR" in result["stdout"]:
        raise HTTPException(status_code=500, detail=_err(500, f"Cannot read directory: {result['stderr']}"))

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

        items.append({
            "type": "directory" if ftype == "d" else "file",
            "path": fpath,
            "size": int(fsize) if fsize and ftype == "f" else None,
        })

    return {"items": items, "count": len(items)}


@router.post("/api/project/files/structure", response_model=ProjectStructureResponse)
async def project_structure_files(req: ProjectStructureRequest, request: Request, _identity: AuthIdentity = Depends(require_scope("ssh:files"))):
    """Get project structure with metadata and git status."""
    await _check_session_ownership(req.session_id, request)


    cmd = f"cd {shlex.quote(req.path)} && find . -maxdepth {req.max_depth} -printf '%y|%p|%s|%m|%TY-%Tm-%Td %TH:%TM:%TS\\n' 2>/dev/null || echo 'ERROR'"
    result = await _state.manager.execute(req.session_id, cmd, timeout=30)

    if result["exit_code"] != 0 or "ERROR" in result["stdout"]:
        raise HTTPException(status_code=500, detail=_err(500, f"Cannot read directory: {result['stderr']}"))

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

        files.append(FileMetadata(
            name=path.split("/")[-1] if "/" in path else path,
            path=path,
            type=file_type,
            size=int(size) if size else 0,
            permissions=permissions,
            modified_at=mtime if mtime else None,
            extension=extension,
        ))

    if req.include_git_status:
        git_cmd = f"cd {shlex.quote(req.path)} && git status --short 2>/dev/null || echo ''"
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
async def batch_edit(req: BatchEditRequest, request: Request, _identity: AuthIdentity = Depends(require_scope("ssh:files"))):
    """Edit multiple files in a single request."""
    await _check_session_ownership(req.session_id, request)

    results = []
    files_changed = 0
    total_operations = 0

    for file_op in req.files:
        try:
            validate_path(file_op.path)
        except ValueError as exc:
            results.append(BatchEditResult(
                path=file_op.path,
                success=False,
                operations_applied=0,
                changed=False,
                error=str(exc),
            ))
            continue

        try:
            result = await _state.file_editor.edit_file(
                req.session_id,
                file_op.path,
                [op.model_dump() for op in file_op.operations],
            )
            results.append(BatchEditResult(
                path=file_op.path,
                success=True,
                operations_applied=result.get("operations_applied", 0),
                changed=result.get("changed", False),
            ))
            total_operations += result.get("operations_applied", 0)
            if result.get("changed", False):
                files_changed += 1
        except Exception as exc:
            results.append(BatchEditResult(
                path=file_op.path,
                success=False,
                operations_applied=0,
                changed=False,
                error=str(exc),
            ))

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
async def bulk_read_files(req: BatchReadRequest, _identity: AuthIdentity = Depends(require_scope("ssh:files"))):
    """Read multiple files concurrently."""
    files = await _state.bulk_ops.read_files_bulk(
        req.session_id,
        req.paths,
        _state.file_editor,
        max_concurrency=20,
    )
    return BatchReadResponse(files=files, errors={})


@router.post("/api/bulk/edit", response_model=BatchEditResponse)
async def bulk_edit_files(req: BatchEditRequest, _identity: AuthIdentity = Depends(require_scope("ssh:files"))):
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
            results.append(BatchEditResult(
                path=file_op.path,
                success=True,
                operations_applied=result.get("operations_applied", 0),
                changed=result.get("changed", False),
            ))
            total_operations += result.get("operations_applied", 0)
            if result.get("changed", False):
                files_changed += 1
        except Exception as exc:
            results.append(BatchEditResult(
                path=file_op.path,
                success=False,
                operations_applied=0,
                changed=False,
                error=str(exc),
            ))

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
    identity = await ws_auth_check(websocket, settings, _state.agent_token_store)
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
            await websocket.send_json({
                "type": "error",
                "code": "SESSION_OWNERSHIP",
                "message": "Agent token cannot access this session",
            })
            await websocket.close()
            return

        last_content = ""
        last_size = 0

        await websocket.send_json({
            "type": "status",
            "data": f"Watching {path} (tail={tail})"
        })

        while True:
            try:
                try:
                    msg = await asyncio.wait_for(websocket.receive_json(), timeout=interval)
                    if msg.get("action") == "stop":
                        break
                except asyncio.TimeoutError:
                    pass

                result = await _state.manager.execute(
                    session_id,
                    f"cat {shlex.quote(path)} 2>/dev/null || echo '__FILE_NOT_FOUND__'",
                    timeout=10
                )

                if "__FILE_NOT_FOUND__" in result["stdout"]:
                    await websocket.send_json({
                        "type": "error",
                        "data": f"File not found: {path}"
                    })
                    await asyncio.sleep(interval)
                    continue

                content = result["stdout"]

                if tail:
                    if len(content) > last_size:
                        new_content = content[last_size:]
                        lines = new_content.strip().split("\n")
                        for line in lines:
                            if line:
                                await websocket.send_json({
                                    "type": "line",
                                    "data": line,
                                    "timestamp": time.time()
                                })
                        last_size = len(content)
                else:
                    if content != last_content:
                        await websocket.send_json({
                            "type": "content",
                            "data": content,
                            "timestamp": time.time()
                        })
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