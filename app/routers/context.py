"""Context management, validation, templates, and project structure routes."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app import state as _state
from app.auth_middleware import AuthIdentity, require_master_key
from app.git_manager import GitStatus
from app.models import (
    AddBookmarkRequest,
    AddCommandRequest,
    AddSearchRequest,
    CloseFileRequest,
    ContextCreateRequest,
    ContextListResponse,
    ContextResponse,
    FileEditWithContextRequest,
    FileEditWithContextResponse,
    FileReadRequest,
    FileReadResponse,
    GitInfoResponse,
    OpenFileRequest,
    ProjectStructureRequest,
    ProjectStructureResponse,
    ScaffoldRequest,
    ScaffoldResponse,
    SmartContextStateResponse,
    TabStateResponse,
    TemplateInfo,
    TemplateListResponse,
    TemplateRenderRequest,
    TemplateRenderResponse,
    UpdateCursorRequest,
    ValidateRequest,
    ValidationReportResponse,
    ValidationStepResult,
)
from app.routers.workspace import assert_workspace_writable
from app.services.context_editing import ContextEditError, edit_file_with_context
from app.services.project_structure import scan_project_structure
from app.services.scaffolding import scaffold_python_class as scaffold_python_class_service
from app.state import _err
from app.template_library import TemplateLibrary

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _context_to_response(ctx) -> ContextResponse:
    """Helper to convert Context to ContextResponse."""
    git_info = ctx.git_info
    smart_state = ctx.smart_state.to_dict()
    return ContextResponse(
        context_id=ctx.context_id,
        name=ctx.name,
        path=ctx.path,
        session_id=ctx.session_id,
        branch=ctx.branch,
        git=GitInfoResponse(
            status=git_info.status.value if git_info else "unknown",
            branch=git_info.branch if git_info else None,
            has_changes=git_info.has_changes if git_info else False,
            last_commit=git_info.last_commit if git_info else None,
            remote_url=git_info.remote_url if git_info else None,
            message=git_info.message if git_info else "",
            can_commit=git_info.can_commit if git_info else False,
        ),
        auto_commit=ctx.auto_commit,
        auto_validate=ctx.auto_validate,
        files_opened=ctx.files_opened,
        smart_state=SmartContextStateResponse(
            tabs=[TabStateResponse(**tab) for tab in smart_state["tabs"]],
            active_tab=smart_state.get("active_tab"),
            command_history=smart_state.get("command_history", []),
            search_history=smart_state.get("search_history", []),
            bookmarks=smart_state.get("bookmarks", []),
            last_edited_file=smart_state.get("last_edited_file"),
            clipboard_size=smart_state.get("clipboard_size", 0),
        ),
        created_at=ctx.created_at,
        message="",
    )


# ---------------------------------------------------------------------------
# Context CRUD
# ---------------------------------------------------------------------------


@router.post("/api/context/create", response_model=ContextResponse)
async def context_create(
    req: ContextCreateRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Create a new development context with git awareness."""
    ctx = await _state.context_manager.create_context(
        session_id=req.session_id,
        name=req.name,
        path=req.path,
        branch=req.branch,
        auto_commit=req.auto_commit,
        auto_validate=req.auto_validate,
    )

    git_info = ctx.git_info
    message = git_info.message if git_info else "Context created"

    # Add Suggestion If Git Not Initialized
    if git_info and git_info.status == GitStatus.NOT_INITIALIZED:
        message += "\n\U0001f4a1 Tip: Use POST /api/git/init to initialize git repository."

    resp = _context_to_response(ctx)
    resp.message = message
    return resp


@router.get("/api/context/list", response_model=ContextListResponse)
async def context_list(
    session_id: str | None = None, _identity: AuthIdentity = Depends(require_master_key)
):
    """List all active contexts."""
    contexts = []
    for _, ctx in _state.context_manager._contexts.items():
        if ctx and (not session_id or ctx.session_id == session_id):
            resp = _context_to_response(ctx)
            resp.message = f"Idle for {ctx.idle_time:.0f}s"
            contexts.append(resp)

    return ContextListResponse(contexts=contexts, count=len(contexts))


@router.get("/api/context/{context_id}", response_model=ContextResponse)
async def context_get(context_id: str, _identity: AuthIdentity = Depends(require_master_key)):
    """Get context details."""
    ctx = await _state.context_manager.get_context(context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, f"Context {context_id} not found"))

    resp = _context_to_response(ctx)
    resp.message = "Context active"
    return resp


@router.delete("/api/context/{context_id}")
async def context_delete(context_id: str, _identity: AuthIdentity = Depends(require_master_key)):
    """Delete a context."""
    success = await _state.context_manager.delete_context(context_id)
    if not success:
        raise HTTPException(status_code=404, detail=_err(404, f"Context {context_id} not found"))
    return {"status": "deleted", "context_id": context_id}


# ---------------------------------------------------------------------------
# Smart Context State
# ---------------------------------------------------------------------------


@router.get("/api/context/{context_id}/state")
async def context_get_state(context_id: str, _identity: AuthIdentity = Depends(require_master_key)):
    """Get smart context state."""
    state = await _state.context_manager.get_smart_state(context_id)
    if not state:
        raise HTTPException(status_code=404, detail=_err(404, f"Context {context_id} not found"))
    return state


# ---------------------------------------------------------------------------
# Context-aware File Operations
# ---------------------------------------------------------------------------


@router.post("/api/context/file/read", response_model=FileReadResponse)
async def context_file_read(
    req: FileReadRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Read a file using context (session_id extracted from context)."""
    ctx = await _state.context_manager.get_context(req.session_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))

    content = await _state.file_editor.read_file(ctx.session_id, req.path)
    await _state.context_manager.add_file_to_context(req.session_id, req.path)
    return FileReadResponse(path=req.path, content=content)


@router.patch("/api/context/file/edit", response_model=FileEditWithContextResponse)
async def context_file_edit(
    req: FileEditWithContextRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Edit a file with context awareness (auto-commit, validation)."""
    assert_workspace_writable(
        actor_type=_identity.token_type,
        actor_name=_identity.name or "",
        actor_fingerprint=_identity.fingerprint[:12],
        route="PATCH /api/context/file/edit",
    )
    ctx = await _state.context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))

    logger.info(f"Context edit: ctx={req.context_id}, path={req.path}, ops={len(req.operations)}")

    try:
        result = await edit_file_with_context(
            ctx,
            _state.context_manager,
            _state.file_editor,
            _state.manager,
            path=req.path,
            operations=[op.model_dump() for op in req.operations],
            commit_message=req.commit_message,
            run_validation=req.run_validation,
        )
    except ContextEditError as exc:
        raise HTTPException(status_code=500, detail=_err(500, f"Edit failed: {exc}")) from exc

    response = FileEditWithContextResponse(
        success=result.success,
        path=result.path,
        operations_applied=result.operations_applied,
        changed=result.changed,
        diff=result.diff,
        git_commit=result.git_commit,
        validation_result=result.validation_result,
        warning=result.warning,
    )
    logger.info(f"Response object: success={response.success}, changed={response.changed}")

    return response


# ---------------------------------------------------------------------------
# Smart Context API (tabs, Cursors, Bookmarks, History)
# ---------------------------------------------------------------------------


@router.post("/api/context/file/open")
async def context_file_open(
    req: OpenFileRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Open file in smart context (creates tab)."""
    await _state.context_manager.add_file_to_context(req.context_id, req.path)
    return {"status": "opened", "path": req.path}


@router.post("/api/context/file/close")
async def context_file_close(
    req: CloseFileRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Close file in smart context (closes tab)."""
    success = await _state.context_manager.close_file(req.context_id, req.path)
    return {"status": "closed" if success else "not_found", "path": req.path}


@router.post("/api/context/cursor")
async def context_update_cursor(
    req: UpdateCursorRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Update cursor position in file."""
    await _state.context_manager.update_cursor(req.context_id, req.path, req.line, req.column)
    return {"status": "updated", "path": req.path, "line": req.line, "column": req.column}


@router.post("/api/context/command")
async def context_add_command(
    req: AddCommandRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Add command to history."""
    result = await _state.context_manager.add_command(req.context_id, req.command, req.directory)
    return {"status": "added", "command": result}


@router.post("/api/context/search")
async def context_add_search(
    req: AddSearchRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Add search query to history."""
    result = await _state.context_manager.add_search(
        req.context_id, req.query, req.path, req.replace_with
    )
    return {"status": "added", "search": result}


@router.post("/api/context/bookmark")
async def context_add_bookmark(
    req: AddBookmarkRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Add bookmark."""
    result = await _state.context_manager.add_bookmark(req.context_id, req.path, req.line, req.note)
    return {"status": "added", "bookmark": result}


@router.delete("/api/context/bookmark")
async def context_remove_bookmark(
    context_id: str = Query(...),
    path: str = Query(...),
    line: int = Query(...),
    _identity: AuthIdentity = Depends(require_master_key),
):
    """Remove bookmark."""
    success = await _state.context_manager.remove_bookmark(context_id, path, line)
    return {"status": "removed" if success else "not_found", "path": path, "line": line}


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


@router.post("/api/scaffold/python-class", response_model=ScaffoldResponse)
async def scaffold_python_class(
    req: ScaffoldRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Scaffold a Python class + test file from template."""
    assert_workspace_writable(
        actor_type=_identity.token_type,
        actor_name=_identity.name or "",
        actor_fingerprint=_identity.fingerprint[:12],
        route="POST /api/scaffold/python-class",
    )
    return await scaffold_python_class_service(
        _state.manager,
        _state.file_editor,
        session_id=req.session_id,
        module_path=req.module_path,
        class_name=req.class_name,
        methods=req.methods,
        include_test=req.include_test,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@router.post("/api/validate", response_model=ValidationReportResponse)
async def validate_context(
    req: ValidateRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Run validation pipeline (mypy + pytest) for context."""
    try:
        report = await _state.context_manager.validate_context(
            req.context_id,
            run_mypy=req.run_mypy,
            run_tests=req.run_tests,
        )

        return ValidationReportResponse(
            overall_status=report.overall_status.value,
            summary=report.summary,
            total_duration=report.total_duration,
            can_commit=report.can_commit,
            steps=[
                ValidationStepResult(
                    name=step.name,
                    status=step.status.value,
                    output=step.output,
                    errors=step.errors,
                    warnings=step.warnings,
                    duration=step.duration,
                )
                for step in report.steps
            ],
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=_err(404, str(exc))) from exc
    except Exception as exc:
        logger.error("Validation error: %s", exc)
        raise HTTPException(status_code=500, detail=_err(500, f"Validation failed: {exc}")) from exc


# ---------------------------------------------------------------------------
# Template Library
# ---------------------------------------------------------------------------


@router.get("/api/templates", response_model=TemplateListResponse)
async def list_templates(_identity: AuthIdentity = Depends(require_master_key)):
    """List all available code templates."""
    templates = TemplateLibrary.list_templates()
    return TemplateListResponse(
        templates=[TemplateInfo(**t) for t in templates], count=len(templates)
    )


@router.get("/api/templates/{template_id}")
async def get_template(template_id: str, _identity: AuthIdentity = Depends(require_master_key)):
    """Get template details."""
    template = TemplateLibrary.get_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail=_err(404, f"Template {template_id} not found"))
    return template


@router.post("/api/templates/render", response_model=TemplateRenderResponse)
async def render_template(
    req: TemplateRenderRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Render template and save to file."""
    assert_workspace_writable(
        actor_type=_identity.token_type,
        actor_name=_identity.name or "",
        actor_fingerprint=_identity.fingerprint[:12],
        route="POST /api/templates/render",
    )
    ctx = await _state.context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))

    try:
        code = TemplateLibrary.render_template(req.template_id, req.params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc))) from exc

    if not code:
        raise HTTPException(
            status_code=404, detail=_err(404, f"Template {req.template_id} not found")
        )

    # Create File With Rendered Code
    result = await _state.manager.execute(
        ctx.session_id,
        f"cat > '{req.target_path}' << 'TEMPLATE_EOF'\n{code}\nTEMPLATE_EOF",
        timeout=10,
    )

    if result["exit_code"] != 0:
        raise HTTPException(
            status_code=500, detail=_err(500, f"Failed to create file: {result['stderr']}")
        )

    # Auto-commit If Enabled
    git_commit = None
    if req.auto_commit:
        commit_result = await _state.context_manager.commit_changes(
            req.context_id, f"Add {req.template_id} template", [req.target_path]
        )
        if commit_result.get("success"):
            git_commit = commit_result.get("hash")

    return TemplateRenderResponse(
        success=True,
        template_id=req.template_id,
        target_path=req.target_path,
        code=code,
        git_commit=git_commit,
    )


# ---------------------------------------------------------------------------
# Project Structure
# ---------------------------------------------------------------------------


@router.post("/api/project/structure", response_model=ProjectStructureResponse)
async def project_structure(
    req: ProjectStructureRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Get project structure with metadata and git status."""
    try:
        files, total_files, total_directories, tree = await scan_project_structure(
            _state.manager,
            req.session_id,
            req.path,
            req.max_depth,
            include_git_status=req.include_git_status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=_err(500, str(exc))) from exc

    return ProjectStructureResponse(
        path=req.path,
        total_files=total_files,
        total_directories=total_directories,
        files=files,
        tree=tree,
    )
